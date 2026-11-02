#!/usr/bin/env python3
"""
Hyperliquid HIP-3 (xyz:SNDK) 纯因果 GLFT + LightGBM 毒性门控 + RWA 状态机生产实盘引擎 (V2.2 Production)
更新与升级项：
1. [正股常规交易时段停机保护] 美股常规盘（09:30 - 16:00 ET）强制休眠，自动全撤挂单，杜绝为链下纳斯达克知情巨鲸接飞刀；
2. [黄金交易时段自适应激活] 仅在休市期（闭盘后与周末）及盘后（16:00 - 20:00 ET）启动做市，独享高点差真空；
3. [大波动紧急撤单保护] 盘口瞬间跳跃 >= 8.5 bps 时立即执行紧急撤单，避开跳空踩踏；
4. [LightGBM 毒性门控] P_toxic > 0.60 强制硬阻断顺向开仓 (BID_CUT / ASK_CUT)；
5. [严格 100% Post-Only (Alo) + 1-Tick 队列优先权保持 (Queue Priority Preservation)]；
6. [单笔名义价值 >= 10.0 USD 动态对齐]。
"""

import os
import sys
import time
import json
import logging
import urllib.request
import datetime
import pytz
import numpy as np
import lightgbm as lgb
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# 配置日志格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/nautilus_cluster/sndk_glft_live.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("GLFT_SNDK_V2_2")

MASTER_WALLET = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
AGENT_PK = "0xd6d4969de4b68c2180fa15e8a40d7d941ca5608b9391f18fddbfc65f8a97d585"
COIN = "xyz:SNDK"
DEX_NAME = "xyz"
TARGET_LEVERAGE = 10
ORDER_NOTIONAL_USD = 11.8 # 严格大于平台 10.0 USD 门槛
MAKER_FEE_BPS = 1.5       # Hyperliquid Maker 1.5 bps
MIN_SPREAD_BPS = 8.0      # 常态全局最小双边利差 8.0 bps
Q_MAX = 0.05              # 严格限制最大绝对库存为 0.05 张
MODEL_PATH = "/opt/nautilus_cluster/models_v2/lgbm_toxicity_gate_SNDK.txt"

# 是否在正股常规盘开盘期强制停机保护做市商
PAUSE_ON_REGULAR_STOCK_HOURS = True

class RWARegimeClassifier:
    """RWA 代币化正股做市状态机"""
    def __init__(self, tz_name="America/New_York"):
        self.tz = pytz.timezone(tz_name)

    def get_market_session(self, utc_timestamp=None):
        if utc_timestamp is None:
            utc_timestamp = time.time()
        dt_utc = datetime.datetime.fromtimestamp(utc_timestamp, pytz.utc)
        dt_local = dt_utc.astimezone(self.tz)
        weekday = dt_local.weekday()

        # 周末全天休市 (周六、周日) -> 纯链上自治黄金期
        if weekday in (5, 6):
            return "WEEKEND_CLOSED", 3.0, True # tag, spread_mult, allow_trade

        minute_of_day = dt_local.hour * 60 + dt_local.minute
        open_time = 9 * 60 + 30
        close_time = 16 * 60
        pre_market_time = 4 * 60
        after_hours_time = 20 * 60

        # 1. 美股常规交易盘中 (9:30 - 16:00 ET): 纳斯达克剧烈波动期，外部信息极度不对称
        if open_time <= minute_of_day < close_time:
            # 若配置了开盘停机保护，则禁止挂单做市
            allow = not PAUSE_ON_REGULAR_STOCK_HOURS
            return "REGULAR_MARKET_HOURS", 1.0, allow

        # 2. 盘前时段 (04:00 - 09:30 ET): 流动性较差，若处于开盘前半小时则提宽 2.5 倍
        elif pre_market_time <= minute_of_day < open_time:
            if (open_time - 30) <= minute_of_day < open_time:
                return "PRE_MARKET_VOLATILE", 2.5, True
            return "PRE_MARKET", 1.8, True

        # 3. 盘后时段 (16:00 - 20:00 ET): 财报与收盘消化，点差加宽 1.5 倍
        elif close_time <= minute_of_day < after_hours_time:
            return "AFTER_HOURS", 1.5, True

        # 4. 夜间全休市时段 (20:00 - 04:00 ET): 纯链上自治期，黄金做市窗口
        else:
            return "OVERNIGHT_CLOSED", 2.5, True

class StationaryGLFTSolver:
    def __init__(self, gamma=0.5, fee_maker_bps=1.5, min_spread_bps=8.0, q_max=Q_MAX):
        self.gamma = gamma
        self.fee_maker_bps = fee_maker_bps
        self.min_spread_bps = min_spread_bps
        self.q_max = q_max

    def compute_quotes(self, mid_price, inventory, sigma, spread_mult=1.0, k_bps=1.5, A=2.0):
        inv_ratio = inventory / self.q_max if self.q_max > 0 else 0.0
        if abs(inv_ratio) >= 0.8:
            lambda_inv = 8.0
        elif abs(inv_ratio) >= 0.5:
            lambda_inv = 3.0
        else:
            lambda_inv = 1.0

        inv_skew = inventory * self.gamma * (sigma ** 2) * lambda_inv * mid_price
        r_price = mid_price - inv_skew

        term_ask_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bps, 0.1)) * 6.0
        term_ask_inv = ((2.0 * inventory + 1.0) * self.gamma * (sigma ** 2) * 10000.0) / (2.0 * max(k_bps, 0.1) * max(A, 0.1))
        d_ask_bps = max(self.min_spread_bps / 2.0, max(1.0, term_ask_base + term_ask_inv)) * spread_mult

        term_bid_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bps, 0.1)) * 6.0
        term_bid_inv = ((2.0 * inventory - 1.0) * self.gamma * (sigma ** 2) * 10000.0) / (2.0 * max(k_bps, 0.1) * max(A, 0.1))
        d_bid_bps = max(self.min_spread_bps / 2.0, max(1.0, term_bid_base - term_bid_inv)) * spread_mult

        min_half = max(self.min_spread_bps / 2.0, self.fee_maker_bps * 2.0) * spread_mult
        d_ask_bps = max(d_ask_bps, min_half)
        d_bid_bps = max(d_bid_bps, min_half)

        bid_p = round(r_price - mid_price * (d_bid_bps * 0.0001), 1)
        ask_p = round(r_price + mid_price * (d_ask_bps * 0.0001), 1)
        return bid_p, ask_p

class HyperliquidLiveMMV22:
    def __init__(self):
        self.agent_account = Account.from_key(AGENT_PK)
        logger.info(f"Agent account initialized: {self.agent_account.address} for Master: {MASTER_WALLET}")

        self.exchange = Exchange(
            wallet=self.agent_account,
            base_url=constants.MAINNET_API_URL,
            account_address=MASTER_WALLET,
            perp_dexs=[DEX_NAME]
        )
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=[DEX_NAME])
        self.regime = RWARegimeClassifier()
        self.solver = StationaryGLFTSolver(gamma=0.5, fee_maker_bps=MAKER_FEE_BPS, min_spread_bps=MIN_SPREAD_BPS, q_max=Q_MAX)
        
        self.lgb_model = None
        if os.path.exists(MODEL_PATH):
            try:
                self.lgb_model = lgb.Booster(model_file=MODEL_PATH)
                logger.info(f"Loaded LightGBM toxicity gate model from {MODEL_PATH}")
            except Exception as e:
                logger.error(f"Failed loading LightGBM model: {e}")

        self.last_bid_fill_time = 0.0
        self.last_ask_fill_time = 0.0
        self.cooldown_sec = 5.0
        self.price_history = []
        self.min_tick = 0.1
        self.last_seen_mid = None

    def get_market_book(self):
        try:
            book = self.info.l2_snapshot(COIN)
            bids = book.get("levels", [[]])[0]
            asks = book.get("levels", [[], []])[1]
            if bids and asks:
                bb = float(bids[0]["px"])
                ba = float(asks[0]["px"])
                return (ba + bb) / 2.0, bb, ba
        except Exception as e:
            logger.error(f"Error fetching L2 book: {e}")
        return None, None, None

    def get_account_state(self):
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=json.dumps({"type": "clearinghouseState", "user": MASTER_WALLET, "dex": DEX_NAME}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())

            inv = 0.0
            for pos in data.get("assetPositions", []):
                p = pos.get("position", {})
                if p.get("coin") == COIN:
                    inv = float(p.get("szi", 0.0))
                    break

            val = float(data.get("marginSummary", {}).get("accountValue", 0.0))
            return inv, val
        except Exception as e:
            logger.error(f"Error fetching account state: {e}")
            return 0.0, 0.0

    def get_current_open_orders(self):
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=json.dumps({"type": "openOrders", "user": MASTER_WALLET, "dex": DEX_NAME}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                orders = json.loads(resp.read().decode())
                return [o for o in orders if o.get("coin") == COIN]
        except Exception as e:
            logger.error(f"Error fetching open orders: {e}")
            return []

    def cancel_order_safe(self, oid):
        try:
            res = self.exchange.cancel(COIN, oid)
            logger.info(f"Cancelled order {oid}: {res}")
            return True
        except Exception as e:
            logger.error(f"Failed cancelling order {oid}: {e}")
            return False

    def cancel_all_orders(self):
        orders = self.get_current_open_orders()
        for o in orders:
            self.cancel_order_safe(o["oid"])

    def place_order_safe(self, is_buy: bool, px: float, sz: float):
        try:
            res = self.exchange.order(
                name=COIN,
                is_buy=is_buy,
                sz=sz,
                limit_px=px,
                order_type={"limit": {"tif": "Alo"}} # 严格 100% Post-Only
            )
            logger.info(f"Placed {'BUY' if is_buy else 'SELL'} @ {px} (Sz: {sz}): {res}")
            return res
        except Exception as e:
            logger.error(f"Failed placing order: {e}")
            return None

    def predict_toxicity(self, mid, sigma, is_buy_side):
        if self.lgb_model is None or len(self.price_history) < 10:
            return 0.0
        try:
            px_mean = np.mean(self.price_history[-10:])
            px_rel = (mid - px_mean) / mid
            feats = np.array([[px_rel, 0.010, sigma, 1 if is_buy_side else 0]])
            p_toxic = float(self.lgb_model.predict(feats)[0])
            return p_toxic
        except Exception as e:
            logger.error(f"Inference error: {e}")
            return 0.0

    def run_strategy_cycle(self):
        logger.info(f"GLFT Active Engine V2.2 LIVE for {COIN} (User: {MASTER_WALLET})...")
        while True:
            try:
                # 1. 检查市场时段 (美股常规盘 09:30-16:00 ET 停机保护)
                session_tag, spread_mult, allow_trade = self.regime.get_market_session()
                
                open_orders = self.get_current_open_orders()

                if not allow_trade:
                    # 处于受保护的正股开盘期：强制撤掉所有挂单，进入安全观察休眠
                    if open_orders:
                        logger.warning(f"Market Session is [{session_tag}]. Regular US market is OPEN! Pausing MM to avoid adverse selection sniper.")
                        self.cancel_all_orders()
                    else:
                        logger.info(f"Market Session [{session_tag}]: Safe Paused (No orders). Waiting for After-Hours/Closed...")
                    time.sleep(10)
                    continue

                mid, best_bid, best_ask = self.get_market_book()
                if not mid:
                    time.sleep(2)
                    continue

                # 2. 检测瞬时大波动与突发跳空 (当 2 秒内价格跳跃超过 8.5 bps 时立即紧急撤单防狙击)
                if self.last_seen_mid is not None:
                    instant_drift_bps = abs(mid - self.last_seen_mid) / self.last_seen_mid * 10000.0
                    if instant_drift_bps >= 8.5 and open_orders:
                        logger.warning(f"High volatility drift detected ({instant_drift_bps:.1f} bps >= 8.5 bps)! Emergency cancelling orders to avoid sniper.")
                        self.cancel_all_orders()
                        self.last_seen_mid = mid
                        time.sleep(3)
                        continue
                self.last_seen_mid = mid

                self.price_history.append(mid)
                if len(self.price_history) > 50:
                    self.price_history.pop(0)

                sigma = max(0.0005, float(np.std(np.diff(np.log(self.price_history))))) if len(self.price_history) >= 10 else 0.001
                inv, acc_val = self.get_account_state()

                if acc_val < 5.0:
                    logger.warning(f"Account value too low: {acc_val:.2f} USDC. Halting.")
                    time.sleep(5)
                    continue

                # 3. GLFT 稳态报价
                target_bid, target_ask = self.solver.compute_quotes(mid, inv, sigma, spread_mult=spread_mult)

                if target_bid >= best_ask:
                    target_bid = round(best_ask - self.min_tick, 1)
                if target_ask <= best_bid:
                    target_ask = round(best_bid + self.min_tick, 1)

                sz_bid = round(max(0.010, ORDER_NOTIONAL_USD / target_bid), 3)
                sz_ask = round(max(0.010, ORDER_NOTIONAL_USD / target_ask), 3)

                now = time.time()
                bid_cooling = (now - self.last_bid_fill_time) < self.cooldown_sec
                ask_cooling = (now - self.last_ask_fill_time) < self.cooldown_sec

                p_toxic_down = self.predict_toxicity(mid, sigma, is_buy_side=True)
                p_toxic_up = self.predict_toxicity(mid, sigma, is_buy_side=False)
                toxic_bid_cut = (p_toxic_down > 0.60)
                toxic_ask_cut = (p_toxic_up > 0.60)

                allow_bid = (not bid_cooling) and (not toxic_bid_cut) and (inv < Q_MAX)
                allow_ask = (not ask_cooling) and (not toxic_ask_cut) and (inv > -Q_MAX)

                # 紧急去库存模式 (持仓超限时优先平仓)
                if inv > Q_MAX:
                    allow_bid = False
                    target_ask = round(max(best_bid + self.min_tick, best_ask), 1)

                current_bid_order = next((o for o in open_orders if o.get("side") == "B"), None)
                current_ask_order = next((o for o in open_orders if o.get("side") == "A"), None)

                # 买单管理
                if not allow_bid and current_bid_order:
                    logger.info(f"BID_CUT (Inv={inv:.2f}, Toxic={p_toxic_down:.2f}), cancelling bid {current_bid_order['oid']}")
                    self.cancel_order_safe(current_bid_order["oid"])
                elif allow_bid:
                    if current_bid_order is None:
                        self.place_order_safe(True, target_bid, sz_bid)
                    else:
                        cur_px = float(current_bid_order["limitPx"])
                        if abs(cur_px - target_bid) > (self.min_tick * 1.5):
                            self.cancel_order_safe(current_bid_order["oid"])
                            self.place_order_safe(True, target_bid, sz_bid)

                # 卖单管理
                if not allow_ask and current_ask_order:
                    logger.info(f"ASK_CUT (Inv={inv:.2f}, Toxic={p_toxic_up:.2f}), cancelling ask {current_ask_order['oid']}")
                    self.cancel_order_safe(current_ask_order["oid"])
                elif allow_ask:
                    if current_ask_order is None:
                        self.place_order_safe(False, target_ask, sz_ask)
                    else:
                        cur_px = float(current_ask_order["limitPx"])
                        if abs(cur_px - target_ask) > (self.min_tick * 1.5):
                            self.cancel_order_safe(current_ask_order["oid"])
                            self.place_order_safe(False, target_ask, sz_ask)

                logger.info(
                    f"Heartbeat | Mid: {mid:.1f} | Inv: {inv:.2f} | Session: {session_tag} (Mult: {spread_mult}x) | "
                    f"P_Toxic[D:{p_toxic_down:.2f}/U:{p_toxic_up:.2f}] | Orders: {len(open_orders)} | AcctVal: {acc_val:.2f} USDC"
                )

                time.sleep(2)

            except Exception as e:
                logger.error(f"Cycle exception: {e}")
                time.sleep(3)

if __name__ == "__main__":
    bot = HyperliquidLiveMMV22()
    bot.run_strategy_cycle()
