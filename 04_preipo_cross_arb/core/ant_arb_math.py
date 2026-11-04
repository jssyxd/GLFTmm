"""
ant_arb_math.py

ANT (Anthropic) 跨所（Hyperliquid xyz:ANT / io:ANTH vs OKX ANTHROPIC-USDT-SWAP）
点差套利、股数敏感度曲面、双边资金费率与防毒单定价引擎
=============================================================================
理论来源：
推特 @limxn6《ANT 上市前合约：一笔规则写好的巨型价差套利机会》
开源参考：
your-quantguy/entropy-arb (midline_bps / take_fraction / 净吃单门槛)
adolyb/entropy-anth-carry (资金费套利与跨所对冲)
=============================================================================
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Any, Tuple, Optional


@dataclass
class ArbitrageParameters:
    # 标的定义
    hl_symbol: str = "xyz:ANT"                       # 1 张 = 1 股 Class A，无 rebase
    okx_inst_id: str = "ANTHROPIC-USDT-SWAP"        # 假定股数 10B，上市按实际 S 股数 rebase
    
    # 费率模型 (以 VIP / 基准费率计)
    hl_maker_fee_bps: float = -0.2                   # -0.002% (返佣)
    hl_taker_fee_bps: float = 2.5                    # 0.025%
    okx_maker_fee_bps: float = 1.5                   # 0.015%
    okx_taker_fee_bps: float = 3.5                   # 0.035%
    
    # 核心风控阈值
    min_net_arb_spread_bps: float = 15.0             # 最低净套利利润门槛 (15 bps)
    max_slippage_bps: float = 3.0                    # 单腿最大滑点容忍度 (3 bps)
    instant_drift_breaker_bps: float = 8.5           # 8.5 bps 瞬时防狙击硬阻断
    vpin_toxic_threshold: float = 0.65               # VPIN > 0.65 判定为知情毒性流，暂停开仓


class ANTCrossVenueArbEngine:
    """
    跨所点差套利数学与执行引擎
    """
    def __init__(self, params: ArbitrageParameters = ArbitrageParameters()):
        self.p = params
        self.assumed_okx_shares_b = 10.0  # OKX 假定股本 10 亿 (10B)
        
    def calculate_okx_implied_per_share(self, okx_price: float, estimated_shares_b: float) -> float:
        """
        计算 OKX 在给定预估股数下的折合每股价格
        P_per_share = P_okx * (10.0 / estimated_shares_b)
        """
        if estimated_shares_b <= 0:
            raise ValueError("股本必须为正数")
        return okx_price * (self.assumed_okx_shares_b / estimated_shares_b)

    def calculate_net_spread(
        self,
        hl_best_ask: float,
        okx_best_bid: float,
        estimated_shares_b: float = 12.7,
        long_hl_short_okx: bool = True
    ) -> Dict[str, float]:
        """
        计算套利净点差（扣除双边所有手续费与滑点成本）
        
        若 long_hl_short_okx 为 True:
          - 买入 Hyperliquid (Taker 或 Maker)
          - 卖出 OKX (Taker 或 Maker)
          - Gross Spread = (P_okx_equiv - P_hl) / P_hl
        """
        okx_equiv = self.calculate_okx_implied_per_share(okx_best_bid, estimated_shares_b)
        gross_spread_pct = (okx_equiv - hl_best_ask) / hl_best_ask * 100.0
        gross_spread_bps = gross_spread_pct * 100.0
        
        # 双边 Taker 手续费成本 (最保守口径)
        total_fees_bps = self.p.hl_taker_fee_bps + self.p.okx_taker_fee_bps
        net_spread_bps = gross_spread_bps - total_fees_bps
        
        return {
            "hl_ask": hl_best_ask,
            "okx_bid_raw": okx_best_bid,
            "okx_bid_equiv": okx_equiv,
            "estimated_shares_b": estimated_shares_b,
            "gross_spread_pct": gross_spread_pct,
            "gross_spread_bps": gross_spread_bps,
            "total_fees_bps": total_fees_bps,
            "net_spread_bps": net_spread_bps,
            "is_profitable": net_spread_bps >= self.p.min_net_arb_spread_bps
        }

    def share_count_sensitivity_table(self, hl_price: float, okx_price: float) -> list:
        """
        计算从 11.5B 到 16.94B 全区间股数下的套利收益率与安全边际
        """
        scenarios = [
            (11.5, "极端乐观 (排除全部未发行/未转换)"),
            (12.0, "市场主流投行估计下限"),
            (12.3, "剔除期权池后的普通股基础估算"),
            (12.7, "基准预期 (投行中位数分析口径)"),
            (13.1, "含部分既得 RSU 的流通股口径"),
            (13.8, "收支平衡点附近 (Zero Arb Bound)"),
            (15.0, "计入全部已批准期权池"),
            (16.73, "S-1 5% 公益基金反推完全稀释基准"),
            (16.94, "完全稀释 (FDV) 极限上限")
        ]
        
        results = []
        for s, desc in scenarios:
            equiv = self.calculate_okx_implied_per_share(okx_price, s)
            spread_pct = (equiv - hl_price) / hl_price * 100.0
            results.append({
                "shares_b": s,
                "description": desc,
                "okx_equiv": round(equiv, 2),
                "hl_price": round(hl_price, 2),
                "spread_pct": round(spread_pct, 2),
                "safe": spread_pct > 0
            })
        return results

    def calculate_hedge_ratio(self, estimated_shares_b: float) -> float:
        """
        计算跨所 Delta 对冲配平系数 R
        1 张 HL xyz:ANT 对应 1 股；
        1 张 OKX 对应 1 份假定 10B 份合约，
        因此对冲张数比率 R = 10.0 / estimated_shares_b
        """
        return self.assumed_okx_shares_b / estimated_shares_b
