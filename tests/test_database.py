import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ripple_tradePilot.storage import database_path, init_database
from ripple_tradePilot.storage.__main__ import main as initialize_storage
from ripple_tradePilot.storage.database import (
    DATABASE_SCHEMA_VERSION,
    ML_MODELS_COLUMNS,
    industry_board_for_symbol,
    list_datasets,
    list_models,
    list_stock_catalog,
    load_daily_bars,
    load_dataset_manifest,
    load_index_bars,
    load_industry_board_bars,
    load_industry_boards,
    load_industry_membership,
    load_market_daily,
    load_model,
    load_promoted_model,
    record_market_daily,
    register_dataset,
    register_model,
    retire_promoted_models,
    set_model_status,
    stock_catalog_industries,
    upsert_daily_bars,
    upsert_index_daily,
    upsert_industry_board_bars,
    upsert_industry_boards,
    upsert_industry_membership,
    upsert_stock_catalog,
    upsert_stock_quotes,
)


class DatabaseInitializationTest(unittest.TestCase):
    def test_creates_database_table_and_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "db" / "tradepilot.db"

            initialized = init_database(target)

            self.assertEqual(initialized, target)
            with sqlite3.connect(target) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(backtest_results)")
                }
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(backtest_results)")
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertIn("annual_return", columns)
            self.assertIn("created_at", columns)
            self.assertIn("user_id", columns)
            self.assertIn("strategy_id", columns)
            self.assertIn("idx_backtest_results_symbol_created", indexes)
            self.assertTrue(
                {
                    "users",
                    "user_sessions",
                    "strategies",
                    "user_watchlist",
                    "stock_catalog",
                    "daily_bars",
                    "stock_quotes",
                }.issubset(tables)
            )
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_storage_startup_command_migrates_market_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-market.db"
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "CREATE TABLE stock_catalog ("
                    "symbol TEXT PRIMARY KEY, name TEXT, market TEXT)"
                )
                connection.execute(
                    "CREATE TABLE daily_bars (id INTEGER PRIMARY KEY)"
                )

            output = StringIO()
            with (
                patch.dict(
                    os.environ, {"TRADEPILOT_BACKTEST_DB": str(target)}
                ),
                redirect_stdout(output),
            ):
                initialize_storage()

            with sqlite3.connect(target) as connection:
                catalog_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(stock_catalog)")
                }
                daily_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(daily_bars)")
                }
                quote_table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'stock_quotes'"
                ).fetchone()
                watchlist_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_watchlist)")
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertTrue(
                {"exchange", "board", "industry", "area"}.issubset(
                    catalog_columns
                )
            )
            self.assertTrue(
                {"pre_close", "change", "pct_chg"}.issubset(daily_columns)
            )
            self.assertIsNotNone(quote_table)
            self.assertIn("default_strategy_id", watchlist_columns)
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)
            self.assertIn(
                f"SQLite schema v{DATABASE_SCHEMA_VERSION} ready", output.getvalue()
            )

    def test_adds_columns_missing_from_legacy_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy.db"
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "CREATE TABLE backtest_results (id INTEGER PRIMARY KEY, symbol TEXT)"
                )

            init_database(target)

            with sqlite3.connect(target) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(backtest_results)")
                }
            self.assertTrue({"name", "total_return", "created_at"}.issubset(columns))

    def test_environment_path_uses_configured_database(self):
        configured = "/tmp/tradepilot-test.db"
        with patch.dict(os.environ, {"TRADEPILOT_BACKTEST_DB": configured}):
            self.assertEqual(database_path(), Path(configured))

    def test_repairs_missing_columns_in_existing_user_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-users.db"
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE backtest_results (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO users (id) VALUES (7)")
                connection.execute("CREATE TABLE user_sessions (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE strategies (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE user_watchlist (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE daily_bars (id INTEGER PRIMARY KEY)")

            init_database(target)

            with sqlite3.connect(target) as connection:
                user_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(users)")
                }
                session_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_sessions)")
                }
                strategy_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(strategies)")
                }
                migrated_role = connection.execute(
                    "SELECT role FROM users WHERE id = 7"
                ).fetchone()[0]
                watchlist_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_watchlist)")
                }
                daily_bar_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(daily_bars)")
                }
            self.assertTrue({"username", "password_hash", "role", "created_at"}.issubset(user_columns))
            self.assertTrue({"user_id", "token_hash", "expires_at"}.issubset(session_columns))
            self.assertTrue(
                {"user_id", "visibility", "parameters_json", "system_key"}.issubset(
                    strategy_columns
                )
            )
            self.assertEqual(migrated_role, "admin")
            self.assertTrue(
                {
                    "user_id",
                    "symbol",
                    "is_watched",
                    "last_updated_at",
                    "default_strategy_id",
                }.issubset(
                    watchlist_columns
                )
            )
            self.assertTrue(
                {
                    "symbol",
                    "trade_date",
                    "close",
                    "pre_close",
                    "change",
                    "pct_chg",
                    "source",
                }.issubset(daily_bar_columns)
            )

    def test_daily_bars_are_upserted_by_symbol_and_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            first = {
                "trade_date": "20260831",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "pre_close": 10.2,
                "change": 0.3,
                "pct_chg": 2.9412,
                "vol": 100,
            }
            upsert_daily_bars("600000.SH", [first], "test", target)
            upsert_daily_bars(
                "600000.SH", [{**first, "close": 10.8, "vol": 120}], "test", target
            )

            rows = load_daily_bars("600000.SH", target)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close"], 10.8)
            self.assertEqual(rows[0]["vol"], 120)
            self.assertEqual(rows[0]["pre_close"], 10.2)
            self.assertEqual(rows[0]["change"], 0.3)
            self.assertEqual(rows[0]["pct_chg"], 2.9412)

    def test_stock_catalog_upsert_updates_watchlist_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    ("catalog-user", "hash"),
                )
                user_id = connection.execute(
                    "SELECT id FROM users WHERE username = ?", ("catalog-user",)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO user_watchlist (user_id, symbol, name) VALUES (?, ?, ?)",
                    (user_id, "600418.SH", "江淮汽车"),
                )

            upsert_stock_catalog(
                [
                    {
                        "symbol": "600418.SH",
                        "name": "ST江淮",
                        "market": "主板",
                        "exchange": "SSE",
                        "board": "主板",
                        "industry": "汽车整车",
                        "area": "安徽",
                        "list_status": "L",
                        "list_date": "20010930",
                    }
                ],
                "tushare",
                target,
            )

            with sqlite3.connect(target) as connection:
                name = connection.execute(
                    "SELECT name FROM user_watchlist WHERE symbol = ?",
                    ("600418.SH",),
                ).fetchone()[0]
            catalog = list_stock_catalog(target)

            self.assertEqual(name, "ST江淮")
            self.assertEqual(catalog[0]["market"], "主板")
            self.assertEqual(catalog[0]["exchange"], "SSE")
            self.assertEqual(catalog[0]["board"], "主板")
            self.assertEqual(catalog[0]["industry"], "汽车整车")
            self.assertEqual(catalog[0]["area"], "安徽")

    def test_realtime_quote_takes_priority_over_official_daily_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600000.SH", "name": "浦发银行"}], "test", target
            )
            upsert_daily_bars(
                "600000.SH",
                [
                    {
                        "trade_date": "20260901",
                        "open": 10,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.2,
                        "pre_close": 10,
                        "change": 0.2,
                        "pct_chg": 2,
                        "vol": 100,
                    }
                ],
                "tushare",
                target,
            )
            upsert_stock_quotes(
                [
                    {
                        "symbol": "600000.SH",
                        "price": 10.6,
                        "pre_close": 10.2,
                        "change": 0.4,
                        "change_pct": 3.9216,
                        "quote_time": "2026-09-02T10:30:00",
                    }
                ],
                "akshare",
                target,
            )

            item = list_stock_catalog(target)[0]

            self.assertEqual(item["price"], 10.6)
            self.assertEqual(item["change"], 0.4)
            self.assertEqual(item["change_pct"], 3.9216)
            self.assertEqual(item["price_kind"], "realtime")
            self.assertEqual(item["price_source"], "akshare")
            self.assertEqual(item["price_time"], "2026-09-02T10:30:00")

    def test_daily_catalog_uses_official_change_fields_on_adjustment_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600000.SH", "name": "浦发银行"}], "test", target
            )
            upsert_daily_bars(
                "600000.SH",
                [
                    {
                        "trade_date": "20260831",
                        "open": 20,
                        "high": 20,
                        "low": 20,
                        "close": 20,
                        "vol": 100,
                    },
                    {
                        "trade_date": "20260901",
                        "open": 9.5,
                        "high": 10.2,
                        "low": 9.4,
                        "close": 10,
                        "pre_close": 9.5,
                        "change": 0.5,
                        "pct_chg": 5.2632,
                        "vol": 120,
                    },
                ],
                "tushare",
                target,
            )

            item = list_stock_catalog(target)[0]

            self.assertEqual(item["price"], 10)
            self.assertEqual(item["change"], 0.5)
            self.assertEqual(item["change_pct"], 5.2632)
            self.assertEqual(item["price_kind"], "daily")


class SchemaV12MigrationTest(unittest.TestCase):
    """v12（A8 落库溯源 + A9 复权溯源）迁移与幂等。"""

    # v11 时代 backtest_results / daily_bars 的列集（不含 v12 新列），
    # 用于构造"旧库"再断言只补齐 v12 增量列。
    V11_BACKTEST_COLS = (
        "id INTEGER PRIMARY KEY, symbol TEXT, name TEXT, start_date TEXT, "
        "end_date TEXT, initial_capital REAL, final_capital REAL, "
        "total_return REAL, annual_return REAL, max_drawdown REAL, "
        "sharpe_ratio REAL, total_trades INTEGER, win_rate REAL, "
        "created_at TIMESTAMP, user_id INTEGER, strategy_id INTEGER, "
        "strategy_key TEXT, bar_count INTEGER, execution TEXT, result_json TEXT"
    )
    V11_DAILY_COLS = (
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, open REAL, "
        "high REAL, low REAL, close REAL, pre_close REAL, change REAL, "
        "pct_chg REAL, volume REAL, amount REAL, source TEXT, updated_at TIMESTAMP"
    )

    def _columns(self, target, table):
        with sqlite3.connect(target) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_fresh_db_has_v12_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fresh.db"
            init_database(target)
            self.assertTrue(
                {"run_kind", "params_json", "profile_source", "report_json"}.issubset(
                    self._columns(target, "backtest_results")
                )
            )
            self.assertTrue(
                {"adjust", "adj_anchor_date", "data_version"}.issubset(
                    self._columns(target, "daily_bars")
                )
            )

    def test_legacy_db_upgrades_to_v12(self):
        # 本地库实际停在 v10，CI/新库为 v11；_ensure_columns 与起始版本无关，
        # 两者都应补齐 v12 增量列并把 user_version 推到 12。
        for legacy_version in (10, 11):
            with self.subTest(legacy_version=legacy_version):
                with tempfile.TemporaryDirectory() as temp_dir:
                    target = Path(temp_dir) / f"legacy-v{legacy_version}.db"
                    with sqlite3.connect(target) as connection:
                        connection.execute(
                            f"CREATE TABLE backtest_results ({self.V11_BACKTEST_COLS})"
                        )
                        connection.execute(
                            f"CREATE TABLE daily_bars ({self.V11_DAILY_COLS})"
                        )
                        connection.execute(f"PRAGMA user_version={legacy_version}")

                    before_bt = self._columns(target, "backtest_results")
                    before_daily = self._columns(target, "daily_bars")
                    self.assertNotIn("run_kind", before_bt)
                    self.assertNotIn("adjust", before_daily)

                    init_database(target)

                    after_bt = self._columns(target, "backtest_results")
                    after_daily = self._columns(target, "daily_bars")
                    # 既有列保留，v12 增量列补齐
                    self.assertTrue(before_bt.issubset(after_bt))
                    self.assertTrue(before_daily.issubset(after_daily))
                    self.assertTrue(
                        {
                            "run_kind",
                            "params_json",
                            "profile_source",
                            "report_json",
                        }.issubset(after_bt)
                    )
                    self.assertTrue(
                        {"adjust", "adj_anchor_date", "data_version"}.issubset(
                            after_daily
                        )
                    )
                    with sqlite3.connect(target) as connection:
                        version = connection.execute("PRAGMA user_version").fetchone()[0]
                    self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_init_database_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "idem.db"
            init_database(target)
            init_database(target)  # 第二次不得抛错或重复加列
            self.assertTrue(
                {"run_kind", "report_json"}.issubset(
                    self._columns(target, "backtest_results")
                )
            )
            self.assertTrue(
                {"adjust", "data_version"}.issubset(self._columns(target, "daily_bars"))
            )

    def test_daily_bars_persist_adjustment_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "adjust.db"
            row = {
                "trade_date": "20260831",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "pre_close": 10.2,
                "change": 0.3,
                "pct_chg": 2.9412,
                "vol": 100,
            }
            upsert_daily_bars(
                "600000.SH",
                [row],
                "tushare",
                target,
                adjust="qfq",
                adj_anchor_date="20260831",
                data_version="tushare|20260831|20260901T000000Z",
            )
            rows = load_daily_bars("600000.SH", target)
            self.assertEqual(rows[0]["adjust"], "qfq")
            self.assertEqual(rows[0]["adj_anchor_date"], "20260831")
            self.assertEqual(
                rows[0]["data_version"], "tushare|20260831|20260901T000000Z"
            )

            # 重新刷新（如检测到复权基准漂移后全量重拉）应覆盖溯源列
            upsert_daily_bars(
                "600000.SH",
                [{**row, "close": 9.45}],
                "tushare",
                target,
                adjust="qfq",
                adj_anchor_date="20260901",
                data_version="tushare|20260901|20260902T000000Z",
            )
            rows = load_daily_bars("600000.SH", target)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close"], 9.45)
            self.assertEqual(rows[0]["adj_anchor_date"], "20260901")
            self.assertEqual(
                rows[0]["data_version"], "tushare|20260901|20260902T000000Z"
            )

    def test_upsert_daily_bars_defaults_adjust_to_qfq(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "default.db"
            upsert_daily_bars(
                "600000.SH",
                [
                    {
                        "trade_date": "20260831",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "vol": 100,
                    }
                ],
                "test",
                target,
            )
            rows = load_daily_bars("600000.SH", target)
            self.assertEqual(rows[0]["adjust"], "qfq")
            self.assertEqual(rows[0]["adj_anchor_date"], "")
            self.assertEqual(rows[0]["data_version"], "")


class SchemaV13MigrationTest(unittest.TestCase):
    """v13（B2 信号台账 + kv_store）迁移与幂等。"""

    LEDGER_COLS = {
        "id", "symbol", "trade_date", "source", "provisional", "recommendation",
        "buy_count", "sell_count", "components_json", "model_id", "p_win",
        "expected_ret", "downside_mae", "entry_price", "exit_price", "horizon",
        "fwd_net_return", "fwd_ret_aux", "fwd_mae", "label_status",
        "data_version", "backtest_id", "filled_at", "created_at",
    }

    def _tables(self, target):
        with sqlite3.connect(target) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def _columns(self, target, table):
        with sqlite3.connect(target) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_fresh_db_has_ledger_and_kv_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fresh13.db"
            init_database(target)
            tables = self._tables(target)
            self.assertIn("signal_ledger", tables)
            self.assertIn("kv_store", tables)
            self.assertTrue(self.LEDGER_COLS.issubset(self._columns(target, "signal_ledger")))
            self.assertTrue(
                {"key", "value", "updated_at"}.issubset(self._columns(target, "kv_store"))
            )
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_legacy_v12_db_upgrades_to_v13(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-v12.db"
            # 构造一个只有 v12 表、无 signal_ledger/kv_store 的旧库
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute("DROP TABLE signal_ledger")
                connection.execute("DROP TABLE kv_store")
                connection.execute("PRAGMA user_version=12")
            self.assertNotIn("signal_ledger", self._tables(target))

            init_database(target)  # 重新初始化应补建 v13 表

            tables = self._tables(target)
            self.assertIn("signal_ledger", tables)
            self.assertIn("kv_store", tables)
            self.assertTrue(self.LEDGER_COLS.issubset(self._columns(target, "signal_ledger")))
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_init_database_idempotent_for_v13(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "idem13.db"
            init_database(target)
            init_database(target)  # 第二次不得抛错或重复建表
            self.assertIn("signal_ledger", self._tables(target))
            self.assertIn("kv_store", self._tables(target))

    def test_ledger_unique_constraint_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "uniq.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                # 表级 UNIQUE 建的是 sqlite_autoindex（sql 为 NULL），须经 index_list/info 查
                index_list = connection.execute(
                    "PRAGMA index_list(signal_ledger)"
                ).fetchall()
                unique_column_sets = []
                for entry in index_list:
                    name, is_unique, origin = entry[1], entry[2], entry[3]
                    if not is_unique:
                        continue
                    cols = {
                        row[2]
                        for row in connection.execute(f"PRAGMA index_info({name})")
                    }
                    unique_column_sets.append((origin, cols))
            # UNIQUE(symbol, trade_date, source, provisional) → origin='u' 的唯一索引
            self.assertTrue(
                any(
                    origin == "u"
                    and {"symbol", "trade_date", "source", "provisional"}.issubset(cols)
                    for origin, cols in unique_column_sets
                ),
                f"未见 (symbol, trade_date, source, provisional) 唯一约束：{unique_column_sets}",
            )


class SchemaV14MigrationTest(unittest.TestCase):
    """v14（C1 指数日线 + C2 市场宽度 + C3 行业三表）迁移、幂等与读写往返。"""

    INDEX_COLS = {
        "id", "index_code", "trade_date", "open", "high", "low", "close",
        "pct_chg", "amount", "volume", "source", "updated_at",
    }
    MARKET_COLS = {
        "trade_date", "advancers", "decliners", "unchanged", "limit_up",
        "limit_down", "total_amount", "up_ratio", "source", "updated_at",
    }
    BOARD_COLS = {"board_code", "board_name", "source", "updated_at"}
    BOARD_BAR_COLS = {
        "id", "board_code", "trade_date", "open", "high", "low", "close",
        "pct_chg", "amount", "turnover_rate", "source", "updated_at",
    }
    MEMBERSHIP_COLS = {"board_code", "symbol", "as_of", "source", "updated_at"}

    def _tables(self, target):
        with sqlite3.connect(target) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def _columns(self, target, table):
        with sqlite3.connect(target) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_fresh_db_has_index_and_market_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fresh14.db"
            init_database(target)
            tables = self._tables(target)
            self.assertIn("index_daily", tables)
            self.assertIn("market_daily", tables)
            self.assertTrue(self.INDEX_COLS.issubset(self._columns(target, "index_daily")))
            self.assertTrue(self.MARKET_COLS.issubset(self._columns(target, "market_daily")))
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_fresh_db_has_industry_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fresh14ind.db"
            init_database(target)
            tables = self._tables(target)
            for table in ("industry_boards", "industry_board_bars", "industry_membership"):
                self.assertIn(table, tables)
            self.assertTrue(self.BOARD_COLS.issubset(self._columns(target, "industry_boards")))
            self.assertTrue(
                self.BOARD_BAR_COLS.issubset(self._columns(target, "industry_board_bars"))
            )
            self.assertTrue(
                self.MEMBERSHIP_COLS.issubset(self._columns(target, "industry_membership"))
            )

    def test_legacy_v13_db_upgrades_to_v14(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-v13.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute("DROP TABLE index_daily")
                connection.execute("DROP TABLE market_daily")
                connection.execute("DROP TABLE industry_boards")
                connection.execute("DROP TABLE industry_board_bars")
                connection.execute("DROP TABLE industry_membership")
                connection.execute("PRAGMA user_version=13")
            self.assertNotIn("index_daily", self._tables(target))
            self.assertNotIn("industry_boards", self._tables(target))

            init_database(target)  # 重新初始化应补建 v14 表

            tables = self._tables(target)
            self.assertIn("index_daily", tables)
            self.assertIn("market_daily", tables)
            self.assertIn("industry_boards", tables)
            self.assertIn("industry_board_bars", tables)
            self.assertIn("industry_membership", tables)
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_init_database_idempotent_for_v14(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "idem14.db"
            init_database(target)
            init_database(target)  # 第二次不得抛错或重复建表
            tables = self._tables(target)
            self.assertIn("index_daily", tables)
            self.assertIn("market_daily", tables)
            self.assertIn("industry_membership", tables)

    def test_index_daily_unique_constraint_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "uniq14.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                index_list = connection.execute("PRAGMA index_list(index_daily)").fetchall()
                unique_sets = []
                for entry in index_list:
                    name, is_unique, origin = entry[1], entry[2], entry[3]
                    if not is_unique:
                        continue
                    cols = {
                        row[2]
                        for row in connection.execute(f"PRAGMA index_info({name})")
                    }
                    unique_sets.append((origin, cols))
            self.assertTrue(
                any(
                    origin == "u"
                    and {"index_code", "trade_date"}.issubset(cols)
                    for origin, cols in unique_sets
                ),
                f"未见 (index_code, trade_date) 唯一约束：{unique_sets}",
            )

    def test_index_daily_upsert_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "index.db"
            rows = [
                {"trade_date": "20260102", "open": 3000, "high": 3050, "low": 2990,
                 "close": 3040, "pct_chg": 1.33, "amount": 1.2e11, "vol": 9.5e9},
                {"trade_date": "20260103", "open": 3040, "high": 3060, "low": 3020,
                 "close": 3055, "pct_chg": 0.49, "amount": 1.1e11, "vol": 9.1e9},
            ]
            self.assertEqual(upsert_index_daily("000300.SH", rows, "tushare", target), 2)
            # 幂等 + 覆盖：重写 20260103 的 close
            upsert_index_daily(
                "000300.SH", [{**rows[1], "close": 3099, "vol": 9.9e9}], "akshare", target
            )
            loaded = load_index_bars("000300.SH", target)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["trade_date"], "20260102")  # 升序
            self.assertEqual(loaded[1]["close"], 3099)
            self.assertEqual(loaded[1]["vol"], 9.9e9)
            self.assertEqual(loaded[1]["source"], "akshare")
            # 不同指数互不干扰
            self.assertEqual(load_index_bars("000001.SH", target), [])

    def test_market_daily_record_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            breadth = {
                "advancers": 2800, "decliners": 1900, "unchanged": 300,
                "limit_up": 45, "limit_down": 12,
                "total_amount": 9.8e11, "up_ratio": 0.56,
            }
            record_market_daily("20260102", breadth, "snapshot", target)
            # 同日收盘终态覆盖盘中 provisional
            record_market_daily(
                "20260102", {**breadth, "advancers": 2950, "up_ratio": 0.59}, "mx", target
            )
            record_market_daily("20260103", breadth, "snapshot", target)

            rows = load_market_daily(target)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["trade_date"], "20260102")  # 升序
            self.assertEqual(rows[0]["advancers"], 2950)
            self.assertEqual(rows[0]["up_ratio"], 0.59)
            self.assertEqual(rows[0]["source"], "mx")

            # 日期闭区间过滤
            only_first = load_market_daily(target, start_date="20260102", end_date="20260102")
            self.assertEqual([r["trade_date"] for r in only_first], ["20260102"])

    def test_industry_boards_upsert_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "boards.db"
            records = [
                {"board_code": "BK0475", "board_name": "银行"},
                {"board_code": "BK1027", "board_name": "小金属"},
            ]
            self.assertEqual(upsert_industry_boards(records, "em", target), 2)
            # 幂等 + 覆盖名称
            upsert_industry_boards([{"board_code": "BK0475", "board_name": "银行Ⅱ"}], "em", target)
            boards = load_industry_boards(target)
            self.assertEqual(len(boards), 2)  # 不产生重复行
            self.assertEqual(boards[0]["board_code"], "BK0475")  # 升序
            self.assertEqual(boards[0]["board_name"], "银行Ⅱ")

    def test_industry_board_bars_unique_and_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "boardbars.db"
            rows = [
                {"trade_date": "20260102", "open": 1000, "high": 1020, "low": 990,
                 "close": 1015, "pct_chg": 1.5, "amount": 8e9, "turnover_rate": 1.1},
                {"trade_date": "20260103", "open": 1015, "high": 1030, "low": 1010,
                 "close": 1025, "pct_chg": 0.99, "amount": 7.5e9, "turnover_rate": 1.0},
            ]
            self.assertEqual(upsert_industry_board_bars("BK0475", rows, "em", target), 2)
            # 幂等 + 覆盖：重写 20260103 的 close
            upsert_industry_board_bars(
                "BK0475", [{**rows[1], "close": 1099}], "em", target
            )
            loaded = load_industry_board_bars("BK0475", target)
            self.assertEqual(len(loaded), 2)  # UNIQUE(board_code, trade_date) 不重复
            self.assertEqual(loaded[1]["close"], 1099)
            self.assertEqual(loaded[1]["turnover_rate"], 1.0)
            # 不同板块互不干扰
            self.assertEqual(load_industry_board_bars("BK1027", target), [])

    def test_industry_membership_and_board_for_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "membership.db"
            upsert_industry_membership("BK0475", ["600000.SH", "000001.SZ"], "20260102", "em", target)
            members = load_industry_membership("BK0475", target)
            self.assertEqual(len(members), 2)
            self.assertEqual(members[0]["symbol"], "000001.SZ")  # 升序
            self.assertEqual(members[0]["as_of"], "20260102")
            # 重刷更新 as_of，不产生重复（PK(board_code, symbol)）
            upsert_industry_membership("BK0475", ["600000.SH", "000001.SZ"], "20260103", "em", target)
            self.assertEqual(len(load_industry_membership("BK0475", target)), 2)
            self.assertEqual(load_industry_membership("BK0475", target)[0]["as_of"], "20260103")
            # symbol → board 反查
            self.assertEqual(industry_board_for_symbol("600000.SH", target), "BK0475")
            self.assertIsNone(industry_board_for_symbol("999999.SH", target))
            # 全表读取（无 board_code）
            self.assertEqual(len(load_industry_membership(path=target)), 2)

    def test_stock_catalog_industries_skips_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "catalog.db"
            upsert_stock_catalog(
                [
                    {"symbol": "600000.SH", "name": "浦发银行", "industry": "银行"},
                    {"symbol": "000001.SZ", "name": "平安银行", "industry": "银行"},
                    {"symbol": "600519.SH", "name": "贵州茅台", "industry": ""},  # 空 → 跳过
                ],
                "synth",
                target,
            )
            industries = stock_catalog_industries(target)
            self.assertEqual(industries, {"600000.SH": "银行", "000001.SZ": "银行"})


def _sample_manifest(dataset_id="ds_abc123def456"):
    """构造一个具代表性的 D2 manifest dict（标量 + 全部 JSON 字段）。"""
    return {
        "dataset_id": dataset_id,
        "n_rows": 882,
        "n_positive": 431,
        "positive_rate": 0.4887,
        "start_date": "20240102",
        "end_date": "20250218",
        "max_trade_date": "20250218",
        "horizon": 5,
        "aux_horizon": 10,
        "index_code": "000300.SH",
        "industry_point_in_time": False,
        "csv_path": "data/ml/ds_abc123def456.csv.gz",
        "symbols": ["002022.SZ", "600309.SH"],
        "rejected": [{"symbol": "999999.SH", "reason": "rebase_suspect@20240730"}],
        "groups": ["signal", "price_volume", "market", "industry"],
        "feature_columns": ["rec_buy", "ret_5", "idx_ret_5", "board_ret_5"],
        "cost_model": {"fee_rate": 0.0003, "stamp_duty": 0.0005},
        "profile_snapshot": {"002022.SZ": {"board_code": "BK0475"}},
        "data_versions": {"002022.SZ": "synth|20260417|seed100"},
        "warnings": ["industry_point_in_time=False：行业为最新单快照，存在前视风险"],
    }


class SchemaV15MigrationTest(unittest.TestCase):
    """v15（D2 ``ml_datasets`` 数据集注册表）迁移、幂等、唯一约束与读写往返。"""

    ML_DATASETS_COLS = {
        "dataset_id", "n_rows", "n_positive", "positive_rate",
        "start_date", "end_date", "max_trade_date", "horizon", "aux_horizon",
        "index_code", "industry_point_in_time", "csv_path",
        "symbols_json", "rejected_json", "groups_json", "feature_columns_json",
        "cost_model_json", "profile_snapshot_json", "data_versions_json",
        "warnings_json", "created_at", "updated_at",
    }

    def _tables(self, target):
        with sqlite3.connect(target) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def _columns(self, target, table):
        with sqlite3.connect(target) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_fresh_db_has_ml_datasets_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fresh15.db"
            init_database(target)
            self.assertIn("ml_datasets", self._tables(target))
            self.assertTrue(
                self.ML_DATASETS_COLS.issubset(self._columns(target, "ml_datasets"))
            )
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)
            self.assertEqual(version, 15)

    def test_legacy_v14_db_upgrades_to_v15(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-v14.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute("DROP TABLE ml_datasets")
                connection.execute("PRAGMA user_version=14")
            self.assertNotIn("ml_datasets", self._tables(target))

            init_database(target)  # 重新初始化应补建 v15 表

            self.assertIn("ml_datasets", self._tables(target))
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_init_database_idempotent_for_v15(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "idem15.db"
            init_database(target)
            init_database(target)  # 第二次不得抛错或重复建表
            self.assertIn("ml_datasets", self._tables(target))

    def test_ml_datasets_primary_key_is_dataset_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "pk15.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                pk_cols = [
                    row[1]
                    for row in connection.execute("PRAGMA table_info(ml_datasets)")
                    if row[5]  # pk flag
                ]
            self.assertEqual(pk_cols, ["dataset_id"])

    def test_register_dataset_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "reg15.db"
            manifest = _sample_manifest()
            dataset_id = register_dataset(manifest, target)
            self.assertEqual(dataset_id, manifest["dataset_id"])

            loaded = load_dataset_manifest(dataset_id, target)
            self.assertIsNotNone(loaded)
            # 标量字段原样回读
            self.assertEqual(loaded["n_rows"], 882)
            self.assertEqual(loaded["n_positive"], 431)
            self.assertAlmostEqual(loaded["positive_rate"], 0.4887)
            self.assertEqual(loaded["horizon"], 5)
            self.assertEqual(loaded["aux_horizon"], 10)
            self.assertEqual(loaded["index_code"], "000300.SH")
            self.assertEqual(loaded["csv_path"], "data/ml/ds_abc123def456.csv.gz")
            # JSON 字段解码回原生 list/dict
            self.assertEqual(loaded["symbols"], ["002022.SZ", "600309.SH"])
            self.assertEqual(loaded["groups"], ["signal", "price_volume", "market", "industry"])
            self.assertEqual(loaded["rejected"][0]["symbol"], "999999.SH")
            self.assertEqual(loaded["cost_model"]["fee_rate"], 0.0003)
            self.assertEqual(
                loaded["profile_snapshot"]["002022.SZ"]["board_code"], "BK0475"
            )
            self.assertEqual(loaded["data_versions"]["002022.SZ"], "synth|20260417|seed100")
            self.assertIn("industry_point_in_time=False", loaded["warnings"][0])
            # bool 往返（INTEGER 0 → False）
            self.assertIs(loaded["industry_point_in_time"], False)
            self.assertIsNotNone(loaded["created_at"])

    def test_register_dataset_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "upsert15.db"
            register_dataset(_sample_manifest(), target)
            # 同 dataset_id 重写 n_rows → upsert 覆盖，不产生第二行
            register_dataset({**_sample_manifest(), "n_rows": 999}, target)
            datasets = list_datasets(target)
            self.assertEqual(len(datasets), 1)
            self.assertEqual(datasets[0]["n_rows"], 999)

    def test_load_missing_dataset_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing15.db"
            init_database(target)
            self.assertIsNone(load_dataset_manifest("nope", target))
            self.assertEqual(list_datasets(target), [])

    def test_list_datasets_returns_all(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "list15.db"
            register_dataset(_sample_manifest("ds_a"), target)
            register_dataset(_sample_manifest("ds_b"), target)
            ids = {d["dataset_id"] for d in list_datasets(target)}
            self.assertEqual(ids, {"ds_a", "ds_b"})


def _sample_model(model_id="logreg-win5-abc1234567", **overrides):
    """构造一个具代表性的 D3 ml_models 行（标量 + 全部 JSON 字段）。"""
    row = {
        "model_id": model_id,
        "kind": "logreg",
        "target": "win5",
        "horizon": 5,
        "dataset_id": "ds_abc123def456",
        "n_features": 28,
        "threshold": 0.5,
        "oos_brier": 0.171,
        "oos_auc": 0.817,
        "oos_logloss": 0.42,
        "oos_ece": 0.03,
        "oos_mae": None,
        "oos_r2": None,
        "base_rate": 0.49,
        "base_rate_brier": 0.2442,
        "coverage_at_threshold": 0.68,
        "win_rate_at_threshold": 0.62,
        "n_oos": 735,
        "n_train": 573,
        "n_rows": 2000,
        "n_positive": 900,
        "max_trade_date": "20260801",
        "status": "candidate",
        "stale": False,
        "artifact_path": "data/ml/models/logreg-win5-abc1234567",
        "sklearn_version": "1.6.1",
        "feature_groups": ["signal", "price_volume", "market", "industry"],
        "selected_params": {"kind": "logreg", "C": 0.1},
        "metrics": {"n_oos": 735, "oos_auc": 0.817},
        "warnings": [],
        "trained_at": "20260901T000000Z",
    }
    row.update(overrides)
    return row


class SchemaV15ModelsTest(unittest.TestCase):
    """v15（D3 ``ml_models`` 模型注册表）迁移、幂等、唯一约束、状态流转与读写往返。"""

    ML_MODELS_COLS = set(ML_MODELS_COLUMNS)

    def _tables(self, target):
        with sqlite3.connect(target) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def _columns(self, target, table):
        with sqlite3.connect(target) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_fresh_db_has_ml_models_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "fresh15m.db"
            init_database(target)
            self.assertIn("ml_models", self._tables(target))
            self.assertTrue(self.ML_MODELS_COLS.issubset(self._columns(target, "ml_models")))
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(ml_models)")
                }
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)
            self.assertIn("idx_ml_models_status", indexes)

    def test_legacy_db_without_ml_models_upgrades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy15m.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute("DROP TABLE ml_models")
                connection.execute("PRAGMA user_version=14")
            self.assertNotIn("ml_models", self._tables(target))

            init_database(target)  # 重新初始化补建 v15 ml_models

            self.assertIn("ml_models", self._tables(target))
            self.assertTrue(self.ML_MODELS_COLS.issubset(self._columns(target, "ml_models")))

    def test_legacy_row_gains_new_columns(self):
        # _ensure_columns 增量补列：旧库缺列时 ALTER TABLE 补齐
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "altcols15m.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute("ALTER TABLE ml_models DROP COLUMN oos_ece")
            self.assertNotIn("oos_ece", self._columns(target, "ml_models"))
            init_database(target)
            self.assertIn("oos_ece", self._columns(target, "ml_models"))

    def test_ml_models_primary_key_is_model_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "pk15m.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                pk_cols = [
                    row[1]
                    for row in connection.execute("PRAGMA table_info(ml_models)")
                    if row[5]
                ]
            self.assertEqual(pk_cols, ["model_id"])

    def test_register_model_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "reg15m.db"
            model = _sample_model()
            model_id = register_model(model, target)
            self.assertEqual(model_id, model["model_id"])

            loaded = load_model(model_id, target)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["kind"], "logreg")
            self.assertEqual(loaded["target"], "win5")
            self.assertEqual(loaded["horizon"], 5)
            self.assertEqual(loaded["n_features"], 28)
            self.assertAlmostEqual(loaded["oos_brier"], 0.171)
            self.assertAlmostEqual(loaded["oos_auc"], 0.817)
            self.assertIsNone(loaded["oos_mae"])  # 分类目标无回归指标
            self.assertEqual(loaded["n_oos"], 735)
            self.assertEqual(loaded["max_trade_date"], "20260801")
            self.assertEqual(loaded["status"], "candidate")
            self.assertIs(loaded["stale"], False)  # INTEGER 0 → bool
            # JSON 字段解码回原生 list/dict
            self.assertEqual(
                loaded["feature_groups"], ["signal", "price_volume", "market", "industry"]
            )
            self.assertEqual(loaded["selected_params"]["C"], 0.1)
            self.assertEqual(loaded["metrics"]["oos_auc"], 0.817)
            self.assertEqual(loaded["warnings"], [])
            self.assertIsNotNone(loaded["created_at"])

    def test_register_model_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "upsert15m.db"
            register_model(_sample_model(), target)
            register_model(_sample_model(status="promoted", oos_auc=0.9), target)
            models = list_models(target)
            self.assertEqual(len(models), 1)
            self.assertEqual(models[0]["status"], "promoted")
            self.assertAlmostEqual(models[0]["oos_auc"], 0.9)

    def test_load_missing_model_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing15m.db"
            init_database(target)
            self.assertIsNone(load_model("nope", target))
            self.assertEqual(list_models(target), [])

    def test_list_models_status_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "listfilter15m.db"
            register_model(_sample_model("logreg-win5-aaa", status="candidate"), target)
            register_model(_sample_model("logreg-win5-bbb", status="promoted"), target)
            register_model(_sample_model("hgb-win5-ccc", status="demo"), target)
            self.assertEqual(len(list_models(target)), 3)
            self.assertEqual(
                [m["model_id"] for m in list_models(target, status="promoted")],
                ["logreg-win5-bbb"],
            )
            self.assertEqual(len(list_models(target, status="demo")), 1)

    def test_set_model_status_updates_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "setstatus15m.db"
            mid = register_model(_sample_model(), target)
            set_model_status(mid, "promoted", stale=True, warnings=["数据滞后"], path=target)
            loaded = load_model(mid, target)
            self.assertEqual(loaded["status"], "promoted")
            self.assertIs(loaded["stale"], True)
            self.assertEqual(loaded["warnings"], ["数据滞后"])

    def test_set_model_status_without_optional_keeps_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "setstatus2.db"
            mid = register_model(_sample_model(stale=True), target)
            set_model_status(mid, "retired", path=target)  # 不传 stale → 保持原值
            loaded = load_model(mid, target)
            self.assertEqual(loaded["status"], "retired")
            self.assertIs(loaded["stale"], True)

    def test_retire_promoted_models_excludes_self(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "retire15m.db"
            register_model(_sample_model("logreg-win5-old", status="promoted"), target)
            register_model(_sample_model("logreg-win5-old2", status="demo"), target)
            register_model(_sample_model("logreg-win5-keep", status="promoted"), target)
            register_model(_sample_model("logreg-ret5-other", status="promoted", target="ret5"), target)
            n = retire_promoted_models("win5", 5, exclude_model_id="logreg-win5-keep", path=target)
            self.assertEqual(n, 2)  # old + old2 退役，keep 排除，ret5 不同目标不动
            self.assertEqual(load_model("logreg-win5-old", target)["status"], "retired")
            self.assertEqual(load_model("logreg-win5-old2", target)["status"], "retired")
            self.assertEqual(load_model("logreg-win5-keep", target)["status"], "promoted")
            self.assertEqual(load_model("logreg-ret5-other", target)["status"], "promoted")

    def test_load_promoted_model_default_excludes_demo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "loadprom15m.db"
            register_model(_sample_model("logreg-win5-demo", status="demo"), target)
            self.assertIsNone(load_promoted_model("win5", path=target))
            loaded = load_promoted_model("win5", include_demo=True, path=target)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["model_id"], "logreg-win5-demo")

    def test_load_promoted_model_picks_latest_and_filters_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "loadprom2.db"
            register_model(
                _sample_model("logreg-win5-early", status="promoted", trained_at="20260101T000000Z"),
                target,
            )
            register_model(
                _sample_model("logreg-win5-late", status="promoted", trained_at="20260901T000000Z"),
                target,
            )
            register_model(_sample_model("logreg-ret5-x", status="promoted", target="ret5"), target)
            latest = load_promoted_model("win5", path=target)
            self.assertEqual(latest["model_id"], "logreg-win5-late")
            # 不带 target → 全局最新（win5-late 的 trained_at 最大）
            any_latest = load_promoted_model(path=target)
            self.assertEqual(any_latest["model_id"], "logreg-win5-late")

    def test_load_promoted_model_none_when_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "loadprom3.db"
            init_database(target)
            self.assertIsNone(load_promoted_model("win5", path=target))


if __name__ == "__main__":
    unittest.main()
