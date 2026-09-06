"""B2 CLI 测试：`tradepilot signals backfill|stats|list` + `backtest --write-signals`。

复用 test_cli_backtest 的 tmp config/DB + mock loader 离线夹具。验证：
- --write-signals 把统一 combo 口径的 BUY 事件写入台账，并把 bars 落库以便立即回填；
- signals backfill 用 daily_bars + B1 标签回填；
- signals stats 输出 coverage/胜率；signals list 可读回；空台账优雅提示。
"""
from __future__ import annotations

import unittest

from test_cli_backtest import FakeLoader, _CliDbTestCase

from ripple_tradePilot.cli import cli
from ripple_tradePilot.signals.facade import evaluate_symbol
from ripple_tradePilot.storage.database import load_daily_bars
from ripple_tradePilot.storage.signal_ledger import list_signals, signal_stats

SYMBOL = "600000.SH"


def _expected_buy_dates():
    """与 CLI --write-signals 同口径：默认 combo 画像在 FakeLoader.bars 上的 BUY 事件日。"""
    evaluation = evaluate_symbol(FakeLoader.bars, profile=None)
    return [
        e.timestamp.strftime("%Y%m%d")
        for e in evaluation.events
        if e.decision.recommendation == "BUY"
    ]


class CliWriteSignalsTest(_CliDbTestCase):
    def test_write_signals_persists_ledger_and_bars(self):
        result = self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("已写入信号台账", result.output)

        rows = list_signals(source="backtest", symbol=SYMBOL)
        expected_dates = _expected_buy_dates()
        self.assertEqual(len(rows), len(expected_dates))
        self.assertEqual({r["trade_date"] for r in rows}, set(expected_dates))
        for row in rows:
            self.assertEqual(row["recommendation"], "BUY")
            self.assertEqual(row["label_status"], "pending")  # 尚未回填
            self.assertEqual(row["horizon"], 5)

        # bars 已落库（--write-signals 的副作用，使台账可立即回填）
        self.assertEqual(len(load_daily_bars(SYMBOL)), 200)

    def test_write_signals_links_backtest_id(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        rows = list_signals(source="backtest", symbol=SYMBOL)
        # 默认落库 → 每行带 backtest_id（指向 backtest_results）
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(row["backtest_id"])

    def test_write_signals_with_no_save_has_null_backtest_id(self):
        result = self.runner.invoke(
            cli,
            ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals", "--no-save"],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        rows = list_signals(source="backtest", symbol=SYMBOL)
        for row in rows:
            self.assertIsNone(row["backtest_id"])  # 未落 backtest_results

    def test_no_write_signals_flag_leaves_ledger_empty(self):
        self.runner.invoke(cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi"])
        self.assertEqual(list_signals(), [])


class CliSignalsBackfillTest(_CliDbTestCase):
    def test_backfill_after_write_signals(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        result = self.runner.invoke(cli, ["signals", "backfill"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("台账回填", result.output)
        # 早期信号（未来 bar 充足）应被回填
        filled = list_signals(source="backtest", status="filled")
        self.assertGreater(len(filled), 0)
        for row in filled:
            self.assertIsNotNone(row["fwd_net_return"])
            self.assertIsNotNone(row["exit_price"])

    def test_backfill_empty_ledger(self):
        result = self.runner.invoke(cli, ["signals", "backfill"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("无 pending 信号", result.output)

    def test_backfill_symbol_filter(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        result = self.runner.invoke(
            cli, ["signals", "backfill", "--symbol", "999999.SH"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        # 过滤到不存在的标的 → 不回填任何 600000.SH 行
        self.assertEqual(len(list_signals(source="backtest", status="filled")), 0)


class CliSignalsStatsTest(_CliDbTestCase):
    def test_stats_empty(self):
        result = self.runner.invoke(cli, ["signals", "stats"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("台账为空", result.output)

    def test_stats_after_write_and_backfill(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        self.runner.invoke(cli, ["signals", "backfill"])
        result = self.runner.invoke(cli, ["signals", "stats"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("覆盖率", result.output)
        self.assertIn("信号台账统计", result.output)
        # 与 API 口径一致
        stats = signal_stats()
        self.assertGreater(stats["total_rows"], 0)

    def test_stats_source_filter(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        result = self.runner.invoke(cli, ["signals", "stats", "--source", "monitor"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("台账为空", result.output)  # 只写入了 backtest 源


class CliSignalsListTest(_CliDbTestCase):
    def test_list_empty(self):
        result = self.runner.invoke(cli, ["signals", "list"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("无匹配的信号记录", result.output)

    def test_list_shows_rows(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        result = self.runner.invoke(cli, ["signals", "list"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(SYMBOL, result.output)
        self.assertIn("BUY", result.output)

    def test_list_status_filter(self):
        self.runner.invoke(
            cli, ["backtest", SYMBOL, "-d", "200", "-s", "rsi", "--write-signals"]
        )
        self.runner.invoke(cli, ["signals", "backfill"])
        pending = self.runner.invoke(cli, ["signals", "list", "--status", "pending"])
        self.assertEqual(pending.exit_code, 0, pending.output)
        # 回填后部分仍 pending（尾部信号未来 bar 不足）
        filled_rows = list_signals(source="backtest", status="filled")
        if len(filled_rows) == len(list_signals(source="backtest")):
            self.assertIn("无匹配的信号记录", pending.output)


if __name__ == "__main__":
    unittest.main()
