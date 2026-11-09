# -*- coding: utf-8 -*-
"""
micro_aggregator.py - 1-Second Microstructure Snapshot & Feature Aggregator
Compresses continuous L2 orderbook feeds and trades into clean 1-second feature vectors.
Reduces 100MB+ tick noise to ~2.5MB per day, enabling friction-free Kaggle retraining.
"""

import os
import time
import json
from typing import Dict, Any, List, Optional
from collections import deque

class MicroAggregator:
    def __init__(self, symbol: str, buffer_max_seconds: int = 14400): # 4 hours buffer
        self.symbol = symbol
        self.buffer_max_seconds = buffer_max_seconds
        self.snapshots = deque(maxlen=buffer_max_seconds)
        
        self.current_sec = 0
        self.current_sec_trades = []
        self.latest_book = None
        
    def on_order_book(self, book_data: Dict[str, Any], timestamp: Optional[float] = None):
        ts = timestamp if timestamp is not None else time.time()
        self.latest_book = (ts, book_data)
        self._check_and_flush_second(int(ts))
        
    def on_trade(self, price: float, size: float, side: str, timestamp: Optional[float] = None):
        ts = timestamp if timestamp is not None else time.time()
        self.current_sec_trades.append({
            'ts': ts,
            'px': price,
            'sz': size,
            'side': side.lower(),
            'vol_usd': price * size
        })
        self._check_and_flush_second(int(ts))

    def _check_and_flush_second(self, new_sec: int):
        if self.current_sec == 0:
            self.current_sec = new_sec
            return
            
        if new_sec > self.current_sec:
            # Aggregate the completed second
            if self.latest_book:
                book_ts, book = self.latest_book
                bids = book.get('bids', [])
                asks = book.get('asks', [])
                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    mid_px = (best_bid + best_ask) / 2.0
                    spread_bps = ((best_ask - best_bid) / mid_px) * 10000.0 if mid_px > 0 else 0.0
                    
                    top5_bid_usd = sum(float(b[0]) * float(b[1]) for b in bids[:5])
                    top5_ask_usd = sum(float(a[0]) * float(a[1]) for a in asks[:5])
                    obi = (top5_bid_usd - top5_ask_usd) / (top5_bid_usd + top5_ask_usd) if (top5_bid_usd + top5_ask_usd) > 0 else 0.0
                    
                    buy_vol = sum(t['vol_usd'] for t in self.current_sec_trades if t['side'] == 'buy')
                    sell_vol = sum(t['vol_usd'] for t in self.current_sec_trades if t['side'] == 'sell')
                    trade_cnt = len(self.current_sec_trades)
                    
                    row = {
                        'sec': self.current_sec,
                        'symbol': self.symbol,
                        'mid_px': round(mid_px, 6),
                        'spread_bps': round(spread_bps, 3),
                        'best_bid': best_bid,
                        'best_ask': best_ask,
                        'top5_bid_usd': round(top5_bid_usd, 2),
                        'top5_ask_usd': round(top5_ask_usd, 2),
                        'obi_top5': round(obi, 4),
                        'buy_vol_usd': round(buy_vol, 4),
                        'sell_vol_usd': round(sell_vol, 4),
                        'trade_count': trade_cnt
                    }
                    self.snapshots.append(row)
                    
            self.current_sec = new_sec
            self.current_sec_trades = []

    def export_jsonl(self, filepath: str) -> int:
        """Export current buffered snapshots to JSONL."""
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        count = len(self.snapshots)
        with open(filepath, 'w', encoding='utf-8') as f:
            for s in self.snapshots:
                f.write(json.dumps(s) + '\n')
        return count

    def get_dataframe(self):
        """Convert snapshots to Pandas DataFrame if available."""
        try:
            import pandas as pd
            return pd.DataFrame(list(self.snapshots))
        except ImportError:
            return list(self.snapshots)
