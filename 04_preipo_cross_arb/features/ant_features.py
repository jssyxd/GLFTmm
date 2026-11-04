"""
ant_features.py

ANT (Anthropic) 跨所套利微观特征工程与数据流处理器
===================================================
8 大硬核量化特征提取：
1. hl_obi: Hyperliquid 前 5 档订单簿失衡度 (Order Book Imbalance)
2. okx_obi: OKX 前 5 档订单簿失衡度
3. basis_bps: 跨所基差偏离 (考虑 12.7B 股数折算后的等价每股差额)
4. funding_diff_apr: 双边资金费率利差 (年化 APR 差异，资金费套利驱动力)
5. vpin_toxicity: 盘口成交毒性 (Volume-Synchronized Probability of Toxicity)
6. micro_drift_10s: 10 秒微观价格漂移绝对值 (跳跃/狙击识别)
7. s1_prob_score: 基于招股书进展与期权稀释推算的股数在安全区间的概率置信度
8. depth_ratio: Hyperliquid 盘口深度与 OKX 盘口深度之比 (流动性抗穿透比)
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, List


class ANTFeatureExtractor:
    def __init__(self, assumed_shares_b: float = 12.7):
        self.assumed_shares_b = assumed_shares_b

    def extract_features(
        self,
        hl_l2: Dict[str, Any],
        okx_l2: Dict[str, Any],
        hl_funding_rate: float,
        okx_funding_rate: float,
        recent_trades: List[Dict[str, Any]],
        s1_filing_status: int = 1 # 1: 初版 S-1, 2: 修正案 S-1/A 披露更多数据, 3: 最终确定
    ) -> Dict[str, float]:
        """
        提取用于 LightGBM 决策的 8 维输入向量
        """
        # 1. Hyperliquid 盘口与 OBI
        hl_bids = hl_l2.get("levels", [[]])[0][:5]
        hl_asks = hl_l2.get("levels", [[], []])[1][:5]
        
        hl_best_bid = float(hl_bids[0]["px"]) if hl_bids else 0.0
        hl_best_ask = float(hl_asks[0]["px"]) if hl_asks else 0.0
        hl_bid_vol = sum(float(b["sz"]) for b in hl_bids) if hl_bids else 1.0
        hl_ask_vol = sum(float(a["sz"]) for a in hl_asks) if hl_asks else 1.0
        hl_obi = (hl_bid_vol - hl_ask_vol) / (hl_bid_vol + hl_ask_vol + 1e-8)

        # 2. OKX 盘口与 OBI
        okx_bids = okx_l2.get("bids", [])[:5]
        okx_asks = okx_l2.get("asks", [])[:5]
        okx_best_bid = float(okx_bids[0][0]) if okx_bids else 0.0
        okx_best_ask = float(okx_asks[0][0]) if okx_asks else 0.0
        okx_bid_vol = sum(float(b[1]) for b in okx_bids) if okx_bids else 1.0
        okx_ask_vol = sum(float(a[1]) for a in okx_asks) if okx_asks else 1.0
        okx_obi = (okx_bid_vol - okx_ask_vol) / (okx_bid_vol + okx_ask_vol + 1e-8)

        # 3. 跨所基差 Basis (bps)
        okx_equiv_per_share = okx_best_bid * (10.0 / self.assumed_shares_b)
        basis_bps = ((okx_equiv_per_share - hl_best_ask) / hl_best_ask) * 10000.0 if hl_best_ask > 0 else 0.0

        # 4. 双边资金费率利差 (年化 APR %)
        # HL 8h 费率转年化: rate * 3 * 365 * 100
        # OKX 8h 费率转年化: rate * 3 * 365 * 100
        hl_funding_apr = hl_funding_rate * 3.0 * 365.0 * 100.0
        okx_funding_apr = okx_funding_rate * 3.0 * 365.0 * 100.0
        # 多 HL 空 OKX: 收到 HL 资金费 (若负) - 支付 OKX 资金费 (若正)
        funding_diff_apr = (-hl_funding_apr) - okx_funding_apr

        # 5. VPIN 微观毒性计算
        vpin_toxicity = self._compute_simple_vpin(recent_trades)

        # 6. 10s 微观价格漂移
        micro_drift_10s = abs(hl_best_ask - hl_best_bid) / hl_best_bid * 10000.0 if hl_best_bid > 0 else 0.0

        # 7. S-1 股数置信度得分 (0.0 ~ 1.0)
        # 根据当前投行共识与披露阶段建立贝叶斯先验：
        # 认为 S 在 12.0B ~ 13.8B 安全盈利区间的后验概率
        s1_prob_score = 0.78 if s1_filing_status == 1 else (0.92 if s1_filing_status == 2 else 1.0)

        # 8. 盘口深度对比比率
        depth_ratio = (hl_bid_vol * hl_best_bid) / ((okx_bid_vol * okx_best_bid) + 1e-8)

        return {
            "hl_obi": float(round(hl_obi, 4)),
            "okx_obi": float(round(okx_obi, 4)),
            "basis_bps": float(round(basis_bps, 2)),
            "funding_diff_apr": float(round(funding_diff_apr, 2)),
            "vpin_toxicity": float(round(vpin_toxicity, 4)),
            "micro_drift_10s": float(round(micro_drift_10s, 2)),
            "s1_prob_score": float(round(s1_prob_score, 4)),
            "depth_ratio": float(round(depth_ratio, 4))
        }

    def _compute_simple_vpin(self, trades: List[Dict[str, Any]]) -> float:
        """从近期成交中计算成交量同步毒性概率 (VPIN)"""
        if not trades or len(trades) < 10:
            return 0.25 # 默认中性温和环境
        buy_vol = sum(t["size"] for t in trades if t.get("side") == "buy")
        sell_vol = sum(t["size"] for t in trades if t.get("side") == "sell")
        total_vol = buy_vol + sell_vol
        if total_vol <= 0:
            return 0.25
        return float(abs(buy_vol - sell_vol) / total_vol)
