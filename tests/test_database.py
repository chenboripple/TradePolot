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
    list_stock_catalog,
    load_daily_bars,
    load_index_bars,
    load_market_daily,
    record_market_daily,
    upsert_daily_bars,
    upsert_index_daily,
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
    """v14（C1 指数日线 + C2 市场宽度）迁移、幂等与读写往返。"""

    INDEX_COLS = {
        "id", "index_code", "trade_date", "open", "high", "low", "close",
        "pct_chg", "amount", "volume", "source", "updated_at",
    }
    MARKET_COLS = {
        "trade_date", "advancers", "decliners", "unchanged", "limit_up",
        "limit_down", "total_amount", "up_ratio", "source", "updated_at",
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
            self.assertEqual(version, 14)

    def test_legacy_v13_db_upgrades_to_v14(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-v13.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute("DROP TABLE index_daily")
                connection.execute("DROP TABLE market_daily")
                connection.execute("PRAGMA user_version=13")
            self.assertNotIn("index_daily", self._tables(target))

            init_database(target)  # 重新初始化应补建 v14 表

            tables = self._tables(target)
            self.assertIn("index_daily", tables)
            self.assertIn("market_daily", tables)
            with sqlite3.connect(target) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 14)

    def test_init_database_idempotent_for_v14(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "idem14.db"
            init_database(target)
            init_database(target)  # 第二次不得抛错或重复建表
            self.assertIn("index_daily", self._tables(target))
            self.assertIn("market_daily", self._tables(target))

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


if __name__ == "__main__":
    unittest.main()
