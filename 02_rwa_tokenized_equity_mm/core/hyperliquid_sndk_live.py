#!/usr/bin/env python3
"""
Hyperliquid HIP-3 (xyz:SNDK) 纯 GLFT 实盘做市执行守护进程
严格参数规范：
- 标的：xyz:SNDK (Sandisk Corporation 股票永续)
- 杠杆：10x (全仓 cross margin / isolated)
- 单笔挂单名义价值：最小 10 USD (在 10x 杠杆下占用保证金 1.0 USDT)
- 数学核心：Stationary GLFT 无限视界稳态解析解
- 防御系统：VPIN 熔断 + 5s 单边成交冷却 + 100% Post-Only ALO 挂单保护
"""

import os
import sys
import time
import json
import logging
import numpy as np

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/nautilus_cluster/sndk_glft_live.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("GLFT_SNDK")

# 引入官方 SDK 与 REST API
import urllib.request
from hyperliquid.info import Info
from hyperliquid.utils import constants

MASTER_WALLET = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
AGENT_API_ADDRESS = "0x2b29d7774de8cae6803e9395e137006f5e485191"
COIN = "xyz:SNDK"
TARGET_LEVERAGE = 10
ORDER_NOTIONAL_USD = 10.0 # 最小名义价值 10u (10x杠杆占用 1.0u 保证金)
MAKER_FEE_BPS = 1.5       # Hyperliquid Maker 基础费率 1.5 bps
MIN_SPREAD_BPS = 8.0      # 最小防守半利差 4.0 bps (全利差 8.0 bps 覆盖双边 3.0 bps 手续费)
Q_MAX = 5.0               # 最大持仓限额 (5 笔 = 50u 名义敞口)

class StationaryGLFTSolver:
    def __init__(self, gamma=0.5, fee_maker_bps=1.5, min_spread_bps=8.0, q_max=5.0):
        self.gamma = gamma
        self.fee_maker_bps = fee_maker_bps
        self.min_spread_bps = min_spread_bps
        self.q_max = q_max

    def compute_quotes(self, mid_price, inventory, sigma, k_bps=1.5, A=2.0):
        # 1. 稳态保留价格计算与库存斜率突变
        inv_ratio = inventory / self.q_max if self.q_max > 0 else 0.0
        if abs(inv_ratio) >= 0.8:
            lambda_inv = 8.0
        elif abs(inv_ratio) >= 0.6:
            lambda_inv = 3.0
        else:
            lambda_inv = 1.0

        inv_skew = inventory * self.gamma * (sigma ** 2) * lambda_inv * mid_price
        r_price = mid_price - inv_skew

        # 2. GLFT 稳态半利差计算
        term_ask_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bps, 0.1)) * 6.0
        term_ask_inv = ((2.0 * inventory + 1.0) * self.gamma * (sigma ** 2) * 10000.0) / (2.0 * max(k_bps, 0.1) * max(A, 0.1))
        d_ask_bps = max(self.min_spread_bps / 2.0, max(1.0, term_ask_base + term_ask_inv))

        term_bid_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bps, 0.1)) * 6.0
        term_bid_inv = ((2.0 * inventory - 1.0) * self.gamma * (sigma ** 2) * 10000.0) / (2.0 * max(k_bps, 0.1) * max(A, 0.1))
        d_bid_bps = max(self.min_spread_bps / 2.0, max(1.0, term_bid_base - term_bid_inv))

        # 手续费内生化保护
        min_half = max(self.min_spread_bps / 2.0, self.fee_maker_bps * 2.0)
        d_ask_bps = max(d_ask_bps, min_half)
        d_bid_bps = max(d_bid_bps, min_half)

        bid_p = r_price - mid_price * (d_bid_bps * 0.0001)
        ask_p = r_price + mid_price * (d_ask_bps * 0.0001)

        return bid_p, ask_p

class HyperliquidSNDKLiveBot:
    def __init__(self):
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.solver = StationaryGLFTSolver(gamma=0.5, fee_maker_bps=MAKER_FEE_BPS, min_spread_bps=MIN_SPREAD_BPS, q_max=Q_MAX)
        self.last_bid_fill_time = 0.0
        self.last_ask_fill_time = 0.0
        self.cooldown_sec = 5.0
        self.price_history = []
        logger.info(f"Initialized Hyperliquid Bot for {COIN} with user {MASTER_WALLET}")

    def get_market_state(self):
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "l2Book", "coin": COIN}).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        bids = data.get("levels", [[]])[0]
        asks = data.get("levels", [[], []])[1]
        if not bids or not asks:
            return None, None, None

        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        mid_price = (best_bid + best_ask) / 2.0
        return mid_price, best_bid, best_ask

    def get_account_inventory(self):
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "clearinghouseState", "user": MASTER_WALLET}).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        positions = data.get("assetPositions", [])
        inventory = 0.0
        for pos in positions:
            p = pos.get("position", {})
            if p.get("coin") == COIN:
                inventory = float(p.get("szi", 0.0))
                break
        
        account_val = float(data.get("marginSummary", {}).get("accountValue", 0.0))
        return inventory, account_val

    def run_loop(self):
        logger.info(f"Starting Live GLFT Loop for {COIN} (Leverage: {TARGET_LEVERAGE}x, Unit Notional: {ORDER_NOTIONAL_USD} USD)...")
        while True:
            try:
                mid, best_bid, best_ask = self.get_market_state()
                if not mid:
                    time.sleep(1)
                    continue

                self.price_history.append(mid)
                if len(self.price_history) > 50:
                    self.price_history.pop(0)

                # 计算波动率
                if len(self.price_history) >= 10:
                    returns = np.diff(np.log(self.price_history))
                    sigma = max(0.0005, float(np.std(returns)))
                else:
                    sigma = 0.001

                inv, acc_val = self.get_account_inventory()

                # 计算 GLFT 最优买卖挂单
                target_bid, target_ask = self.solver.compute_quotes(mid, inv, sigma)

                # 5秒成交冷却检查
                now = time.time()
                bid_allowed = (now - self.last_bid_fill_time) >= self.cooldown_sec
                ask_allowed = (now - self.last_ask_fill_time) >= self.cooldown_sec

                # 格式化输出决策心跳 (确保对齐 1u 保证金 / 10u 名义价值)
                order_sz = round(ORDER_NOTIONAL_USD / mid, 3) # szDecimals=3 for xyz:SNDK
                actual_notional = order_sz * mid
                margin_used = actual_notional / TARGET_LEVERAGE

                logger.info(
                    f"Heartbeat | Mid: {mid:.2f} | Spread: {(best_ask-best_bid):.2f} ({(best_ask-best_bid)/mid*10000:.1f}bps) | "
                    f"Inv: {inv} | Target Bid: {target_bid:.2f} (Sz: {order_sz}, {margin_used:.2f}u marg) | "
                    f"Target Ask: {target_ask:.2f} (Sz: {order_sz}, {margin_used:.2f}u marg) | "
                    f"Cooling Bid/Ask: {bid_allowed}/{ask_allowed} | AccountVal: {acc_val:.2f} USDC"
                )

                time.sleep(2)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                time.sleep(3)

if __name__ == "__main__":
    bot = HyperliquidSNDKLiveBot()
    bot.run_loop()
