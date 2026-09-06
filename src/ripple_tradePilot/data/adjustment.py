"""复权基准漂移检测（A9 · 纯函数）。

前复权（qfq）序列以"拉取时最新交易日"为锚：每当上游发生新的除权除息，整段历史
会被按新锚重算（缩放）。增量刷新若只回看很短窗口，就会把"新锚的近期数据"接到
"旧锚的存量数据"上，产生**复权混接**——在接点处出现一段虚假跳空，污染信号与盈亏。

本模块提供与 DB/网络无关的纯函数，供 ``stock_service.refresh``（落库前检测）与
``monitor.daily_eval``（盘中快照对齐，A3）复用：

- :func:`detect_rebase`：比对库内存量与新拉取序列的重叠日，识别"连续≥N日、方向一致、
  幅度近似恒定"的乘法偏差 → 判上游 qfq 重算（需全量重拉）。单日孤立偏差只记录不触发。
- :func:`audit_series`：用已有 ``pct_chg`` 列校验 close 环比一致性，离线巡检存量混接点。
- :func:`estimate_rebase_factor`：盘中快照原始 pre_close 与库内 qfq last_close 的比值，
  作为把历史缩放到当日原始基准的复权因子（A3 复用）。
- :func:`scale_ohlc`：按复权因子缩放 OHLC（A3 评估前对齐用）。

行数据形状兼容 ``load_daily_bars`` 的 dict 与 ``DataFrame.to_dict("records")``：
按 ``trade_date`` 索引，取 ``close``/``pct_chg``/``pre_close``，缺失或 NaN 安全跳过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "RebaseReport",
    "AuditAnomaly",
    "AuditReport",
    "detect_rebase",
    "audit_series",
    "estimate_rebase_factor",
    "scale_ohlc",
]


@dataclass(frozen=True)
class RebaseReport:
    """重叠区复权漂移检测结果。"""

    rebased: bool
    # 估计的乘法复权因子（fetched_close / stored_close 的中位数）；未检出为 1.0
    factor: float
    overlap_count: int
    deviation_count: int
    # 最长连续偏差run的长度
    max_run: int
    # 该 run 是否方向一致且幅度近似恒定
    consistent: bool
    # 触发（或最长）run 的 (trade_date, ratio) 采样，最多保留 12 条
    samples: Tuple[Tuple[str, float], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class AuditAnomaly:
    trade_date: str
    expected_ret: float  # 由 close 环比推出（ fraction，如 0.0294）
    actual_ret: float  # 由 pct_chg/100 推出（fraction）
    diff: float  # abs(expected_ret - actual_ret)


@dataclass(frozen=True)
class AuditReport:
    checked: int
    anomalies: Tuple[AuditAnomaly, ...]
    clean: bool
    # 便于上层日志/展示
    worst: Optional[AuditAnomaly] = None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _row_get(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _index_by_date(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Optional[float]]]:
    """按 trade_date（YYYYMMDD 字符串，字典序==时间序）索引，提取 close/pct_chg/pre_close。"""
    indexed: Dict[str, Dict[str, Optional[float]]] = {}
    for row in rows:
        raw_date = _row_get(row, "trade_date", "date")
        if raw_date is None:
            continue
        key = str(raw_date)
        indexed[key] = {
            "close": _to_float(_row_get(row, "close")),
            "pct_chg": _to_float(_row_get(row, "pct_chg", "change_pct")),
            "pre_close": _to_float(_row_get(row, "pre_close")),
        }
    return {date: indexed[date] for date in sorted(indexed)}


def detect_rebase(
    stored_rows: Iterable[Mapping[str, Any]],
    fetched_rows: Iterable[Mapping[str, Any]],
    tol: float = 0.005,
    min_consecutive: int = 2,
) -> RebaseReport:
    """检测上游 qfq 是否重算了复权基准。

    比对 stored（库内存量）与 fetched（新拉取）在重叠交易日的 close 比值
    ``ratio = fetched_close / stored_close``：

    - ``|ratio - 1| > tol`` 视为该日偏差；
    - 在重叠序列中找**最长的连续偏差 run**；
    - run 长度 ≥ ``min_consecutive`` 且方向一致（全 <1 或全 >1）且幅度近似恒定
      （run 内各 ratio 与中位数的相对偏离 ≤ tol）→ 判 ``rebased=True``，
      ``factor`` 取该 run 的中位 ratio。

    单日孤立偏差（run 长度 < min_consecutive）只记入 samples，不触发，避免把
    上游单点数据修正误判为整段复权重算。
    """
    if min_consecutive < 1:
        raise ValueError("min_consecutive 必须 ≥ 1")
    stored = _index_by_date(stored_rows)
    fetched = _index_by_date(fetched_rows)

    # 重叠日（按时间序），计算 close 比值
    ratios: List[Tuple[str, float]] = []
    for date in fetched:  # fetched 已按日期排序
        if date not in stored:
            continue
        stored_close = stored[date]["close"]
        fetched_close = fetched[date]["close"]
        if stored_close and stored_close > 0 and fetched_close and fetched_close > 0:
            ratios.append((date, fetched_close / stored_close))

    overlap_count = len(ratios)
    deviating_idx = [i for i, (_, r) in enumerate(ratios) if abs(r - 1.0) > tol]
    deviation_count = len(deviating_idx)

    if not deviating_idx:
        return RebaseReport(
            rebased=False,
            factor=1.0,
            overlap_count=overlap_count,
            deviation_count=0,
            max_run=0,
            consistent=False,
            reason="重叠日 close 一致，无复权漂移",
        )

    # 找最长连续（按重叠序列下标）偏差 run
    runs: List[List[int]] = []
    current: List[int] = [deviating_idx[0]]
    for idx in deviating_idx[1:]:
        if idx == current[-1] + 1:
            current.append(idx)
        else:
            runs.append(current)
            current = [idx]
    runs.append(current)
    longest = max(runs, key=len)
    max_run = len(longest)
    run_ratios = [ratios[i][1] for i in longest]
    samples = tuple((ratios[i][0], ratios[i][1]) for i in longest[:12])

    # 方向一致 + 幅度近似恒定
    median = _median(run_ratios)
    same_direction = all(r < 1.0 for r in run_ratios) or all(r > 1.0 for r in run_ratios)
    constant_magnitude = all(abs(r - median) <= tol * max(median, 1e-9) for r in run_ratios)
    consistent = same_direction and constant_magnitude
    rebased = max_run >= min_consecutive and consistent

    if rebased:
        reason = (
            f"连续 {max_run} 日方向一致、幅度恒定的乘法偏差（factor≈{median:.4f}），"
            "判上游 qfq 重算，需全量重拉"
        )
    elif max_run < min_consecutive:
        reason = f"仅 {max_run} 日孤立偏差（< min_consecutive={min_consecutive}），记录但不触发"
    else:
        reason = "偏差方向或幅度不一致，疑似噪音/单点修正而非整段复权重算"

    return RebaseReport(
        rebased=rebased,
        factor=median if rebased else 1.0,
        overlap_count=overlap_count,
        deviation_count=deviation_count,
        max_run=max_run,
        consistent=consistent,
        samples=samples,
        reason=reason,
    )


def audit_series(
    rows: Iterable[Mapping[str, Any]], tol: float = 0.01
) -> AuditReport:
    """用已有 ``pct_chg`` 列校验 close 环比一致性，巡检存量复权混接点。

    对每个相邻日对，比较：
      expected_ret = close[i] / close[i-1] - 1
      actual_ret   = pct_chg[i] / 100
    若 ``|expected_ret - actual_ret| > tol``（tol 为收益率 fraction，0.01=1 个百分点）
    → 记为异常。同一复权锚内二者应一致到舍入误差；混接点处会出现整段复权因子量级的跳变。

    缺少 ``pct_chg`` 或前一日 close 非正的行跳过（无法审计），不计入 checked。
    """
    indexed = _index_by_date(rows)
    dates = list(indexed)
    anomalies: List[AuditAnomaly] = []
    checked = 0
    for i in range(1, len(dates)):
        prev_close = indexed[dates[i - 1]]["close"]
        cur = indexed[dates[i]]
        cur_close = cur["close"]
        pct = cur["pct_chg"]
        if pct is None or not prev_close or prev_close <= 0 or not cur_close:
            continue
        checked += 1
        expected_ret = cur_close / prev_close - 1.0
        actual_ret = pct / 100.0
        diff = abs(expected_ret - actual_ret)
        if diff > tol:
            anomalies.append(
                AuditAnomaly(
                    trade_date=dates[i],
                    expected_ret=expected_ret,
                    actual_ret=actual_ret,
                    diff=diff,
                )
            )
    worst = max(anomalies, key=lambda a: a.diff) if anomalies else None
    return AuditReport(
        checked=checked,
        anomalies=tuple(anomalies),
        clean=not anomalies,
        worst=worst,
    )


def estimate_rebase_factor(
    last_close: Optional[float], snapshot_pre_close: Optional[float]
) -> float:
    """盘中快照原始 pre_close 与库内 qfq last_close 的比值（A3 复用）。

    当日快照的 ``pre_close`` 是交易所口径的"昨日真实收盘"（原始基准）；库内最后一根
    qfq close 是旧锚基准。二者比值即把库内历史缩放到当日原始基准所需的复权因子。
    任一缺失/非正 → 返回 1.0（不缩放）。
    """
    lc = _to_float(last_close)
    sp = _to_float(snapshot_pre_close)
    if not lc or lc <= 0 or not sp or sp <= 0:
        return 1.0
    return sp / lc


def scale_ohlc(
    rows: Iterable[Mapping[str, Any]], factor: float
) -> List[Dict[str, Any]]:
    """按复权因子缩放每行的 open/high/low/close/pre_close（A3 评估前对齐用）。

    factor≈1.0 时原样返回（仍复制为 dict）。volume/pct_chg/trade_date 等非价格字段不动。
    """
    scaled_factor = _to_float(factor)
    if scaled_factor is None or scaled_factor <= 0:
        raise ValueError("factor 必须为正数")
    out: List[Dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        if abs(scaled_factor - 1.0) < 1e-12:
            out.append(new_row)
            continue
        for key in ("open", "high", "low", "close", "pre_close"):
            value = _to_float(new_row.get(key))
            if value is not None:
                new_row[key] = value * scaled_factor
        out.append(new_row)
    return out


def _median(values: List[float]) -> float:
    if not values:
        return 1.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
