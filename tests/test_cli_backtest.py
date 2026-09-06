"""A8 CLI 回测/walk-forward 落库 + A9 data audit 巡检的离线测试。

CliRunner + tmp config（含 token）+ tmp DB（TRADEPILOT_BACKTEST_DB）+ mock loader，
全程零网络。断言落库行 user_id 为 NULL、run_kind/params_json/profile_source/result_json
（walk-forward 为 report_json）正确，且 CLI 列表/巡检命令可读回。
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import synth
from click.testing import CliRunner

from ripple_tradePilot.cli import cli
from ripple_tradePilot.storage.database import (
    init_database,
    load_market_daily,
    upsert_daily_bars,
    upsert_stock_quotes,
)
from ripple_tradePilot.storage.user_store import (
    get_backtest_run,
    list_backtest_runs,
)


class FakeLoader:
    """替换 TushareDataLoader：load_bars 返回合成日线。

    C1：CLI ``--benchmark`` 改走 ``market_service.load_index_bars``（DB 优先），
    不再经 loader.get_index_bars，故此处不再 mock 指数（基准测试单独 patch
    ``market_service.load_index_bars``）。
    """

    bars = synth.daily_bars(200, seed=21)

    def __init__(self, token, rate_limit_delay=1.5):
        self.token = token

    def load_bars(self, symbol, start_date=None, end_date=None):
        return list(type(self).bars)


class _CliDbTestCase(unittest.TestCase):
    """共享 tmp config + tmp DB + 环境变量隔离。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = root / "config.yaml"
        self.config.write_text("tushare:\n  token: TESTTOKEN\n", encoding="utf-8")
        self.db = root / "backtest.db"
        init_database(self.db)
        env = patch.dict(
            os.environ,
            {
                "TRADEPILOT_CONFIG": str(self.config),
                "TRADEPILOT_BACKTEST_DB": str(self.db),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.runner = CliRunner()
        self._loader_patch = patch(
            "ripple_tradePilot.data.tushare_loader.TushareDataLoader", FakeLoader
        )
        self._loader_patch.start()
        self.addCleanup(self._loader_patch.stop)


class CliBacktestSaveTest(_CliDbTestCase):
    def test_backtest_saves_by_default(self):
        result = self.runner.invoke(
            cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("已落库", result.output)

        runs = list_backtest_runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["run_kind"], "backtest")
        self.assertEqual(run["symbol"], "600000.SH")
        self.assertEqual(run["strategy_key"], "rsi")
        self.assertEqual(run["execution"], "next_open")
        self.assertEqual(run["bar_count"], 200)

        full = get_backtest_run(run["id"])
        # user_id 为 NULL → 不进任何用户列表
        self.assertIsNone(full["user_id"])
        # A5：-s rsi 无显式参数 → 走缺省解析链，单策略落 profile_source="default"
        # （改造前硬编码 "cli"；现由 resolve_backtest_strategy 统一裁决来源）
        self.assertEqual(full["profile_source"], "default")
        params = json.loads(full["params_json"])
        self.assertEqual(params["strategy"], "rsi")
        self.assertEqual(params["execution"], "next_open")
        self.assertEqual(params["params"], {})  # 无 --param → 空显式参数
        self.assertIsNone(params["profile"])
        # result_json 与 Web 端同构、可反序列化、可回放
        data = json.loads(full["result_json"])
        self.assertEqual(data["symbol"], "600000.SH")
        self.assertEqual(data["strategy"], "rsi")
        for key in ("metrics", "trades", "equity_curve", "fills", "disclaimer"):
            self.assertIn(key, data)
        self.assertEqual(len(data["equity_curve"]), 200)

    def test_backtest_no_save_flag_skips_persistence(self):
        result = self.runner.invoke(
            cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi", "--no-save"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("已落库", result.output)
        self.assertEqual(list_backtest_runs(), [])

    def test_backtest_benchmark_uses_load_index_bars(self):
        # C1：--benchmark 走 DB 优先的 market_service.load_index_bars（离线 mock），
        # 取到指数则打印基准对比行；零网络。
        index_rows = [
            {"trade_date": f"202601{i:02d}", "close": 3000.0 + i * 10}
            for i in range(1, 9)
        ]
        with patch(
            "ripple_tradePilot.data.market_service.load_index_bars",
            return_value=index_rows,
        ) as mock_load:
            result = self.runner.invoke(
                cli,
                ["backtest", "600000.SH", "-d", "200", "-s", "rsi", "--benchmark"],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("沪深300基准", result.output)
        self.assertIn("超额收益", result.output)
        # 确认拉的是沪深300基准
        self.assertEqual(mock_load.call_args[0][0], "000300.SH")

    def test_backtest_benchmark_degrades_on_empty_index(self):
        # load_index_bars 返回空（离线/无数据）→ 跳过基准对比，回测主体不报错
        with patch(
            "ripple_tradePilot.data.market_service.load_index_bars", return_value=[]
        ):
            result = self.runner.invoke(
                cli,
                ["backtest", "600000.SH", "-d", "200", "-s", "rsi", "--benchmark"],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("未取到沪深300基准数据", result.output)

    def test_backtests_list_shows_records(self):
        self.runner.invoke(cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi"])
        result = self.runner.invoke(cli, ["backtests", "list"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("600000.SH", result.output)
        self.assertIn("backtest", result.output)

    def test_backtests_list_filters_by_kind(self):
        self.runner.invoke(cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi"])
        result = self.runner.invoke(
            cli, ["backtests", "list", "--kind", "walkforward"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("暂无", result.output)

    def test_user_backtests_exclude_null_user_rows(self):
        # CLI 落库的 NULL-user 行不应出现在按 user_id 过滤的用户列表里
        from ripple_tradePilot.storage.user_store import list_user_backtests

        self.runner.invoke(cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi"])
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                ("u1", "hash"),
            )
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'u1'"
            ).fetchone()[0]
        self.assertEqual(list_user_backtests(user_id), [])
        self.assertEqual(len(list_backtest_runs()), 1)


class CliBacktestA5Test(_CliDbTestCase):
    """A5：CLI backtest 的 --param/--profile/--vote-threshold + combo_vote + provenance。"""

    def _run(self, *args):
        result = self.runner.invoke(cli, ["backtest", "600000.SH", "-d", "200", *args])
        self.assertEqual(result.exit_code, 0, result.output)
        run = list_backtest_runs()[0]
        return result, get_backtest_run(run["id"])

    def test_explicit_param_provenance(self):
        # -p period=7 → profile_source="explicit"，参数落 params_json 与 result_json
        result, full = self._run("-s", "rsi", "-p", "period=7")
        self.assertEqual(full["profile_source"], "explicit")
        self.assertEqual(full["strategy_key"], "rsi")
        params = json.loads(full["params_json"])
        self.assertEqual(params["params"], {"period": 7})
        data = json.loads(full["result_json"])
        self.assertEqual(data["strategy_params"], {"period": 7})
        self.assertEqual(data["profile_source"], "explicit")
        self.assertIn("画像来源：explicit", result.output)

    def test_explicit_param_changes_fills(self):
        # 极端 RSI 阈值（几乎不触发）vs 宽松阈值 → 交易回合数可区分，证明参数确实生效
        _, tight = self._run("-s", "rsi", "-p", "period=14", "-p", "oversold=1", "-p", "overbought=99")
        runs = list_backtest_runs()
        self.runner.invoke(cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi",
                                 "-p", "period=14", "-p", "oversold=45", "-p", "overbought=55"])
        loose = get_backtest_run(list_backtest_runs()[0]["id"])
        tight_trades = json.loads(tight["result_json"])["trades"]["num_trades"]
        loose_trades = json.loads(loose["result_json"])["trades"]["num_trades"]
        self.assertGreater(loose_trades, tight_trades)
        self.assertGreaterEqual(len(runs), 1)

    def test_combo_vote_default_profile(self):
        # -s combo_vote 无参 → 缺省链画像，profile_source="default"，参数摘要含阈值+组件
        result, full = self._run("-s", "combo_vote")
        self.assertEqual(full["strategy_key"], "combo_vote")
        self.assertEqual(full["profile_source"], "default")
        data = json.loads(full["result_json"])
        self.assertEqual(data["strategy"], "combo_vote")
        sp = data["strategy_params"]
        self.assertIn("vote_threshold", sp)
        self.assertIn("components", sp)
        self.assertIn("画像来源：default", result.output)

    def test_combo_vote_threshold_flag_is_explicit(self):
        # --vote-threshold 3 → 显式参数，profile_source="explicit"，阈值=3
        _, full = self._run("-s", "combo_vote", "--vote-threshold", "3")
        self.assertEqual(full["profile_source"], "explicit")
        sp = json.loads(full["result_json"])["strategy_params"]
        self.assertEqual(sp["vote_threshold"], 3)

    def test_illegal_param_exits_2(self):
        result = self.runner.invoke(
            cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi", "-p", "bogus=1"]
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("策略解析失败", result.output)
        self.assertEqual(list_backtest_runs(), [])

    def test_param_without_equals_is_usage_error(self):
        result = self.runner.invoke(
            cli, ["backtest", "600000.SH", "-d", "200", "-s", "rsi", "-p", "novalue"]
        )
        self.assertEqual(result.exit_code, 2)  # click.BadParameter → 用法错误
        self.assertEqual(list_backtest_runs(), [])

    def test_vote_threshold_out_of_range_is_usage_error(self):
        result = self.runner.invoke(
            cli, ["backtest", "600000.SH", "-d", "200", "-s", "combo_vote", "--vote-threshold", "9"]
        )
        self.assertEqual(result.exit_code, 2)  # IntRange(1,3) 拦截


class CliWalkforwardA5Test(_CliDbTestCase):
    """A5：CLI walkforward 的 combo_vote + --grid-json + 网格白名单校验。"""

    def test_walkforward_combo_vote_saves(self):
        result = self.runner.invoke(
            cli, ["walkforward", "600000.SH", "-d", "200", "-s", "combo_vote", "--splits", "3"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("已落库", result.output)
        runs = list_backtest_runs(kind="walkforward")
        self.assertEqual(len(runs), 1)
        full = get_backtest_run(runs[0]["id"])
        self.assertEqual(full["strategy_key"], "combo_vote")
        # combo_vote 走缺省画像链 → profile_source="default"（非 "cli"）
        self.assertEqual(full["profile_source"], "default")
        params = json.loads(full["params_json"])
        self.assertEqual(params["grid"], {"vote_threshold": [1, 2, 3]})
        self.assertIn("基准画像来源：default", result.output)

    def test_walkforward_grid_json_override(self):
        result = self.runner.invoke(
            cli, ["walkforward", "600000.SH", "-d", "200", "-s", "ma", "--splits", "3",
                  "--grid-json", '{"fast": [5, 10], "slow": [20]}']
        )
        self.assertEqual(result.exit_code, 0, result.output)
        full = get_backtest_run(list_backtest_runs(kind="walkforward")[0]["id"])
        params = json.loads(full["params_json"])
        self.assertEqual(params["grid"], {"fast": [5, 10], "slow": [20]})

    def test_walkforward_illegal_grid_exits_2(self):
        result = self.runner.invoke(
            cli, ["walkforward", "600000.SH", "-d", "200", "-s", "ma",
                  "--grid-json", '{"bogus": [1]}']
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("不支持的参数", result.output)
        self.assertEqual(list_backtest_runs(kind="walkforward"), [])

    def test_walkforward_bad_grid_json_exits_2(self):
        result = self.runner.invoke(
            cli, ["walkforward", "600000.SH", "-d", "200", "-s", "ma", "--grid-json", "not json"]
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--grid-json", result.output)

    def test_walkforward_single_strategy_profile_source_cli(self):
        # 单策略 walk-forward 不解析画像 → profile_source 仍为 "cli"（网格即参数，无基准画像）
        self.runner.invoke(
            cli, ["walkforward", "600000.SH", "-d", "200", "-s", "ma", "--splits", "3"]
        )
        full = get_backtest_run(list_backtest_runs(kind="walkforward")[0]["id"])
        self.assertEqual(full["profile_source"], "cli")


class CliWalkforwardSaveTest(_CliDbTestCase):
    def test_walkforward_saves_report_json(self):
        result = self.runner.invoke(
            cli,
            ["walkforward", "600000.SH", "-d", "200", "-s", "ma", "--splits", "3"],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("已落库", result.output)

        runs = list_backtest_runs(kind="walkforward")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_kind"], "walkforward")
        self.assertEqual(runs[0]["strategy_key"], "ma")

        full = get_backtest_run(runs[0]["id"])
        self.assertIsNone(full["result_json"])  # walk-forward 不存单一权益曲线
        report = json.loads(full["report_json"])
        self.assertEqual(len(report["splits"]), 3)
        for key in (
            "oos_total_return",
            "avg_is_return",
            "avg_oos_return",
            "avg_oos_sharpe",
            "overfit_gap",
        ):
            self.assertIn(key, report)
        # splits 结构自洽：序号连续、train_end==test_start、预热前缀落在训练窗内
        for index, split in enumerate(report["splits"]):
            self.assertEqual(split["split_index"], index)
            self.assertEqual(split["train_end"], split["test_start"])
            self.assertGreaterEqual(split["warmup_start"], split["train_start"])
            self.assertLessEqual(split["warmup_start"], split["test_start"])
            for metric_key in ("is_metrics", "oos_metrics"):
                self.assertTrue(
                    {"total_return", "annual_return", "max_drawdown", "sharpe"}.issubset(
                        split[metric_key]
                    )
                )
            self.assertIsInstance(split["best_params"], dict)
        params = json.loads(full["params_json"])
        self.assertEqual(params["splits"], 3)
        self.assertEqual(params["strategy"], "ma")

    def test_walkforward_no_save_flag(self):
        result = self.runner.invoke(
            cli,
            ["walkforward", "600000.SH", "-d", "200", "-s", "ma", "--splits", "3", "--no-save"],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(list_backtest_runs(kind="walkforward"), [])


class CliDataAuditTest(_CliDbTestCase):
    def test_audit_clean_series(self):
        rows = synth.daily_rows(synth.daily_bars(40, seed=31))
        upsert_daily_bars("600000.SH", rows, "synth", self.db)
        result = self.runner.invoke(cli, ["data", "audit", "--adjust"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("未发现复权混接点", result.output)

    def test_audit_flags_splice(self):
        rows = synth.daily_rows(synth.daily_bars(40, seed=31))
        spliced = synth.scale_rows(rows, 0.9, start=25)
        upsert_daily_bars("600000.SH", spliced, "synth", self.db)
        result = self.runner.invoke(cli, ["data", "audit", "--adjust"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("600000.SH", result.output)
        self.assertIn("异常", result.output)

    def test_audit_requires_adjust_flag(self):
        result = self.runner.invoke(cli, ["data", "audit"])
        self.assertNotEqual(result.exit_code, 0)

    def test_audit_single_symbol(self):
        rows = synth.daily_rows(synth.daily_bars(40, seed=31))
        upsert_daily_bars("600000.SH", rows, "synth", self.db)
        upsert_daily_bars("000001.SZ", synth.scale_rows(rows, 0.9, start=25), "synth", self.db)
        result = self.runner.invoke(
            cli, ["data", "audit", "--adjust", "--symbol", "600000.SH"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        # 只巡检 600000.SH（干净），不应报告 000001.SZ
        self.assertIn("未发现复权混接点", result.output)
        self.assertNotIn("000001.SZ", result.output)


class CliDataRefreshTest(_CliDbTestCase):
    """C4：``tradepilot data refresh --market/--industry``（mock 网络，离线）。"""

    def _seed_quotes(self):
        # 涨/跌/涨停混合，供 aggregate_breadth 聚合
        upsert_stock_quotes(
            [
                {"symbol": "600000.SH", "price": 10.0, "change_pct": 9.85, "amount": 1e9},
                {"symbol": "000001.SZ", "price": 12.0, "change_pct": -1.0, "amount": 2e9},
                {"symbol": "300750.SZ", "price": 200.0, "change_pct": 19.9, "amount": 3e9},
            ],
            "synth",
            self.db,
        )

    def test_no_flags_exits_2(self):
        result = self.runner.invoke(cli, ["data", "refresh"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("请至少指定", result.output)

    def test_market_writes_breadth_and_indexes(self):
        self._seed_quotes()
        index_report = {"refreshed": [{"index_code": "000300.SH", "source": "tushare", "rows": 5}],
                        "failed": []}
        with patch("ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
                   return_value=index_report) as mock_idx, \
             patch("ripple_tradePilot.data.stock_service.StockDataService.refresh_quotes",
                   return_value={"count": 0}):
            result = self.runner.invoke(cli, ["data", "refresh", "--market"])
        self.assertEqual(result.exit_code, 0, result.output)
        mock_idx.assert_called_once()
        self.assertIn("指数日线：成功 1 个", result.output)
        self.assertIn("市场宽度", result.output)
        # 宽度已落库（涨跌停分板块：600000 主板涨停 + 300750 创业板涨停 = 2）
        rows = load_market_daily(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["limit_up"], 2)
        self.assertEqual(rows[0]["advancers"], 2)
        self.assertEqual(rows[0]["decliners"], 1)

    def test_market_index_failure_does_not_block_breadth(self):
        # 独立 try/except：指数刷新异常不应阻断宽度落库
        self._seed_quotes()
        with patch("ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
                   side_effect=RuntimeError("index down")), \
             patch("ripple_tradePilot.data.stock_service.StockDataService.refresh_quotes",
                   return_value={"count": 0}):
            result = self.runner.invoke(cli, ["data", "refresh", "--market"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("指数日线刷新异常", result.output)
        self.assertEqual(len(load_market_daily(self.db)), 1)  # 宽度仍写入

    def test_industry_config_pool_calls_refresh_for_symbols(self):
        report = {"boards_total": 1, "boards_refreshed": ["BK0475"], "boards_failed": [],
                  "symbols_total": 1, "symbols_resolved": 1, "symbols_unresolved": [],
                  "membership_rows": 3, "bar_rows": 5}
        with patch("ripple_tradePilot.cli.load_config",
                   return_value={"symbols": [{"code": "600000.SH", "name": "浦发"}]}), \
             patch("ripple_tradePilot.data.industry_service.IndustryDataService.refresh_for_symbols",
                   return_value=report) as mock_refresh:
            result = self.runner.invoke(cli, ["data", "refresh", "--industry", "--pool", "config"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(mock_refresh.call_args[0][0], ["600000.SH"])  # 解析出的股票池
        self.assertIn("板块：目标 1 个", result.output)
        self.assertIn("成分股 3 行", result.output)

    def test_industry_watchlist_pool(self):
        report = {"boards_total": 1, "boards_refreshed": ["BK0475"], "boards_failed": [],
                  "symbols_total": 1, "symbols_resolved": 1, "symbols_unresolved": [],
                  "membership_rows": 2, "bar_rows": 4}
        with patch("ripple_tradePilot.storage.user_store.list_all_watched_symbols",
                   return_value=[{"symbol": "000001.SZ", "name": "平安"}]), \
             patch("ripple_tradePilot.data.industry_service.IndustryDataService.refresh_for_symbols",
                   return_value=report) as mock_refresh:
            result = self.runner.invoke(cli, ["data", "refresh", "--industry", "--pool", "watchlist"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(mock_refresh.call_args[0][0], ["000001.SZ"])

    def test_industry_empty_pool_skips(self):
        # 默认 tmp config 无 symbols → config 池为空 → 跳过，不调用 refresh_for_symbols
        with patch("ripple_tradePilot.data.industry_service.IndustryDataService.refresh_for_symbols") as mock_refresh:
            result = self.runner.invoke(cli, ["data", "refresh", "--industry", "--pool", "config"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("无标的，跳过行业刷新", result.output)
        mock_refresh.assert_not_called()

    def test_industry_reports_unresolved_symbols(self):
        report = {"boards_total": 1, "boards_refreshed": ["BK0475"], "boards_failed": [],
                  "symbols_total": 2, "symbols_resolved": 1, "symbols_unresolved": ["999999.SH"],
                  "membership_rows": 1, "bar_rows": 2}
        with patch("ripple_tradePilot.cli.load_config",
                   return_value={"symbols": [{"code": "600000.SH"}, {"code": "999999.SH"}]}), \
             patch("ripple_tradePilot.data.industry_service.IndustryDataService.refresh_for_symbols",
                   return_value=report):
            result = self.runner.invoke(cli, ["data", "refresh", "--industry"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("无法映射到板块", result.output)
        self.assertIn("999999.SH", result.output)


if __name__ == "__main__":
    unittest.main()
