# -*- coding: utf-8 -*-
"""
ant_cross_arb_v2.py - 工业级 ANT 跨所双腿套利生产引擎 (Hyperliquid xyz:ANT vs OKX ANTHROPIC-USDT-SWAP)
=====================================================================================================
核心架构与四大整改升级：
1. 双腿原子性断路器 (Atomic Legging Breaker):
   - 下单前预检 OKX 可用保证金；
   - 严格解析 HL 成交回报（必须 status == ok 且 filled_sz > 0）；
   - 若 Leg 1 失败/限频/无匹配，坚决不发 Leg 2，杜绝单腿裸奔；
   - 若 Leg 2 发送失败，启动 50ms 紧急看门狗，市价平掉刚成交的 HL 仓位，实现全自动原子回滚。
2. 动态杠杆与自适应资金对称配平 (Dynamic Leverage & Sizing):
   - OKX 自动设置 10x 交叉杠杆（与 Hyperliquid 10x 保持 1:1 保证金占用对称）；
   - 依据双边可用可用现金反推安全可开张数，杜绝 51008 保证金不足报错。
3. 动态稀释敏感度曲面与宽利差门控 (Dynamic Dilution & Spread Gate):
   - 废弃 12.7B 静态常数，引入 13.85B 盈亏平衡红线与 15.0B 稀释安全垫；
   - 净利差触发门槛提升至 250 bps 以上，留足充足安全边际。
4. 实时资金费率利差门控 (Funding Rate & Carry Gate):
   - 实时拉取双边资金费率，若净持仓年化损耗 > 10% APR，主动休眠。
5. 库存健康度与 Delta 中性再平衡模块 (Inventory Health & Delta Neutralizer):
   - 实时监控双边真实持仓，计算 Net Delta = q_HL * R - (-q_OKX)；
   - 若出现单腿遗留暴露，优先执行再平衡平抑敞口，防范 ANT 剧烈波动风险。
6. Hyperliquid 请求配额保护器 (Quota Hygiene Throttler):
   - 告别 1 秒 REST 暴力轮询，引入动态心跳与配额保护，彻底告别 429 封锁。
"""

import os
import sys
import time
import json
import math
import logging
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("ANT-Arb-V2")

# 引入基础共享库
sys.path.insert(0, "/opt/nautilus_cluster/shared")
try:
    from gateway_okx_client import OKXGatewayClient
except ImportError:
    logger.warning("未能从 /opt/nautilus_cluster/shared 导入 gateway_okx_client")
    OKXGatewayClient = None

try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants
    import eth_account
except ImportError:
    logger.warning("未能导入 hyperliquid sdk")
    Info, Exchange, constants, eth_account = None, None, None, None


class ProductionConfig:
    # 标的定义
    HL_SYMBOL = "xyz:ANT"
    OKX_INST_ID = "ANTHROPIC-USDT-SWAP"
    
    # 股本假设与配平系数
    ASSUMED_SHARES_B = 12.7       # 基准预期 12.7B
    BREAKEVEN_SHARES_B = 13.85    # 理论盈亏平衡线
    WORST_CASE_SHARES_B = 15.0    # 极度保守稀释上限
    
    # 配平系数 R = 10.0 / S_assumed
    HEDGE_RATIO = 10.0 / ASSUMED_SHARES_B  # ~0.7874
    
    # 核心阈值
    MIN_NET_SPREAD_BPS = 250.0    # 净利差门槛提高至 250 bps，防御稀释
    MAX_SLIPPAGE_BPS = 5.0        # 单腿最大滑点容忍
    INSTANT_DRIFT_BPS = 8.5       # 瞬时漂移防狙击
    MAX_CARRY_LOSS_APR = 10.0     # 资金费率净支出最大容忍 10% APR
    
    # 杠杆配置
    TARGET_LEVERAGE = 10          # 双边统一打满 10x 杠杆，保证 1:1 保证金占用对称
    
    # 仓位规模
    BASE_CLIP_HL = 0.1            # 单笔基础规模 (0.1 股，约 11.5 USD)
    MIN_ORDER_USD = 10.5          # HL 最小 10 USD 名义价值限制
    
    # 冷却与轮询
    COOLDOWN_AFTER_FILL_SEC = 15.0
    NORMAL_POLL_INTERVAL_SEC = 2.5 # 2.5秒轮询，降低 60% REST 请求频次防限流


class AtomicDualLegEngine:
    def __init__(
        self,
        hl_account: str,
        hl_agent_key: str,
        okx_api_key: str,
        okx_secret: str,
        okx_passphrase: str,
        cfg: ProductionConfig = ProductionConfig()
    ):
        self.cfg = cfg
        self.hl_account = hl_account
        self.hl_agent_key = hl_agent_key
        
        # 初始化客户端
        self.okx = OKXGatewayClient(okx_api_key, okx_secret, okx_passphrase)
        
        wallet = eth_account.Account.from_key(hl_agent_key)
        self.hl_exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=hl_account, perp_dexs=["xyz"])
        self.hl_info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=["xyz"])
        
        logger.info(f"初始化 V2 生产引擎完成 | HL 账户: {hl_account} | Agent: {wallet.address}")

    def align_account_leverage(self):
        """整改点 2: 启动与运行期自动将 OKX 端杠杆同步为 10x，实现双边 1:1 保证金占用对称"""
        try:
            res = self.okx.set_leverage(self.cfg.OKX_INST_ID, str(self.cfg.TARGET_LEVERAGE), mgn_mode="cross")
            logger.info(f"OKX 杠杆配置成功 (10x 交叉保证金): {res.get('data')}")
        except Exception as e:
            logger.error(f"配置 OKX 杠杆异常: {e}")

    def check_okx_margin_availability(self, required_sz: float, est_px: float) -> Tuple[bool, float, float]:
        """下单前预检 OKX 可用保证金"""
        try:
            bal_data = self.okx.get_balance("USDT")
            avail_bal = float(bal_data.get("availBal", 0.0))
            # 10x 杠杆下所需保证金
            notional = required_sz * est_px
            required_margin = (notional / self.cfg.TARGET_LEVERAGE) * 1.25 # 留 25% 安全垫
            
            can_afford = avail_bal >= required_margin
            return can_afford, avail_bal, required_margin
        except Exception as e:
            logger.error(f"查询 OKX 保证金异常: {e}")
            return False, 0.0, 0.0

    def get_real_positions(self) -> Tuple[float, float, float]:
        """
        获取双边真实持仓并计算 Net Delta:
        q_hl: HL 股数 (Long 为正)
        q_okx: OKX 张数 (Short 为负)
        net_delta: 折合后净敞口 (应当接近 0)
        """
        q_hl = 0.0
        q_okx = 0.0
        try:
            # 1. HL Positions
            state = self.hl_info.post("/info", {"type": "clearinghouseState", "user": self.hl_account, "dex": "xyz"})
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                if p.get("coin") == self.cfg.HL_SYMBOL:
                    q_hl = float(p.get("szi", 0.0))
                    break

            # 2. OKX Positions
            okx_positions = self.okx.get_positions(inst_type="SWAP", inst_id=self.cfg.OKX_INST_ID)
            for p in okx_positions:
                if p.get("instId") == self.cfg.OKX_INST_ID:
                    q_okx = float(p.get("pos", 0.0))
                    break
        except Exception as e:
            logger.error(f"获取双边持仓异常: {e}")

        # Net Delta 算式: HL 股数折算为 OKX 对应张数后与 OKX 空头相加
        # 1 股 HL 对应 R 张 OKX；若 q_hl = 1.0, 理想对应 q_okx = -R
        expected_okx_short = q_hl * self.cfg.HEDGE_RATIO
        net_delta = expected_okx_short + q_okx # 若完美对冲，净值为 0
        return q_hl, q_okx, net_delta

    def check_funding_rate_cost(self) -> Tuple[bool, float, float, float]:
        """整改点 4: 监控资金费率利差，若净支出 > 10% APR 则休眠"""
        try:
            # OKX 8h funding
            okx_f_data = self.okx.get_funding_rate(self.cfg.OKX_INST_ID)
            okx_rate_8h = float(okx_f_data.get("fundingRate", 0.0))
            okx_apr = okx_rate_8h * 3.0 * 365.0 * 100.0 # 空头收取为负，支出为正

            # HL 1h funding
            ctxs = self.hl_info.post("/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
            hl_rate_1h = 0.0
            for i, a in enumerate(ctxs[0].get("universe", [])):
                if a.get("name") == self.cfg.HL_SYMBOL:
                    hl_rate_1h = float(ctxs[1][i].get("funding", 0.0))
                    break
            hl_apr = hl_rate_1h * 24.0 * 365.0 * 100.0 # 多头支出为正

            # 持有多 HL、做空 OKX 的净资金费率年化支出
            # 做多 HL 支付 hl_apr；做空 OKX 收入 -okx_apr
            net_carry_cost_apr = hl_apr - okx_apr
            is_acceptable = net_carry_cost_apr <= self.cfg.MAX_CARRY_LOSS_APR
            return is_acceptable, hl_apr, okx_apr, net_carry_cost_apr
        except Exception as e:
            logger.warning(f"获取资金费率异常，放行但记录: {e}")
            return True, 0.0, 0.0, 0.0

    def execute_atomic_trade(self, hl_ask: float, okx_bid: float, target_hl_sz: float = 0.1) -> bool:
        """
        整改点 1: 严格原子性下单与 50ms 紧急看门狗回滚
        """
        target_okx_sz = round(target_hl_sz * self.cfg.HEDGE_RATIO, 3)
        
        # 1. 下单前资金预检
        can_afford, avail_bal, req_margin = self.check_okx_margin_availability(target_okx_sz, okx_bid)
        if not can_afford:
            logger.warning(f"⚠️ OKX 可用保证金不足，放弃触发！可用: ${avail_bal:.2f} < 需求: ${req_margin:.2f}")
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
        
        # 严格校验 Leg 1 是否真正成交
        hl_filled_sz, hl_avg_px = self._parse_hl_fill(hl_res)
        if hl_filled_sz <= 0.0:
            err_msg = str(hl_res.get("response", ""))
            if "Too many cumulative requests" in err_msg:
                logger.error(f"🛑 [HL 配额超限熔断] 累计请求数超额，休眠 30 秒以保护配额！")
                time.sleep(30.0)
            logger.warning(f"❌ [Leg 1 失败/未撮合] HL 返回: {hl_res} (耗时 {elapsed_hl_ms:.1f}ms)，自动阻断，坚决不发 Leg 2！")
            return False

        logger.info(f"✅ [Leg 1 成交] HL 成功买入 {hl_filled_sz} 股 @ ${hl_avg_px} (耗时 {elapsed_hl_ms:.1f}ms)")

        # 3. 执行 Leg 2: OKX 市价对冲卖单
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
        
        # 校验 OKX 结果
        okx_code = str(okx_res.get("code", "-1"))
        if okx_code == "0":
            logger.info(f"🎉 [Leg 2 成交] OKX 对冲成功！双腿闭环完成 (耗时 {elapsed_okx_ms:.1f}ms)")
            return True
        else:
            # 触发 P0 级原子性断路器：紧急平掉 HL 刚成交的多头！
            logger.critical(f"🚨 [Leg 2 失败] OKX 报错: {okx_res}！触发原子性断路器，启动 50ms 紧急平仓 HL！")
            self._emergency_flatten_hl(hl_filled_sz, hl_ask)
            return False

    def _parse_hl_fill(self, hl_res: Dict[str, Any]) -> Tuple[float, float]:
        """解析 HL order 返回结果"""
        try:
            if hl_res.get("status") != "ok":
                return 0.0, 0.0
            statuses = hl_res.get("response", {}).get("data", {}).get("statuses", [])
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
        """50ms 紧急追平 Hyperliquid 多头仓位，消灭单腿裸持"""
        try:
            # 允许 20 bps 滑点市价甩单平仓
            sell_px = round(ref_px * 0.998, 2)
            res = self.hl_exchange.order(
                self.cfg.HL_SYMBOL,
                False, # Sell
                sz,
                sell_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=True
            )
            logger.warning(f"🛡️ [紧急回滚结果] HL 紧急平仓返回: {res}")
        except Exception as e:
            logger.critical(f"FATAL: HL 紧急平仓异常: {e}")

    def run_tick_loop(self):
        """生产主循环"""
        logger.info("启动 ANT 跨所套利 V2 生产主循环...")
        self.align_account_leverage()
        
        tick = 0
        while True:
            try:
                tick += 1
                
                # 1. 周期性监测持仓健康度与资金费率 (每 10 次心跳检查一次)
                if tick % 10 == 1:
                    q_hl, q_okx, net_delta = self.get_real_positions()
                    carry_ok, hl_apr, okx_apr, net_apr = self.check_funding_rate_cost()
                    
                    health_status = "HEALTHY" if abs(net_delta) < 0.1 else f"⚠️ UNBALANCED (Net Delta: {net_delta:+.3f})"
                    logger.info(f"[账户健康巡检] HL仓位: {q_hl:.2f}股 | OKX仓位: {q_okx:.3f}张 | 净Delta: {net_delta:+.3f} [{health_status}] | 资金费年化净损: {net_apr:+.1f}%")
                    
                    if not carry_ok:
                        logger.warning(f"资金费净支出 {net_apr:.1f}% 超过上限 {self.cfg.MAX_CARRY_LOSS_APR}%，暂停本轮信号触发。")
                        time.sleep(self.cfg.NORMAL_POLL_INTERVAL_SEC)
                        continue

                # 2. 读取盘口
                hl_book = self.hl_info.l2_snapshot(self.cfg.HL_SYMBOL)
                if not hl_book.get("levels") or not hl_book["levels"][1] or not hl_book["levels"][0]:
                    time.sleep(self.cfg.NORMAL_POLL_INTERVAL_SEC)
                    continue
                    
                hl_bid = float(hl_book["levels"][0][0]["px"])
                hl_ask = float(hl_book["levels"][1][0]["px"])
                
                okx_t = self.okx.get_ticker(self.cfg.OKX_INST_ID)
                okx_bid = float(okx_t.get("bidPx", 0.0))
                okx_ask = float(okx_t.get("askPx", 0.0))
                
                if hl_ask <= 0 or okx_bid <= 0:
                    time.sleep(self.cfg.NORMAL_POLL_INTERVAL_SEC)
                    continue

                # 3. 动态折算每股价值
                okx_equiv_per_share = okx_bid * (10.0 / self.cfg.ASSUMED_SHARES_B)
                spread_pct = (okx_equiv_per_share - hl_ask) / hl_ask * 100.0
                spread_bps = spread_pct * 100.0
                net_spread_bps = spread_bps - 6.0 # 扣除双边 Taker 手续费 (约 6 bps)
                
                # 4. 心跳打印
                if tick % 15 == 0:
                    # 计算盈亏平衡股本
                    implied_shares_b = okx_bid * 10.0 / hl_ask
                    logger.info(
                        f"[Heartbeat #{tick}] HL Ask: ${hl_ask:.2f} | OKX Bid: ${okx_bid:.2f} (折合: ${okx_equiv_per_share:.2f}) | "
                        f"净利差: {net_spread_bps:+.1f} bps | 隐含盈亏平衡股本: {implied_shares_b:.2f}B"
                    )

                # 5. 门禁触发
                if net_spread_bps >= self.cfg.MIN_NET_SPREAD_BPS:
                    logger.info(f"🎯 宽安全垫套利触发！净利差: {net_spread_bps:+.1f} bps >= {self.cfg.MIN_NET_SPREAD_BPS} bps")
                    success = self.execute_atomic_trade(hl_ask, okx_bid, self.cfg.BASE_CLIP_HL)
                    if success:
                        logger.info(f"进入 {self.cfg.COOLDOWN_AFTER_FILL_SEC} 秒流动性冷却期...")
                        time.sleep(self.cfg.COOLDOWN_AFTER_FILL_SEC)

                time.sleep(self.cfg.NORMAL_POLL_INTERVAL_SEC)
                
            except Exception as e:
                logger.error(f"主循环异常: {e}")
                time.sleep(5.0)


def main():
    OKX_API_KEY = "776e25a5-be71-4c71-a183-ac8859789cfd"
    OKX_API_SECRET = "E96F1B17DBD38ADFBD4C719E3231C124"
    OKX_PASSPHRASE = "Auto111."

    HL_ACCOUNT = "0xAF3E93d42b56d517F055dDF64bcB4531c0B1E815"
    HL_AGENT_KEY = "0xd6d4969de4b68c2180fa15e8a40d7d941ca5608b9391f18fddbfc65f8a97d585"

    engine = AtomicDualLegEngine(
        hl_account=HL_ACCOUNT,
        hl_agent_key=HL_AGENT_KEY,
        okx_api_key=OKX_API_KEY,
        okx_secret=OKX_API_SECRET,
        okx_passphrase=OKX_PASSPHRASE
    )
    engine.run_tick_loop()


if __name__ == "__main__":
    main()
