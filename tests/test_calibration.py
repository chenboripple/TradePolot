"""B4 校准与决策价值测试（对 synth.ml_frame 逻辑斯谛真值链路验证）。

ml_frame 的 p_true 是构造性完美校准（y ~ Bernoulli(p_true)），故：
- Brier ≈ E[p(1-p)]、log-loss ≈ E[二元熵]、ECE 小、slope≈1、intercept≈0；
- 人为锐化/钝化 logit 可解析地推出 slope≈0.5 / ≈2，验证斜率语义；
- 常数预测的 ECE 可手算；阈值边界 coverage 1.0/0.0。
"""
from __future__ import annotations

import math
import unittest

import numpy as np
import synth
from ripple_tradePilot.ml.calibration import (
    DecisionValue,
    ReliabilityBin,
    brier_score,
    calibration_slope_intercept,
    coverage_at_threshold,
    decision_value_report,
    expected_calibration_error,
    log_loss,
    reliability_table,
)

N = 4000  # 大样本收紧统计涨落，让"≈理论值"断言稳定


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.asarray(z, dtype=float)))


class PerfectCalibrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.X, cls.y, cls.ret, cls.p = synth.ml_frame(n_rows=N, seed=42)

    def test_brier_near_theoretical(self):
        # E[(p-y)^2 | p] = p(1-p) → Brier ≈ mean(p(1-p))
        theoretical = float(np.mean(self.p * (1 - self.p)))
        self.assertAlmostEqual(brier_score(self.y, self.p), theoretical, delta=0.015)

    def test_log_loss_near_theoretical_entropy(self):
        ent = -np.mean(
            self.p * np.log(self.p) + (1 - self.p) * np.log(1 - self.p)
        )
        self.assertAlmostEqual(log_loss(self.y, self.p), float(ent), delta=0.02)

    def test_brier_beats_constant_prediction(self):
        const = float(np.mean(self.y))
        self.assertLess(
            brier_score(self.y, self.p), brier_score(self.y, [const] * len(self.y))
        )

    def test_ece_small(self):
        self.assertLess(expected_calibration_error(self.y, self.p, n_bins=10), 0.05)

    def test_slope_intercept_near_identity(self):
        slope, intercept = calibration_slope_intercept(self.y, self.p)
        self.assertAlmostEqual(slope, 1.0, delta=0.15)
        self.assertAlmostEqual(intercept, 0.0, delta=0.2)

    def test_reliability_bins_sum_to_n(self):
        bins = reliability_table(self.y, self.p, n_bins=10)
        self.assertEqual(len(bins), 10)
        self.assertEqual(sum(b.count for b in bins), len(self.y))
        # 完美校准：每个非空桶 mean_predicted ≈ mean_actual
        for b in bins:
            if b.count >= 50:
                self.assertAlmostEqual(b.mean_predicted, b.mean_actual, delta=0.06)
                self.assertIsInstance(b, ReliabilityBin)
                self.assertAlmostEqual(b.gap, b.mean_predicted - b.mean_actual, places=12)


class SlopeSemanticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.X, cls.y, cls.ret, cls.p = synth.ml_frame(n_rows=N, seed=7)

    def test_overconfident_slope_below_one(self):
        # 锐化：logit×2 → 拟合斜率应 ≈ 0.5（过度自信）
        p_sharp = _sigmoid(2.0 * _logit(self.p))
        slope, _ = calibration_slope_intercept(self.y, p_sharp)
        self.assertLess(slope, 0.8)

    def test_underconfident_slope_above_one(self):
        # 钝化：logit×0.5 → 拟合斜率应 ≈ 2（不够自信）
        p_soft = _sigmoid(0.5 * _logit(self.p))
        slope, _ = calibration_slope_intercept(self.y, p_soft)
        self.assertGreater(slope, 1.2)


class ConstantPredictionTest(unittest.TestCase):
    def test_ece_hand_computed(self):
        y = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1]  # mean = 0.6
        c = 0.3
        p = [c] * len(y)
        # 全部落入含 0.3 的单桶 → ECE = |0.3 - 0.6| = 0.3
        self.assertAlmostEqual(expected_calibration_error(y, p, n_bins=10), 0.3, places=9)

    def test_reliability_single_bin(self):
        y = [1, 0, 1, 1, 0]
        p = [0.3] * 5
        bins = reliability_table(y, p, n_bins=10)
        nonempty = [b for b in bins if b.count]
        self.assertEqual(len(nonempty), 1)
        self.assertEqual(nonempty[0].count, 5)
        self.assertAlmostEqual(nonempty[0].mean_predicted, 0.3, places=9)
        self.assertAlmostEqual(nonempty[0].mean_actual, 0.6, places=9)

    def test_perfectly_calibrated_constant_zero_ece(self):
        y = [1, 0, 1, 0]  # mean = 0.5
        p = [0.5] * 4
        self.assertAlmostEqual(expected_calibration_error(y, p, n_bins=10), 0.0, places=9)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.p = [0.1, 0.3, 0.5, 0.7, 0.9]

    def test_threshold_zero_covers_all(self):
        self.assertAlmostEqual(coverage_at_threshold(self.p, 0.0), 1.0, places=12)

    def test_threshold_above_max_covers_none(self):
        self.assertAlmostEqual(coverage_at_threshold(self.p, 0.95), 0.0, places=12)

    def test_threshold_boundary_inclusive(self):
        # p >= threshold：0.5 阈值含 0.5/0.7/0.9 → 3/5
        self.assertAlmostEqual(coverage_at_threshold(self.p, 0.5), 0.6, places=12)

    def test_monotone_decreasing(self):
        covs = [coverage_at_threshold(self.p, t) for t in (0.0, 0.3, 0.6, 0.9)]
        self.assertEqual(covs, sorted(covs, reverse=True))

    def test_empty_returns_zero(self):
        self.assertAlmostEqual(coverage_at_threshold([], 0.5), 0.0, places=12)


class DecisionValueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.X, cls.y, cls.ret, cls.p = synth.ml_frame(n_rows=N, seed=11)

    def test_threshold_zero_covers_all_matches_overall(self):
        dv = decision_value_report(self.y, self.ret, self.p, threshold=0.0)
        self.assertIsInstance(dv, DecisionValue)
        self.assertEqual(dv.n_total, len(self.y))
        self.assertEqual(dv.n_signals, len(self.y))
        self.assertAlmostEqual(dv.coverage, 1.0, places=12)
        self.assertAlmostEqual(dv.win_rate, float(np.mean(self.y)), places=9)
        self.assertAlmostEqual(dv.avg_net_return, float(np.mean(self.ret)), places=9)
        self.assertAlmostEqual(dv.expectancy, dv.avg_net_return, places=12)

    def test_subset_stats_internally_consistent(self):
        thr = 0.6
        dv = decision_value_report(self.y, self.ret, self.p, threshold=thr)
        mask = np.asarray(self.p) >= thr
        self.assertEqual(dv.n_signals, int(mask.sum()))
        self.assertAlmostEqual(dv.coverage, dv.n_signals / dv.n_total, places=12)
        self.assertAlmostEqual(dv.win_rate, float(np.asarray(self.y)[mask].mean()), places=9)
        self.assertAlmostEqual(
            dv.avg_net_return, float(np.asarray(self.ret)[mask].mean()), places=9
        )
        wins = np.asarray(self.ret)[mask & (np.asarray(self.y) == 1)]
        losses = np.asarray(self.ret)[mask & (np.asarray(self.y) == 0)]
        self.assertAlmostEqual(dv.avg_win, float(wins.mean()), places=9)
        self.assertAlmostEqual(dv.avg_loss, float(losses.mean()), places=9)

    def test_high_threshold_raises_win_rate_over_baseline(self):
        base = decision_value_report(self.y, self.ret, self.p, threshold=0.0)
        filtered = decision_value_report(self.y, self.ret, self.p, threshold=0.75)
        self.assertLess(filtered.coverage, base.coverage)  # 覆盖率有代价
        self.assertGreater(filtered.win_rate, base.win_rate)  # 但胜率提升

    def test_empty_subset(self):
        dv = decision_value_report(self.y, self.ret, self.p, threshold=1.5)
        self.assertEqual(dv.n_signals, 0)
        self.assertAlmostEqual(dv.coverage, 0.0, places=12)
        self.assertAlmostEqual(dv.win_rate, 0.0, places=12)
        self.assertAlmostEqual(dv.avg_net_return, 0.0, places=12)

    def test_empty_inputs(self):
        dv = decision_value_report([], [], [], threshold=0.5)
        self.assertEqual(dv.n_total, 0)
        self.assertAlmostEqual(dv.coverage, 0.0, places=12)


class ValidationAndEdgeTest(unittest.TestCase):
    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            brier_score([1, 0], [0.5])
        with self.assertRaises(ValueError):
            log_loss([1, 0], [0.5])
        with self.assertRaises(ValueError):
            reliability_table([1, 0], [0.5])
        with self.assertRaises(ValueError):
            calibration_slope_intercept([1, 0], [0.5])
        with self.assertRaises(ValueError):
            # y/net_ret 长度 2，p 长度 1 → 不一致
            decision_value_report([1, 0], [0.1, 0.2], [0.5], 0.5)

    def test_bad_n_bins_raises(self):
        with self.assertRaises(ValueError):
            reliability_table([1, 0], [0.5, 0.5], n_bins=0)

    def test_log_loss_clips_extremes_finite(self):
        val = log_loss([1, 0], [1.0, 0.0])  # 完美但极端 → 夹取后有限
        self.assertTrue(math.isfinite(val))
        self.assertLess(val, 1e-6)

    def test_brier_empty_is_zero(self):
        self.assertAlmostEqual(brier_score([], []), 0.0, places=12)

    def test_slope_intercept_tiny_sample_safe(self):
        slope, intercept = calibration_slope_intercept([1], [0.6])
        self.assertEqual((slope, intercept), (1.0, 0.0))  # n<2 退化为恒等


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
