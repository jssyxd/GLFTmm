import math

class ConcentratedLPDynamicRangeManager:
    """
    Uniswap V3 / HyperSwap 集中流动性动态区间管理与 Delta 动态对冲计算器
    将被动 50/50 提供流动性重构为依据 GLFT 保留价格的主动挂单与无常损失 (LVR) 免疫系统。
    """
    def __init__(self, half_range_bps: float = 15.0):
        self.half_range_bps = half_range_bps # 默认围绕保留价格挂载 ±15 bps 的极窄流动性

    def compute_target_ticks(self, reservation_price: float, tick_spacing: int = 10) -> tuple[int, int]:
        """
        根据 GLFT 稳态保留价格，计算对应的 [Lower_Tick, Upper_Tick]
        Uniswap V3 价格与 Tick 关系：P = 1.0001^tick
        """
        p_lower = reservation_price * (1.0 - self.half_range_bps * 0.0001)
        p_upper = reservation_price * (1.0 + self.half_range_bps * 0.0001)

        raw_tick_lower = int(math.floor(math.log(p_lower) / math.log(1.0001)))
        raw_tick_upper = int(math.ceil(math.log(p_upper) / math.log(1.0001)))

        # 向上/向下吸附到合法 tick_spacing 整数倍
        tick_lower = (raw_tick_lower // tick_spacing) * tick_spacing
        tick_upper = (raw_tick_upper // tick_spacing) * tick_spacing

        if tick_lower >= tick_upper:
            tick_upper = tick_lower + tick_spacing

        return tick_lower, tick_upper

    def compute_hedge_delta_required(self, current_token_holding: float, current_perp_short_holding: float) -> float:
        """
        Spot-Perp Basis Hedging:
        计算 CEX / Hyperliquid 永续合约所需补开或平仓的空单张数，锁定现货价格敞口 Delta = 0
        """
        required_short = current_token_holding
        delta_to_execute = required_short - current_perp_short_holding
        return delta_to_execute
