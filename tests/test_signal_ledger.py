"""B2 信号台账测试（tmp DB 全链路：record → backfill → stats，离线手核）。

覆盖：v13 表读写、幂等 upsert（重复记录不重置已回填结果）、provisional 0/1 分条、
record_decision 消费 VoteDecision、backfill 用 B1 标签回填（filled/pending/bad_data
三态）、coverage 护栏、kv_store。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import synth
from ripple_tradePilot.backtest.costs import CostModel
from ripple_tradePilot.ml.labels import horizon_label
from ripple_tradePilot.models.types import Side
from ripple_tradePilot.signals.components import ComponentVote
from ripple_tradePilot.signals.voting import VoteDecision
from ripple_tradePilot.storage.database import init_database, load_daily_bars, upsert_daily_bars
from ripple_tradePilot.storage.signal_ledger import (
    backfill_outcomes,
    get_signal,
    kv_delete,
    kv_get,
    kv_set,
    list_pending,
    list_signals,
    record_decision,
    record_signal,
    signal_stats,
)

SYMBOL = "600000.SH"


class LedgerTestCase(unittest.TestCase):
    """带临时库 + 30 根确定性日线（已落 daily_bars）的基类。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "ledger.db"
        init_database(self.db)
        self.bars = synth.daily_bars(30, seed=5)
        upsert_daily_bars(SYMBOL, synth.daily_rows(self.bars), "synth", self.db)
        self.loaded = load_daily_bars(SYMBOL, self.db)
        self.opens = [b["open"] for b in self.loaded]
        self.highs = [b["high"] for b in self.loaded]
        self.lows = [b["low"] for b in self.loaded]
        self.closes = [b["close"] for b in self.loaded]

    def tearDown(self):
        self._tmp.cleanup()

    def _date(self, index: int) -> str:
        return self.bars[index].timestamp.strftime("%Y%m%d")


class RecordSignalTest(LedgerTestCase):
    def test_record_creates_pending_row(self):
        sid = record_signal(SYMBOL, self._date(3), recommendation="BUY",
                            buy_count=2, sell_count=0, vote_threshold=2, path=self.db)
        row = get_signal(sid, self.db)
        self.assertEqual(row["symbol"], SYMBOL)
        self.assertEqual(row["trade_date"], self._date(3))
        self.assertEqual(row["label_status"], "pending")
        self.assertEqual(row["source"], "monitor")
        self.assertEqual(row["provisional"], 0)
        self.assertEqual(row["horizon"], 5)
        self.assertIsNone(row["fwd_net_return"])

    def test_idempotent_same_key_single_row(self):
        a = record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        b = record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        self.assertEqual(a, b)
        rows = list_signals(symbol=SYMBOL, path=self.db)
        self.assertEqual(len(rows), 1)

    def test_rerecord_updates_decision_fields(self):
        sid = record_signal(SYMBOL, self._date(3), recommendation="HOLD", path=self.db)
        record_signal(SYMBOL, self._date(3), recommendation="BUY", buy_count=3, path=self.db)
        row = get_signal(sid, self.db)
        self.assertEqual(row["recommendation"], "BUY")
        self.assertEqual(row["buy_count"], 3)
        self.assertEqual(len(list_signals(symbol=SYMBOL, path=self.db)), 1)

    def test_rerecord_preserves_filled_outcome(self):
        """已回填的行被重复记录时，结果列不被重置（幂等不破坏回填）。"""
        sid = record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        backfill_outcomes(symbol=SYMBOL, path=self.db)
        filled = get_signal(sid, self.db)
        self.assertEqual(filled["label_status"], "filled")
        self.assertIsNotNone(filled["fwd_net_return"])
        # 重复记录（如 monitor 重启）只更新决策字段
        record_signal(SYMBOL, self._date(3), recommendation="SELL", sell_count=2, path=self.db)
        after = get_signal(sid, self.db)
        self.assertEqual(after["recommendation"], "SELL")  # 决策字段更新
        self.assertEqual(after["label_status"], "filled")  # 结果保留
        self.assertEqual(after["fwd_net_return"], filled["fwd_net_return"])
        self.assertIsNotNone(after["filled_at"])

    def test_provisional_distinct_rows(self):
        rec_final = record_signal(SYMBOL, self._date(3), provisional=0,
                                  recommendation="BUY", path=self.db)
        rec_prov = record_signal(SYMBOL, self._date(3), provisional=1,
                                 recommendation="CONFLICT", path=self.db)
        self.assertNotEqual(rec_final, rec_prov)
        rows = list_signals(symbol=SYMBOL, path=self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["provisional"] for r in rows}, {0, 1})

    def test_trade_date_normalized_from_datetime(self):
        # 传 datetime 与传 YYYYMMDD 落到同一行
        dt = self.bars[3].timestamp
        a = record_signal(SYMBOL, dt, recommendation="BUY", path=self.db)
        b = record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        self.assertEqual(a, b)

    def test_source_isolation(self):
        m = record_signal(SYMBOL, self._date(3), source="monitor", path=self.db)
        b = record_signal(SYMBOL, self._date(3), source="backtest", path=self.db)
        self.assertNotEqual(m, b)
        self.assertEqual(len(list_signals(symbol=SYMBOL, path=self.db)), 2)


class RecordDecisionTest(LedgerTestCase):
    def _vote(self, index=3):
        return VoteDecision(
            timestamp=self.bars[index].timestamp,
            recommendation="BUY",
            buy_count=2,
            sell_count=0,
            vote_threshold=2,
            components=(
                ComponentVote(name="ma", kind="ma", side=Side.BUY, strength=1.0),
                ComponentVote(name="rsi", kind="rsi", side=Side.BUY, strength=0.8),
                ComponentVote(name="bollinger", kind="bollinger", side=None, strength=1.0),
            ),
            reason="MA 金叉 + RSI 超卖",
        )

    def test_record_decision_serializes_vote(self):
        decision = self._vote(3)
        sid = record_decision(SYMBOL, decision, source="monitor", path=self.db)
        row = get_signal(sid, self.db)
        self.assertEqual(row["trade_date"], self._date(3))  # 取自 decision.timestamp
        self.assertEqual(row["recommendation"], "BUY")
        self.assertEqual(row["buy_count"], 2)
        self.assertEqual(row["vote_threshold"], 2)
        components = json.loads(row["components_json"])
        self.assertEqual(len(components), 3)
        self.assertEqual([c["name"] for c in components], ["ma", "rsi", "bollinger"])
        self.assertEqual([c["side"] for c in components], ["BUY", "BUY", None])

    def test_record_decision_explicit_trade_date_overrides(self):
        decision = self._vote(3)
        sid = record_decision(SYMBOL, decision, trade_date=self._date(5), path=self.db)
        self.assertEqual(get_signal(sid, self.db)["trade_date"], self._date(5))

    def test_record_decision_idempotent(self):
        decision = self._vote(3)
        a = record_decision(SYMBOL, decision, path=self.db)
        b = record_decision(SYMBOL, decision, path=self.db)
        self.assertEqual(a, b)
        self.assertEqual(len(list_signals(symbol=SYMBOL, path=self.db)), 1)


class BackfillTest(LedgerTestCase):
    def test_backfill_fills_and_matches_b1(self):
        sid = record_signal(SYMBOL, self._date(3), recommendation="BUY",
                            horizon=5, path=self.db)
        summary = backfill_outcomes(symbol=SYMBOL, path=self.db)
        self.assertEqual(summary["filled"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["bad_data"], 0)
        row = get_signal(sid, self.db)
        self.assertEqual(row["label_status"], "filled")
        # 与 B1 在同一组 bars 上的标签逐位一致
        expected = horizon_label(self.opens, self.highs, self.lows, self.closes,
                                 i=3, horizon=5)
        self.assertAlmostEqual(row["fwd_net_return"], expected.net_return, places=12)
        self.assertAlmostEqual(row["fwd_mae"], expected.mae, places=12)
        self.assertAlmostEqual(row["entry_price"], expected.entry, places=9)
        self.assertAlmostEqual(row["exit_price"], expected.exit, places=9)
        self.assertIsNotNone(row["filled_at"])

    def test_backfill_net_return_matches_manual_cost_formula(self):
        record_signal(SYMBOL, self._date(3), recommendation="BUY", horizon=5, path=self.db)
        backfill_outcomes(symbol=SYMBOL, path=self.db)
        row = list_signals(symbol=SYMBOL, status="filled", path=self.db)[0]
        # 独立手算：entry=open[4]、exit=open[9]（horizon=5）
        c = CostModel()
        entry = self.opens[4]
        exit_price = self.opens[9]
        buy_ps = entry * (1 + c.slippage) * (1 + c.fee_rate)
        sell_ps = exit_price * (1 - c.slippage) * (1 - c.fee_rate - c.stamp_duty)
        self.assertAlmostEqual(row["entry_price"], entry, places=9)
        self.assertAlmostEqual(row["exit_price"], exit_price, places=9)
        self.assertAlmostEqual(row["fwd_net_return"], sell_ps / buy_ps - 1.0, places=12)

    def test_aux_horizon_filled(self):
        # index 3，aux_horizon=10 → 需 open[14]，30 根足够
        sid = record_signal(SYMBOL, self._date(3), recommendation="BUY",
                            horizon=5, path=self.db)
        backfill_outcomes(symbol=SYMBOL, aux_horizon=10, path=self.db)
        row = get_signal(sid, self.db)
        aux = horizon_label(self.opens, self.highs, self.lows, self.closes,
                            i=3, horizon=10)
        self.assertIsNotNone(aux)
        self.assertAlmostEqual(row["fwd_ret_aux"], aux.net_return, places=12)

    def test_aux_horizon_insufficient_leaves_main_filled(self):
        # index 18，horizon=5 → exit=open[24] 有效；aux=10 → exit=open[29] 有效；
        # index 20，horizon=5 → exit=open[26]；aux=10 → open[31] 越界 → aux 留空
        sid = record_signal(SYMBOL, self._date(20), recommendation="BUY",
                            horizon=5, path=self.db)
        backfill_outcomes(symbol=SYMBOL, aux_horizon=10, path=self.db)
        row = get_signal(sid, self.db)
        self.assertEqual(row["label_status"], "filled")
        self.assertIsNotNone(row["fwd_net_return"])
        self.assertIsNone(row["fwd_ret_aux"])  # 辅助 horizon 数据不足

    def test_pending_when_bars_insufficient(self):
        # index 27，horizon=5 → exit=open[33] 越界（只有 30 根）→ 保持 pending
        sid = record_signal(SYMBOL, self._date(27), recommendation="BUY",
                            horizon=5, path=self.db)
        summary = backfill_outcomes(symbol=SYMBOL, path=self.db)
        self.assertEqual(summary["filled"], 0)
        self.assertEqual(summary["pending"], 1)
        row = get_signal(sid, self.db)
        self.assertEqual(row["label_status"], "pending")
        self.assertIsNone(row["fwd_net_return"])

    def test_bad_data_when_decision_date_missing(self):
        sid = record_signal(SYMBOL, "20200101", recommendation="BUY", path=self.db)
        summary = backfill_outcomes(symbol=SYMBOL, path=self.db)
        self.assertEqual(summary["bad_data"], 1)
        self.assertEqual(get_signal(sid, self.db)["label_status"], "bad_data")

    def test_backfill_only_touches_pending(self):
        record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(27), recommendation="BUY", path=self.db)  # 不足
        first = backfill_outcomes(symbol=SYMBOL, path=self.db)
        self.assertEqual(first["filled"], 1)
        self.assertEqual(first["pending"], 1)
        # 第二次：已 filled 的不再处理，仍 pending 的依旧 pending
        second = backfill_outcomes(symbol=SYMBOL, path=self.db)
        self.assertEqual(second["total"], 1)
        self.assertEqual(second["filled"], 0)
        self.assertEqual(second["pending"], 1)

    def test_backfill_fills_when_more_bars_arrive(self):
        """index 27 先 pending，补入未来 bars 后应能回填（台账闭环核心）。"""
        sid = record_signal(SYMBOL, self._date(27), recommendation="BUY",
                            horizon=5, path=self.db)
        self.assertEqual(backfill_outcomes(symbol=SYMBOL, path=self.db)["pending"], 1)
        # 追加更多 bars（延长同一序列）
        extended = synth.daily_bars(40, seed=5)
        upsert_daily_bars(SYMBOL, synth.daily_rows(extended), "synth", self.db)
        summary = backfill_outcomes(symbol=SYMBOL, path=self.db)
        self.assertEqual(summary["filled"], 1)
        self.assertEqual(get_signal(sid, self.db)["label_status"], "filled")


class ListAndStatsTest(LedgerTestCase):
    def test_list_pending_filters(self):
        record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(4), recommendation="HOLD", path=self.db)
        backfill_outcomes(symbol=SYMBOL, path=self.db)  # index3 filled, index4 不足? 30根够
        pending = list_pending(symbol=SYMBOL, path=self.db)
        # index3、index4 都能回填（exit=open[9]/open[10] < 30）→ pending 为空
        self.assertEqual(len(pending), 0)
        filled = list_signals(symbol=SYMBOL, status="filled", path=self.db)
        self.assertEqual(len(filled), 2)

    def test_list_signals_source_and_limit(self):
        record_signal(SYMBOL, self._date(3), source="monitor", path=self.db)
        record_signal(SYMBOL, self._date(4), source="backtest", path=self.db)
        record_signal(SYMBOL, self._date(5), source="monitor", path=self.db)
        self.assertEqual(len(list_signals(source="monitor", path=self.db)), 2)
        self.assertEqual(len(list_signals(source="backtest", path=self.db)), 1)
        self.assertEqual(len(list_signals(limit=2, path=self.db)), 2)

    def test_coverage_guardrail(self):
        # 4 个标的·日：2 BUY（可执行）+ 2 HOLD → coverage = 2/4 = 0.5
        record_signal(SYMBOL, self._date(2), recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(3), recommendation="HOLD", path=self.db)
        record_signal(SYMBOL, self._date(4), recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(5), recommendation="HOLD", path=self.db)
        stats = signal_stats(path=self.db)
        self.assertEqual(stats["total_rows"], 4)
        self.assertEqual(stats["evaluated_days"], 4)
        self.assertEqual(stats["signal_days"], 2)
        self.assertAlmostEqual(stats["coverage"], 0.5, places=9)
        self.assertEqual(stats["by_recommendation"]["BUY"], 2)
        self.assertEqual(stats["by_recommendation"]["HOLD"], 2)
        # 尚未回填 → 胜率/期望为 0，filled_count=0
        self.assertEqual(stats["filled_count"], 0)
        self.assertAlmostEqual(stats["win_rate"], 0.0, places=9)

    def test_stats_win_rate_over_filled_only(self):
        record_signal(SYMBOL, self._date(3), recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(4), recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(27), recommendation="BUY", path=self.db)  # 不足→pending
        backfill_outcomes(symbol=SYMBOL, path=self.db)
        stats = signal_stats(path=self.db)
        filled_rows = list_signals(symbol=SYMBOL, status="filled", path=self.db)
        self.assertEqual(stats["filled_count"], len(filled_rows))
        returns = [r["fwd_net_return"] for r in filled_rows]
        expected_win = sum(1 for x in returns if x > 0) / len(returns)
        self.assertAlmostEqual(stats["win_rate"], expected_win, places=9)
        self.assertAlmostEqual(stats["avg_net_return"], sum(returns) / len(returns), places=9)
        # pending 行不计入 filled_count
        self.assertEqual(stats["by_status"].get("pending", 0), 1)

    def test_stats_source_filter(self):
        record_signal(SYMBOL, self._date(3), source="monitor", recommendation="BUY", path=self.db)
        record_signal(SYMBOL, self._date(4), source="backtest", recommendation="BUY", path=self.db)
        self.assertEqual(signal_stats(source="monitor", path=self.db)["total_rows"], 1)
        self.assertEqual(signal_stats(source="backtest", path=self.db)["total_rows"], 1)
        self.assertEqual(signal_stats(path=self.db)["total_rows"], 2)

    def test_stats_empty(self):
        stats = signal_stats(path=self.db)
        self.assertEqual(stats["total_rows"], 0)
        self.assertAlmostEqual(stats["coverage"], 0.0, places=9)
        self.assertEqual(stats["date_range"], (None, None))


class KvStoreTest(LedgerTestCase):
    def test_set_get_delete(self):
        self.assertIsNone(kv_get("missing", path=self.db))
        self.assertEqual(kv_get("missing", "fallback", self.db), "fallback")
        kv_set("last_finalize", "20260905", self.db)
        self.assertEqual(kv_get("last_finalize", path=self.db), "20260905")
        kv_set("last_finalize", "20260906", self.db)  # upsert
        self.assertEqual(kv_get("last_finalize", path=self.db), "20260906")
        kv_delete("last_finalize", self.db)
        self.assertIsNone(kv_get("last_finalize", path=self.db))

    def test_value_coerced_to_string(self):
        kv_set("count", 42, self.db)
        self.assertEqual(kv_get("count", path=self.db), "42")

    def test_none_value_stored_as_null(self):
        kv_set("empty", None, self.db)
        self.assertIsNone(kv_get("empty", path=self.db))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
