import numpy as np

class StationaryGLFTEngine:
    """
    Stationary Guéant-Tapia-Manziadi (GLFT) 稳态做市解求解器
    彻底剔除古典 A-S 的 (T - t) 震荡，适配无到期日的永续合约与高频现货做市。
    """
    def __init__(self, gamma: float = 0.5, fee_maker_bps: float = 2.0, min_spread_bps: float = 7.0, q_max: float = 10.0):
        self.gamma = gamma                    # 风险厌恶系数
        self.fee_maker_bps = fee_maker_bps    # 单边 Maker 手续费 (bps)
        self.min_spread_bps = min_spread_bps  # 最小硬性价差门槛 (bps)
        self.q_max = q_max                    # 最大绝对持仓箱体限制

    def compute_reservation_price(self, micro_price: float, q: float, sigma: float) -> float:
        """
        计算无限视界稳态保留价格 r(s, q)
        在库存接近 q_max 时进行非线性放大惩罚 (斜率突变)
        """
        inv_ratio = q / self.q_max if self.q_max > 0 else 0.0
        
        # 库存硬箱体非线性斜率突变：
        # 达到 60% 时斜率放大 3 倍；达到 80% 时放大 8 倍
        if abs(inv_ratio) >= 0.8:
            lambda_inv = 8.0
        elif abs(inv_ratio) >= 0.6:
            lambda_inv = 3.0
        else:
            lambda_inv = 1.0
            
        skew = q * self.gamma * (sigma ** 2) * lambda_inv * micro_price * 0.0001
        return micro_price - skew

    def compute_optimal_spreads(self, micro_price: float, q: float, sigma: float, 
                                A_ask: float, k_ask: float, A_bid: float, k_bid: float) -> tuple[float, float]:
        """
        求解稳态半利差 delta_ask, delta_bid，并强制执行手续费内生化硬门槛
        """
        # GLFT 稳态半利差解析解 (基于到达强度 A 与衰减率 k)
        term_ask_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_ask, 1e-5))
        term_ask_inv = ((2.0 * q + 1.0) * self.gamma * (sigma ** 2)) / (2.0 * max(k_ask, 1e-5) * max(A_ask, 1e-5))
        delta_ask_bps = max(0.1, (term_ask_base + term_ask_inv) * 10000.0)

        term_bid_base = (1.0 / self.gamma) * np.log(1.0 + self.gamma / max(k_bid, 1e-5))
        term_bid_inv = ((2.0 * q - 1.0) * self.gamma * (sigma ** 2)) / (2.0 * max(k_bid, 1e-5) * max(A_bid, 1e-5))
        delta_bid_bps = max(0.1, (term_bid_base - term_bid_inv) * 10000.0)

        # 手续费内生化保护：单边半利差必须覆盖 2.5x maker 手续费并满足全局最小半利差
        min_half_hurdle_bps = max(self.min_spread_bps / 2.0, self.fee_maker_bps * 2.5)
        
        final_delta_ask_bps = max(delta_ask_bps, min_half_hurdle_bps)
        final_delta_bid_bps = max(delta_bid_bps, min_half_hurdle_bps)

        # 换算回物理价格差
        delta_ask_price = micro_price * (final_delta_ask_bps * 0.0001)
        delta_bid_price = micro_price * (final_delta_bid_bps * 0.0001)

        return delta_ask_price, delta_bid_price
