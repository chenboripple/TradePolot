from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Mapping


DATABASE_SCHEMA_VERSION = 13


BACKTEST_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT",
    "name": "name TEXT",
    "start_date": "start_date TEXT",
    "end_date": "end_date TEXT",
    "initial_capital": "initial_capital REAL",
    "final_capital": "final_capital REAL",
    "total_return": "total_return REAL",
    "annual_return": "annual_return REAL",
    "max_drawdown": "max_drawdown REAL",
    "sharpe_ratio": "sharpe_ratio REAL",
    "total_trades": "total_trades INTEGER",
    "win_rate": "win_rate REAL",
    "created_at": "created_at TIMESTAMP",
    "user_id": "user_id INTEGER",
    "strategy_id": "strategy_id INTEGER",
    # 记录回测入参，供前端按原参数一键重跑
    "strategy_key": "strategy_key TEXT",
    "bar_count": "bar_count INTEGER",
    "execution": "execution TEXT",
    # 完整回测结果（权益曲线/成交明细/指标），供前端历史回放
    "result_json": "result_json TEXT",
    # v12（A8）：CLI 回测/walk-forward 落库与来源溯源。
    # run_kind='backtest'|'walkforward'；user_id 为 NULL 表示 CLI 跑的非用户记录
    # （list_user_backtests 按 user_id 过滤，NULL 行不进用户列表）。
    "run_kind": "run_kind TEXT DEFAULT 'backtest'",
    "params_json": "params_json TEXT",
    "profile_source": "profile_source TEXT",
    "report_json": "report_json TEXT",
}

USER_COLUMNS = {
    "id": "id INTEGER",
    "username": "username TEXT",
    "password_hash": "password_hash TEXT",
    "role": "role TEXT DEFAULT 'user'",
    "created_at": "created_at TIMESTAMP",
}

SESSION_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "token_hash": "token_hash TEXT",
    "expires_at": "expires_at TIMESTAMP",
    "created_at": "created_at TIMESTAMP",
}

STRATEGY_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "name": "name TEXT",
    "asset_class": "asset_class TEXT DEFAULT 'stock'",
    "symbol": "symbol TEXT DEFAULT ''",
    "profile": "profile TEXT DEFAULT ''",
    "parameters_json": "parameters_json TEXT DEFAULT '{}'",
    "visibility": "visibility TEXT DEFAULT 'private'",
    "created_at": "created_at TIMESTAMP",
    "updated_at": "updated_at TIMESTAMP",
    "system_key": "system_key TEXT",
}

WATCHLIST_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "symbol": "symbol TEXT DEFAULT ''",
    "name": "name TEXT DEFAULT ''",
    "is_watched": "is_watched INTEGER NOT NULL DEFAULT 1",
    "created_at": "created_at TIMESTAMP",
    "last_updated_at": "last_updated_at TIMESTAMP",
    "default_strategy_id": "default_strategy_id INTEGER",
}

DAILY_BAR_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT DEFAULT ''",
    "trade_date": "trade_date TEXT DEFAULT ''",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "pre_close": "pre_close REAL",
    "change": "change REAL",
    "pct_chg": "pct_chg REAL",
    "volume": "volume REAL DEFAULT 0",
    "amount": "amount REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
    # v12（A9）：复权溯源。adjust='qfq'|'raw'；adj_anchor_date=复权锚定日
    # （拉取时最新交易日，混接根因）；data_version='source|anchor|utc_ts'
    # （signal_ledger 与 ml manifest 引用做溯源）。
    "adjust": "adjust TEXT DEFAULT 'qfq'",
    "adj_anchor_date": "adj_anchor_date TEXT DEFAULT ''",
    "data_version": "data_version TEXT DEFAULT ''",
}

STOCK_CATALOG_COLUMNS = {
    "symbol": "symbol TEXT DEFAULT ''",
    "name": "name TEXT DEFAULT ''",
    "market": "market TEXT DEFAULT ''",
    "exchange": "exchange TEXT DEFAULT ''",
    "board": "board TEXT DEFAULT ''",
    "industry": "industry TEXT DEFAULT ''",
    "area": "area TEXT DEFAULT ''",
    "list_status": "list_status TEXT DEFAULT 'L'",
    "list_date": "list_date TEXT DEFAULT ''",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

STOCK_QUOTE_COLUMNS = {
    "symbol": "symbol TEXT DEFAULT ''",
    "price": "price REAL",
    "pre_close": "pre_close REAL",
    "change": "change REAL",
    "change_pct": "change_pct REAL",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "volume": "volume REAL DEFAULT 0",
    "amount": "amount REAL",
    "turnover_rate": "turnover_rate REAL",
    "quote_time": "quote_time TEXT DEFAULT ''",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# v13（B2）：信号台账。每个 (symbol, trade_date, source, provisional) 一行，记录
# 当时的投票决策、（D 阶段）模型输出，以及用 B1 标签回填的前瞻净收益/下行风险。
# label_status: pending（待回填）| filled（已回填）| expired（永不回填）| bad_data（决策日缺 bar）。
# 与 backtest_results（run 粒度）、paper_ledger（fill 粒度）通过 backtest_id/symbol/trade_date
# 松耦合，不合并。详见 docs/prediction-target.md。
SIGNAL_LEDGER_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT",
    "trade_date": "trade_date TEXT",
    "source": "source TEXT DEFAULT 'monitor'",
    "provisional": "provisional INTEGER DEFAULT 0",
    "profile_name": "profile_name TEXT",
    "params_json": "params_json TEXT",
    "vote_threshold": "vote_threshold INTEGER",
    "recommendation": "recommendation TEXT",
    "buy_count": "buy_count INTEGER",
    "sell_count": "sell_count INTEGER",
    "components_json": "components_json TEXT",
    "features_json": "features_json TEXT",
    "model_id": "model_id TEXT",
    "p_win": "p_win REAL",
    "expected_ret": "expected_ret REAL",
    "downside_mae": "downside_mae REAL",
    "entry_price": "entry_price REAL",
    "exit_price": "exit_price REAL",
    "horizon": "horizon INTEGER DEFAULT 5",
    "fwd_net_return": "fwd_net_return REAL",
    "fwd_ret_aux": "fwd_ret_aux REAL",
    "fwd_mae": "fwd_mae REAL",
    "label_status": "label_status TEXT DEFAULT 'pending'",
    "data_version": "data_version TEXT",
    "backtest_id": "backtest_id INTEGER",
    "filled_at": "filled_at TIMESTAMP",
    "created_at": "created_at TIMESTAMP",
}

# v13（B2）：通用键值表，monitor 收盘例程记录"上次执行日"等状态，防重启重复跑。
KV_STORE_COLUMNS = {
    "key": "key TEXT",
    "value": "value TEXT",
    "updated_at": "updated_at TIMESTAMP",
}


def database_path() -> Path:
    configured = os.getenv("TRADEPILOT_BACKTEST_DB")
    if configured:
        return Path(configured)

    data_dir = Path(os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data"))
    return data_dir / "backtest" / "backtest_results.db"


def _ensure_columns(connection: sqlite3.Connection, table_name: str, columns: Mapping[str, str]) -> None:
    existing = {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }
    for column_name, definition in columns.items():
        if column_name not in existing:
            connection.execute(f'ALTER TABLE "{table_name}" ADD COLUMN {definition}')


def init_database(path: Path | None = None) -> Path:
    target = path or database_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                name TEXT,
                start_date TEXT,
                end_date TEXT,
                initial_capital REAL,
                final_capital REAL,
                total_return REAL,
                annual_return REAL,
                max_drawdown REAL,
                sharpe_ratio REAL,
                total_trades INTEGER,
                win_rate REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "backtest_results", BACKTEST_COLUMNS)
        connection.execute(
            "UPDATE backtest_results SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_results_symbol_created "
            "ON backtest_results(symbol, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_results_user_created "
            "ON backtest_results(user_id, created_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "users", USER_COLUMNS)
        connection.execute(
            "UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        connection.execute("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''")
        has_admin = connection.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        first_user = connection.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not has_admin and first_user:
            connection.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?", (first_user[0],)
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
            "ON users(username COLLATE NOCASE)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_columns(connection, "user_sessions", SESSION_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_user "
            "ON user_sessions(user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry "
            "ON user_sessions(expires_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                asset_class TEXT NOT NULL CHECK(asset_class IN ('stock', 'future')),
                symbol TEXT NOT NULL,
                profile TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'private'
                    CHECK(visibility IN ('public', 'private')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                system_key TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_columns(connection, "strategies", STRATEGY_COLUMNS)
        connection.execute(
            "UPDATE strategies SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        connection.execute(
            "UPDATE strategies SET updated_at = created_at WHERE updated_at IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategies_owner_updated "
            "ON strategies(user_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategies_visibility_updated "
            "ON strategies(visibility, updated_at DESC)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_strategies_system_key "
            "ON strategies(system_key) WHERE system_key IS NOT NULL"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                is_watched INTEGER NOT NULL DEFAULT 1 CHECK(is_watched IN (0, 1)),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP,
                default_strategy_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(default_strategy_id) REFERENCES strategies(id) ON DELETE SET NULL,
                UNIQUE(user_id, symbol)
            )
            """
        )
        _ensure_columns(connection, "user_watchlist", WATCHLIST_COLUMNS)
        connection.execute(
            "UPDATE user_watchlist SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        )
        connection.execute(
            "UPDATE user_watchlist SET is_watched = 1 WHERE is_watched IS NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_watchlist_owner_symbol "
            "ON user_watchlist(user_id, symbol)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_watchlist_owner_created "
            "ON user_watchlist(user_id, created_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_catalog (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                board TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                area TEXT NOT NULL DEFAULT '',
                list_status TEXT NOT NULL DEFAULT 'L',
                list_date TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "stock_catalog", STOCK_CATALOG_COLUMNS)
        connection.execute(
            "UPDATE stock_catalog SET board = market "
            "WHERE (board IS NULL OR board = '') AND market <> ''"
        )
        connection.execute(
            """
            UPDATE stock_catalog
            SET exchange = CASE
                WHEN symbol LIKE '%.SH' THEN 'SSE'
                WHEN symbol LIKE '%.SZ' THEN 'SZSE'
                WHEN symbol LIKE '%.BJ' THEN 'BSE'
                ELSE exchange
            END
            WHERE exchange IS NULL OR exchange = ''
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_stock_catalog_name "
            "ON stock_catalog(name)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                pre_close REAL,
                change REAL,
                pct_chg REAL,
                volume REAL NOT NULL DEFAULT 0,
                amount REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_date)
            )
            """
        )
        _ensure_columns(connection, "daily_bars", DAILY_BAR_COLUMNS)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_bars_symbol_date "
            "ON daily_bars(symbol, trade_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_bars_date "
            "ON daily_bars(trade_date DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_quotes (
                symbol TEXT PRIMARY KEY,
                price REAL NOT NULL,
                pre_close REAL,
                change REAL,
                change_pct REAL,
                open REAL,
                high REAL,
                low REAL,
                volume REAL NOT NULL DEFAULT 0,
                amount REAL,
                turnover_rate REAL,
                quote_time TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "stock_quotes", STOCK_QUOTE_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_stock_quotes_time "
            "ON stock_quotes(quote_time DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'monitor'
                    CHECK(source IN ('monitor', 'backtest', 'dataset', 'manual')),
                provisional INTEGER NOT NULL DEFAULT 0 CHECK(provisional IN (0, 1)),
                profile_name TEXT,
                params_json TEXT,
                vote_threshold INTEGER,
                recommendation TEXT,
                buy_count INTEGER,
                sell_count INTEGER,
                components_json TEXT,
                features_json TEXT,
                model_id TEXT,
                p_win REAL,
                expected_ret REAL,
                downside_mae REAL,
                entry_price REAL,
                exit_price REAL,
                horizon INTEGER NOT NULL DEFAULT 5,
                fwd_net_return REAL,
                fwd_ret_aux REAL,
                fwd_mae REAL,
                label_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(label_status IN ('pending', 'filled', 'expired', 'bad_data')),
                data_version TEXT,
                backtest_id INTEGER,
                filled_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_date, source, provisional)
            )
            """
        )
        _ensure_columns(connection, "signal_ledger", SIGNAL_LEDGER_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ledger_status "
            "ON signal_ledger(label_status, symbol)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ledger_symbol_date "
            "ON signal_ledger(symbol, trade_date DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ledger_backtest "
            "ON signal_ledger(backtest_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "kv_store", KV_STORE_COLUMNS)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {target}: {integrity}")
        connection.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")

    return target


def load_daily_bars(
    symbol: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, pre_close,
                   change, pct_chg, volume AS vol, amount, source,
                   adjust, adj_anchor_date, data_version
            FROM daily_bars
            WHERE symbol = ?
            ORDER BY trade_date
            """,
            (symbol,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_daily_bar_symbols(path: Path | None = None) -> List[str]:
    """列出 daily_bars 中出现过的全部标的（A9 全库复权巡检用）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute(
            "SELECT DISTINCT symbol FROM daily_bars "
            "WHERE symbol IS NOT NULL AND symbol != '' ORDER BY symbol"
        ).fetchall()
    return [row[0] for row in rows]


def upsert_daily_bars(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
    adjust: str = "qfq",
    adj_anchor_date: str = "",
    data_version: str = "",
) -> int:
    """写入/更新某标的日线。

    A9 复权溯源：``adjust``/``adj_anchor_date``/``data_version`` 是序列级元数据
    （一次刷新一份），随整批写入；data_version 形如 ``source|anchor|utc_ts``，
    供 signal_ledger 与 ml manifest 引用做前视/混接溯源。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO daily_bars (
                symbol, trade_date, open, high, low, close,
                pre_close, change, pct_chg, volume, amount, source,
                adjust, adj_anchor_date, data_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                pre_close = excluded.pre_close,
                change = excluded.change,
                pct_chg = excluded.pct_chg,
                volume = excluded.volume,
                amount = excluded.amount,
                source = excluded.source,
                adjust = excluded.adjust,
                adj_anchor_date = excluded.adj_anchor_date,
                data_version = excluded.data_version,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    symbol,
                    row["trade_date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row.get("pre_close"),
                    row.get("change"),
                    row.get("pct_chg"),
                    row.get("vol", 0),
                    row.get("amount"),
                    source,
                    adjust,
                    adj_anchor_date,
                    data_version,
                )
                for row in records
            ],
        )
    return len(records)


def upsert_stock_catalog(
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO stock_catalog (
                symbol, name, market, exchange, board, industry, area,
                list_status, list_date, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                name = excluded.name,
                market = CASE
                    WHEN excluded.market <> '' THEN excluded.market
                    ELSE stock_catalog.market
                END,
                exchange = CASE
                    WHEN excluded.exchange <> '' THEN excluded.exchange
                    ELSE stock_catalog.exchange
                END,
                board = CASE
                    WHEN excluded.board <> '' THEN excluded.board
                    ELSE stock_catalog.board
                END,
                industry = CASE
                    WHEN excluded.industry <> '' THEN excluded.industry
                    ELSE stock_catalog.industry
                END,
                area = CASE
                    WHEN excluded.area <> '' THEN excluded.area
                    ELSE stock_catalog.area
                END,
                list_status = excluded.list_status,
                list_date = CASE
                    WHEN excluded.list_date <> '' THEN excluded.list_date
                    ELSE stock_catalog.list_date
                END,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(row["symbol"]).upper(),
                    str(row["name"]).strip(),
                    str(row.get("market") or row.get("board") or "").strip(),
                    str(row.get("exchange") or "").strip(),
                    str(row.get("board") or row.get("market") or "").strip(),
                    str(row.get("industry") or "").strip(),
                    str(row.get("area") or "").strip(),
                    str(row.get("list_status") or "L").strip(),
                    str(row.get("list_date") or "").strip(),
                    source,
                )
                for row in records
            ],
        )
        connection.execute(
            """
            UPDATE user_watchlist
            SET name = (
                SELECT stock_catalog.name
                FROM stock_catalog
                WHERE stock_catalog.symbol = user_watchlist.symbol
            )
            WHERE EXISTS (
                SELECT 1 FROM stock_catalog
                WHERE stock_catalog.symbol = user_watchlist.symbol
                  AND stock_catalog.name <> user_watchlist.name
            )
            """
        )
    return len(records)


def upsert_stock_quotes(
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO stock_quotes (
                symbol, price, pre_close, change, change_pct,
                open, high, low, volume, amount, turnover_rate,
                quote_time, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                price = excluded.price,
                pre_close = excluded.pre_close,
                change = excluded.change,
                change_pct = excluded.change_pct,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                volume = excluded.volume,
                amount = excluded.amount,
                turnover_rate = excluded.turnover_rate,
                quote_time = excluded.quote_time,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(row["symbol"]).upper(),
                    row["price"],
                    row.get("pre_close"),
                    row.get("change"),
                    row.get("change_pct"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("volume", 0),
                    row.get("amount"),
                    row.get("turnover_rate"),
                    str(row.get("quote_time") or ""),
                    source,
                )
                for row in records
            ],
        )
    return len(records)


def load_stock_quotes(path: Path | None = None) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT symbol, price, pre_close, change, change_pct,
                   open, high, low, volume, amount, turnover_rate,
                   quote_time, source
            FROM stock_quotes
            ORDER BY symbol
            """
        ).fetchall()
    return [dict(row) for row in rows]


def stock_catalog_name(symbol: str, path: Path | None = None) -> str | None:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        row = connection.execute(
            "SELECT name FROM stock_catalog WHERE symbol = ?", (symbol,)
        ).fetchone()
    return str(row[0]) if row else None


def stock_catalog_names(path: Path | None = None) -> Mapping[str, str]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute("SELECT symbol, name FROM stock_catalog").fetchall()
    return {str(symbol): str(name) for symbol, name in rows}


def list_stock_catalog(path: Path | None = None) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            WITH ranked_bars AS (
                SELECT
                    symbol,
                    trade_date,
                    close,
                    pre_close,
                    change,
                    pct_chg,
                    source,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol ORDER BY trade_date DESC
                    ) AS position
                FROM daily_bars
            )
            SELECT
                stock_catalog.symbol,
                stock_catalog.name,
                stock_catalog.market,
                stock_catalog.exchange,
                stock_catalog.board,
                stock_catalog.industry,
                stock_catalog.area,
                stock_catalog.list_status,
                stock_catalog.list_date,
                stock_catalog.source,
                stock_catalog.updated_at,
                latest.trade_date AS latest_date,
                latest.close AS daily_price,
                latest.pre_close AS daily_pre_close,
                latest.change AS daily_change,
                latest.pct_chg AS daily_change_pct,
                latest.source AS daily_source,
                previous.close AS previous_price,
                stock_quotes.price AS quote_price,
                stock_quotes.pre_close AS quote_pre_close,
                stock_quotes.change AS quote_change,
                stock_quotes.change_pct AS quote_change_pct,
                stock_quotes.volume AS quote_volume,
                stock_quotes.amount AS quote_amount,
                stock_quotes.turnover_rate,
                stock_quotes.quote_time,
                stock_quotes.source AS quote_source
            FROM stock_catalog
            LEFT JOIN ranked_bars AS latest
                ON latest.symbol = stock_catalog.symbol AND latest.position = 1
            LEFT JOIN ranked_bars AS previous
                ON previous.symbol = stock_catalog.symbol AND previous.position = 2
            LEFT JOIN stock_quotes
                ON stock_quotes.symbol = stock_catalog.symbol
            ORDER BY stock_catalog.symbol
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        quote_price = item.pop("quote_price")
        daily_price = item.pop("daily_price")
        quote_pre_close = item.pop("quote_pre_close")
        daily_pre_close = item.pop("daily_pre_close")
        quote_change = item.pop("quote_change")
        daily_change = item.pop("daily_change")
        quote_change_pct = item.pop("quote_change_pct")
        daily_change_pct = item.pop("daily_change_pct")
        previous_price = item.pop("previous_price")
        using_quote = quote_price is not None
        price = quote_price if using_quote else daily_price
        pre_close = quote_pre_close if using_quote else daily_pre_close
        change = quote_change if using_quote else daily_change
        change_pct = quote_change_pct if using_quote else daily_change_pct
        comparison_price = pre_close if pre_close not in (None, 0) else previous_price
        if change is None and price is not None and comparison_price is not None:
            change = float(price) - float(comparison_price)
        if change_pct is None and price is not None and comparison_price not in (None, 0):
            change_pct = (float(price) / float(comparison_price) - 1) * 100
        item["price"] = price
        item["pre_close"] = pre_close
        item["change"] = change
        item["change_pct"] = change_pct
        item["price_time"] = (
            item.get("quote_time") if using_quote else item.get("latest_date")
        )
        item["price_source"] = (
            item.get("quote_source") if using_quote else item.get("daily_source")
        )
        item["price_kind"] = (
            "realtime"
            if using_quote
            else "daily" if price is not None else "unavailable"
        )
        if not using_quote:
            item["quote_volume"] = None
            item["quote_amount"] = None
            item["turnover_rate"] = None
            item["quote_time"] = None
        item.pop("quote_source", None)
        item.pop("daily_source", None)
        items.append(item)
    return items
