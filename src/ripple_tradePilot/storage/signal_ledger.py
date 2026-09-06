"""信号台账（B2 · schema v13）。

把每一次"决策"落成一行可追溯、可回填、可统计的记录，闭合"预测 → 验证"的回路：

    决策当时（T 日）        未来 bar 入库后           汇总
    record_signal/   ──▶   backfill_outcomes  ──▶   signal_stats
    record_decision         （用 B1 horizon_label     （coverage / 胜率 /
    （pending）              回填 fwd_net_return）      平均净收益）

为什么需要它：改造前系统只在通知里"喊单"，从不记录喊过什么、后来对没对，胜率/期望
无从谈起，更无法防"几乎不交易刷高胜率"。台账把信号与其**前瞻净收益**绑定，是 D 阶段
训练数据的真实来源之一，也是诚实绩效的底座。

设计要点：

- **幂等 upsert**：冲突键 ``(symbol, trade_date, source, provisional)``；monitor 重启
  重复记录同一信号不会产生重复行。冲突时只更新决策/模型字段，**不触碰已回填的结果列**
  （``fwd_*``/``exit_price``/``label_status``/``filled_at``），重复记录不会把已完成的
  回填重置。
- **provisional 0/1 各一条**：盘中预估 bar 与收盘确认 bar 分别落库，互不覆盖。
- **回填诚实**：决策日在库但未来 bar 不足以走完整 horizon → 保持 ``pending``（绝不
  截断尾价冒充）；决策日不在库 → ``bad_data``。
- **松耦合**：与 ``backtest_results``（run 粒度）、``paper_ledger``（fill 粒度）通过
  ``backtest_id``/``symbol``/``trade_date`` 关联，不合并表。
- ``kv_store``：通用键值表，monitor 收盘例程记录"上次执行日"等状态防重启重复跑。

口径与 ``docs/prediction-target.md`` 一致：entry=open[T+1]、持有 horizon、exit=open[T+1+horizon]、
净收益镜像 :class:`~ripple_tradePilot.backtest.costs.CostModel`。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .database import init_database, load_daily_bars

__all__ = [
    "ACTIONABLE_RECOMMENDATIONS",
    "record_signal",
    "record_decision",
    "list_signals",
    "list_pending",
    "get_signal",
    "backfill_outcomes",
    "signal_stats",
    "kv_get",
    "kv_set",
    "kv_delete",
]

# 可执行（产生入场动作）的 recommendation；coverage 的分子。
ACTIONABLE_RECOMMENDATIONS = ("BUY", "SELL")


def _normalize_trade_date(value: Any) -> str:
    """把 datetime/date/str 统一成 ``YYYYMMDD``（与 daily_bars.trade_date 同口径）。"""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return text.replace("-", "")


def record_signal(
    symbol: str,
    trade_date: Any,
    *,
    source: str = "monitor",
    provisional: int = 0,
    profile_name: Optional[str] = None,
    params_json: Optional[str] = None,
    vote_threshold: Optional[int] = None,
    recommendation: Optional[str] = None,
    buy_count: Optional[int] = None,
    sell_count: Optional[int] = None,
    components_json: Optional[str] = None,
    features_json: Optional[str] = None,
    model_id: Optional[str] = None,
    p_win: Optional[float] = None,
    expected_ret: Optional[float] = None,
    downside_mae: Optional[float] = None,
    entry_price: Optional[float] = None,
    horizon: int = 5,
    data_version: Optional[str] = None,
    backtest_id: Optional[int] = None,
    path: Optional[Path] = None,
) -> int:
    """幂等写入一条信号台账，返回 row id。

    新行 ``label_status='pending'``；冲突（同 symbol/trade_date/source/provisional）时
    只更新决策与模型字段，保留已回填的结果列（见模块 docstring）。
    """
    target = init_database(path)
    td = _normalize_trade_date(trade_date)
    prov = 1 if provisional else 0
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO signal_ledger (
                symbol, trade_date, source, provisional, profile_name, params_json,
                vote_threshold, recommendation, buy_count, sell_count, components_json,
                features_json, model_id, p_win, expected_ret, downside_mae,
                entry_price, horizon, data_version, backtest_id, label_status, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'pending', CURRENT_TIMESTAMP
            )
            ON CONFLICT(symbol, trade_date, source, provisional) DO UPDATE SET
                profile_name = excluded.profile_name,
                params_json = excluded.params_json,
                vote_threshold = excluded.vote_threshold,
                recommendation = excluded.recommendation,
                buy_count = excluded.buy_count,
                sell_count = excluded.sell_count,
                components_json = excluded.components_json,
                features_json = excluded.features_json,
                model_id = excluded.model_id,
                p_win = excluded.p_win,
                expected_ret = excluded.expected_ret,
                downside_mae = excluded.downside_mae,
                entry_price = excluded.entry_price,
                horizon = excluded.horizon,
                data_version = excluded.data_version,
                backtest_id = excluded.backtest_id
            """,
            (
                symbol, td, source, prov, profile_name, params_json,
                vote_threshold, recommendation, buy_count, sell_count,
                components_json, features_json, model_id, p_win, expected_ret,
                downside_mae, entry_price, int(horizon), data_version, backtest_id,
            ),
        )
        row = connection.execute(
            "SELECT id FROM signal_ledger "
            "WHERE symbol = ? AND trade_date = ? AND source = ? AND provisional = ?",
            (symbol, td, source, prov),
        ).fetchone()
    return int(row[0])


def record_decision(
    symbol: str,
    decision: Any,
    *,
    source: str = "monitor",
    provisional: int = 0,
    profile_name: Optional[str] = None,
    params_json: Optional[str] = None,
    features_json: Optional[str] = None,
    model_id: Optional[str] = None,
    p_win: Optional[float] = None,
    expected_ret: Optional[float] = None,
    downside_mae: Optional[float] = None,
    entry_price: Optional[float] = None,
    horizon: int = 5,
    data_version: Optional[str] = None,
    backtest_id: Optional[int] = None,
    trade_date: Optional[Any] = None,
    path: Optional[Path] = None,
) -> int:
    """把 ``signals.VoteDecision`` 写入台账（A3 收盘例程 / A5 回测写入方共用入口）。

    ``trade_date`` 缺省取 ``decision.timestamp``；``components`` 序列化为
    ``[{name, kind, side, strength}]`` 存 ``components_json``。
    """
    td = trade_date if trade_date is not None else decision.timestamp
    components = [
        {
            "name": component.name,
            "kind": component.kind,
            "side": (component.side.value if component.side is not None else None),
            "strength": component.strength,
        }
        for component in decision.components
    ]
    return record_signal(
        symbol,
        td,
        source=source,
        provisional=provisional,
        profile_name=profile_name,
        params_json=params_json,
        vote_threshold=decision.vote_threshold,
        recommendation=decision.recommendation,
        buy_count=decision.buy_count,
        sell_count=decision.sell_count,
        components_json=json.dumps(components, ensure_ascii=False),
        features_json=features_json,
        model_id=model_id,
        p_win=p_win,
        expected_ret=expected_ret,
        downside_mae=downside_mae,
        entry_price=entry_price,
        horizon=horizon,
        data_version=data_version,
        backtest_id=backtest_id,
        path=path,
    )


def list_signals(
    *,
    status: Optional[str] = None,
    source: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: Optional[int] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """按条件列出台账行（全字段），默认按 trade_date 倒序。"""
    target = init_database(path)
    query = "SELECT * FROM signal_ledger WHERE 1 = 1"
    params: List[Any] = []
    if status is not None:
        query += " AND label_status = ?"
        params.append(status)
    if source is not None:
        query += " AND source = ?"
        params.append(source)
    if symbol is not None:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY trade_date DESC, symbol ASC, id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def list_pending(
    *,
    source: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: Optional[int] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """列出 ``label_status='pending'`` 的待回填行。"""
    return list_signals(
        status="pending", source=source, symbol=symbol, limit=limit, path=path
    )


def get_signal(signal_id: int, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """按 id 取单行（全字段），不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM signal_ledger WHERE id = ?", (int(signal_id),)
        ).fetchone()
    return dict(row) if row else None


def _set_status(target: Path, signal_id: int, status: str) -> None:
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            "UPDATE signal_ledger SET label_status = ? WHERE id = ?",
            (status, int(signal_id)),
        )


def _fill_outcome(
    target: Path,
    signal_id: int,
    *,
    entry: float,
    exit_price: float,
    fwd_net_return: float,
    fwd_mae: float,
    fwd_ret_aux: Optional[float],
) -> None:
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            UPDATE signal_ledger SET
                entry_price = ?, exit_price = ?, fwd_net_return = ?, fwd_mae = ?,
                fwd_ret_aux = ?, label_status = 'filled', filled_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (entry, exit_price, fwd_net_return, fwd_mae, fwd_ret_aux, int(signal_id)),
        )


def backfill_outcomes(
    *,
    symbol: Optional[str] = None,
    aux_horizon: int = 10,
    costs: Optional[Any] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """对 ``pending`` 行用 DB 日线 + B1 ``horizon_label`` 回填前瞻结果。

    每行按**自己的** ``horizon`` 与 ``trade_date`` 定位 entry/exit：

    - 决策日在库且未来 bar 足够 → ``filled``，写 ``entry_price``/``exit_price``/
      ``fwd_net_return``/``fwd_mae``，并按 ``aux_horizon`` 写 ``fwd_ret_aux``（辅助
      horizon 数据不足则该列留空，不影响主回填）；
    - 决策日在库但未来 bar 不足以走完整 horizon → 保持 ``pending``（待更多数据）；
    - 决策日不在库（缺 bar / 数据缺口）→ ``bad_data``。

    返回 ``{'filled', 'pending', 'bad_data', 'total', 'details'}``。
    """
    from ..ml.labels import horizon_label

    target = init_database(path)
    query = (
        "SELECT id, symbol, trade_date, horizon FROM signal_ledger "
        "WHERE label_status = 'pending'"
    )
    params: List[Any] = []
    if symbol is not None:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY symbol ASC, trade_date ASC"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        pending = [dict(row) for row in connection.execute(query, params).fetchall()]

    filled = still_pending = bad = 0
    details: List[Dict[str, Any]] = []
    cache: Dict[str, Any] = {}
    for row in pending:
        sym = row["symbol"]
        if sym not in cache:
            bars = load_daily_bars(sym, target)
            cache[sym] = (
                [b["open"] for b in bars],
                [b["high"] for b in bars],
                [b["low"] for b in bars],
                [b["close"] for b in bars],
                {b["trade_date"]: i for i, b in enumerate(bars)},
            )
        opens, highs, lows, closes, date_idx = cache[sym]
        td = row["trade_date"]
        if td not in date_idx:
            _set_status(target, row["id"], "bad_data")
            bad += 1
            details.append(
                {"id": row["id"], "symbol": sym, "trade_date": td, "status": "bad_data"}
            )
            continue
        index = date_idx[td]
        horizon = int(row.get("horizon") or 5)
        label = horizon_label(opens, highs, lows, closes, index, horizon=horizon, costs=costs)
        if label is None:
            still_pending += 1
            details.append(
                {"id": row["id"], "symbol": sym, "trade_date": td, "status": "pending"}
            )
            continue
        aux = horizon_label(
            opens, highs, lows, closes, index, horizon=aux_horizon, costs=costs
        )
        _fill_outcome(
            target,
            row["id"],
            entry=label.entry,
            exit_price=label.exit,
            fwd_net_return=label.net_return,
            fwd_mae=label.mae,
            fwd_ret_aux=(aux.net_return if aux is not None else None),
        )
        filled += 1
        details.append(
            {
                "id": row["id"],
                "symbol": sym,
                "trade_date": td,
                "status": "filled",
                "fwd_net_return": label.net_return,
            }
        )
    return {
        "filled": filled,
        "pending": still_pending,
        "bad_data": bad,
        "total": len(pending),
        "details": details,
    }


def signal_stats(*, source: Optional[str] = None, path: Optional[Path] = None) -> Dict[str, Any]:
    """台账汇总，**必含 coverage**（防"几乎不交易刷胜率"）。

    - ``coverage`` = 有可执行信号（BUY/SELL）的 (symbol, trade_date) 单元数 / 全部已评估
      单元数；
    - ``win_rate`` / ``avg_net_return`` 仅在 ``filled`` 行上计算（pending/bad_data 不计入，
      避免用未验证信号粉饰绩效）。
    """
    target = init_database(path)
    query = (
        "SELECT symbol, trade_date, recommendation, label_status, fwd_net_return, horizon "
        "FROM signal_ledger WHERE 1 = 1"
    )
    params: List[Any] = []
    if source is not None:
        query += " AND source = ?"
        params.append(source)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(query, params).fetchall()]

    by_status: Dict[str, int] = {}
    by_recommendation: Dict[str, int] = {}
    evaluated = set()
    signal_cells = set()
    symbols = set()
    dates: List[str] = []
    fwd_returns: List[float] = []
    horizons: List[int] = []
    for row in rows:
        status = row["label_status"] or "pending"
        by_status[status] = by_status.get(status, 0) + 1
        rec = row["recommendation"] or "NONE"
        by_recommendation[rec] = by_recommendation.get(rec, 0) + 1
        cell = (row["symbol"], row["trade_date"])
        evaluated.add(cell)
        symbols.add(row["symbol"])
        dates.append(row["trade_date"])
        if row["recommendation"] in ACTIONABLE_RECOMMENDATIONS:
            signal_cells.add(cell)
        if status == "filled" and row["fwd_net_return"] is not None:
            fwd_returns.append(float(row["fwd_net_return"]))
            if row["horizon"] is not None:
                horizons.append(int(row["horizon"]))

    n_evaluated = len(evaluated)
    n_signal = len(signal_cells)
    filled_count = len(fwd_returns)
    wins = sum(1 for value in fwd_returns if value > 0)
    return {
        "total_rows": len(rows),
        "by_status": by_status,
        "by_recommendation": by_recommendation,
        "symbols": len(symbols),
        "evaluated_days": n_evaluated,
        "signal_days": n_signal,
        "coverage": (n_signal / n_evaluated) if n_evaluated else 0.0,
        "filled_count": filled_count,
        "win_rate": (wins / filled_count) if filled_count else 0.0,
        "avg_net_return": (sum(fwd_returns) / filled_count) if filled_count else 0.0,
        "avg_horizon": (sum(horizons) / len(horizons)) if horizons else 0.0,
        "date_range": (min(dates), max(dates)) if dates else (None, None),
    }


def kv_get(key: str, default: Optional[str] = None, path: Optional[Path] = None) -> Optional[str]:
    """读 kv_store；不存在返回 ``default``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        row = connection.execute(
            "SELECT value FROM kv_store WHERE key = ?", (str(key),)
        ).fetchone()
    return row[0] if row else default


def kv_set(key: str, value: Any, path: Optional[Path] = None) -> None:
    """写 kv_store（upsert）。``value`` 统一存为字符串（None 存 NULL）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO kv_store (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (str(key), None if value is None else str(value)),
        )


def kv_delete(key: str, path: Optional[Path] = None) -> None:
    """删 kv_store 键（不存在则无操作）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute("DELETE FROM kv_store WHERE key = ?", (str(key),))
