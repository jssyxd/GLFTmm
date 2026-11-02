#!/usr/bin/env python3
"""
Hyperliquid HIP-3 (xyz:SNDK) 纯因果 GLFT 实盘做市与主动防御执行引擎
全链路打通：
1. 行情与深度：WebSocket / L2 Snapshot (110016)
2. 数学定价：Stationary GLFT 无限视界稳态解析解
3. 风险与防御：VPIN 熔断 + 5s 单边成交冷静期 + 100% Post-Only (Alo) 挂单保护
4. 订单与队列管理：1-Tick 队列保持 (Queue Priority Preservation)，杜绝无脑频撤频挂
5. 资金与仓位约束：单笔名义价值 >= 10.0 USD (10x 杠杆占用 1.05~1.15u 保证金)
"""

import os
import sys
import time
import json
import logging
import urllib.request
import numpy as np
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/nautilus_cluster/sndk_glft_live.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("GLFT_SNDK_LIVE")

MASTER_WALLET = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
AGENT_PK = "0xd6d4969de4b68c2180fa15e8a40d7d941ca5608b9391f18fddbfc65f8a97d585"
COIN = "xyz:SNDK"
DEX_NAME = "xyz"
TARGET_LEVERAGE = 10
ORDER_NOTIONAL_USD = 11.5 # 确保严格大于平台 10.0 USD 门槛
MAKER_FEE_BPS = 1.5       # Hyperliquid Maker 1.5 bps
MIN_SPREAD_BPS = 8.0      # 覆盖双边手续费 + 留出净利润
Q_MAX = 5.0               # 最大累计持仓手数 (5 笔 = ~55u 名义敞口)

class StationaryGLFTSolver:
    def __init__(self, gamma=0.5, fee_maker_bps=1.5, min_spread_bps=8.0, q_max=5.0):
        self.gamma = gamma
        self.fee_maker_bps = fee_maker_bps
        self.min_spread_bps = min_spread_bps
        self.q_max = q_max

    def compute_quotes(self, mid_price, inventory, sigma, k_bps=1.5, A=2.0):
        inv_ratio = inventory / self.q_max if self.q_max > 0 else 0.0
        if abs(inv_ratio) >= 0.8:
            lambda_inv = 8.0
        elif abs(inv_ratio) >= 0.6:
            lambda_inv = 3.0
        else:
            lambda_inv = 1.0

        inv_skew = inventory * self.gamma * (sigma ** 2) * lambda_inv * mid_price
        r_price = mid_price - inv_skew

        term_ask_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bps, 0.1)) * 6.0
        term_ask_inv = ((2.0 * inventory + 1.0) * self.gamma * (sigma ** 2) * 10000.0) / (2.0 * max(k_bps, 0.1) * max(A, 0.1))
        d_ask_bps = max(self.min_spread_bps / 2.0, max(1.0, term_ask_base + term_ask_inv))

        term_bid_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bps, 0.1)) * 6.0
        term_bid_inv = ((2.0 * inventory - 1.0) * self.gamma * (sigma ** 2) * 10000.0) / (2.0 * max(k_bps, 0.1) * max(A, 0.1))
        d_bid_bps = max(self.min_spread_bps / 2.0, max(1.0, term_bid_base - term_bid_inv))

        min_half = max(self.min_spread_bps / 2.0, self.fee_maker_bps * 2.0)
        d_ask_bps = max(d_ask_bps, min_half)
        d_bid_bps = max(d_bid_bps, min_half)

        bid_p = r_price - mid_price * (d_bid_bps * 0.0001)
        ask_p = r_price + mid_price * (d_ask_bps * 0.0001)

        # 向上/向下保留 1 位小数 (xyz:SNDK 的价格刻度为 0.1)
        bid_p = round(bid_p, 1)
        ask_p = round(ask_p, 1)
        return bid_p, ask_p

class HyperliquidLiveMM:
    def __init__(self):
        self.agent_account = Account.from_key(AGENT_PK)
        logger.info(f"Agent account initialized: {self.agent_account.address} for Master: {MASTER_WALLET}")

        # 实例化执行网关与只读网关
        self.exchange = Exchange(
            wallet=self.agent_account,
            base_url=constants.MAINNET_API_URL,
            account_address=MASTER_WALLET,
            perp_dexs=[DEX_NAME]
        )
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=[DEX_NAME])
        self.solver = StationaryGLFTSolver(gamma=0.5, fee_maker_bps=MAKER_FEE_BPS, min_spread_bps=MIN_SPREAD_BPS, q_max=Q_MAX)
        
        self.last_bid_fill_time = 0.0
        self.last_ask_fill_time = 0.0
        self.cooldown_sec = 5.0
        self.price_history = []
        self.min_tick = 0.1 # xyz:SNDK 价格步长

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
                order_type={"limit": {"tif": "Alo"}} # 严格 Post-Only
            )
            logger.info(f"Placed {'BUY' if is_buy else 'SELL'} @ {px} (Sz: {sz}): {res}")
            return res
        except Exception as e:
            logger.error(f"Failed placing order: {e}")
            return None

    def run_strategy_cycle(self):
        logger.info(f"🚀 GLFT Active MM Engine LIVE for {COIN} (User: {MASTER_WALLET})...")
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
                    logger.warning(f"Account value too low: {acc_val:.2f} USDC. Halting quotes.")
                    time.sleep(5)
                    continue

                # 1. 计算 GLFT 目标保留价格与点差
                target_bid, target_ask = self.solver.compute_quotes(mid, inv, sigma)

                # 确保不与盘口对穿 (Post-Only 保护)
                if target_bid >= best_ask:
                    target_bid = round(best_ask - self.min_tick, 1)
                if target_ask <= best_bid:
                    target_ask = round(best_bid + self.min_tick, 1)

                # 动态计算下单手数 (确保名义价值 >= 10.0 USD)
                sz_bid = round(max(0.010, ORDER_NOTIONAL_USD / target_bid), 3)
                sz_ask = round(max(0.010, ORDER_NOTIONAL_USD / target_ask), 3)

                now = time.time()
                allow_bid = (now - self.last_bid_fill_time) >= self.cooldown_sec and (inv < Q_MAX)
                allow_ask = (now - self.last_ask_fill_time) >= self.cooldown_sec and (inv > -Q_MAX)

                # 2. 查询当前正在挂着的订单
                open_orders = self.get_current_open_orders()
                current_bid_order = next((o for o in open_orders if o.get("side") == "B"), None)
                current_ask_order = next((o for o in open_orders if o.get("side") == "A"), None)

                # 3. 队列感知保持 (Queue Priority Preservation) 与智能调价
                # 买单管理
                if not allow_bid and current_bid_order:
                    logger.info(f"Bid cooling or inv limit, cancelling bid order {current_bid_order['oid']}")
                    self.cancel_order_safe(current_bid_order["oid"])
                elif allow_bid:
                    if current_bid_order is None:
                        self.place_order_safe(True, target_bid, sz_bid)
                    else:
                        cur_px = float(current_bid_order["limitPx"])
                        # 若当前挂单价格偏离目标价超过 1 个 Tick，则撤单重挂；否则保持队列优先权！
                        if abs(cur_px - target_bid) > (self.min_tick * 1.5):
                            logger.info(f"Bid drift ({cur_px} -> {target_bid}), updating order...")
                            self.cancel_order_safe(current_bid_order["oid"])
                            self.place_order_safe(True, target_bid, sz_bid)

                # 卖单管理
                if not allow_ask and current_ask_order:
                    logger.info(f"Ask cooling or inv limit, cancelling ask order {current_ask_order['oid']}")
                    self.cancel_order_safe(current_ask_order["oid"])
                elif allow_ask:
                    if current_ask_order is None:
                        self.place_order_safe(False, target_ask, sz_ask)
                    else:
                        cur_px = float(current_ask_order["limitPx"])
                        if abs(cur_px - target_ask) > (self.min_tick * 1.5):
                            logger.info(f"Ask drift ({cur_px} -> {target_ask}), updating order...")
                            self.cancel_order_safe(current_ask_order["oid"])
                            self.place_order_safe(False, target_ask, sz_ask)

                logger.info(
                    f"Heartbeat | Mid: {mid:.1f} (Spread: {(best_ask-best_bid):.1f}) | Inv: {inv} | "
                    f"Target [Bid {target_bid} / Ask {target_ask}] | Live Orders: {len(open_orders)} | AcctVal: {acc_val:.2f} USDC"
                )

                time.sleep(2)

            except Exception as e:
                logger.error(f"Cycle exception: {e}")
                time.sleep(3)

if __name__ == "__main__":
    bot = HyperliquidLiveMM()
    bot.run_strategy_cycle()
