"""A9 复权基准漂移检测纯函数测试。

夹具沿用 tests/synth.py：``daily_bars(ex_div=(i, f))`` 造除权跳变序列，
``daily_rows`` 转 upsert 行格式，``scale_rows`` 构造"新旧复权基准"两组数据。
"""

import unittest

import synth

from ripple_tradePilot.data.adjustment import (
    audit_series,
    detect_rebase,
    estimate_rebase_factor,
    scale_ohlc,
)


class DetectRebaseTest(unittest.TestCase):
    def test_identical_series_no_rebase(self):
        rows = synth.daily_rows(synth.daily_bars(40, seed=3))
        report = detect_rebase(rows, rows)
        self.assertFalse(report.rebased)
        self.assertEqual(report.factor, 1.0)
        self.assertEqual(report.deviation_count, 0)
        self.assertEqual(report.overlap_count, 40)

    def test_uniform_rebase_detected(self):
        # 上游整段按新锚重算（×0.9）：重叠日比值恒为 0.9 → 命中
        stored = synth.daily_rows(synth.daily_bars(200, seed=7))
        fetched = synth.scale_rows(stored, 0.9)
        report = detect_rebase(stored, fetched)
        self.assertTrue(report.rebased)
        self.assertAlmostEqual(report.factor, 0.9, places=3)
        self.assertEqual(report.max_run, 200)
        self.assertEqual(report.deviation_count, 200)
        self.assertTrue(report.consistent)

    def test_ex_div_within_window_detected(self):
        # ex_div=(150,0.9)：第 150 根起复权基准下移，重叠区 [150:200] 连续偏差 → 命中
        stored = synth.daily_rows(synth.daily_bars(200, seed=7))
        fetched = synth.daily_rows(synth.daily_bars(200, seed=7, ex_div=(150, 0.9)))
        report = detect_rebase(stored, fetched)
        self.assertTrue(report.rebased)
        self.assertAlmostEqual(report.factor, 0.9, places=3)
        self.assertEqual(report.max_run, 50)
        self.assertEqual(report.deviation_count, 50)
        # 触发 run 起点应为除权日
        self.assertEqual(report.samples[0][0], stored[150]["trade_date"])

    def test_single_isolated_deviation_not_triggered(self):
        # 除权落在最后一根 → 仅 1 日偏差，记录但不触发（误报保护）
        stored = synth.daily_rows(synth.daily_bars(40, seed=5))
        fetched = synth.daily_rows(synth.daily_bars(40, seed=5, ex_div=(39, 0.9)))
        report = detect_rebase(stored, fetched)
        self.assertFalse(report.rebased)
        self.assertEqual(report.deviation_count, 1)
        self.assertEqual(report.max_run, 1)
        self.assertEqual(report.factor, 1.0)

    def test_inconsistent_magnitude_not_rebase(self):
        # 连续 2 日偏差但幅度不恒定（0.9 / 0.75）→ 疑似噪音，不判复权重算
        stored = synth.daily_rows(synth.daily_bars(10, seed=1))
        fetched = [dict(row) for row in stored]
        fetched[5]["close"] = stored[5]["close"] * 0.9
        fetched[6]["close"] = stored[6]["close"] * 0.75
        report = detect_rebase(stored, fetched)
        self.assertFalse(report.rebased)
        self.assertEqual(report.max_run, 2)
        self.assertFalse(report.consistent)

    def test_mixed_direction_not_rebase(self):
        # 连续 2 日偏差但方向相反（0.9 / 1.1）→ 不一致，不触发
        stored = synth.daily_rows(synth.daily_bars(10, seed=1))
        fetched = [dict(row) for row in stored]
        fetched[5]["close"] = stored[5]["close"] * 0.9
        fetched[6]["close"] = stored[6]["close"] * 1.1
        report = detect_rebase(stored, fetched)
        self.assertFalse(report.rebased)
        self.assertFalse(report.consistent)

    def test_min_consecutive_respected(self):
        stored = synth.daily_rows(synth.daily_bars(3, seed=1))
        fetched = synth.scale_rows(stored, 0.9)
        self.assertFalse(detect_rebase(stored, fetched, min_consecutive=5).rebased)
        self.assertTrue(detect_rebase(stored, fetched, min_consecutive=2).rebased)

    def test_no_overlap_no_rebase(self):
        rows = synth.daily_rows(synth.daily_bars(20, seed=2))
        report = detect_rebase(rows[:5], rows[10:15])
        self.assertFalse(report.rebased)
        self.assertEqual(report.overlap_count, 0)

    def test_invalid_min_consecutive_raises(self):
        rows = synth.daily_rows(synth.daily_bars(5, seed=1))
        with self.assertRaises(ValueError):
            detect_rebase(rows, rows, min_consecutive=0)


class AuditSeriesTest(unittest.TestCase):
    def test_clean_series_passes(self):
        rows = synth.daily_rows(synth.daily_bars(60, seed=3))
        report = audit_series(rows)
        self.assertTrue(report.clean)
        self.assertEqual(report.checked, 59)
        self.assertEqual(report.anomalies, ())

    def test_splice_point_flagged(self):
        # 旧锚 head + 新锚 tail 混接：接点处 close 跳变但 pct_chg 未更新 → 恰好 1 处异常
        rows = synth.daily_rows(synth.daily_bars(60, seed=3))
        spliced = synth.scale_rows(rows, 0.9, start=40)
        report = audit_series(spliced)
        self.assertFalse(report.clean)
        self.assertEqual(len(report.anomalies), 1)
        self.assertEqual(report.anomalies[0].trade_date, rows[40]["trade_date"])
        self.assertIsNotNone(report.worst)
        # 接点偏差量级 ≈ 复权因子偏离（0.1），远超 tol
        self.assertGreater(report.worst.diff, 0.05)

    def test_genuine_ex_div_with_consistent_pct_chg_is_clean(self):
        # daily_rows 依跳变后 close 重算 pct_chg → 序列自洽，audit 不应误报
        rows = synth.daily_rows(synth.daily_bars(60, seed=4, ex_div=(30, 0.9)))
        report = audit_series(rows)
        self.assertTrue(report.clean)

    def test_rows_without_pct_chg_skipped(self):
        rows = synth.daily_rows(synth.daily_bars(10, seed=1))
        for row in rows:
            row.pop("pct_chg", None)
        report = audit_series(rows)
        self.assertTrue(report.clean)
        self.assertEqual(report.checked, 0)


class EstimateRebaseFactorTest(unittest.TestCase):
    def test_ratio_computed(self):
        self.assertAlmostEqual(estimate_rebase_factor(10.0, 9.0), 0.9, places=6)

    def test_missing_or_non_positive_returns_one(self):
        self.assertEqual(estimate_rebase_factor(10.0, None), 1.0)
        self.assertEqual(estimate_rebase_factor(None, 9.0), 1.0)
        self.assertEqual(estimate_rebase_factor(0.0, 9.0), 1.0)
        self.assertEqual(estimate_rebase_factor(10.0, 0.0), 1.0)


class ScaleOhlcTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "trade_date": "20260101",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.4,
                "vol": 100,
                "pct_chg": 2.94,
            }
        ]

    def test_prices_scaled_volume_untouched(self):
        scaled = scale_ohlc(self.rows, 0.9)
        self.assertAlmostEqual(scaled[0]["close"], 9.45, places=6)
        self.assertAlmostEqual(scaled[0]["open"], 9.0, places=6)
        self.assertAlmostEqual(scaled[0]["pre_close"], 9.36, places=6)
        self.assertEqual(scaled[0]["vol"], 100)
        self.assertEqual(scaled[0]["pct_chg"], 2.94)
        self.assertEqual(scaled[0]["trade_date"], "20260101")

    def test_factor_one_returns_copy(self):
        scaled = scale_ohlc(self.rows, 1.0)
        self.assertEqual(scaled[0]["close"], 10.5)
        self.assertIsNot(scaled[0], self.rows[0])

    def test_non_positive_factor_raises(self):
        with self.assertRaises(ValueError):
            scale_ohlc(self.rows, 0.0)
        with self.assertRaises(ValueError):
            scale_ohlc(self.rows, -1.0)


if __name__ == "__main__":
    unittest.main()
