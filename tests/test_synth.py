"""tests/synth.py 夹具自身的冒烟测试：确定性与结构约定。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import synth


class SynthFixturesTest(unittest.TestCase):
    def test_daily_bars_deterministic_and_positive(self):
        first = synth.daily_bars(120, seed=3)
        second = synth.daily_bars(120, seed=3)
        self.assertEqual(len(first), 120)
        self.assertEqual(first, second)  # 同 seed 逐位一致
        self.assertNotEqual(first, synth.daily_bars(120, seed=4))
        for bar in first:
            self.assertGreater(bar.close, 0)
            self.assertGreater(bar.open, 0)
            self.assertGreaterEqual(bar.high, bar.low)
        # 时间戳唯一且严格递增，全部为工作日
        stamps = [bar.timestamp for bar in first]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertTrue(all(stamp.weekday() < 5 for stamp in stamps))

    def test_ex_div_creates_level_jump(self):
        bars = synth.bars_from_closes(
            synth.linear_closes(60, step=0.0), ex_div=(30, 0.9)
        )
        # 除权日前后收盘价跳变 ≈ factor
        self.assertAlmostEqual(bars[30].close / bars[29].close, 0.9, places=3)
        self.assertAlmostEqual(bars[31].close / bars[30].close, 1.0, places=3)

    def test_sine_closes_trigger_rsi_extremes(self):
        from ripple_tradePilot.indicators import rsi_series

        closes = synth.sine_closes(80, mean=10.0, amplitude=1.5, period=20)
        values = [v for v in rsi_series(closes, 14) if v is not None]
        self.assertTrue(any(v <= 30 for v in values), "正弦序列应触发超卖")
        self.assertTrue(any(v >= 70 for v in values), "正弦序列应触发超买")

    def test_daily_rows_round_trip(self):
        bars = synth.daily_bars(40, seed=1)
        rows = synth.daily_rows(bars)
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[0]["trade_date"], bars[0].timestamp.strftime("%Y%m%d"))
        for row in rows:
            self.assertIn("open", row)
            self.assertIn("pct_chg", row)

        # 能直接灌入真实 schema（tmp DB）
        from ripple_tradePilot.storage.database import (
            init_database,
            load_daily_bars,
            upsert_daily_bars,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "synth.db"
            init_database(db_path)
            written = upsert_daily_bars("002022.SZ", rows, "synth", db_path)
            self.assertEqual(written, 40)
            loaded = load_daily_bars("002022.SZ", path=db_path)
            self.assertEqual(len(loaded), 40)

    def test_scale_rows_only_touches_price_columns(self):
        rows = synth.daily_rows(synth.daily_bars(10, seed=2))
        scaled = synth.scale_rows(rows, 0.9, start=5)
        for index in range(5):
            self.assertEqual(scaled[index], rows[index])
        for index in range(5, 10):
            self.assertAlmostEqual(scaled[index]["close"], rows[index]["close"] * 0.9, places=3)
            self.assertEqual(scaled[index]["trade_date"], rows[index]["trade_date"])

    def test_ml_frame_true_probability_chain(self):
        import numpy as np

        X, y, ret, p_true = synth.ml_frame(n_rows=5000, n_features=4, seed=0)
        self.assertEqual(X.shape, (5000, 4))
        self.assertEqual(set(np.unique(y)), {0, 1})
        # y 的经验均值应逼近 p_true（大样本）
        self.assertAlmostEqual(float(y.mean()), float(p_true.mean()), delta=0.03)
        # 正例平均收益高于负例（ret 构造保证）
        self.assertGreater(ret[y == 1].mean(), ret[y == 0].mean())

    def test_seed_market_db_multi_symbol(self):
        from ripple_tradePilot.storage.database import load_daily_bars

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            synth.seed_market_db(db_path, days=120)
            for symbol in ("002022.SZ", "600309.SH", "601816.SH"):
                rows = load_daily_bars(symbol, path=db_path)
                self.assertEqual(len(rows), 120, symbol)
            # 确定性：重灌后数值一致
            again = synth.daily_bars(120, seed=100, drift=0.0003 * -1)
            self.assertEqual(again, synth.daily_bars(120, seed=100, drift=-0.0003))


if __name__ == "__main__":
    unittest.main()
