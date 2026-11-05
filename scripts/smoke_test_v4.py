# -*- coding: utf-8 -*-
"""
smoke_test_v4.py - V4 自适应复利引擎单元逻辑与边界测试脚本
"""
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, "W:/twinCheck/repo/04_preipo_cross_arb/core")
from ant_cross_arb_v4_adaptive import EngineConfigV4, AtomicDualLegEngineV4

class TestV4EngineLogic(unittest.TestCase):
    def setUp(self):
        self.cfg = EngineConfigV4()
        # Mock exchange and info
        self.engine = AtomicDualLegEngineV4.__new__(AtomicDualLegEngineV4)
        self.engine.cfg = self.cfg
        self.engine.current_q_hl = 0.0
        self.engine.current_q_okx = 0.0
        self.engine.hl_entry_px = 0.0
        self.engine.okx_entry_px = 0.0
        self.engine.hl_exchange = MagicMock()
        self.engine.okx = MagicMock()

    def test_single_leg_drift_trigger(self):
        """测试单腿 8% 价格偏离硬断路触发"""
        self.engine.current_q_hl = 1.0
        self.engine.current_q_okx = -0.787
        self.engine.hl_entry_px = 100.0
        self.engine.okx_entry_px = 150.0

        # HL 变动 5% -> 不触发
        self.assertFalse(self.engine.check_single_leg_price_drift(105.0, 150.0))

        # HL 变动 8.5% -> 必须触发
        self.engine.execute_atomic_close = MagicMock(return_value=True)
        self.assertTrue(self.engine.check_single_leg_price_drift(108.5, 150.0))
        self.engine.execute_atomic_close.assert_called_once()

    def test_combined_pnl_take_profit(self):
        """测试双腿合计净利润率 >= 3% 止盈"""
        self.engine.current_q_hl = 1.0        # 1 股 HL @ $100
        self.engine.current_q_okx = -0.787    # 0.787 张 OKX @ $150
        self.engine.hl_entry_px = 100.0
        self.engine.okx_entry_px = 150.0

        # 总初始保证金: (100 / 10) + (0.787 * 150 / 10) = 10 + 11.805 = 21.805 USD
        # 3% 目标利润 = 21.805 * 0.03 = 0.654 USD

        # 情景 1: HL 亏损 6 USD ($94), OKX 盈利 6.2 USD ($142.12) -> 净利 0.2 USD (ROI 0.9%) -> 不触发
        self.assertFalse(self.engine.check_combined_pnl_take_profit(94.0, 142.12))

        # 情景 2: HL 亏损 6 USD ($94), OKX 盈利 8.0 USD ($139.83) -> 净利 2.0 USD (ROI 9.17% > 3%) -> 必须触发止盈
        self.engine.execute_atomic_close = MagicMock(return_value=True)
        self.assertTrue(self.engine.check_combined_pnl_take_profit(94.0, 139.83))
        self.engine.execute_atomic_close.assert_called_once()

    def test_orphan_reconciliation(self):
        """测试孤儿单自动对冲清理器"""
        self.engine.execute_atomic_close = MagicMock(return_value=True)

        # OKX 归零，HL 仍持有 1.0 股
        res = self.engine.reconcile_orphan_position(1.0, 0.0)
        self.assertTrue(res)
        self.engine.hl_exchange.market_close.assert_called_with("xyz:ANT")

if __name__ == "__main__":
    unittest.main()
