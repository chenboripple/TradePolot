from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DATABASE_SCHEMA_VERSION = 15


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

# v14（C1）：指数日线落库。每个 (index_code, trade_date) 一行，供基准对比与市场
# 特征复用——此前指数历史从不落库，benchmark 每次回测都实时拉 Tushare（离线即降级）。
# index_code 用 tushare 风格代码（000300.SH / 000001.SH / 399001.SZ / 399006.SZ）。
INDEX_DAILY_COLUMNS = {
    "id": "id INTEGER",
    "index_code": "index_code TEXT",
    "trade_date": "trade_date TEXT",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "pct_chg": "pct_chg REAL",
    "amount": "amount REAL",
    "volume": "volume REAL DEFAULT 0",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# v14（C2）：市场宽度按交易日积累。无免费历史宽度 API，故走"增量积累制"——
# monitor 收盘例程 / `data refresh-market` 用当日 stock_quotes 终态快照聚合写入。
# 涨跌停家数用 A7 price_limit_for_symbol 分板块判定（替代旧的 ±9.8% 一刀切）。
# trade_date 为主键：每个交易日一行，重复刷新 upsert 覆盖。
MARKET_DAILY_COLUMNS = {
    "trade_date": "trade_date TEXT",
    "advancers": "advancers INTEGER DEFAULT 0",
    "decliners": "decliners INTEGER DEFAULT 0",
    "unchanged": "unchanged INTEGER DEFAULT 0",
    "limit_up": "limit_up INTEGER DEFAULT 0",
    "limit_down": "limit_down INTEGER DEFAULT 0",
    "total_amount": "total_amount REAL DEFAULT 0",
    "up_ratio": "up_ratio REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# v14（C3）：行业板块基建。东财（akshare）为唯一主源（tushare 120 积分无免费行业指数
# 历史）。三张表：industry_boards（板块登记）、industry_board_bars（板块日线）、
# industry_membership（成分股最新快照 + as_of 观察日）。失败板块的特征组在 D 阶段自动
# NaN 降级，绝不造假。board_code 用东财板块代码（如 BK0475）。
INDUSTRY_BOARDS_COLUMNS = {
    "board_code": "board_code TEXT",
    "board_name": "board_name TEXT",
    "source": "source TEXT DEFAULT 'em'",
    "updated_at": "updated_at TIMESTAMP",
}

INDUSTRY_BOARD_BARS_COLUMNS = {
    "id": "id INTEGER",
    "board_code": "board_code TEXT",
    "trade_date": "trade_date TEXT",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "pct_chg": "pct_chg REAL",
    "amount": "amount REAL",
    "turnover_rate": "turnover_rate REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# PK(board_code, symbol)：每个板块对每只成分股保留一行"最新快照"，as_of 记录观察日。
# 现状单快照（非逐日 point-in-time），D 阶段读取时按 as_of<=trade_date 取最新并标
# industry_point_in_time=False 前视警告（诚实标注局限）。
INDUSTRY_MEMBERSHIP_COLUMNS = {
    "board_code": "board_code TEXT",
    "symbol": "symbol TEXT",
    "as_of": "as_of TEXT",
    "source": "source TEXT DEFAULT 'em'",
    "updated_at": "updated_at TIMESTAMP",
}

# v15（D2）：ML 数据集 manifest 落库（dataset_id 主键；列表/字典字段 JSON 编码）。
# 与 ml_models（D3）同属 v15——D2 先建 ml_datasets，D3 再增 ml_models，不另起版本号。
ML_DATASETS_COLUMNS = {
    "dataset_id": "dataset_id TEXT",
    "n_rows": "n_rows INTEGER NOT NULL DEFAULT 0",
    "n_positive": "n_positive INTEGER NOT NULL DEFAULT 0",
    "positive_rate": "positive_rate REAL",
    "start_date": "start_date TEXT",
    "end_date": "end_date TEXT",
    "max_trade_date": "max_trade_date TEXT",
    "horizon": "horizon INTEGER NOT NULL DEFAULT 5",
    "aux_horizon": "aux_horizon INTEGER NOT NULL DEFAULT 10",
    "index_code": "index_code TEXT",
    "industry_point_in_time": "industry_point_in_time INTEGER NOT NULL DEFAULT 0",
    "csv_path": "csv_path TEXT",
    "symbols_json": "symbols_json TEXT",
    "rejected_json": "rejected_json TEXT",
    "groups_json": "groups_json TEXT",
    "feature_columns_json": "feature_columns_json TEXT",
    "cost_model_json": "cost_model_json TEXT",
    "profile_snapshot_json": "profile_snapshot_json TEXT",
    "data_versions_json": "data_versions_json TEXT",
    "warnings_json": "warnings_json TEXT",
    "created_at": "created_at TIMESTAMP",
    "updated_at": "updated_at TIMESTAMP",
}

ML_MODELS_COLUMNS = {
    "model_id": "model_id TEXT",
    "kind": "kind TEXT",
    "target": "target TEXT",
    "horizon": "horizon INTEGER NOT NULL DEFAULT 5",
    "dataset_id": "dataset_id TEXT",
    "n_features": "n_features INTEGER NOT NULL DEFAULT 0",
    "threshold": "threshold REAL",
    "oos_brier": "oos_brier REAL",
    "oos_auc": "oos_auc REAL",
    "oos_logloss": "oos_logloss REAL",
    "oos_ece": "oos_ece REAL",
    "oos_mae": "oos_mae REAL",
    "oos_r2": "oos_r2 REAL",
    "base_rate": "base_rate REAL",
    "base_rate_brier": "base_rate_brier REAL",
    "coverage_at_threshold": "coverage_at_threshold REAL",
    "win_rate_at_threshold": "win_rate_at_threshold REAL",
    "n_oos": "n_oos INTEGER NOT NULL DEFAULT 0",
    "n_train": "n_train INTEGER NOT NULL DEFAULT 0",
    "n_rows": "n_rows INTEGER NOT NULL DEFAULT 0",
    "n_positive": "n_positive INTEGER NOT NULL DEFAULT 0",
    "max_trade_date": "max_trade_date TEXT",
    "status": "status TEXT NOT NULL DEFAULT 'candidate'",
    "stale": "stale INTEGER NOT NULL DEFAULT 0",
    "artifact_path": "artifact_path TEXT",
    "sklearn_version": "sklearn_version TEXT",
    "feature_groups_json": "feature_groups_json TEXT",
    "selected_params_json": "selected_params_json TEXT",
    "metrics_json": "metrics_json TEXT",
    "warnings_json": "warnings_json TEXT",
    "trained_at": "trained_at TIMESTAMP",
    "created_at": "created_at TIMESTAMP",
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS index_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                index_code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pct_chg REAL,
                amount REAL,
                volume REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(index_code, trade_date)
            )
            """
        )
        _ensure_columns(connection, "index_daily", INDEX_DAILY_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_index_daily_code_date "
            "ON index_daily(index_code, trade_date)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_daily (
                trade_date TEXT PRIMARY KEY,
                advancers INTEGER NOT NULL DEFAULT 0,
                decliners INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                limit_up INTEGER NOT NULL DEFAULT 0,
                limit_down INTEGER NOT NULL DEFAULT 0,
                total_amount REAL NOT NULL DEFAULT 0,
                up_ratio REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "market_daily", MARKET_DAILY_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_daily_date "
            "ON market_daily(trade_date DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_boards (
                board_code TEXT PRIMARY KEY,
                board_name TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'em',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "industry_boards", INDUSTRY_BOARDS_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_board_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pct_chg REAL,
                amount REAL,
                turnover_rate REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(board_code, trade_date)
            )
            """
        )
        _ensure_columns(connection, "industry_board_bars", INDUSTRY_BOARD_BARS_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_industry_board_bars_code_date "
            "ON industry_board_bars(board_code, trade_date)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_membership (
                board_code TEXT NOT NULL,
                symbol TEXT NOT NULL,
                as_of TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'em',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(board_code, symbol)
            )
            """
        )
        _ensure_columns(connection, "industry_membership", INDUSTRY_MEMBERSHIP_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_industry_membership_symbol "
            "ON industry_membership(symbol)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ml_datasets (
                dataset_id TEXT PRIMARY KEY,
                n_rows INTEGER NOT NULL DEFAULT 0,
                n_positive INTEGER NOT NULL DEFAULT 0,
                positive_rate REAL,
                start_date TEXT,
                end_date TEXT,
                max_trade_date TEXT,
                horizon INTEGER NOT NULL DEFAULT 5,
                aux_horizon INTEGER NOT NULL DEFAULT 10,
                index_code TEXT,
                industry_point_in_time INTEGER NOT NULL DEFAULT 0,
                csv_path TEXT,
                symbols_json TEXT,
                rejected_json TEXT,
                groups_json TEXT,
                feature_columns_json TEXT,
                cost_model_json TEXT,
                profile_snapshot_json TEXT,
                data_versions_json TEXT,
                warnings_json TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "ml_datasets", ML_DATASETS_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ml_models (
                model_id TEXT PRIMARY KEY,
                kind TEXT,
                target TEXT,
                horizon INTEGER NOT NULL DEFAULT 5,
                dataset_id TEXT,
                n_features INTEGER NOT NULL DEFAULT 0,
                threshold REAL,
                oos_brier REAL,
                oos_auc REAL,
                oos_logloss REAL,
                oos_ece REAL,
                oos_mae REAL,
                oos_r2 REAL,
                base_rate REAL,
                base_rate_brier REAL,
                coverage_at_threshold REAL,
                win_rate_at_threshold REAL,
                n_oos INTEGER NOT NULL DEFAULT 0,
                n_train INTEGER NOT NULL DEFAULT 0,
                n_rows INTEGER NOT NULL DEFAULT 0,
                n_positive INTEGER NOT NULL DEFAULT 0,
                max_trade_date TEXT,
                status TEXT NOT NULL DEFAULT 'candidate',
                stale INTEGER NOT NULL DEFAULT 0,
                artifact_path TEXT,
                sklearn_version TEXT,
                feature_groups_json TEXT,
                selected_params_json TEXT,
                metrics_json TEXT,
                warnings_json TEXT,
                trained_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "ml_models", ML_MODELS_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ml_models_status ON ml_models(status, target)"
        )
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


def upsert_index_daily(
    index_code: str,
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    """写入/更新某指数日线（C1）。

    ``rows`` 的 ``trade_date`` 须为 ``YYYYMMDD``（market_service 落库前统一归一化），
    成交量列接受 ``vol`` 或 ``volume``。按 ``(index_code, trade_date)`` upsert 幂等。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO index_daily (
                index_code, trade_date, open, high, low, close,
                pct_chg, amount, volume, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(index_code, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                pct_chg = excluded.pct_chg,
                amount = excluded.amount,
                volume = excluded.volume,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    index_code,
                    row.get("trade_date"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("pct_chg"),
                    row.get("amount"),
                    row.get("vol", row.get("volume")) or 0,
                    source,
                )
                for row in records
            ],
        )
    return len(records)


def load_index_bars(
    index_code: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """纯 DB 读取某指数日线（升序）。键与 ``load_daily_bars`` 对齐（成交量为 ``vol``）。

    离线只读，不触发任何网络。DB 优先 + 有界补拉的封装见
    ``data.market_service.load_index_bars``。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, pct_chg,
                   amount, volume AS vol, source
            FROM index_daily
            WHERE index_code = ?
            ORDER BY trade_date
            """,
            (index_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_market_daily(
    trade_date: str,
    breadth: Mapping[str, Any],
    source: str = "snapshot",
    path: Path | None = None,
) -> None:
    """写入/更新某交易日的市场宽度（C2）。

    ``breadth`` 取 ``aggregate_breadth`` 的规范输出（advancers/decliners/unchanged/
    limit_up/limit_down/total_amount/up_ratio）。按 ``trade_date`` 主键 upsert 幂等，
    同日重复刷新（盘中 provisional → 收盘 final）覆盖为最新终态。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO market_daily (
                trade_date, advancers, decliners, unchanged,
                limit_up, limit_down, total_amount, up_ratio, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(trade_date) DO UPDATE SET
                advancers = excluded.advancers,
                decliners = excluded.decliners,
                unchanged = excluded.unchanged,
                limit_up = excluded.limit_up,
                limit_down = excluded.limit_down,
                total_amount = excluded.total_amount,
                up_ratio = excluded.up_ratio,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                trade_date,
                int(breadth.get("advancers", 0) or 0),
                int(breadth.get("decliners", 0) or 0),
                int(breadth.get("unchanged", 0) or 0),
                int(breadth.get("limit_up", 0) or 0),
                int(breadth.get("limit_down", 0) or 0),
                float(breadth.get("total_amount", 0.0) or 0.0),
                breadth.get("up_ratio"),
                source,
            ),
        )


def load_market_daily(
    path: Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> List[Mapping[str, Any]]:
    """读取市场宽度历史（升序）。``start_date``/``end_date`` 为 ``YYYYMMDD`` 闭区间过滤。"""
    target = init_database(path)
    query = "SELECT * FROM market_daily"
    clauses: List[str] = []
    params: List[Any] = []
    if start_date:
        clauses.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("trade_date <= ?")
        params.append(end_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY trade_date"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# v14（C3）：行业板块基建（东财 akshare 为唯一主源）
# ---------------------------------------------------------------------------
def upsert_industry_boards(
    records: Iterable[Mapping[str, Any]],
    source: str = "em",
    path: Path | None = None,
) -> int:
    """写入/更新行业板块登记表（C3）。``records`` 形如 ``{"board_code", "board_name"}``。

    按 ``board_code`` 主键 upsert 幂等。
    """
    rows = list(records)
    if not rows:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO industry_boards (board_code, board_name, source, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(board_code) DO UPDATE SET
                board_name = excluded.board_name,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(record.get("board_code")),
                    str(record.get("board_name") or ""),
                    source,
                )
                for record in rows
                if record.get("board_code")
            ],
        )
    return len(rows)


def load_industry_boards(path: Path | None = None) -> List[Mapping[str, Any]]:
    """读取板块登记表（按 board_code 升序）。离线只读，不触发网络。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT board_code, board_name, source FROM industry_boards ORDER BY board_code"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_industry_board_bars(
    board_code: str,
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    """写入/更新某板块日线（C3）。``rows`` 的 ``trade_date`` 须为 ``YYYYMMDD``
    （industry_service 落库前统一归一化），按 ``(board_code, trade_date)`` upsert 幂等。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO industry_board_bars (
                board_code, trade_date, open, high, low, close,
                pct_chg, amount, turnover_rate, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(board_code, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                pct_chg = excluded.pct_chg,
                amount = excluded.amount,
                turnover_rate = excluded.turnover_rate,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    board_code,
                    row.get("trade_date"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("pct_chg"),
                    row.get("amount"),
                    row.get("turnover_rate"),
                    source,
                )
                for row in records
            ],
        )
    return len(records)


def load_industry_board_bars(
    board_code: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """纯 DB 读取某板块日线（升序）。离线只读，不触发网络。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, pct_chg,
                   amount, turnover_rate, source
            FROM industry_board_bars
            WHERE board_code = ?
            ORDER BY trade_date
            """,
            (board_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_industry_membership(
    board_code: str,
    symbols: Iterable[str],
    as_of: str,
    source: str = "em",
    path: Path | None = None,
) -> int:
    """写入/更新某板块成分股最新快照（C3）。

    按 ``(board_code, symbol)`` upsert，``as_of`` 记录观察日（``YYYYMMDD``）。现状单快照
    （非逐日 point-in-time）：成分变动靠下次刷新覆盖，离板旧行可能残留（D 阶段按
    as_of<=trade_date 取最新并标前视警告，诚实标注此局限）。
    """
    unique_symbols = sorted({str(symbol) for symbol in symbols if symbol})
    if not unique_symbols:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO industry_membership (board_code, symbol, as_of, source, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(board_code, symbol) DO UPDATE SET
                as_of = excluded.as_of,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [(board_code, symbol, as_of, source) for symbol in unique_symbols],
        )
    return len(unique_symbols)


def load_industry_membership(
    board_code: str | None = None, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """读取成分股快照。给定 ``board_code`` 只返回该板块，否则全表（按 board_code, symbol）。"""
    target = init_database(path)
    query = "SELECT board_code, symbol, as_of, source FROM industry_membership"
    params: List[Any] = []
    if board_code:
        query += " WHERE board_code = ?"
        params.append(board_code)
    query += " ORDER BY board_code, symbol"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def industry_board_for_symbol(symbol: str, path: Path | None = None) -> str | None:
    """返回某股票所属板块代码（成分快照中 as_of 最新者；无则 None）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        row = connection.execute(
            "SELECT board_code FROM industry_membership WHERE symbol = ? "
            "ORDER BY as_of DESC, board_code LIMIT 1",
            (symbol,),
        ).fetchone()
    return row[0] if row else None


def stock_catalog_industries(path: Path | None = None) -> Mapping[str, str]:
    """轻量读取 ``stock_catalog`` 的 symbol→industry 静态标签（C3 兜底模糊匹配用）。

    东财成分快照拉取失败时，用此静态标签按板块名模糊匹配兜底；匹配不上则该股票行业
    特征缺失（绝不造假）。只返回 industry 非空的条目。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute(
            "SELECT symbol, industry FROM stock_catalog "
            "WHERE industry IS NOT NULL AND industry <> ''"
        ).fetchall()
    return {str(symbol): str(industry) for symbol, industry in rows}


# ---------------------------------------------------------------------------
# v15（D2）：ML 数据集 manifest 落库
# ---------------------------------------------------------------------------
# manifest 中列表/字典字段以 JSON 文本存储；register 序列化、load/list 反序列化，
# 调用方（ml.dataset）始终拿到/传入原生 Python 对象。
_DATASET_JSON_FIELDS = (
    "symbols",
    "rejected",
    "groups",
    "feature_columns",
    "cost_model",
    "profile_snapshot",
    "data_versions",
    "warnings",
)


def register_dataset(manifest: Mapping[str, Any], path: Path | None = None) -> str:
    """写入/更新 ``ml_datasets``（D2）。按 ``dataset_id`` upsert 幂等，返回 dataset_id。

    ``manifest`` 的列表/字典字段（symbols/rejected/groups/feature_columns/cost_model/
    profile_snapshot/data_versions/warnings）以 JSON 编码落库；标量字段直接存。
    """
    dataset_id = str(manifest["dataset_id"])

    def _dump(key: str) -> str:
        return json.dumps(manifest.get(key), ensure_ascii=False, default=str)

    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO ml_datasets (
                dataset_id, n_rows, n_positive, positive_rate,
                start_date, end_date, max_trade_date, horizon, aux_horizon,
                index_code, industry_point_in_time, csv_path,
                symbols_json, rejected_json, groups_json, feature_columns_json,
                cost_model_json, profile_snapshot_json, data_versions_json,
                warnings_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(dataset_id) DO UPDATE SET
                n_rows = excluded.n_rows,
                n_positive = excluded.n_positive,
                positive_rate = excluded.positive_rate,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                max_trade_date = excluded.max_trade_date,
                horizon = excluded.horizon,
                aux_horizon = excluded.aux_horizon,
                index_code = excluded.index_code,
                industry_point_in_time = excluded.industry_point_in_time,
                csv_path = excluded.csv_path,
                symbols_json = excluded.symbols_json,
                rejected_json = excluded.rejected_json,
                groups_json = excluded.groups_json,
                feature_columns_json = excluded.feature_columns_json,
                cost_model_json = excluded.cost_model_json,
                profile_snapshot_json = excluded.profile_snapshot_json,
                data_versions_json = excluded.data_versions_json,
                warnings_json = excluded.warnings_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                dataset_id,
                int(manifest.get("n_rows", 0) or 0),
                int(manifest.get("n_positive", 0) or 0),
                manifest.get("positive_rate"),
                manifest.get("start_date"),
                manifest.get("end_date"),
                manifest.get("max_trade_date"),
                int(manifest.get("horizon", 5) or 5),
                int(manifest.get("aux_horizon", 10) or 10),
                manifest.get("index_code"),
                1 if manifest.get("industry_point_in_time") else 0,
                manifest.get("csv_path"),
                _dump("symbols"),
                _dump("rejected"),
                _dump("groups"),
                _dump("feature_columns"),
                _dump("cost_model"),
                _dump("profile_snapshot"),
                _dump("data_versions"),
                _dump("warnings"),
            ),
        )
    return dataset_id


def _row_to_dataset_manifest(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ml_datasets 行解码为原生 manifest dict（JSON 字段反序列化）。"""
    data = dict(row)  # sqlite3.Row 无 .get()，先转 dict

    def _load(key: str) -> Any:
        raw = data.get(f"{key}_json")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    manifest: Dict[str, Any] = {
        "dataset_id": data["dataset_id"],
        "n_rows": data["n_rows"],
        "n_positive": data["n_positive"],
        "positive_rate": data["positive_rate"],
        "start_date": data["start_date"],
        "end_date": data["end_date"],
        "max_trade_date": data["max_trade_date"],
        "horizon": data["horizon"],
        "aux_horizon": data["aux_horizon"],
        "index_code": data["index_code"],
        "industry_point_in_time": bool(data["industry_point_in_time"]),
        "csv_path": data["csv_path"],
        "created_at": data.get("created_at"),
    }
    for key in _DATASET_JSON_FIELDS:
        manifest[key] = _load(key)
    return manifest


def load_dataset_manifest(
    dataset_id: str, path: Path | None = None
) -> Optional[Dict[str, Any]]:
    """读取某数据集 manifest（JSON 字段解码）；不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM ml_datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    return _row_to_dataset_manifest(row) if row else None


def list_datasets(path: Path | None = None) -> List[Dict[str, Any]]:
    """列出全部数据集 manifest（按 created_at 降序、再按 dataset_id），JSON 字段解码。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM ml_datasets ORDER BY created_at DESC, dataset_id DESC"
        ).fetchall()
    return [_row_to_dataset_manifest(row) for row in rows]


_MODEL_JSON_FIELDS = (
    "feature_groups",
    "selected_params",
    "metrics",
    "warnings",
)

# 标量列（与 ML_MODELS_COLUMNS 对应，JSON 列单独处理）
_MODEL_SCALAR_FIELDS = (
    "model_id", "kind", "target", "horizon", "dataset_id", "n_features", "threshold",
    "oos_brier", "oos_auc", "oos_logloss", "oos_ece", "oos_mae", "oos_r2",
    "base_rate", "base_rate_brier", "coverage_at_threshold", "win_rate_at_threshold",
    "n_oos", "n_train", "n_rows", "n_positive", "max_trade_date",
    "status", "stale", "artifact_path", "sklearn_version", "trained_at",
)


def register_model(model: Mapping[str, Any], path: Path | None = None) -> str:
    """写入/更新 ``ml_models``（D3）。按 ``model_id`` upsert 幂等，返回 model_id。

    ``model`` 的 list/dict 字段（feature_groups/selected_params/metrics/warnings）以 JSON
    编码落库；标量字段直接存。新建默认 ``status='candidate'``（调用方可显式覆盖）。
    """
    model_id = str(model["model_id"])

    def _dump(key: str) -> str:
        return json.dumps(model.get(key), ensure_ascii=False, default=str)

    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO ml_models (
                model_id, kind, target, horizon, dataset_id, n_features, threshold,
                oos_brier, oos_auc, oos_logloss, oos_ece, oos_mae, oos_r2,
                base_rate, base_rate_brier, coverage_at_threshold, win_rate_at_threshold,
                n_oos, n_train, n_rows, n_positive, max_trade_date,
                status, stale, artifact_path, sklearn_version,
                feature_groups_json, selected_params_json, metrics_json, warnings_json,
                trained_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT(model_id) DO UPDATE SET
                kind = excluded.kind,
                target = excluded.target,
                horizon = excluded.horizon,
                dataset_id = excluded.dataset_id,
                n_features = excluded.n_features,
                threshold = excluded.threshold,
                oos_brier = excluded.oos_brier,
                oos_auc = excluded.oos_auc,
                oos_logloss = excluded.oos_logloss,
                oos_ece = excluded.oos_ece,
                oos_mae = excluded.oos_mae,
                oos_r2 = excluded.oos_r2,
                base_rate = excluded.base_rate,
                base_rate_brier = excluded.base_rate_brier,
                coverage_at_threshold = excluded.coverage_at_threshold,
                win_rate_at_threshold = excluded.win_rate_at_threshold,
                n_oos = excluded.n_oos,
                n_train = excluded.n_train,
                n_rows = excluded.n_rows,
                n_positive = excluded.n_positive,
                max_trade_date = excluded.max_trade_date,
                status = excluded.status,
                stale = excluded.stale,
                artifact_path = excluded.artifact_path,
                sklearn_version = excluded.sklearn_version,
                feature_groups_json = excluded.feature_groups_json,
                selected_params_json = excluded.selected_params_json,
                metrics_json = excluded.metrics_json,
                warnings_json = excluded.warnings_json,
                trained_at = excluded.trained_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                model_id,
                model.get("kind"),
                model.get("target"),
                int(model.get("horizon", 5) or 5),
                model.get("dataset_id"),
                int(model.get("n_features", 0) or 0),
                model.get("threshold"),
                model.get("oos_brier"),
                model.get("oos_auc"),
                model.get("oos_logloss"),
                model.get("oos_ece"),
                model.get("oos_mae"),
                model.get("oos_r2"),
                model.get("base_rate"),
                model.get("base_rate_brier"),
                model.get("coverage_at_threshold"),
                model.get("win_rate_at_threshold"),
                int(model.get("n_oos", 0) or 0),
                int(model.get("n_train", 0) or 0),
                int(model.get("n_rows", 0) or 0),
                int(model.get("n_positive", 0) or 0),
                model.get("max_trade_date"),
                str(model.get("status", "candidate")),
                1 if model.get("stale") else 0,
                model.get("artifact_path"),
                model.get("sklearn_version"),
                _dump("feature_groups"),
                _dump("selected_params"),
                _dump("metrics"),
                _dump("warnings"),
                model.get("trained_at"),
            ),
        )
    return model_id


def _row_to_model(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ml_models 行解码为原生 dict（JSON 字段反序列化）。"""
    data = dict(row)  # sqlite3.Row 无 .get()，先转 dict

    def _load(key: str) -> Any:
        raw = data.get(f"{key}_json")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    model: Dict[str, Any] = {field: data.get(field) for field in _MODEL_SCALAR_FIELDS}
    model["stale"] = bool(data.get("stale"))
    model["created_at"] = data.get("created_at")
    model["updated_at"] = data.get("updated_at")
    for key in _MODEL_JSON_FIELDS:
        model[key] = _load(key)
    return model


def load_model(model_id: str, path: Path | None = None) -> Optional[Dict[str, Any]]:
    """读取某模型行（JSON 字段解码）；不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM ml_models WHERE model_id = ?", (model_id,)
        ).fetchone()
    return _row_to_model(row) if row else None


def list_models(
    path: Path | None = None, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """列出模型（按 trained_at 降序）；``status`` 非空时过滤。"""
    target = init_database(path)
    query = "SELECT * FROM ml_models"
    params: Tuple[Any, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY trained_at DESC, model_id DESC"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [_row_to_model(row) for row in rows]


def set_model_status(
    model_id: str,
    status: str,
    *,
    stale: Optional[bool] = None,
    warnings: Optional[Sequence[str]] = None,
    path: Path | None = None,
) -> None:
    """更新模型 ``status``（晋升/退役/标 demo），可同步 ``stale`` 与 ``warnings``。"""
    target = init_database(path)
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: List[Any] = [status]
    if stale is not None:
        sets.append("stale = ?")
        params.append(1 if stale else 0)
    if warnings is not None:
        sets.append("warnings_json = ?")
        params.append(json.dumps(list(warnings), ensure_ascii=False, default=str))
    params.append(model_id)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            f"UPDATE ml_models SET {', '.join(sets)} WHERE model_id = ?", tuple(params)
        )


def retire_promoted_models(
    target_name: str,
    horizon: int,
    *,
    exclude_model_id: Optional[str] = None,
    path: Path | None = None,
) -> int:
    """把同 (target, horizon) 的其它 promoted/demo 模型退役，保证每个目标只有一个在位。"""
    db = init_database(path)
    with sqlite3.connect(db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT model_id FROM ml_models WHERE target = ? AND horizon = ? "
            "AND status IN ('promoted', 'demo')",
            (target_name, int(horizon)),
        ).fetchall()
        retired = [
            r["model_id"] for r in rows if r["model_id"] != exclude_model_id
        ]
        for mid in retired:
            connection.execute(
                "UPDATE ml_models SET status = 'retired', updated_at = CURRENT_TIMESTAMP "
                "WHERE model_id = ?",
                (mid,),
            )
    return len(retired)


def load_promoted_model(
    target_name: Optional[str] = None,
    *,
    include_demo: bool = False,
    path: Path | None = None,
) -> Optional[Dict[str, Any]]:
    """取在位模型（``status='promoted'``，可选含 ``demo``）；无则 ``None``。

    多个时取 trained_at 最新的一个。``target_name`` 非空时按目标过滤（D5 scoring 默认
    取 ``win5`` 分类模型）。
    """
    db = init_database(path)
    statuses = ("promoted", "demo") if include_demo else ("promoted",)
    placeholders = ",".join("?" for _ in statuses)
    query = f"SELECT * FROM ml_models WHERE status IN ({placeholders})"
    params: List[Any] = list(statuses)
    if target_name:
        query += " AND target = ?"
        params.append(target_name)
    query += " ORDER BY trained_at DESC, model_id DESC LIMIT 1"
    with sqlite3.connect(db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(query, tuple(params)).fetchone()
    return _row_to_model(row) if row else None


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
