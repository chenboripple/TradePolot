"""A3 日线评估编排（同步、可单测；信号链路不触网）。

把"取数 → 复权防护 → 合成盘中 provisional bar → ``evaluate_symbol`` → 当日新事件门控"
收敛成纯同步函数；monitor 的异步轮询只负责调度快照与发通知。本模块仅
``evaluate_symbol_daily`` 读一次 DB（``load_daily_bars``），其余（``build_daily_series`` /
``synthesize_provisional_bar``）是纯函数，可离线单测。

为什么切日线：改造前 monitor 每标的每轮拉近 5 天 1min 线（N 次网络/轮），分钟线不落库、
与网页/CLI 回测的日线口径分裂——监控实际跑的策略从未被回测验证过。统一切日线后：历史 =
``load_daily_bars``（SQLite qfq），当日未收盘 = ``stock_quotes`` 快照合成 provisional bar，
每轮全市场只 refresh 一次快照。分钟线降级为可选价格预警（``price_alert.py``，默认关）。

复权口径防护（衔接 A9）：盘中快照是交易所**原始价**，库内历史是**前复权(qfq)**。若当日除权，
快照 ``pre_close``（昨日真实收盘）与库内最后一根 qfq close 会偏离；偏离 >``REBASE_TOL`` 判当日
除权，用 ``estimate_rebase_factor`` 的比值经 ``scale_ohlc`` 把历史缩放到当日基准再评估，并打标
``rebased=True``（调用方据此把该标的列入收盘后强制全量重拉名单）。

口径与 ``signals`` / ``docs/prediction-target.md`` 一致：组件状态票 + strict 投票；事件 =
recommendation **进入** BUY/SELL 的转移点；"当日新事件" = 转移点恰落在序列最后一根（今日）bar。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..data.adjustment import estimate_rebase_factor, scale_ohlc
from ..indicators import DEFAULT_VOTE_THRESHOLD
from ..models.types import Bar, Side
from ..signals.facade import coerce_bars, evaluate_symbol
from ..signals.profile import ProfileSpec
from ..signals.voting import VoteDecision, VoteEvent
from ..storage.database import load_daily_bars

__all__ = [
    "MIN_DAILY_BARS",
    "REBASE_TOL",
    "ACTIONABLE_RECOMMENDATIONS",
    "DailyEvaluation",
    "synthesize_provisional_bar",
    "build_daily_series",
    "evaluate_symbol_daily",
    "today_str",
]

# 日线数据门槛：少于此根数不出信号（指标预热 + 统计意义）。改造前是分钟线 <30 根跳过。
MIN_DAILY_BARS = 60
# 复权漂移容差：快照 pre_close 与库内 last close 偏离超过此比例判当日除权（A9 同口径 0.5%）。
REBASE_TOL = 0.005
# 产生入场/出场动作的 recommendation（通知门控与台账 coverage 分子）。
ACTIONABLE_RECOMMENDATIONS = ("BUY", "SELL")


def today_str(moment: Optional[datetime] = None) -> str:
    """当前（或给定）时刻的 ``YYYYMMDD``，与 daily_bars.trade_date 同口径。"""
    return (moment or datetime.now()).strftime("%Y%m%d")


def _to_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 过滤 NaN


def synthesize_provisional_bar(quote: Mapping[str, Any], trade_date: str) -> Optional[Dict[str, Any]]:
    """由一条 ``stock_quotes`` 快照行合成"今日 provisional bar"（dict，喂 ``coerce_bars``）。

    快照字段 → bar 字段：``price``→close（盘中最新价当作未收盘的当日收盘），open/high/low 直取，
    ``volume``→vol。价格缺失/非正 → 返回 ``None``（无有效快照，不合成）。high/low 缺失时用 price
    兜底，保证 OHLC 自洽（high≥close≥low）。
    """
    price = _to_float(quote.get("price"))
    if not price or price <= 0:
        return None
    open_price = _to_float(quote.get("open")) or price
    high = _to_float(quote.get("high")) or price
    low = _to_float(quote.get("low")) or price
    high = max(high, price, open_price)
    low = min(low, price, open_price)
    volume = _to_float(quote.get("volume")) or 0.0
    return {
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": price,
        "vol": volume,
        "pre_close": _to_float(quote.get("pre_close")),
        "provisional": True,
    }


def build_daily_series(
    db_rows: Sequence[Mapping[str, Any]],
    quote: Optional[Mapping[str, Any]] = None,
    *,
    trade_date: Optional[str] = None,
    rebase_tol: float = REBASE_TOL,
) -> Tuple[List[Dict[str, Any]], bool, bool, float]:
    """库内日线行 + 可选今日快照 → ``(rows, provisional, rebased, rebase_factor)``（纯函数）。

    - **不触网、不触库**：``db_rows`` 来自 ``load_daily_bars``，``quote`` 来自 ``stock_quotes`` 行；
    - **复权防护**：仅当需要追加 provisional bar（库内还没有今日 bar）且库内非空时，比对快照
      ``pre_close`` 与库内最后一根 close；偏离 >``rebase_tol`` → ``rebased=True`` 并 ``scale_ohlc``
      缩放历史到当日基准（provisional bar 本就是当日原始价，缩放后两者同锚）；
    - **provisional bar**：快照有效且其交易日（``trade_date``，缺省今日）晚于库内最后一根 → 追加，
      ``provisional=True``；库内已有今日 bar（如收盘 refresh 后）→ 不追加，``provisional=False``。

    返回的 ``rows`` 是 dict 副本列表（不修改入参），可直接喂 ``evaluate_symbol``。
    """
    rows: List[Dict[str, Any]] = [dict(row) for row in db_rows]
    last_db_date = str(rows[-1].get("trade_date")) if rows else None
    today = trade_date or today_str()

    provisional = False
    rebased = False
    factor = 1.0

    want_provisional = quote is not None and last_db_date != today
    if want_provisional:
        synthesized = synthesize_provisional_bar(quote, today)
        if synthesized is not None:
            # 复权防护：快照原始 pre_close vs 库内 qfq last close
            if rows:
                estimated = estimate_rebase_factor(
                    rows[-1].get("close"), quote.get("pre_close")
                )
                if abs(estimated - 1.0) > rebase_tol:
                    rebased = True
                    factor = estimated
                    rows = scale_ohlc(rows, factor)
            rows.append(synthesized)
            provisional = True

    return rows, provisional, rebased, factor


@dataclass(frozen=True)
class DailyEvaluation:
    """单标的一次日线评估的完整结果（monitor 通知门控 / 台账写入 / 报告共用）。

    ``insufficient=True`` 时 ``decision``/``spec`` 为 ``None``（数据不足，不出信号），其余字段
    仍可用于诊断（``n_bars``）。``recommendation`` 是 ``signals`` 内部编码（BUY/SELL/HOLD/
    CONFLICT），展示层（monitor 的 🔴/🟢/🟡 常量）负责映射，本数据类不含 emoji。
    """

    symbol: str
    n_bars: int
    provisional: bool
    rebased: bool
    rebase_factor: float
    trade_date: Optional[str]
    insufficient: bool
    spec: Optional[ProfileSpec] = None
    decision: Optional[VoteDecision] = None
    events: Tuple[VoteEvent, ...] = ()
    bars: Tuple[Bar, ...] = ()

    @property
    def recommendation(self) -> Optional[str]:
        return self.decision.recommendation if self.decision else None

    @property
    def is_actionable(self) -> bool:
        return self.recommendation in ACTIONABLE_RECOMMENDATIONS

    @property
    def latest_bar(self) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    @property
    def latest_price(self) -> float:
        bar = self.latest_bar
        return bar.close if bar else 0.0

    @property
    def new_event(self) -> Optional[VoteEvent]:
        """当日**新**边沿事件：转移点恰落在序列最后一根（今日）bar，否则 ``None``。

        通知门控核心——只有今日新进入 BUY/SELL 才推送；昨日进入、今日维持的状态不重复推送
        （``decision_events`` 只在转移点出事件，维持态本就无事件，此处再校验落在最后一根）。
        """
        if self.insufficient or not self.events:
            return None
        last = self.events[-1]
        return last if last.index == self.n_bars - 1 else None

    @property
    def trigger_components(self) -> Tuple[str, ...]:
        """当前决策里与多数方向同向的组件名（通知内"触发组件"注解，不再驱动门控）。"""
        if not self.decision:
            return ()
        target = Side.BUY if self.decision.recommendation == "BUY" else (
            Side.SELL if self.decision.recommendation == "SELL" else None
        )
        if target is None:
            return ()
        return tuple(
            component.name
            for component in self.decision.components
            if component.side is target
        )


def evaluate_symbol_daily(
    symbol: str,
    profile: Union[Mapping[str, Any], ProfileSpec, None],
    *,
    db_path: Optional[Any] = None,
    quote: Optional[Mapping[str, Any]] = None,
    trade_date: Optional[str] = None,
    default_threshold: int = DEFAULT_VOTE_THRESHOLD,
    source: str = "config",
    min_bars: int = MIN_DAILY_BARS,
    rebase_tol: float = REBASE_TOL,
) -> DailyEvaluation:
    """读库内日线 + 可选今日快照 → 统一口径 ``DailyEvaluation``。

    唯一触库点（``load_daily_bars``）；不触网（快照由调用方一次性 refresh 后传入）。数据不足
    ``min_bars`` → ``insufficient=True``（不出信号，调用方跳过）。``profile`` 解析失败（如 breakout
    豁免 kind）由调用方先行分流——本函数只接受 ``signals`` 支持的画像。
    """
    db_rows = load_daily_bars(symbol, db_path)
    rows, provisional, rebased, factor = build_daily_series(
        db_rows, quote, trade_date=trade_date, rebase_tol=rebase_tol
    )
    today = trade_date or today_str()
    evaluated_date = str(rows[-1].get("trade_date")) if rows else None

    if len(rows) < min_bars:
        return DailyEvaluation(
            symbol=symbol,
            n_bars=len(rows),
            provisional=provisional,
            rebased=rebased,
            rebase_factor=factor,
            trade_date=evaluated_date or today,
            insufficient=True,
            bars=tuple(coerce_bars(rows)) if rows else (),
        )

    evaluation = evaluate_symbol(
        rows, profile, default_threshold=default_threshold, source=source
    )
    return DailyEvaluation(
        symbol=symbol,
        n_bars=len(rows),
        provisional=provisional,
        rebased=rebased,
        rebase_factor=factor,
        trade_date=evaluated_date,
        insufficient=False,
        spec=evaluation.spec,
        decision=evaluation.latest,
        events=tuple(evaluation.events),
        bars=tuple(coerce_bars(rows)),
    )
