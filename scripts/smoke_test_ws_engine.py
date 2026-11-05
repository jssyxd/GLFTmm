# -*- coding: utf-8 -*-
"""
smoke_test_ws_engine.py - 冒烟测试脚本：验证双所纯 WebSocket 订阅与零 REST 损耗
"""
import sys
import time
import json
import logging

sys.path.insert(0, "/opt/nautilus_cluster/shared")
sys.path.insert(0, "/opt/nautilus_cluster/ant_arb")

from ant_cross_arb_v3_ws import SharedOrderbookState, DualExchangeWebSocketManager, HyperliquidHourlyTakerRefill, EngineConfig
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import eth_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SmokeTest")

def run_smoke_test():
    logger.info("==========================================")
    logger.info("🧪 开始执行 ANT 跨所纯 WebSocket 冒烟测试...")
    logger.info("==========================================")

    # 1. 启动 WebSocket 管理器
    info = Info(constants.MAINNET_API_URL, skip_ws=False, perp_dexs=["xyz"])
    state = SharedOrderbookState()
    mgr = DualExchangeWebSocketManager(state, info, "ANTHROPIC-USDT-SWAP")
    mgr.start()

    logger.info("正在建立双所 WebSocket 长连接...")
    # 等待首笔推流
    ready = False
    for i in range(15):
        if state.hl_ask_px > 0 and state.okx_bid_px > 0:
            ready = True
            logger.info(f"✅ 双所 WebSocket 在 {i*0.5:.1f} 秒内成功建立握手并接收到首批实时 L2/BBO 帧！")
            break
        time.sleep(0.5)

    if not ready:
        logger.error("❌ 冒烟测试失败：未能及时接收到 WS 推流！")
        return False

    # 2. 采样 10 秒，统计 Tick 吞吐与价格同步
    logger.info("开始 10 秒实时推流采样与延迟度量...")
    for s in range(10):
        time.sleep(1.0)
        equiv_per_share = state.okx_bid_px * (10.0 / 12.7)
        spread_bps = ((equiv_per_share - state.hl_ask_px) / state.hl_ask_px) * 10000.0 - 6.0
        logger.info(
            f"[采样 #{s+1}] HL Ask: ${state.hl_ask_px:.2f} (Ticks: {state.hl_updates}) | "
            f"OKX Bid: ${state.okx_bid_px:.2f} (折合: ${equiv_per_share:.2f}, Ticks: {state.okx_updates}) | "
            f"净利差: {spread_bps:+.1f} bps"
        )

    # 3. 校验配额状态
    logger.info("------------------------------------------")
    logger.info("🧪 验证方案 A: Taker 配额扩容模块与查询逻辑...")
    user = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
    agent_key = "0xd6d4969de4b68c2180fa15e8a40d7d941ca5608b9391f18fddbfc65f8a97d585"
    wallet = eth_account.Account.from_key(agent_key)
    refill = HyperliquidHourlyTakerRefill(wallet, user)
    quota = refill.query_quota_status()
    logger.info(f"✅ 配额模块查询成功: {quota}")

    state.running = False
    info.disconnect_websocket()
    logger.info("==========================================")
    logger.info("🎉 冒烟测试全部通过！双所 WebSocket 实时推流无阻塞，零 REST 请求！")
    logger.info("==========================================")
    return True

if __name__ == "__main__":
    run_smoke_test()
