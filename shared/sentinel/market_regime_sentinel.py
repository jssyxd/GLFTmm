# -*- coding: utf-8 -*-
"""
market_regime_sentinel.py - Real-Time Market Microstructure Regime & Hurdle Sentinel
Compliant with GLFTmm and AS-Grid quantitative architectures.
Monitors L2 top-5 book snapshots and recent trades to dynamically compute:
1. Dynamic Minimum Spread Hurdle:
   min_spread_bps = max(base_spread, 2.5 * total_maker_fee + alpha * vol_bps + beta * toxic_penalty)
2. Order Book Imbalance (OBI) & Top-5 Liquidity Depths
3. Regime classification: HEALTHY_MM, EXPAND_SPREAD, HALT_TOXIC, DEPTH_INSUFFICIENT
"""

import time
import math
from typing import Dict, Any, List, Optional
from collections import deque

class MarketRegimeSentinel:
    def __init__(
        self,
        symbol: str,
        maker_fee_bps: float = 2.0,
        min_base_spread_bps: float = 12.0,
        min_depth_usd: float = 1500.0,
        panic_depth_usd: float = 500.0,
        vol_window_sec: int = 15,
        alpha_vol: float = 1.2,
        beta_toxic: float = 6.0
    ):
        self.symbol = symbol
        self.maker_fee_bps = maker_fee_bps
        self.total_roundtrip_fee_bps = maker_fee_bps * 2.0
        self.fee_hard_floor_bps = self.total_roundtrip_fee_bps * 2.5 # Minimum 10.0 bps for 2 bps maker
        self.min_base_spread_bps = max(min_base_spread_bps, self.fee_hard_floor_bps)
        self.min_depth_usd = min_depth_usd
        self.panic_depth_usd = panic_depth_usd
        self.vol_window_sec = vol_window_sec
        self.alpha_vol = alpha_vol
        self.beta_toxic = beta_toxic
        
        # Micro-state history: deque of (timestamp, mid_px)
        self.mid_history = deque(maxlen=300)
        # Recent trades history: deque of (timestamp, sz_usd, side)
        self.trade_history = deque(maxlen=500)
        
    def record_trade(self, price: float, size_usd: float, side: str, timestamp: Optional[float] = None):
        ts = timestamp if timestamp is not None else time.time()
        self.trade_history.append((ts, size_usd, side.lower()))
        
    def _calculate_micro_volatility(self, current_time: float) -> float:
        """Calculate rolling realized volatility in bps over vol_window_sec."""
        cutoff = current_time - self.vol_window_sec
        # Filter recent points
        recent = [px for ts, px in self.mid_history if ts >= cutoff]
        if len(recent) < 2:
            return 0.0
            
        returns = []
        for i in range(1, len(recent)):
            if recent[i-1] > 0:
                ret = (recent[i] - recent[i-1]) / recent[i-1]
                returns.append(ret)
                
        if not returns:
            return 0.0
            
        # Annualized or direct window std in bps
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_bps = math.sqrt(var_r) * 10000.0
        return std_bps

    def _calculate_trade_flow_imbalance(self, current_time: float) -> float:
        """Calculate taker trade flow imbalance in [-1.0, 1.0] over last 5 seconds."""
        cutoff = current_time - 5.0
        recent = [(sz, side) for ts, sz, side in self.trade_history if ts >= cutoff]
        if not recent:
            return 0.0
            
        buy_vol = sum(sz for sz, side in recent if side == 'buy')
        sell_vol = sum(sz for sz, side in recent if side == 'sell')
        tot_vol = buy_vol + sell_vol
        if tot_vol <= 0:
            return 0.0
        return (buy_vol - sell_vol) / tot_vol

    def evaluate_regime(
        self,
        book_data: Dict[str, Any],
        recent_trades: Optional[List[Dict[str, Any]]] = None,
        toxic_prob: float = 0.0,
        current_inventory_usd: float = 0.0
    ) -> Dict[str, Any]:
        """
        Evaluate full L2 book state and return operational regime and dynamic hurdles.
        book_data expects:
          'bids': [[px, sz], ...] or list of tuples
          'asks': [[px, sz], ...]
        """
        now = time.time()
        bids = book_data.get('bids', [])
        asks = book_data.get('asks', [])
        
        if not bids or not asks:
            return {
                "allow_quote": False,
                "allow_open": False,
                "status": "DEPTH_INSUFFICIENT",
                "recommended_min_spread_bps": self.min_base_spread_bps,
                "current_spread_bps": 0.0,
                "obi_top5": 0.0,
                "realized_vol_bps": 0.0,
                "reason": "Empty orderbook"
            }
            
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid <= 0 or best_ask <= best_bid:
            return {
                "allow_quote": False,
                "allow_open": False,
                "status": "DEPTH_INSUFFICIENT",
                "recommended_min_spread_bps": self.min_base_spread_bps,
                "current_spread_bps": 0.0,
                "obi_top5": 0.0,
                "realized_vol_bps": 0.0,
                "reason": f"Crossed or non-positive book: {best_bid} vs {best_ask}"
            }
            
        mid_px = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / mid_px) * 10000.0
        self.mid_history.append((now, mid_px))
        
        # Digest recent trades if provided
        if recent_trades:
            for tr in recent_trades:
                p = float(tr.get('px', tr.get('price', 0)))
                s = float(tr.get('sz', tr.get('size', 0)))
                sd = tr.get('side', 'buy')
                t_ts = tr.get('time', tr.get('ts', now * 1000.0)) / 1000.0
                self.record_trade(p, p * s, sd, timestamp=t_ts)
                
        # 1. Top-5 Depth and OBI Calculation
        top5_bids = bids[:5]
        top5_asks = asks[:5]
        top5_bid_usd = sum(float(b[0]) * float(b[1]) for b in top5_bids)
        top5_ask_usd = sum(float(a[0]) * float(a[1]) for a in top5_asks)
        min_depth = min(top5_bid_usd, top5_ask_usd)
        
        obi = 0.0
        if (top5_bid_usd + top5_ask_usd) > 0:
            obi = (top5_bid_usd - top5_ask_usd) / (top5_bid_usd + top5_ask_usd)
            
        # 2. Realized Volatility & Trade Flow
        vol_bps = self._calculate_micro_volatility(now)
        flow_imb = self._calculate_trade_flow_imbalance(now)
        
        # 3. Dynamic Spread Hurdle Calculation
        # Base hurdle must strictly cover 2.5 * total maker fees
        # Plus volatility cushion and ML toxic penalty
        toxic_penalty = max(0.0, toxic_prob - 0.5) * self.beta_toxic * 10.0
        vol_cushion = vol_bps * self.alpha_vol
        
        recommended_spread = max(
            self.min_base_spread_bps,
            self.fee_hard_floor_bps + vol_cushion + toxic_penalty
        )
        
        # 4. Regime Determination
        allow_quote = True
        allow_open = True
        status = "HEALTHY_MM"
        reason = "Normal microstructure"
        
        # Panic depth breach
        if min_depth < self.panic_depth_usd:
            allow_quote = False
            allow_open = False
            status = "DEPTH_INSUFFICIENT"
            reason = f"Top-5 depth critical failure (${min_depth:.1f} < panic ${self.panic_depth_usd:.1f})"
        elif min_depth < self.min_depth_usd:
            # Insufficient depth: forbid opening new inventory, but allow soft close
            allow_quote = abs(current_inventory_usd) > 1.0 # only quote to reduce inventory
            allow_open = False
            status = "DEPTH_INSUFFICIENT"
            reason = f"Top-5 depth insufficient (${min_depth:.1f} < min ${self.min_depth_usd:.1f})"
        elif toxic_prob >= 0.70 or abs(flow_imb) > 0.85:
            # Extreme toxicity / whale market sweep
            allow_quote = False
            allow_open = False
            status = "HALT_TOXIC"
            reason = f"Extreme toxicity detected: P_toxic={toxic_prob:.2f}, FlowImb={flow_imb:.2f}"
        elif vol_bps > 20.0 or toxic_prob >= 0.55:
            # High volatility or moderate toxicity: expand spread
            allow_quote = True
            allow_open = True
            status = "EXPAND_SPREAD"
            reason = f"High micro-volatility ({vol_bps:.1f} bps) or moderate toxicity ({toxic_prob:.2f})"
            recommended_spread = max(recommended_spread, self.fee_hard_floor_bps * 1.8)
            
        return {
            "allow_quote": allow_quote,
            "allow_open": allow_open,
            "status": status,
            "recommended_min_spread_bps": round(recommended_spread, 2),
            "current_spread_bps": round(spread_bps, 2),
            "top5_bid_usd": round(top5_bid_usd, 2),
            "top5_ask_usd": round(top5_ask_usd, 2),
            "min_depth_usd": round(min_depth, 2),
            "obi_top5": round(obi, 4),
            "realized_vol_bps": round(vol_bps, 2),
            "trade_flow_imb": round(flow_imb, 4),
            "toxic_prob": round(toxic_prob, 4),
            "fee_floor_bps": self.fee_hard_floor_bps,
            "reason": reason
        }
