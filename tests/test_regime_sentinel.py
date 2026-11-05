# -*- coding: utf-8 -*-
"""
test_regime_sentinel.py - Unit tests for MarketRegimeSentinel & MicroAggregator
"""

import time
import pytest
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from shared.sentinel.market_regime_sentinel import MarketRegimeSentinel
from shared.aggregator.micro_aggregator import MicroAggregator

def test_sentinel_fee_floor():
    sentinel = MarketRegimeSentinel(symbol="TEST-USDT", maker_fee_bps=2.0, min_base_spread_bps=8.0)
    # Fee floor is 2.0 * 2.0 * 2.5 = 10.0 bps. Base spread 8.0 must be overridden to 10.0.
    assert sentinel.fee_hard_floor_bps == 10.0
    assert sentinel.min_base_spread_bps == 10.0

def test_sentinel_normal_regime():
    sentinel = MarketRegimeSentinel(symbol="TEST-USDT", maker_fee_bps=2.0, min_base_spread_bps=12.0)
    book = {
        'bids': [[100.0, 50.0], [99.9, 50.0], [99.8, 50.0], [99.7, 50.0], [99.6, 50.0]],
        'asks': [[100.2, 50.0], [100.3, 50.0], [100.4, 50.0], [100.5, 50.0], [100.6, 50.0]]
    }
    res = sentinel.evaluate_regime(book, toxic_prob=0.1)
    assert res['allow_quote'] is True
    assert res['allow_open'] is True
    assert res['status'] == "HEALTHY_MM"
    assert res['recommended_min_spread_bps'] >= 12.0
    assert abs(res['obi_top5']) < 0.01

def test_sentinel_depth_insufficient():
    sentinel = MarketRegimeSentinel(symbol="TEST-USDT", min_depth_usd=1500.0, panic_depth_usd=500.0)
    # Book with only $100 depth
    book = {
        'bids': [[10.0, 5.0]],
        'asks': [[10.1, 5.0]]
    }
    res = sentinel.evaluate_regime(book, current_inventory_usd=0.0)
    assert res['allow_open'] is False
    assert res['status'] == "DEPTH_INSUFFICIENT"

def test_sentinel_toxic_halt():
    sentinel = MarketRegimeSentinel(symbol="TEST-USDT")
    book = {
        'bids': [[100.0, 50.0], [99.9, 50.0], [99.8, 50.0], [99.7, 50.0], [99.6, 50.0]],
        'asks': [[100.2, 50.0], [100.3, 50.0], [100.4, 50.0], [100.5, 50.0], [100.6, 50.0]]
    }
    res = sentinel.evaluate_regime(book, toxic_prob=0.75)
    assert res['allow_quote'] is False
    assert res['allow_open'] is False
    assert res['status'] == "HALT_TOXIC"

def test_micro_aggregator():
    agg = MicroAggregator(symbol="TEST-USDT")
    t0 = 1700000000.1
    book1 = {'bids': [[10.0, 10.0]], 'asks': [[10.1, 10.0]]}
    agg.on_order_book(book1, timestamp=t0)
    agg.on_trade(10.05, 5.0, 'buy', timestamp=t0 + 0.2)
    # Next second
    agg.on_order_book(book1, timestamp=t0 + 1.2)
    
    assert len(agg.snapshots) >= 1
    s = agg.snapshots[0]
    assert s['sec'] == int(t0)
    assert s['buy_vol_usd'] == 10.05 * 5.0

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
