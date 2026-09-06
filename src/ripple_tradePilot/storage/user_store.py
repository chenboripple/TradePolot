from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ripple_tradePilot.storage.database import database_path, init_database


PASSWORD_ITERATIONS = 390_000
SESSION_DAYS = 14


class UserStoreError(RuntimeError):
    pass


class UsernameTakenError(UserStoreError):
    pass


class StrategyNotFoundError(UserStoreError):
    pass


class WatchlistExistsError(UserStoreError):
    pass


class WatchlistNotFoundError(UserStoreError):
    pass


class BacktestNotFoundError(UserStoreError):
    pass


def _target(path: Path | None) -> Path:
    return init_database(path or database_path())


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _public_user(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


def create_user(username: str, password: str, path: Path | None = None) -> Dict[str, Any]:
    target = _target(path)
    try:
        with sqlite3.connect(target, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            role = (
                "admin"
                if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                else "user"
            )
            cursor = connection.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, _password_hash(password), role),
            )
            row = connection.execute(
                "SELECT id, username, role, created_at FROM users WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise UsernameTakenError("用户名已被使用") from error
    return _public_user(row)


def authenticate_user(
    username: str, password: str, path: Path | None = None
) -> Optional[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not _password_matches(password, row["password_hash"]):
        return None
    return _public_user(row)


def create_session(user_id: int, path: Path | None = None) -> str:
    target = _target(path)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            "DELETE FROM user_sessions WHERE expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        connection.execute(
            "INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (user_id, token_hash, expires_at.isoformat()),
        )
    return token


def user_for_session(token: str, path: Path | None = None) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    target = _target(path)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT users.id, users.username, users.role, users.created_at
            FROM user_sessions
            JOIN users ON users.id = user_sessions.user_id
            WHERE user_sessions.token_hash = ? AND user_sessions.expires_at > ?
            """,
            (token_hash, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    return _public_user(row) if row else None


def delete_session(token: str, path: Path | None = None) -> None:
    if not token:
        return
    target = _target(path)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            "DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,)
        )


def create_strategy(
    user_id: int, strategy: Dict[str, Any], path: Path | None = None
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            INSERT INTO strategies (
                user_id, name, asset_class, symbol, profile,
                parameters_json, visibility
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                strategy["name"],
                strategy["asset_class"],
                strategy["symbol"],
                strategy["profile"],
                json.dumps(strategy["parameters"], ensure_ascii=False, separators=(",", ":")),
                strategy["visibility"],
            ),
        )
        row = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    return _strategy_dict(row, user_id)


def _strategy_dict(row: sqlite3.Row, viewer_id: int) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "asset_class": row["asset_class"],
        "symbol": row["symbol"],
        "profile": row["profile"],
        "parameters": json.loads(row["parameters_json"]),
        "visibility": row["visibility"],
        "owner": row["owner_username"],
        "is_owner": row["user_id"] == viewer_id,
        "is_system": bool(row["system_key"]),
        "system_key": row["system_key"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ensure_system_strategies(
    owner_username: str,
    strategies: List[Dict[str, Any]],
    path: Path | None = None,
) -> List[str]:
    """Create editable DB-backed copies once the configured owner exists."""
    if not owner_username or not strategies:
        return []
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        owner = connection.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (owner_username,),
        ).fetchone()
        if owner is None:
            return []
        bound_keys = []
        for strategy in strategies:
            system_key = strategy["system_key"]
            existing = connection.execute(
                "SELECT id, user_id FROM strategies WHERE system_key = ?",
                (system_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO strategies (
                        user_id, name, asset_class, symbol, profile,
                        parameters_json, visibility, system_key
                    ) VALUES (?, ?, ?, ?, ?, ?, 'public', ?)
                    """,
                    (
                        owner["id"],
                        strategy["name"],
                        strategy["asset_class"],
                        strategy["symbol"],
                        strategy["profile"],
                        json.dumps(
                            strategy["parameters"],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        system_key,
                    ),
                )
            elif existing["user_id"] != owner["id"]:
                connection.execute(
                    """
                    UPDATE strategies
                    SET user_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (owner["id"], existing["id"]),
                )
            bound_keys.append(system_key)
    return bound_keys


def get_system_strategy(
    symbol: str,
    asset_class: str = "stock",
    path: Path | None = None,
) -> Optional[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.system_key = ?
            """,
            (f"{asset_class}:{symbol.upper()}",),
        ).fetchone()
    return _strategy_dict(row, row["user_id"]) if row else None


def list_visible_strategies(
    viewer_id: int, path: Path | None = None
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.user_id = ? OR strategies.visibility = 'public'
            ORDER BY strategies.user_id = ? DESC, strategies.updated_at DESC
            """,
            (viewer_id, viewer_id),
        ).fetchall()
    return [_strategy_dict(row, viewer_id) for row in rows]


def update_strategy_visibility(
    strategy_id: int,
    user_id: int,
    visibility: str,
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            UPDATE strategies
            SET visibility = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (visibility, strategy_id, user_id),
        )
        if cursor.rowcount == 0:
            raise StrategyNotFoundError("策略不存在或无权修改")
        row = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.id = ?
            """,
            (strategy_id,),
        ).fetchone()
    return _strategy_dict(row, user_id)


def update_strategy(
    strategy_id: int,
    user_id: int,
    strategy: Dict[str, Any],
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            UPDATE strategies
            SET name = ?, profile = ?, parameters_json = ?, visibility = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (
                strategy["name"],
                strategy["profile"],
                json.dumps(
                    strategy["parameters"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                strategy["visibility"],
                strategy_id,
                user_id,
            ),
        )
        if cursor.rowcount == 0:
            raise StrategyNotFoundError("策略不存在或无权修改")
        row = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.id = ?
            """,
            (strategy_id,),
        ).fetchone()
    return _strategy_dict(row, user_id)


def record_backtest_run(
    backtest: Dict[str, Any],
    path: Path | None = None,
    *,
    user_id: Optional[int] = None,
    run_kind: str = "backtest",
    params_json: Optional[str] = None,
    profile_source: Optional[str] = None,
    report_json: Optional[str] = None,
) -> int:
    """把一次回测 / walk-forward 结果写入 backtest_results，返回记录 id。

    Web 端传 ``user_id``（用户记录），CLI 端不传（``user_id`` 为 NULL，非用户记录）；
    ``list_user_backtests`` 按 user_id 过滤，故 NULL 行不进任何用户列表。

    ``backtest`` 字典需提供 symbol/name/start_date/end_date 等字段（含 result_json），
    数值字段缺省时写 0，保证回测记录页始终有可展示的行；strategy_key/bar_count/execution
    记录回测入参，供前端按原参数重跑与历史回放。

    A8 溯源列：``run_kind``='backtest'|'walkforward'；``params_json`` 记录入参；
    ``profile_source`` 记录画像来源（A5：explicit/system/config:名字/default）；
    walk-forward 的完整分段报告序列化进 ``report_json``。
    """
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        cursor = connection.execute(
            """
            INSERT INTO backtest_results (
                user_id, symbol, name, start_date, end_date,
                initial_capital, final_capital, total_return, annual_return,
                max_drawdown, sharpe_ratio, total_trades, win_rate,
                created_at, strategy_id, strategy_key, bar_count, execution, result_json,
                run_kind, params_json, profile_source, report_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                user_id,
                backtest.get("symbol", ""),
                backtest.get("name", ""),
                backtest.get("start_date", ""),
                backtest.get("end_date", ""),
                backtest.get("initial_capital", 0.0),
                backtest.get("final_capital", 0.0),
                backtest.get("total_return", 0.0),
                backtest.get("annual_return", 0.0),
                backtest.get("max_drawdown", 0.0),
                backtest.get("sharpe_ratio", 0.0),
                backtest.get("total_trades", 0),
                backtest.get("win_rate", 0.0),
                backtest.get("strategy_id"),
                backtest.get("strategy_key"),
                backtest.get("bar_count", 0),
                backtest.get("execution", ""),
                backtest.get("result_json"),
                run_kind,
                params_json,
                profile_source,
                report_json,
            ),
        )
        return int(cursor.lastrowid)


def list_backtest_runs(
    path: Path | None = None,
    *,
    kind: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """列出 CLI 回测 / walk-forward 记录（user_id 为 NULL 的运行），按 id 倒序。

    ``kind`` 给定时只返回该 run_kind。供 ``tradepilot backtests list`` 使用；
    不含 result_json/report_json 大字段，详情按需另取。
    """
    target = _target(path)
    query = """
        SELECT id, symbol, name, run_kind, strategy_key, start_date, end_date,
               total_return, annual_return, max_drawdown, sharpe_ratio,
               total_trades, win_rate, bar_count, execution, profile_source,
               params_json, created_at
        FROM backtest_results
        WHERE user_id IS NULL
    """
    params: List[Any] = []
    if kind:
        query += " AND run_kind = ?"
        params.append(kind)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_backtest_run(
    run_id: int, path: Path | None = None
) -> Dict[str, Any] | None:
    """按 id 取单条 CLI 运行记录（含 result_json/report_json 大字段）。"""
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM backtest_results WHERE id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def get_user_backtest(
    backtest_id: int, user_id: int, path: Path | None = None
) -> Dict[str, Any] | None:
    """取单条回测记录（含 result_json）；不存在或属于他人时返回 None。"""
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT backtest_results.*, strategies.name AS strategy_name
            FROM backtest_results
            LEFT JOIN strategies ON strategies.id = backtest_results.strategy_id
            WHERE backtest_results.id = ? AND backtest_results.user_id = ?
            """,
            (backtest_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_user_backtests(
    user_id: int, path: Path | None = None, limit: int = 100
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT backtest_results.*, strategies.name AS strategy_name
            FROM backtest_results
            LEFT JOIN strategies ON strategies.id = backtest_results.strategy_id
            WHERE backtest_results.user_id = ?
            ORDER BY backtest_results.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_user_backtest(
    backtest_id: int, user_id: int, path: Path | None = None
) -> bool:
    """删除属于指定用户的回测记录，返回是否真正删除了行。

    记录不存在或属于其他用户时不删除任何行（返回 False），
    由调用方据此区分 204/404。
    """
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        cursor = connection.execute(
            "DELETE FROM backtest_results WHERE id = ? AND user_id = ?",
            (backtest_id, user_id),
        )
        return cursor.rowcount > 0


def _watchlist_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "name": row["name"],
        "is_watched": bool(row["is_watched"]),
        "created_at": row["created_at"],
        "last_updated_at": row["last_updated_at"],
        "default_strategy_id": row["default_strategy_id"],
        "user_added": True,
    }


def list_user_watchlist(
    user_id: int, path: Path | None = None
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, symbol, name, is_watched, created_at, last_updated_at,
                   default_strategy_id
            FROM user_watchlist
            WHERE user_id = ? AND is_watched = 1
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()
    return [_watchlist_dict(row) for row in rows]


def list_all_watched_symbols(path: Path | None = None) -> List[Dict[str, str]]:
    """所有用户观察中的标的（去重），供监控进程联动使用。"""
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT symbol, MAX(name) AS name
            FROM user_watchlist
            WHERE is_watched = 1
            GROUP BY symbol
            ORDER BY symbol
            """
        ).fetchall()
    return [{"symbol": row["symbol"], "name": row["name"]} for row in rows]


def list_user_stocks(
    user_id: int, path: Path | None = None
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, symbol, name, is_watched, created_at, last_updated_at,
                   default_strategy_id
            FROM user_watchlist
            WHERE user_id = ?
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()
    return [_watchlist_dict(row) for row in rows]


def add_watchlist_item(
    user_id: int,
    symbol: str,
    name: str,
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    try:
        with sqlite3.connect(target, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                """
                INSERT INTO user_watchlist (
                    user_id, symbol, name, is_watched, last_updated_at
                ) VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                """,
                (user_id, symbol, name),
            )
            row = connection.execute(
                """
                SELECT id, symbol, name, is_watched, created_at, last_updated_at,
                       default_strategy_id
                FROM user_watchlist WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise WatchlistExistsError("该股票已在观察池中") from error
    return _watchlist_dict(row)


def upsert_watchlist_item(
    user_id: int,
    symbol: str,
    name: str,
    is_watched: bool,
    *,
    mark_updated: bool = False,
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            INSERT INTO user_watchlist (
                user_id, symbol, name, is_watched, last_updated_at
            ) VALUES (?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP END)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                name = excluded.name,
                is_watched = excluded.is_watched,
                last_updated_at = CASE
                    WHEN ? THEN CURRENT_TIMESTAMP
                    ELSE user_watchlist.last_updated_at
                END
            """,
            (
                user_id,
                symbol,
                name,
                int(is_watched),
                int(mark_updated),
                int(mark_updated),
            ),
        )
        row = connection.execute(
            """
            SELECT id, symbol, name, is_watched, created_at, last_updated_at,
                   default_strategy_id
            FROM user_watchlist
            WHERE user_id = ? AND symbol = ?
            """,
            (user_id, symbol),
        ).fetchone()
    return _watchlist_dict(row)


def set_watchlist_default_strategy(
    user_id: int,
    symbol: str,
    strategy_id: Optional[int],
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            UPDATE user_watchlist
            SET default_strategy_id = ?
            WHERE user_id = ? AND symbol = ? AND is_watched = 1
            """,
            (strategy_id, user_id, symbol),
        )
        if cursor.rowcount == 0:
            raise WatchlistNotFoundError("观察池中不存在该股票")
        row = connection.execute(
            """
            SELECT id, symbol, name, is_watched, created_at, last_updated_at,
                   default_strategy_id
            FROM user_watchlist
            WHERE user_id = ? AND symbol = ?
            """,
            (user_id, symbol),
        ).fetchone()
    return _watchlist_dict(row)


def delete_watchlist_item(
    user_id: int, symbol: str, path: Path | None = None
) -> None:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        cursor = connection.execute(
            """
            UPDATE user_watchlist SET is_watched = 0
            WHERE user_id = ? AND symbol = ? AND is_watched = 1
            """,
            (user_id, symbol),
        )
        if cursor.rowcount == 0:
            raise WatchlistNotFoundError("观察池中不存在该股票")
