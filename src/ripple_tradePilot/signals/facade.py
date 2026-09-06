"""三消费方（dashboard / monitor / 回测）的共同入口。

evaluate_symbol 是纯函数：bars（Bar 或 Mapping 行）+ profile（原始 dict 或
ProfileSpec）→ SymbolEvaluation（逐 bar 决策序列 + 边沿事件 + 最新快照）。
DB 读取、快照合成、复权防护都在调用方（A3 的 monitor/daily_eval.py 等），
本模块不触网、不触库。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Union

from ..indicators import DEFAULT_VOTE_THRESHOLD
from ..models.types import Bar
from .components import ComponentStateEngine, ComponentVote, make_engine
from .profile import ProfileSpec, parse_profile
from .voting import VoteDecision, VoteEvent, decide, decision_events, vote_series

BarLike = Union[Bar, Mapping[str, Any]]


@dataclass(frozen=True)
class SymbolEvaluation:
    """单标的完整评估结果。"""

    spec: ProfileSpec
    decisions: Sequence[VoteDecision]
    events: Sequence[VoteEvent]

    @property
    def latest(self) -> VoteDecision:
        return self.decisions[-1]

    @property
    def latest_event(self) -> Optional[VoteEvent]:
        return self.events[-1] if self.events else None

    def votes_by_name(self, index: int = -1) -> dict:
        """某根 bar 的组件票字典（默认最新一拍），dashboard votes 字段口径。"""
        return self.decisions[index].votes


def coerce_bars(bars: Sequence[BarLike]) -> List[Bar]:
    """接受 Bar 或 Mapping 行（load_daily_bars dict / 快照合成行），统一为 Bar。

    Mapping 行时间戳优先级：timestamp → trade_date（YYYYMMDD 或 ISO 字符串）；
    成交量兼容 volume/vol 两种键名。缺关键字段抛 ValueError。
    """
    coerced: List[Bar] = []
    for item in bars:
        if isinstance(item, Bar):
            coerced.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"无法识别的 bar 类型: {type(item).__name__}")
        timestamp = _coerce_timestamp(item.get("timestamp") or item.get("trade_date"))
        try:
            coerced.append(
                Bar(
                    timestamp=timestamp,
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item.get("volume", item.get("vol", 0.0)) or 0.0),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"bar 行缺少必需字段: {item!r}") from exc
    return coerced


def _coerce_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if raw is None:
        raise ValueError("bar 行缺少 timestamp/trade_date")
    text = str(raw).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间戳: {raw!r}") from exc


def evaluate_symbol(
    bars: Sequence[BarLike],
    profile: Union[Mapping[str, Any], ProfileSpec, None],
    default_threshold: int = DEFAULT_VOTE_THRESHOLD,
    source: str = "config",
) -> SymbolEvaluation:
    """bars 逐根喂组件引擎 → 决策序列 → 边沿事件。空 bars 抛 ValueError。"""
    coerced = coerce_bars(bars)
    if not coerced:
        raise ValueError("evaluate_symbol 需要至少一根 K 线")
    spec = (
        profile
        if isinstance(profile, ProfileSpec)
        else parse_profile(profile, default_threshold=default_threshold, source=source)
    )
    engines: List[ComponentStateEngine] = [
        make_engine(component) for component in spec.components
    ]
    timestamps: List[datetime] = []
    bar_votes: List[List[ComponentVote]] = []
    for bar in coerced:
        timestamps.append(bar.timestamp)
        bar_votes.append([engine.push(bar) for engine in engines])
    decisions = vote_series(timestamps, bar_votes, spec.vote_threshold)
    return SymbolEvaluation(
        spec=spec,
        decisions=tuple(decisions),
        events=tuple(decision_events(decisions)),
    )


def evaluate_incremental(
    engines: Sequence[ComponentStateEngine],
    bar: Bar,
    vote_threshold: int,
) -> VoteDecision:
    """单 bar 增量评估（A4 回测适配器 / A3 盘中逐轮评估复用同一 decide 口径）。"""
    return decide(bar.timestamp, [engine.push(bar) for engine in engines], vote_threshold)


__all__ = [
    "BarLike",
    "SymbolEvaluation",
    "coerce_bars",
    "evaluate_incremental",
    "evaluate_symbol",
]
