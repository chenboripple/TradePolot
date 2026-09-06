"""A3 日线评估编排单测（monitor/daily_eval.py）。

覆盖：provisional bar 合成、build_daily_series 的复权防护（rebase 缩放/打标 + 容差内不缩放）
与 provisional 追加规则、evaluate_symbol_daily 的数据门槛与库读、DailyEvaluation 的通知门控
属性（new_event 落在最后一根才算"当日新事件"）与 trigger_components。

全部离线：纯函数直接喂合成行；DB 用例用 tempfile 库并显式传 db_path（不碰环境变量）。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import synth

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.monitor.daily_eval import (
    MIN_DAILY_BARS,
    REBASE_TOL,
    DailyEvaluation,
    build_daily_series,
    evaluate_symbol_daily,
    synthesize_provisional_bar,
    today_str,
)
from ripple_tradePilot.signals.components import ComponentVote
from ripple_tradePilot.signals.voting import VoteDecision, VoteEvent
from ripple_tradePilot.storage.database import init_database, load_daily_bars, upsert_daily_bars

SYMBOL = "600000.SH"
TS = datetime(2024, 1, 2)


def _rows(closes, start_date="20240101"):
    """构造连续交易日的库内日线行（精确控制 close，供 rebase 断言）。"""
    base = datetime.strptime(start_date, "%Y%m%d")
    rows = []
    for offset, close in enumerate(closes):
        day = (base + timedelta(days=offset)).strftime("%Y%m%d")
        rows.append({
            "trade_date": day,
            "open": close,
            "high": round(close * 1.01, 4),
            "low": round(close * 0.99, 4),
            "close": close,
            "pre_close": close,
            "vol": 100000.0,
        })
    return rows


def _quote(price, pre_close, open_=None, high=None, low=None, volume=1000000.0):
    return {
        "symbol": SYMBOL,
        "price": price,
        "pre_close": pre_close,
        "open": open_ if open_ is not None else price,
        "high": high if high is not None else price,
        "low": low if low is not None else price,
        "volume": volume,
    }


def _decision(recommendation, components, buy_count=None, sell_count=None, threshold=2):
    buy = sum(1 for c in components if c.side is Side.BUY) if buy_count is None else buy_count
    sell = sum(1 for c in components if c.side is Side.SELL) if sell_count is None else sell_count
    return VoteDecision(
        timestamp=TS,
        recommendation=recommendation,
        buy_count=buy,
        sell_count=sell,
        vote_threshold=threshold,
        components=tuple(components),
    )


class SynthesizeProvisionalBarTest(unittest.TestCase):
    def test_valid_quote_maps_price_to_close(self):
        bar = synthesize_provisional_bar(_quote(10.5, 10.0, open_=10.1, high=10.8, low=10.0), "20240103")
        self.assertIsNotNone(bar)
        self.assertEqual(bar["trade_date"], "20240103")
        self.assertEqual(bar["close"], 10.5)
        self.assertEqual(bar["vol"], 1000000.0)
        self.assertEqual(bar["pre_close"], 10.0)
        self.assertTrue(bar["provisional"])
        self.assertGreaterEqual(bar["high"], bar["close"])
        self.assertGreaterEqual(bar["close"], bar["low"])

    def test_price_none_returns_none(self):
        self.assertIsNone(synthesize_provisional_bar(_quote(None, 10.0), "20240103"))

    def test_price_zero_returns_none(self):
        self.assertIsNone(synthesize_provisional_bar(_quote(0.0, 10.0), "20240103"))

    def test_price_negative_returns_none(self):
        self.assertIsNone(synthesize_provisional_bar(_quote(-3.0, 10.0), "20240103"))

    def test_missing_high_low_fall_back_to_price(self):
        quote = {"price": 12.0, "pre_close": 11.0, "open": 11.5, "volume": 500.0}
        bar = synthesize_provisional_bar(quote, "20240103")
        self.assertIsNotNone(bar)
        # high/low 缺失 → 用 price 兜底，OHLC 自洽
        self.assertGreaterEqual(bar["high"], bar["close"])
        self.assertGreaterEqual(bar["close"], bar["low"])
        self.assertEqual(bar["close"], 12.0)


class BuildDailySeriesTest(unittest.TestCase):
    def test_no_quote_returns_copies_without_mutation(self):
        db_rows = _rows([10.0, 10.5, 11.0])
        snapshot = [dict(r) for r in db_rows]
        rows, provisional, rebased, factor = build_daily_series(db_rows, None, trade_date="20240105")
        self.assertFalse(provisional)
        self.assertFalse(rebased)
        self.assertEqual(factor, 1.0)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["close"] for r in rows], [10.0, 10.5, 11.0])
        # 入参未被修改
        self.assertEqual(db_rows, snapshot)
        # 返回的是副本（改副本不影响入参）
        rows[0]["close"] = 999.0
        self.assertEqual(db_rows[0]["close"], 10.0)

    def test_quote_after_last_db_date_appends_provisional(self):
        db_rows = _rows([10.0, 10.0])  # 末根 close=10.0，日期 20240102
        quote = _quote(10.3, 10.0)     # pre_close 与末根一致 → 不 rebase
        rows, provisional, rebased, factor = build_daily_series(db_rows, quote, trade_date="20240103")
        self.assertTrue(provisional)
        self.assertFalse(rebased)
        self.assertEqual(factor, 1.0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["close"], 10.3)
        self.assertEqual(rows[-1]["trade_date"], "20240103")
        self.assertTrue(rows[-1]["provisional"])

    def test_db_already_has_today_no_append(self):
        db_rows = _rows([10.0, 10.0], start_date="20240102")  # 末根日期 20240103
        quote = _quote(10.3, 10.0)
        rows, provisional, rebased, factor = build_daily_series(db_rows, quote, trade_date="20240103")
        self.assertFalse(provisional)
        self.assertFalse(rebased)
        self.assertEqual(len(rows), 2)  # 未追加

    def test_rebase_scales_history_and_flags(self):
        db_rows = _rows([10.0, 10.0])      # 末根 close=10.0
        quote = _quote(9.5, 9.0)           # 快照 pre_close=9.0 → factor=0.9（偏离 10% > 0.5%）
        rows, provisional, rebased, factor = build_daily_series(db_rows, quote, trade_date="20240103")
        self.assertTrue(rebased)
        self.assertTrue(provisional)
        self.assertAlmostEqual(factor, 0.9, places=6)
        self.assertEqual(len(rows), 3)
        # 历史被缩放到当日基准：末根历史 close 10.0*0.9=9.0
        self.assertAlmostEqual(rows[-2]["close"], 9.0, places=6)
        # provisional bar 是当日原始价，不被缩放
        self.assertEqual(rows[-1]["close"], 9.5)

    def test_within_tolerance_no_rebase(self):
        db_rows = _rows([10.0, 10.0])
        quote = _quote(10.05, 10.02)       # factor=1.002（偏离 0.2% < 0.5%）
        rows, provisional, rebased, factor = build_daily_series(db_rows, quote, trade_date="20240103")
        self.assertFalse(rebased)
        self.assertTrue(provisional)
        self.assertEqual(factor, 1.0)      # 容差内返回默认 1.0，不是 estimated
        self.assertAlmostEqual(rows[-2]["close"], 10.0, places=6)  # 历史未缩放

    def test_rebase_tolerance_constant_matches_a9(self):
        self.assertAlmostEqual(REBASE_TOL, 0.005, places=6)


class EvaluateSymbolDailyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "tradepilot.db"
        init_database(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, count, seed=5):
        bars = synth.daily_bars(count, seed=seed)
        upsert_daily_bars(SYMBOL, synth.daily_rows(bars), "synth", self.db_path)
        return bars

    def test_insufficient_data(self):
        self._seed(30)
        ev = evaluate_symbol_daily(SYMBOL, None, db_path=self.db_path)
        self.assertTrue(ev.insufficient)
        self.assertIsNone(ev.decision)
        self.assertIsNone(ev.spec)
        self.assertEqual(ev.n_bars, 30)
        self.assertIsNone(ev.recommendation)
        self.assertFalse(ev.is_actionable)

    def test_sufficient_data_returns_decision(self):
        self._seed(200)
        ev = evaluate_symbol_daily(SYMBOL, None, db_path=self.db_path)
        self.assertFalse(ev.insufficient)
        self.assertIsInstance(ev.decision, VoteDecision)
        self.assertIn(ev.recommendation, {"BUY", "SELL", "HOLD", "CONFLICT"})
        self.assertEqual(ev.n_bars, 200)
        self.assertFalse(ev.provisional)
        self.assertEqual(len(ev.bars), 200)
        self.assertIsNotNone(ev.latest_bar)
        self.assertGreater(ev.latest_price, 0.0)

    def test_min_bars_threshold_constant(self):
        self.assertEqual(MIN_DAILY_BARS, 60)
        # 恰好低于门槛 → insufficient
        self._seed(MIN_DAILY_BARS - 1, seed=9)
        ev = evaluate_symbol_daily(SYMBOL, None, db_path=self.db_path)
        self.assertTrue(ev.insufficient)

    def test_quote_for_future_date_marks_provisional(self):
        bars = self._seed(200)
        last_close = bars[-1].close
        quote = _quote(last_close, last_close)  # pre_close==末根 close → 不 rebase
        ev = evaluate_symbol_daily(
            SYMBOL, None, db_path=self.db_path, quote=quote, trade_date="20991231"
        )
        self.assertFalse(ev.insufficient)
        self.assertTrue(ev.provisional)
        self.assertFalse(ev.rebased)
        self.assertEqual(ev.n_bars, 201)


class DailyEvaluationPropertyTest(unittest.TestCase):
    def _eval(self, *, n_bars, events, decision=None, insufficient=False, bars=()):
        return DailyEvaluation(
            symbol=SYMBOL,
            n_bars=n_bars,
            provisional=False,
            rebased=False,
            rebase_factor=1.0,
            trade_date="20240102",
            insufficient=insufficient,
            decision=decision,
            events=tuple(events),
            bars=tuple(bars),
        )

    def test_new_event_when_index_is_last_bar(self):
        dec = _decision("BUY", [ComponentVote("ma", "ma", Side.BUY), ComponentVote("rsi", "rsi", Side.BUY)])
        event = VoteEvent(index=0, timestamp=TS, side=Side.BUY, decision=dec, previous_recommendation="HOLD")
        ev = self._eval(n_bars=1, events=[event], decision=dec)
        self.assertIs(ev.new_event, event)

    def test_new_event_none_when_event_is_stale(self):
        dec = _decision("BUY", [ComponentVote("ma", "ma", Side.BUY), ComponentVote("rsi", "rsi", Side.BUY)])
        event = VoteEvent(index=2, timestamp=TS, side=Side.BUY, decision=dec, previous_recommendation="HOLD")
        ev = self._eval(n_bars=5, events=[event], decision=dec)  # 末根 index=4，事件在 2
        self.assertIsNone(ev.new_event)

    def test_new_event_none_when_no_events(self):
        dec = _decision("HOLD", [ComponentVote("ma", "ma", None)])
        ev = self._eval(n_bars=3, events=[], decision=dec)
        self.assertIsNone(ev.new_event)

    def test_new_event_none_when_insufficient(self):
        dec = _decision("BUY", [ComponentVote("ma", "ma", Side.BUY), ComponentVote("rsi", "rsi", Side.BUY)])
        event = VoteEvent(index=0, timestamp=TS, side=Side.BUY, decision=dec, previous_recommendation="HOLD")
        ev = self._eval(n_bars=1, events=[event], decision=dec, insufficient=True)
        self.assertIsNone(ev.new_event)

    def test_trigger_components_buy(self):
        components = [
            ComponentVote("ma", "ma", Side.BUY),
            ComponentVote("rsi", "rsi", Side.BUY),
            ComponentVote("bollinger", "bollinger", None),
        ]
        dec = _decision("BUY", components)
        ev = self._eval(n_bars=1, events=[], decision=dec)
        self.assertEqual(ev.trigger_components, ("ma", "rsi"))

    def test_trigger_components_empty_for_conflict(self):
        components = [
            ComponentVote("ma", "ma", Side.BUY),
            ComponentVote("rsi", "rsi", Side.SELL),
        ]
        dec = _decision("CONFLICT", components, buy_count=1, sell_count=1)
        ev = self._eval(n_bars=1, events=[], decision=dec)
        self.assertEqual(ev.trigger_components, ())

    def test_recommendation_and_actionable(self):
        dec = _decision("SELL", [ComponentVote("ma", "ma", Side.SELL), ComponentVote("rsi", "rsi", Side.SELL)])
        ev = self._eval(n_bars=1, events=[], decision=dec)
        self.assertEqual(ev.recommendation, "SELL")
        self.assertTrue(ev.is_actionable)
        hold = self._eval(n_bars=1, events=[], decision=_decision("HOLD", [ComponentVote("ma", "ma", None)]))
        self.assertFalse(hold.is_actionable)

    def test_latest_price_zero_without_bars(self):
        ev = self._eval(n_bars=0, events=[], decision=None)
        self.assertIsNone(ev.latest_bar)
        self.assertEqual(ev.latest_price, 0.0)

    def test_latest_price_from_last_bar(self):
        bar = Bar(timestamp=TS, open=10.0, high=11.0, low=9.0, close=10.5, volume=1000.0)
        ev = self._eval(n_bars=1, events=[], decision=None, bars=[bar])
        self.assertIs(ev.latest_bar, bar)
        self.assertEqual(ev.latest_price, 10.5)


class TodayStrTest(unittest.TestCase):
    def test_formats_given_moment(self):
        self.assertEqual(today_str(datetime(2026, 9, 6)), "20260906")

    def test_defaults_to_now(self):
        self.assertEqual(today_str(), datetime.now().strftime("%Y%m%d"))


if __name__ == "__main__":
    unittest.main()
