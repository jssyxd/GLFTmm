# -*- coding: utf-8 -*-
"""
ant_cross_arb_v4_adaptive.py - 工业级 ANT 跨所双腿套利自适应复利与高阶风控引擎
=====================================================================================================
核心架构与四大系统级升级：
1. 开平仓闭环与波段收割飞轮 (Mean-Reverting Basis Swing & Spread Exit):
   - 开仓门禁：Net Spread >= 750 bps (当前盘口约 830 bps)；
   - 波段止盈平仓：当反向利差回落至 <= 500 bps 时，立即并发双平，吃下 250~300 bps 差价波动收益；
   - 终极收敛平仓：当 SEC S-1 披露或上市使得基差归零 (<= 50 bps) 时执行清仓交割。
2. 双腿合计净利润率止盈守卫 (Combined PnL Take-Profit Gate):
   - 实时监控两腿综合未实现盈亏与已占用保证金；
   - 当两腿合计净收益率 >= 3.0% (例如 HL 亏 6% 而 OKX 盈 9.8%)，立即市价双平锁定确定性利润并释放资金！
3. 极端非对称强平硬断路器 (Single-Leg 8% Price Deviation Breaker):
   - 监控任一腿价格相对开仓加权均价的变动率 |P_cur - P_entry| / P_entry；
   - 任一腿变动 >= 8.0% (在 10x 杠杆下对应 80% 权益波动，逼近交易所强平红线) 时，立即并发市价双平，规避单边穿仓。
4. 孤儿单自动对冲与同步清理器 (Orphan Leg Auto-Reconciler):
   - 后台每 5 秒校验底层真实持仓：若检测到一侧仓位归零 (因外力强平、止损或人工平仓)，而另一侧仍有裸持仓位；
   - 立即向存留侧发送市价单强平，将净敞口瞬间归零，杜绝单腿裸奔，随后重置状态重新进入监控。
5. 木桶短板自适应复利算法 (Bottleneck Adaptive Sizing):
   - 动态读取双边可用现金，取木桶短板 W_bottleneck = min(W_HL, W_OKX)；
   - 设定总持仓名义价值 <= 2.5 * W_bottleneck (保留 75% 游隙现金抵御波动)；
   - 单笔切片受 11.5 USD 保底与 1,500 USD 盘口前三档深度封顶保护，实现利润自动滚存复利。
"""

import os
import sys
import time
import json
import math
import logging
import asyncio
import threading
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("ANT-Arb-V4-Adaptive")

sys.path.insert(0, "/opt/nautilus_cluster/shared")
try:
    from gateway_okx_client import OKXGatewayClient
except ImportError:
    OKXGatewayClient = None

try:
    import websockets
except ImportError:
    websockets = None

try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants
    import eth_account
except ImportError:
    Info, Exchange, constants, eth_account = None, None, None, None


class EngineConfigV4:
    # 标的定义
    HL_SYMBOL = "xyz:ANT"
    OKX_INST_ID = "ANTHROPIC-USDT-SWAP"
    REFILL_SYMBOL = "BTC"
    
    # 股本假设与配平系数
    ASSUMED_SHARES_B = 12.7       # 投行中位数基准 12.7B
    HEDGE_RATIO = 10.0 / ASSUMED_SHARES_B  # ~0.7874
    
    # 核心利差门禁
    ENTRY_SPREAD_BPS = 750.0            # 开仓利差门槛 (当前约为 830 bps)
    EXIT_SWING_PROFIT_BPS = 500.0       # 波段止盈平仓线 (从 800+ 回落至 <= 500 bps 时锁定 250+ bps 波动差价)
    EXIT_TERMINAL_CONVERGE_BPS = 50.0   # 终极规则收敛平仓线 (S-1 修正案或上市抹平利差)
    
    # 收益与风控红线
    TARGET_NET_ROI_PCT = 3.0            # 双腿合计净收益率 >= 3.0% (on margin) 立即止盈双平
    SINGLE_LEG_MAX_PRICE_DRIFT = 0.08   # 单腿价格偏离开仓均价 8% (8% * 10x = 80% 权益)，强制双平止损防爆仓
    MAX_EFFECTIVE_LEVERAGE = 2.5        # 总持仓有效杠杆上限 (2.5x)，保留 75% 游隙现金抵御波动
    
    # 滑点与费率
    MAX_SLIPPAGE_BPS = 5.0              # 单腿最大滑点容忍
    INSTANT_DRIFT_BPS = 8.5             # 瞬时漂移防狙击
    MAX_CARRY_LOSS_APR = 10.0           # 资金费率净支出最大容忍 10% APR
    TARGET_LEVERAGE = 10                # 双边统一打满 10x 杠杆，1:1 保证金占用对称
    
    # 自适应单笔切片约束
    MIN_ORDER_USD = 11.5                # HL 最小 10 USD 名义价值保底
    MAX_LOB_DEPTH_CLIP_USD = 1500.0     # 单笔切片上限，防止击穿前三档盘口产生严重滑点
    
    # 冷却周期
    COOLDOWN_AFTER_FILL_SEC = 15.0      # 成交后冷却期
    SAFETY_FAILURE_COOLDOWN_SEC = 12.0  # 未撮合时 12 秒安全冷却 (遵守 10s 写入约束)
    EMERGENCY_STOP_COOLDOWN_SEC = 60.0  # 触发 8% 止损后的防震荡冷静期


class SharedOrderbookState:
    """双所内存共享盘口与状态缓存 (由 WebSocket 纯异步更新)"""
    def __init__(self):
        self.hl_bid_px = 0.0
        self.hl_bid_sz = 0.0
        self.hl_ask_px = 0.0
        self.hl_ask_sz = 0.0
        self.hl_last_ts = 0.0
        self.hl_updates = 0
        
        self.okx_bid_px = 0.0
        self.okx_bid_sz = 0.0
        self.okx_ask_px = 0.0
        self.okx_ask_sz = 0.0
        self.okx_last_ts = 0.0
        self.okx_updates = 0
        
        self.running = True


class DualExchangeWebSocketManager:
    """双所 WebSocket 监听中继器"""
    def __init__(self, state: SharedOrderbookState, hl_info: Any, okx_inst_id: str):
        self.state = state
        self.hl_info = hl_info
        self.okx_inst_id = okx_inst_id
        self._threads = []

    def start(self):
        t_hl = threading.Thread(target=self._run_hl_ws, daemon=True, name="HL-WS-Thread")
        t_hl.start()
        self._threads.append(t_hl)

        t_okx = threading.Thread(target=self._run_okx_ws_thread, daemon=True, name="OKX-WS-Thread")
        t_okx.start()
        self._threads.append(t_okx)

        logger.info("双所纯 WebSocket 监听中继器已成功启动！")

    def _run_hl_ws(self):
        def on_book_update(msg):
            try:
                data = msg.get("data", {})
                levels = data.get("levels", [[], []])
                bids = levels[0]
                asks = levels[1]
                if bids and asks:
                    self.state.hl_bid_px = float(bids[0]["px"])
                    self.state.hl_bid_sz = float(bids[0]["sz"])
                    self.state.hl_ask_px = float(asks[0]["px"])
                    self.state.hl_ask_sz = float(asks[0]["sz"])
                    self.state.hl_last_ts = time.time()
                    self.state.hl_updates += 1
            except Exception as e:
                logger.error(f"HL WS 解析异常: {e}")

        try:
            sub_id = self.hl_info.subscribe({"type": "l2Book", "coin": "xyz:ANT"}, on_book_update)
            logger.info(f"Hyperliquid xyz:ANT L2Book 订阅成功 (Sub ID: {sub_id})")
            while self.state.running:
                time.sleep(1.0)
        except Exception as e:
            logger.error(f"Hyperliquid WS 崩溃: {e}")

    def _run_okx_ws_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._okx_ws_coro())

    async def _okx_ws_coro(self):
        url = "wss://ws.okx.com:443/ws/v5/public"
        while self.state.running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    sub_payload = {
                        "op": "subscribe",
                        "args": [{"channel": "bbo-tbt", "instId": self.okx_inst_id}]
                    }
                    await ws.send(json.dumps(sub_payload))
                    logger.info("OKX ANTHROPIC-USDT-SWAP bbo-tbt 订阅请求已发送")

                    while self.state.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
                            if raw == "pong":
                                continue
                            msg = json.loads(raw)
                            if "data" in msg and msg["data"]:
                                item = msg["data"][0]
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                if bids and asks:
                                    self.state.okx_bid_px = float(bids[0][0])
                                    self.state.okx_bid_sz = float(bids[0][1])
                                    self.state.okx_ask_px = float(asks[0][0])
                                    self.state.okx_ask_sz = float(asks[0][1])
                                    self.state.okx_last_ts = time.time()
                                    self.state.okx_updates += 1
                        except asyncio.TimeoutError:
                            await ws.send("ping")
            except Exception as e:
                logger.warning(f"OKX WS 连接断开，3 秒后重连: {e}")
                await asyncio.sleep(3.0)


class AtomicDualLegEngineV4:
    def __init__(
        self,
        hl_account: str,
        hl_agent_key: str,
        okx_api_key: str,
        okx_secret: str,
        okx_passphrase: str,
        cfg: EngineConfigV4 = EngineConfigV4()
    ):
        self.cfg = cfg
        self.hl_account = hl_account
        self.hl_agent_key = hl_agent_key
        
        # 客户端初始化
        self.okx = OKXGatewayClient(okx_api_key, okx_secret, okx_passphrase)
        wallet = eth_account.Account.from_key(hl_agent_key)
        self.hl_exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=hl_account, perp_dexs=["xyz"])
        self.hl_info = Info(constants.MAINNET_API_URL, skip_ws=False, perp_dexs=["xyz"])
        
        # WebSocket 行情中继器
        self.state = SharedOrderbookState()
        self.ws_mgr = DualExchangeWebSocketManager(self.state, self.hl_info, self.cfg.OKX_INST_ID)
        
        # 实时动态仓位与均价追踪 (内存状态机)
        self.current_q_hl = 0.0
        self.current_q_okx = 0.0
        self.hl_entry_px = 0.0
        self.okx_entry_px = 0.0
        
        logger.info(f"V4 自适应复利与全风控闭环引擎初始化完成 | HL 地址: {hl_account} | Agent: {wallet.address}")

    def align_account_leverage(self):
        """启动时强制通过 API 同步 OKX 为 10x 交叉杠杆，保持 1:1 保证金占用对称"""
        for attempt in range(1, 4):
            try:
                res = self.okx.set_leverage(self.cfg.OKX_INST_ID, str(self.cfg.TARGET_LEVERAGE), mgn_mode="cross")
                if str(res.get("code", "-1")) == "0":
                    logger.info(f"✅ [OKX 杠杆强制初始化成功] (10x 交叉保证金模式已确认): {res.get('data')}")
                    return
                else:
                    logger.warning(f"⚠️ OKX 杠杆设置第 {attempt} 次失败: {res}，1秒后重试...")
                    time.sleep(1.0)
            except Exception as e:
                logger.error(f"配置 OKX 杠杆异常 (尝试 {attempt}): {e}")
                time.sleep(1.0)

    def get_real_positions(self) -> Tuple[float, float, float]:
        """获取底层真实持仓并计算 Net Delta"""
        q_hl = 0.0
        q_okx = 0.0
        hl_px = 0.0
        okx_px = 0.0
        try:
            # 1. Hyperliquid 真实仓位
            state = self.hl_info.post("/info", {"type": "clearinghouseState", "user": self.hl_account, "dex": "xyz"})
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                if p.get("coin") == self.cfg.HL_SYMBOL:
                    q_hl = float(p.get("szi", 0.0))
                    hl_px = float(p.get("entryPx", 0.0))
                    break

            # 2. OKX 真实仓位
            okx_positions = self.okx.get_positions(inst_type="SWAP", inst_id=self.cfg.OKX_INST_ID)
            for p in okx_positions:
                if p.get("instId") == self.cfg.OKX_INST_ID:
                    pos_str = p.get("pos", "0.0")
                    q_okx = float(pos_str) if pos_str else 0.0
                    avg_px_str = p.get("avgPx", "0.0")
                    okx_px = float(avg_px_str) if avg_px_str else 0.0
        except Exception as e:
            logger.error(f"获取持仓异常: {e}")

        self.current_q_hl = q_hl
        self.current_q_okx = q_okx
        if q_hl > 0.0 and hl_px > 0.0:
            self.hl_entry_px = hl_px
        if abs(q_okx) > 0.0 and okx_px > 0.0:
            self.okx_entry_px = okx_px

        expected_okx_short = q_hl * self.cfg.HEDGE_RATIO
        net_delta = expected_okx_short + q_okx
        return q_hl, q_okx, net_delta

    def get_account_balances(self) -> Tuple[float, float, float, float]:
        """
        获取双边可用资金与总权益
        返回: (w_hl_free, w_okx_free, w_hl_equity, w_okx_equity)
        """
        w_hl_free = 0.0
        w_hl_equity = 0.0
        w_okx_free = 0.0
        w_okx_equity = 0.0

        try:
            # HL 余额
            spot = self.hl_info.spot_user_state(self.hl_account)
            for b in spot.get("balances", []):
                if b.get("coin") == "USDC":
                    w_hl_equity = float(b.get("total", 0.0))
                    break
            # 考虑维持保证金后可用
            avail_pairs = spot.get("tokenToAvailableAfterMaintenance", [])
            if avail_pairs:
                w_hl_free = float(avail_pairs[0][1])
            else:
                w_hl_free = w_hl_equity

            # OKX 余额
            okx_bal = self.okx.get_balance("USDT")
            w_okx_free = float(okx_bal.get("availBal", 0.0))
            w_okx_equity = float(okx_bal.get("eq", 0.0))

        except Exception as e:
            logger.error(f"获取账户资金异常: {e}")

        return w_hl_free, w_okx_free, w_hl_equity, w_okx_equity

    def calculate_adaptive_clip(self, hl_ask: float) -> Tuple[bool, float, str]:
        """
        木桶短板自适应动态 Sizing 算法:
        1. 读取 W_HL_free 与 W_OKX_free
        2. 寻找短板 W_bottleneck = min(W_HL_free, W_OKX_free)
        3. 总持仓有效杠杆上限拦截: notional <= 2.5 * W_bottleneck_equity
        4. 单笔切片受 11.5 USD 保底与 1,500 USD 盘口封顶约束
        """
        try:
            w_hl_free, w_okx_free, w_hl_eq, w_okx_eq = self.get_account_balances()
            w_free_bottleneck = min(w_hl_free, w_okx_free)
            w_eq_bottleneck = min(w_hl_eq, w_okx_eq)

            if w_free_bottleneck < 2.0:
                return False, 0.0, f"可用资金短板不足 2.0 USD (HL: ${w_hl_free:.2f}, OKX: ${w_okx_free:.2f})"

            # 总持仓有效杠杆约束
            current_notional = self.current_q_hl * hl_ask
            max_allowed_notional = w_eq_bottleneck * self.cfg.MAX_EFFECTIVE_LEVERAGE

            if current_notional >= max_allowed_notional:
                return False, 0.0, f"总持仓 (${current_notional:.1f}) 已达有效杠杆上限 (${max_allowed_notional:.1f}, 2.5x)，停止加仓以保留 75% 游隙"

            # 计算单笔切片: 可用短板 * 5% * 10x 杠杆
            raw_clip_usd = w_free_bottleneck * 0.05 * self.cfg.TARGET_LEVERAGE
            # 严格双边约束: 最小 11.5 USD 保底，最大 1500 USD 盘口封顶
            safe_clip_usd = min(max(raw_clip_usd, self.cfg.MIN_ORDER_USD), self.cfg.MAX_LOB_DEPTH_CLIP_USD)
            target_sz_hl = round(safe_clip_usd / hl_ask, 2)
            if target_sz_hl < 0.01:
                target_sz_hl = 0.01

            return True, target_sz_hl, f"切片计算成功: ${safe_clip_usd:.1f} (可用短板: ${w_free_bottleneck:.1f}, 现持名义: ${current_notional:.1f}/${max_allowed_notional:.1f})"

        except Exception as e:
            logger.error(f"自适应切片计算异常: {e}")
            return False, 0.0, str(e)

    def execute_atomic_trade(self, hl_ask: float, okx_bid: float, target_hl_sz: float = 0.1) -> bool:
        """
        严格单向串联原子开仓:
        HL 买入 IOC -> 确认成交 -> 60ms 避开 STP -> OKX 市价对冲卖单
        """
        target_okx_sz = round(target_hl_sz * self.cfg.HEDGE_RATIO, 3)

        # 1. 预检 OKX 可用资金
        notional_okx = target_okx_sz * okx_bid
        req_margin_okx = (notional_okx / self.cfg.TARGET_LEVERAGE) * 1.25
        w_hl_free, w_okx_free, _, _ = self.get_account_balances()
        if w_okx_free < req_margin_okx:
            logger.warning(f"⚠️ OKX 保证金不足放弃触发！可用: ${w_okx_free:.2f} < 需求: ${req_margin_okx:.2f}")
            return False

        # 2. 执行 Leg 1: Hyperliquid IOC 买单
        limit_buy_px = round(hl_ask * (1.0 + self.cfg.MAX_SLIPPAGE_BPS / 10000.0), 2)
        logger.info(f"⚡ [Leg 1] 发送 HL 买入: {target_hl_sz:.2f} 股 @ 限价 ${limit_buy_px:.2f} (IOC)...")

        t0 = time.perf_counter()
        hl_res = self.hl_exchange.order(
            self.cfg.HL_SYMBOL,
            True,
            target_hl_sz,
            limit_buy_px,
            {"limit": {"tif": "Ioc"}}
        )
        elapsed_hl_ms = (time.perf_counter() - t0) * 1000.0

        hl_filled_sz, hl_avg_px = self._parse_hl_fill(hl_res)
        if hl_filled_sz <= 0.0:
            logger.warning(f"❌ [Leg 1 失败/未撮合] HL 返回: {hl_res} (耗时 {elapsed_hl_ms:.1f}ms)，自动阻断，坚决不发 Leg 2！")
            return False

        logger.info(f"✅ [Leg 1 成交] HL 成功买入 {hl_filled_sz} 股 @ ${hl_avg_px} (耗时 {elapsed_hl_ms:.1f}ms)")

        # 3. 60ms 避开 STP 规则，随后发送 Leg 2
        time.sleep(0.06)
        actual_okx_sz = round(hl_filled_sz * self.cfg.HEDGE_RATIO, 3)
        logger.info(f"⚡ [Leg 2] 发送 OKX 对冲卖单: {actual_okx_sz} 张...")

        t1 = time.perf_counter()
        okx_res = self.okx.place_order(
            inst_id=self.cfg.OKX_INST_ID,
            td_mode="cross",
            side="sell",
            ord_type="market",
            sz=str(actual_okx_sz)
        )
        elapsed_okx_ms = (time.perf_counter() - t1) * 1000.0

        if str(okx_res.get("code", "-1")) == "0":
            logger.info(f"🎉 [Leg 2 成交] OKX 对冲成功！双腿闭环完成 (耗时 {elapsed_okx_ms:.1f}ms)")
            # 更新内部状态
            self.get_real_positions()
            return True
        else:
            logger.critical(f"🚨 [Leg 2 失败] OKX 报错: {okx_res}！触发原子断路器，启动 50ms 紧急平仓 HL！")
            self._emergency_flatten_hl(hl_filled_sz, hl_ask)
            return False

    def execute_atomic_close(self, reason: str = "波段止盈平仓") -> bool:
        """
        双腿并发市价平仓通道 (波段止盈 / 规则收敛 / 8% 极端止损)
        """
        q_hl, q_okx, _ = self.get_real_positions()
        if q_hl <= 0.01 and abs(q_okx) <= 0.005:
            logger.info("当前无有效持仓，无需平仓。")
            return True

        logger.warning(f"🚨 [执行双腿平仓] 原因: {reason} | 目标平仓: HL {q_hl:.2f} 股, OKX {abs(q_okx):.3f} 张")

        # 1. 平掉 HL 多头
        hl_close_ok = False
        try:
            res_hl = self.hl_exchange.market_close(self.cfg.HL_SYMBOL)
            logger.info(f"HL 平仓回报: {res_hl}")
            hl_close_ok = res_hl.get("status") == "ok"
            if not hl_close_ok:
                logger.warning("HL 平仓首发未成功，等待 11 秒避开频控窗口后重试平仓...")
                time.sleep(11.0)
                res_hl2 = self.hl_exchange.market_close(self.cfg.HL_SYMBOL)
                logger.info(f"HL 二次平仓回报: {res_hl2}")
                hl_close_ok = res_hl2.get("status") == "ok"
        except Exception as e:
            logger.error(f"HL 平仓异常: {e}")

        # 2. 平掉 OKX 空头
        okx_close_ok = False
        try:
            if abs(q_okx) > 0.001:
                res_okx = self.okx.place_order(
                    inst_id=self.cfg.OKX_INST_ID,
                    td_mode="cross",
                    side="buy", # 买入平空
                    ord_type="market",
                    sz=str(abs(q_okx))
                )
                logger.info(f"OKX 平仓回报: {res_okx}")
                okx_close_ok = str(res_okx.get("code", "-1")) == "0"
            else:
                okx_close_ok = True
        except Exception as e:
            logger.error(f"OKX 平仓异常: {e}")

        if hl_close_ok and okx_close_ok:
            logger.info(f"🎉 [双腿平仓完成] 原因: {reason}，双边仓位已清空恢复中性。")
            self.hl_entry_px = 0.0
            self.okx_entry_px = 0.0
            self.current_q_hl = 0.0
            self.current_q_okx = 0.0
            return True
        return False

    def reconcile_orphan_position(self, q_hl: float, q_okx: float) -> bool:
        """
        孤儿仓位监控与自动清理器:
        若某一腿被强平、止损或外部人工平仓导致归零，而另一腿仍有裸持仓位，立即市价平掉残存腿！
        """
        # 情况 1: OKX 空头已归零，但 HL 仍有多头裸持
        if q_hl > 0.05 and abs(q_okx) < 0.01:
            logger.critical(f"🚨 [检测到单腿裸奔孤儿仓] OKX 空头已归零，HL 仍持有 {q_hl:.2f} 股多头！立即强制市价平仓 HL！")
            try:
                res = self.hl_exchange.market_close(self.cfg.HL_SYMBOL)
                logger.warning(f"HL 孤儿平仓结果: {res}")
                self.hl_entry_px = 0.0
                self.current_q_hl = 0.0
                return True
            except Exception as e:
                logger.error(f"HL 孤儿平仓异常: {e}")
                return False

        # 情况 2: HL 多头已归零，但 OKX 仍有空头裸持
        if q_hl < 0.01 and abs(q_okx) > 0.02:
            logger.critical(f"🚨 [检测到单腿裸奔孤儿仓] HL 多头已归零，OKX 仍持有 {q_okx:.3f} 张空头！立即强制市价平仓 OKX！")
            try:
                res = self.okx.place_order(
                    inst_id=self.cfg.OKX_INST_ID,
                    td_mode="cross",
                    side="buy",
                    ord_type="market",
                    sz=str(abs(q_okx))
                )
                logger.warning(f"OKX 孤儿平仓结果: {res}")
                self.okx_entry_px = 0.0
                self.current_q_okx = 0.0
                return True
            except Exception as e:
                logger.error(f"OKX 孤儿平仓异常: {e}")
                return False

        return False

    def check_single_leg_price_drift(self, hl_mid: float, okx_mid: float) -> bool:
        """
        任一腿价格波动 >= 8% (对应 10x 杠杆下权益 80% 剧烈波动)，立即双平防爆仓
        """
        if self.current_q_hl > 0.05 and self.hl_entry_px > 0.0:
            drift_hl = abs(hl_mid - self.hl_entry_px) / self.hl_entry_px
            if drift_hl >= self.cfg.SINGLE_LEG_MAX_PRICE_DRIFT:
                logger.critical(f"🛑 [触发单腿 8% 价格偏离硬断路] HL 价格漂移达到 {drift_hl:.2%} (入场价: ${self.hl_entry_px:.2f}, 现价: ${hl_mid:.2f})！紧急双平防爆仓！")
                self.execute_atomic_close(reason=f"HL 价格偏离 {drift_hl:.2%} >= 8%")
                return True

        if abs(self.current_q_okx) > 0.05 and self.okx_entry_px > 0.0:
            drift_okx = abs(okx_mid - self.okx_entry_px) / self.okx_entry_px
            if drift_okx >= self.cfg.SINGLE_LEG_MAX_PRICE_DRIFT:
                logger.critical(f"🛑 [触发单腿 8% 价格偏离硬断路] OKX 价格漂移达到 {drift_okx:.2%} (入场价: ${self.okx_entry_px:.2f}, 现价: ${okx_mid:.2f})！紧急双平防爆仓！")
                self.execute_atomic_close(reason=f"OKX 价格偏离 {drift_okx:.2%} >= 8%")
                return True

        return False

    def check_combined_pnl_take_profit(self, hl_mid: float, okx_mid: float) -> bool:
        """
        双腿合计净利润率止盈守卫:
        当综合净收益率 >= 3.0% (on margin)，立即并发双平，锁定利润实现频繁交易飞轮
        """
        if self.current_q_hl <= 0.05 or abs(self.current_q_okx) <= 0.005:
            return False
        if self.hl_entry_px <= 0.0 or self.okx_entry_px <= 0.0:
            return False

        # 1. 计算 HL 浮动盈亏 (多头)
        pnl_hl = self.current_q_hl * (hl_mid - self.hl_entry_px)
        # 10x 杠杆占用保证金
        margin_hl = (self.current_q_hl * self.hl_entry_px) / self.cfg.TARGET_LEVERAGE

        # 2. 计算 OKX 浮动盈亏 (空头)
        pnl_okx = abs(self.current_q_okx) * (self.okx_entry_px - okx_mid)
        margin_okx = (abs(self.current_q_okx) * self.okx_entry_px) / self.cfg.TARGET_LEVERAGE

        total_pnl = pnl_hl + pnl_okx
        total_margin = margin_hl + margin_okx

        if total_margin > 0.0:
            roi_on_margin = (total_pnl / total_margin) * 100.0
            if roi_on_margin >= self.cfg.TARGET_NET_ROI_PCT:
                logger.info(f"🎯 [触发综合净利润止盈] 双腿净浮盈: +${total_pnl:.2f} USD (ROI: +{roi_on_margin:.2f}% >= {self.cfg.TARGET_NET_ROI_PCT}%)！立即双平落袋为安！")
                self.execute_atomic_close(reason=f"双腿净收益率 +{roi_on_margin:.2f}% >= {self.cfg.TARGET_NET_ROI_PCT}% 止盈")
                return True

        return False

    def _parse_hl_fill(self, hl_res: Dict[str, Any]) -> Tuple[float, float]:
        try:
            if hl_res.get("status") != "ok":
                return 0.0, 0.0
            resp_data = hl_res.get("response", {})
            statuses = resp_data.get("data", {}).get("statuses", []) if isinstance(resp_data, dict) else []
            if not statuses:
                return 0.0, 0.0
            st = statuses[0]
            if "filled" in st:
                sz = float(st["filled"].get("totalSz", 0.0))
                px = float(st["filled"].get("avgPx", 0.0))
                return sz, px
            return 0.0, 0.0
        except Exception:
            return 0.0, 0.0

    def _emergency_flatten_hl(self, sz: float, ref_px: float):
        try:
            sell_px = round(ref_px * 0.998, 2)
            res = self.hl_exchange.order(
                self.cfg.HL_SYMBOL,
                False,
                sz,
                sell_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=True
            )
            logger.warning(f"🛡️ [紧急回滚结果] HL 紧急平仓返回: {res}")
        except Exception as e:
            logger.critical(f"FATAL: HL 紧急平仓异常: {e}")

    def run(self):
        logger.info("启动 ANT 跨所套利 V4 自适应复利主循环...")
        self.align_account_leverage()
        self.ws_mgr.start()

        logger.info("等待 WebSocket 盘口首帧接收...")
        for _ in range(30):
            if self.state.hl_ask_px > 0 and self.state.okx_bid_px > 0:
                break
            time.sleep(0.5)

        logger.info(f"WebSocket 数据流就绪！初始盘口 -> HL: ${self.state.hl_bid_px:.2f}/${self.state.hl_ask_px:.2f} | OKX: ${self.state.okx_bid_px:.2f}/${self.state.okx_ask_px:.2f}")

        # 初始同步一次真实持仓与均价
        self.get_real_positions()

        tick = 0
        while self.state.running:
            try:
                tick += 1

                hl_ask = self.state.hl_ask_px
                hl_bid = self.state.hl_bid_px
                okx_bid = self.state.okx_bid_px
                okx_ask = self.state.okx_ask_px

                if hl_ask <= 0 or hl_bid <= 0 or okx_bid <= 0 or okx_ask <= 0:
                    time.sleep(1.0)
                    continue

                # 1. 行情陈旧性死区守卫 (任一边超 2.0s 判定数据无效)
                now_t = time.time()
                hl_age = now_t - self.state.hl_last_ts
                okx_age = now_t - self.state.okx_last_ts
                if hl_age > 6.0 or okx_age > 2.0:
                    if tick % 15 == 0:
                        logger.warning(f"⚠️ [行情陈旧性死区拦截] WS 推流延迟超限 (HL: {hl_age:.2f}s > 6.0s 或 OKX: {okx_age:.2f}s > 2.0s)")
                    time.sleep(0.5)
                    continue

                hl_mid = (hl_ask + hl_bid) / 2.0
                okx_mid = (okx_ask + okx_bid) / 2.0

                # 2. 定期全量持仓与风控巡检 (每 5 秒巡检一次)
                if tick % 5 == 1:
                    q_hl, q_okx, net_delta = self.get_real_positions()

                    # 2.1 孤儿单自动对冲清理器
                    if self.reconcile_orphan_position(q_hl, q_okx):
                        time.sleep(self.cfg.COOLDOWN_AFTER_FILL_SEC)
                        continue

                    # 2.2 单腿 8% 价格偏离硬断路止损
                    if self.check_single_leg_price_drift(hl_mid, okx_mid):
                        time.sleep(self.cfg.EMERGENCY_STOP_COOLDOWN_SEC)
                        continue

                    # 2.3 双腿综合净收益率 >= 3% 止盈
                    if self.check_combined_pnl_take_profit(hl_mid, okx_mid):
                        time.sleep(self.cfg.COOLDOWN_AFTER_FILL_SEC)
                        continue

                    health = "HEALTHY" if abs(net_delta) < 0.08 else f"⚠️ UNBALANCED (Delta: {net_delta:+.3f})"
                    if tick % 15 == 1:
                        logger.info(
                            f"[全量健康巡检] HL仓位: {q_hl:.2f}股 (@${self.hl_entry_px:.2f}) | "
                            f"OKX仓位: {q_okx:.3f}张 (@${self.okx_entry_px:.2f}) | "
                            f"净Delta: {net_delta:+.3f} [{health}]"
                        )

                # 3. 实时利差计算
                # 3.1 开仓利差: 买 HL (hl_ask) + 卖 OKX (okx_bid)
                okx_equiv_bid = okx_bid * (10.0 / self.cfg.ASSUMED_SHARES_B)
                open_spread_bps = ((okx_equiv_bid - hl_ask) / hl_ask) * 10000.0 - 6.0

                # 3.2 平仓反向利差: 卖 HL (hl_bid) + 买 OKX (okx_ask)
                okx_equiv_ask = okx_ask * (10.0 / self.cfg.ASSUMED_SHARES_B)
                close_spread_bps = ((okx_equiv_ask - hl_bid) / hl_bid) * 10000.0 + 6.0

                # 4. 心跳打印
                if tick % 15 == 0:
                    implied_shares = okx_bid * 10.0 / hl_ask
                    logger.info(
                        f"[WS Heartbeat #{tick}] HL Ask: ${hl_ask:.2f} | OKX Bid: ${okx_bid:.2f} (折合: ${okx_equiv_bid:.2f}) | "
                        f"开仓利差: {open_spread_bps:+.1f} bps | 平仓反向利差: {close_spread_bps:+.1f} bps | 隐含股本: {implied_shares:.2f}B"
                    )

                # 5. 状态机动作判断
                # 5.1 检查是否满足平仓条件 (持仓中且利差收窄)
                if self.current_q_hl > 0.05 and abs(self.current_q_okx) > 0.005:
                    # 触发波段止盈 (<= 500 bps) 或 规则终极收敛 (<= 50 bps)
                    if close_spread_bps <= self.cfg.EXIT_SWING_PROFIT_BPS:
                        logger.info(f"🎯 [触发波段止盈平仓] 反向利差回落至 {close_spread_bps:.1f} bps <= {self.cfg.EXIT_SWING_PROFIT_BPS} bps！立即双平落袋为安！")
                        closed = self.execute_atomic_close(reason=f"利差收敛至 {close_spread_bps:.1f} bps 止盈")
                        if closed:
                            time.sleep(self.cfg.COOLDOWN_AFTER_FILL_SEC)
                            continue

                # 5.2 检查是否满足开仓/加仓条件
                if open_spread_bps >= self.cfg.ENTRY_SPREAD_BPS:
                    can_open, dynamic_sz, reason = self.calculate_adaptive_clip(hl_ask)
                    if can_open:
                        logger.info(f"🎯 开仓门禁满足！净利差: {open_spread_bps:+.1f} bps >= {self.cfg.ENTRY_SPREAD_BPS} bps | {reason}")
                        success = self.execute_atomic_trade(hl_ask, okx_bid, dynamic_sz)
                        if success:
                            time.sleep(self.cfg.COOLDOWN_AFTER_FILL_SEC)
                        else:
                            time.sleep(self.cfg.SAFETY_FAILURE_COOLDOWN_SEC)
                    else:
                        if tick % 15 == 0:
                            logger.info(f"开仓门禁满足但风控拦截: {reason}")

                time.sleep(1.0)

            except Exception as e:
                logger.error(f"主调度异常: {e}")
                time.sleep(5.0)


def main():
    OKX_API_KEY = "776e25a5-be71-4c71-a183-ac8859789cfd"
    OKX_API_SECRET = "E96F1B17DBD38ADFBD4C719E3231C124"
    OKX_PASSPHRASE = "Auto111."

    HL_ACCOUNT = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
    HL_AGENT_KEY = "0xd6d4969de4b68c2180fa15e8a40d7d941ca5608b9391f18fddbfc65f8a97d585"

    engine = AtomicDualLegEngineV4(
        hl_account=HL_ACCOUNT,
        hl_agent_key=HL_AGENT_KEY,
        okx_api_key=OKX_API_KEY,
        okx_secret=OKX_API_SECRET,
        okx_passphrase=OKX_PASSPHRASE
    )
    engine.run()


if __name__ == "__main__":
    main()
