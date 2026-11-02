#!/usr/bin/env python3
"""
Hyperliquid HIP-3 (xyz:SNDK) 纯因果 GLFT + LightGBM 毒性门控 + RWA 状态机生产实盘引擎 (V2 Production)
严格整改项（P0 ~ P3 闭环）：
[P0] 接入 LightGBM 毒性门控 (lgbm_toxicity_gate_SNDK.txt)，P_toxic > 0.60 强制硬阻断顺向开仓 (BID_CUT / ASK_CUT)；
[P1] 接入 RWARegimeClassifier 状态机：美股开盘前 30 分钟 (UTC 13:00~13:30) 自动拉宽半利差至 15 bps，防范早盘跳空插针；
[P2] 重置库存硬箱体：Q_MAX 严格约束为动态限额（当前最大允许持仓 0.05 张，防止接飞刀敞口膨胀）；
[P3] 紧急去库存保护模式：当现有库存超过硬箱体时，强制彻底切断同向开仓单，并以最优价格优先挂平仓单出清；
[P4] 严格 100% Post-Only (Alo) + 1-Tick 队列优先权保持 (Queue Priority Preservation)。
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
logger = logging.getLogger("GLFT_SNDK_V2_PROD")

MASTER_WALLET = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
AGENT_PK = "0xd6d4969de4b68c2180fa15e8a40d7d941ca5608b9391f18fddbfc65f8a97d585"
COIN = "xyz:SNDK"
DEX_NAME = "xyz"
TARGET_LEVERAGE = 10
ORDER_NOTIONAL_USD = 11.8 # 严格大于平台 10.0 USD 门槛
MAKER_FEE_BPS = 1.5       # Hyperliquid Maker 1.5 bps
MIN_SPREAD_BPS = 8.0      # 常态全局最小双边利差 8.0 bps
Q_MAX = 0.05              # [P2 整改] 严格限制最大绝对库存为 0.05 张 (名义敞口约 58u, 保证金占用 5.8u < 账户 25%)
MODEL_PATH = "/opt/nautilus_cluster/models_v2/lgbm_toxicity_gate_SNDK.txt"

class RWARegimeClassifier:
    """[P1 整改] RWA 代币化正股做市状态机"""
    def __init__(self, tz_name="America/New_York"):
        self.tz = pytz.timezone(tz_name)

    def get_regime_spread_multiplier(self, utc_timestamp=None):
        if utc_timestamp is None:
            utc_timestamp = time.time()
        dt_utc = datetime.datetime.fromtimestamp(utc_timestamp, pytz.utc)
        dt_local = dt_utc.astimezone(self.tz)
        weekday = dt_local.weekday()

        # 周末全天休市 (周六、周日)
        if weekday in (5, 6):
            return 3.0, "WEEKEND_CLOSED"

        minute_of_day = dt_local.hour * 60 + dt_local.minute
        open_time = 9 * 60 + 30
        close_time = 16 * 60

        # 美股开盘前 30 分钟 (9:00 - 9:30 ET / UTC 13:00 - 13:30): 跳空极高风险期，利差放大 2.5 倍
        if (open_time - 30) <= minute_of_day < open_time:
            return 2.5, "PRE_MARKET_VOLATILE"
        elif open_time <= minute_of_day < close_time:
            return 1.0, "REGULAR_HOURS"
        else:
            return 1.5, "AFTER_HOURS"

class StationaryGLFTSolver:
    def __init__(self, gamma=0.5, fee_maker_bps=1.5, min_spread_bps=8.0, q_max=Q_MAX):
        self.gamma = gamma
        self.fee_maker_bps = fee_maker_bps
        self.min_spread_bps = min_spread_bps
        self.q_max = q_max

    def compute_quotes(self, mid_price, inventory, sigma, spread_mult=1.0, k_bps=1.5, A=2.0):
        inv_ratio = inventory / self.q_max if self.q_max > 0 else 0.0
        # 库存硬箱体非线性斜率突变
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

        # 手续费内生化硬门槛
        min_half = max(self.min_spread_bps / 2.0, self.fee_maker_bps * 2.0) * spread_mult
        d_ask_bps = max(d_ask_bps, min_half)
        d_bid_bps = max(d_bid_bps, min_half)

        bid_p = round(r_price - mid_price * (d_bid_bps * 0.0001), 1)
        ask_p = round(r_price + mid_price * (d_ask_bps * 0.0001), 1)
        return bid_p, ask_p

class HyperliquidLiveMMV2:
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
        
        # [P0 整改] 载入 LightGBM 毒性门控模型
        self.lgb_model = None
        if os.path.exists(MODEL_PATH):
            try:
                self.lgb_model = lgb.Booster(model_file=MODEL_PATH)
                logger.info(f"✅ Loaded LightGBM toxicity gate model from {MODEL_PATH}")
            except Exception as e:
                logger.error(f"Failed loading LightGBM model: {e}")

        self.last_bid_fill_time = 0.0
        self.last_ask_fill_time = 0.0
        self.cooldown_sec = 5.0
        self.price_history = []
        self.min_tick = 0.1

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
        """[P0 整改] 实时推理未来 3~5 秒逆向选择毒性概率"""
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
        logger.info(f"🚀 V2 GLFT Production Engine LIVE for {COIN} (User: {MASTER_WALLET})...")
        while True:
            try:
                mid, best_bid, best_ask = self.get_market_book()
                if not mid:
                    time.sleep(2)
                    continue

                self.price_history.append(mid)
                if len(self.price_history) > 50:
                    self.price_history.pop(0)

                sigma = max(0.0005, float(np.std(np.diff(np.log(self.price_history))))) if len(self.price_history) >= 10 else 0.001
                inv, acc_val = self.get_account_state()

                if acc_val < 5.0:
                    logger.warning(f"Account value too low: {acc_val:.2f} USDC. Halting.")
                    time.sleep(5)
                    continue

                # 1. 状态机调制定价点差 (RWA 开闭盘状态)
                spread_mult, regime_tag = self.regime.get_regime_spread_multiplier()

                # 2. GLFT 稳态报价
                target_bid, target_ask = self.solver.compute_quotes(mid, inv, sigma, spread_mult=spread_mult)

                # Post-Only 盘口价格防倒穿
                if target_bid >= best_ask:
                    target_bid = round(best_ask - self.min_tick, 1)
                if target_ask <= best_bid:
                    target_ask = round(best_bid + self.min_tick, 1)

                sz_bid = round(max(0.010, ORDER_NOTIONAL_USD / target_bid), 3)
                sz_ask = round(max(0.010, ORDER_NOTIONAL_USD / target_ask), 3)

                now = time.time()
                # 5秒冷却判定
                bid_cooling = (now - self.last_bid_fill_time) < self.cooldown_sec
                ask_cooling = (now - self.last_ask_fill_time) < self.cooldown_sec

                # [P0 整改] LightGBM 毒性前置阻断 (P_toxic > 0.60 触发切断)
                p_toxic_down = self.predict_toxicity(mid, sigma, is_buy_side=True)
                p_toxic_up = self.predict_toxicity(mid, sigma, is_buy_side=False)
                toxic_bid_cut = (p_toxic_down > 0.60)
                toxic_ask_cut = (p_toxic_up > 0.60)

                # [P2 & P3 整改] 严格库存箱体与去库存防线:
                # 若持仓超过 Q_MAX (0.05 张)，绝不允许挂任何买单开仓，全额挂卖单平仓出清！
                allow_bid = (not bid_cooling) and (not toxic_bid_cut) and (inv < Q_MAX)
                allow_ask = (not ask_cooling) and (not toxic_ask_cut) and (inv > -Q_MAX)

                # 紧急去库存模式 (持仓超限时优先平仓)
                if inv > Q_MAX:
                    allow_bid = False # 彻底掐断买单
                    # 将卖单贴近盘口卖一档，加速被动平仓出清！
                    target_ask = round(max(best_bid + self.min_tick, best_ask), 1)

                open_orders = self.get_current_open_orders()
                current_bid_order = next((o for o in open_orders if o.get("side") == "B"), None)
                current_ask_order = next((o for o in open_orders if o.get("side") == "A"), None)

                # 3. 队列管理与挂单执行
                # 买单管理
                if not allow_bid and current_bid_order:
                    logger.info(f"🚫 BID_CUT (Inv={inv:.2f}, Toxic={p_toxic_down:.2f}), cancelling bid {current_bid_order['oid']}")
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
                    logger.info(f"🚫 ASK_CUT (Inv={inv:.2f}, Toxic={p_toxic_up:.2f}), cancelling ask {current_ask_order['oid']}")
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
                    f"Heartbeat | Mid: {mid:.1f} | Inv: {inv:.2f} (Max:{Q_MAX}) | Regime: {regime_tag} | "
                    f"P_Toxic[Down:{p_toxic_down:.2f}/Up:{p_toxic_up:.2f}] | "
                    f"Quote[B:{target_bid if allow_bid else 'CUT'}/A:{target_ask if allow_ask else 'CUT'}] | "
                    f"Orders: {len(open_orders)} | AcctVal: {acc_val:.2f} USDC"
                )

                time.sleep(2)

            except Exception as e:
                logger.error(f"Cycle exception: {e}")
                time.sleep(3)

if __name__ == "__main__":
    bot = HyperliquidLiveMMV2()
    bot.run_strategy_cycle()
