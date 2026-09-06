"""B1 前瞻收益标签测试（纯函数 · 精确手算）。

钉住口径：T 收盘决策 → entry=open[T+1] → 持有 horizon → exit=open[T+1+horizon]；
净收益镜像 CostModel 比例成本（不含 min_fee）；bars 不足 → None（绝不截断冒充）。
"""
from __future__ import annotations

import unittest

import synth
from ripple_tradePilot.backtest.costs import CostModel
from ripple_tradePilot.ml.labels import (
    HorizonLabel,
    horizon_label,
    label_series,
    round_trip_cost_factor,
)


def _flat(n: int, price: float = 100.0):
    """常数 OHLC 序列（长度 n）。"""
    return [price] * n, [price] * n, [price] * n, [price] * n


class RoundTripCostFactorTest(unittest.TestCase):
    def test_factor_is_positive(self):
        self.assertGreater(round_trip_cost_factor(), 0.0)

    def test_factor_matches_manual_formula(self):
        c = CostModel()
        buy_ps = 1.0 * (1 + c.slippage) * (1 + c.fee_rate)
        sell_ps = 1.0 * (1 - c.slippage) * (1 - c.fee_rate - c.stamp_duty)
        self.assertAlmostEqual(round_trip_cost_factor(), 1.0 - sell_ps / buy_ps, places=12)

    def test_zero_cost_factor_is_zero(self):
        zero = CostModel(fee_rate=0.0, stamp_duty=0.0, slippage=0.0, min_fee=0.0)
        self.assertAlmostEqual(round_trip_cost_factor(zero), 0.0, places=12)


class HorizonLabelTest(unittest.TestCase):
    def test_constant_price_net_return_equals_minus_cost_factor(self):
        opens, highs, lows, closes = _flat(12, 100.0)
        label = horizon_label(opens, highs, lows, closes, i=0, horizon=5)
        self.assertIsNotNone(label)
        self.assertAlmostEqual(label.gross_return, 0.0, places=12)
        # 平价往返：净收益恰为 -成本因子
        self.assertAlmostEqual(label.net_return, -round_trip_cost_factor(), places=12)
        self.assertFalse(label.win)
        self.assertAlmostEqual(label.mae, 0.0, places=12)
        self.assertAlmostEqual(label.mfe, 0.0, places=12)
        self.assertTrue(label.complete)

    def test_single_jump_exact_net_return(self):
        # entry=open[1]=100，exit=open[6]=110（horizon=5）→ gross=0.10
        opens = [99.0, 100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
        highs = [110.0] * 7
        lows = [90.0] * 7
        closes = [100.0] * 7
        label = horizon_label(opens, highs, lows, closes, i=0, horizon=5)
        self.assertAlmostEqual(label.entry, 100.0, places=9)
        self.assertAlmostEqual(label.exit, 110.0, places=9)
        self.assertAlmostEqual(label.gross_return, 0.10, places=9)
        # 独立手算净收益（不调 _net_return），验证成本口径
        c = CostModel()
        buy_ps = 100.0 * (1 + c.slippage) * (1 + c.fee_rate)
        sell_ps = 110.0 * (1 - c.slippage) * (1 - c.fee_rate - c.stamp_duty)
        expected_net = sell_ps / buy_ps - 1.0
        self.assertAlmostEqual(label.net_return, expected_net, places=12)
        self.assertTrue(label.win)
        self.assertLess(label.net_return, label.gross_return)  # 成本吃掉一部分

    def test_zero_cost_net_equals_gross(self):
        zero = CostModel(fee_rate=0.0, stamp_duty=0.0, slippage=0.0, min_fee=0.0)
        opens = [99.0, 100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
        label = horizon_label(opens, [110.0] * 7, [90.0] * 7, [100.0] * 7,
                             i=0, horizon=5, costs=zero)
        self.assertAlmostEqual(label.net_return, label.gross_return, places=12)

    def test_v_shape_mae_mfe_hand_computed(self):
        # 持有期 [i+1, i+5] = 索引 1..5；entry=open[1]=100，exit=open[6]=105
        opens = [99.0, 100.0, 97.0, 93.0, 95.0, 100.0, 105.0]
        highs = [100.0, 101.0, 100.0, 95.0, 110.0, 120.0, 106.0]
        lows = [98.0, 99.0, 95.0, 90.0, 92.0, 98.0, 104.0]
        closes = [100.0] * 7
        label = horizon_label(opens, highs, lows, closes, i=0, horizon=5)
        # min(lows[1:6]) = min(99,95,90,92,98) = 90 → mae = -0.10
        self.assertAlmostEqual(label.mae, 90.0 / 100.0 - 1.0, places=12)
        # max(highs[1:6]) = max(101,100,95,110,120) = 120 → mfe = +0.20
        self.assertAlmostEqual(label.mfe, 120.0 / 100.0 - 1.0, places=12)
        self.assertAlmostEqual(label.gross_return, 0.05, places=9)
        self.assertLessEqual(label.mae, 0.0)
        self.assertGreaterEqual(label.mfe, label.gross_return)

    def test_tail_insufficient_returns_none(self):
        opens, highs, lows, closes = _flat(7, 100.0)
        # i=1 → exit_index=1+1+5=7 >= len(7) → None
        self.assertIsNone(horizon_label(opens, highs, lows, closes, i=1, horizon=5))
        # i=0 → exit_index=6 < 7 → 有效
        self.assertIsNotNone(horizon_label(opens, highs, lows, closes, i=0, horizon=5))

    def test_negative_index_returns_none(self):
        opens, highs, lows, closes = _flat(12, 100.0)
        self.assertIsNone(horizon_label(opens, highs, lows, closes, i=-1, horizon=5))

    def test_invalid_horizon_raises(self):
        opens, highs, lows, closes = _flat(12, 100.0)
        with self.assertRaises(ValueError):
            horizon_label(opens, highs, lows, closes, i=0, horizon=0)

    def test_zero_entry_returns_none(self):
        opens = [0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 110.0]
        label = horizon_label(opens, [110.0] * 7, [90.0] * 7, [100.0] * 7,
                             i=0, horizon=5)
        self.assertIsNone(label)  # entry=open[1]=0 → 非法

    def test_horizon_10_uses_open_i_plus_11(self):
        opens = [100.0] * 13
        opens[1] = 100.0   # entry
        opens[11] = 130.0  # exit for horizon=10 (i+1+10=11)
        label = horizon_label(opens, [130.0] * 13, [100.0] * 13, [100.0] * 13,
                             i=0, horizon=10)
        self.assertIsNotNone(label)
        self.assertAlmostEqual(label.exit, 130.0, places=9)
        self.assertAlmostEqual(label.gross_return, 0.30, places=9)


class LabelSeriesTest(unittest.TestCase):
    def test_length_and_tail_none(self):
        bars = synth.daily_bars(20, seed=3)
        labels = label_series(bars, horizon=5)
        self.assertEqual(len(labels), 20)
        # 有效 i ≤ N-horizon-2 = 13；尾部 horizon+1=6 个为 None
        self.assertIsNotNone(labels[13])
        for tail in labels[14:]:
            self.assertIsNone(tail)

    def test_horizon_10_longer_tail(self):
        bars = synth.daily_bars(20, seed=3)
        labels = label_series(bars, horizon=10)
        # 有效 i ≤ 20-10-2 = 8；尾部 11 个 None
        self.assertIsNotNone(labels[8])
        self.assertIsNone(labels[9])
        self.assertEqual(sum(1 for x in labels if x is None), 11)

    def test_labels_match_entry_exit_from_bars(self):
        bars = synth.daily_bars(30, seed=5)
        labels = label_series(bars, horizon=5)
        for i in (0, 5, 10, 20):
            label = labels[i]
            self.assertIsNotNone(label)
            self.assertAlmostEqual(label.entry, bars[i + 1].open, places=9)
            self.assertAlmostEqual(label.exit, bars[i + 6].open, places=9)

    def test_returns_horizon_label_instances(self):
        bars = synth.daily_bars(15, seed=1)
        labels = label_series(bars, horizon=5)
        self.assertIsInstance(labels[0], HorizonLabel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
