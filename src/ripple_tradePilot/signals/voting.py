"""投票决策层：状态票 → VoteDecision 序列 → 边沿事件。

统一规则（strict，即旧 ComboVoteStrategy 语义，废弃 dashboard majority
与 monitor 不查 sell_count 的分支）：
- BUY  ：同向票 >= vote_threshold 且反向票 == 0；
- SELL ：同向票 >= vote_threshold 且反向票 == 0；
- CONFLICT：有票但未过阈值，或多空同时有票；
- HOLD ：所有组件都不投票。

事件语义：decision_events 输出 recommendation **进入** BUY/SELL 的状态转移点
（首根 bar 的"上一状态"视为 HOLD），dashboard 展示消费状态序列本身，
monitor 通知与回测撮合消费事件序列——两者派生自同一 decisions，天然一致。

recommendation 使用内部编码 BUY/SELL/HOLD/CONFLICT；展示层（monitor 的
🔴/🟢/🟡 常量、前端文案）负责映射，本模块不出现 emoji。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from ..models.types import Side
from .components import ComponentVote

REC_BUY = "BUY"
REC_SELL = "SELL"
REC_HOLD = "HOLD"
REC_CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class VoteDecision:
    """单根 bar 上的投票决策快照。"""

    timestamp: datetime
    recommendation: str
    buy_count: int
    sell_count: int
    vote_threshold: int
    components: Tuple[ComponentVote, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def is_conflict(self) -> bool:
        return self.recommendation == REC_CONFLICT

    @property
    def vote_ratio(self) -> float:
        """多数方向票占组件总数的比例 [0,1]。

        取代旧 dashboard 的伪 confidence：这是规则票占比，**不是概率**，
        展示层文案必须注明（概率语义由 D 阶段模型输出承担）。
        """
        if not self.components:
            return 0.0
        return max(self.buy_count, self.sell_count) / len(self.components)

    @property
    def votes(self) -> dict:
        """组件名 → 'BUY'/'SELL'/'HOLD'（dashboard 响应 votes 字段口径）。"""
        return {
            component.name: (component.side.value if component.side else "HOLD")
            for component in self.components
        }


@dataclass(frozen=True)
class VoteEvent:
    """recommendation 进入 BUY/SELL 的转移点（边沿事件）。"""

    index: int
    timestamp: datetime
    side: Side
    decision: VoteDecision
    previous_recommendation: str


def decide(
    timestamp: datetime,
    component_votes: Sequence[ComponentVote],
    vote_threshold: int,
) -> VoteDecision:
    """对单根 bar 的组件状态票应用 strict 投票规则。"""
    votes = tuple(component_votes)
    buy_count = sum(1 for vote in votes if vote.side is Side.BUY)
    sell_count = sum(1 for vote in votes if vote.side is Side.SELL)
    if buy_count >= vote_threshold and sell_count == 0:
        recommendation = REC_BUY
    elif sell_count >= vote_threshold and buy_count == 0:
        recommendation = REC_SELL
    elif buy_count or sell_count:
        recommendation = REC_CONFLICT
    else:
        recommendation = REC_HOLD
    return VoteDecision(
        timestamp=timestamp,
        recommendation=recommendation,
        buy_count=buy_count,
        sell_count=sell_count,
        vote_threshold=vote_threshold,
        components=votes,
        reason=build_reason(votes, recommendation, vote_threshold),
    )


def vote_series(
    timestamps: Sequence[datetime],
    bar_votes: Sequence[Sequence[ComponentVote]],
    vote_threshold: int,
) -> List[VoteDecision]:
    """逐 bar 决策序列；timestamps 与 bar_votes 必须等长。"""
    if len(timestamps) != len(bar_votes):
        raise ValueError(
            f"timestamps({len(timestamps)}) 与 bar_votes({len(bar_votes)}) 长度不一致"
        )
    return [
        decide(timestamp, votes, vote_threshold)
        for timestamp, votes in zip(timestamps, bar_votes)
    ]


def decision_events(decisions: Sequence[VoteDecision]) -> List[VoteEvent]:
    """状态转移 → 边沿事件（首根的 previous 视为 HOLD；CONFLICT/HOLD 也刷新 previous）。"""
    events: List[VoteEvent] = []
    previous = REC_HOLD
    for index, decision in enumerate(decisions):
        recommendation = decision.recommendation
        if recommendation in (REC_BUY, REC_SELL) and recommendation != previous:
            events.append(
                VoteEvent(
                    index=index,
                    timestamp=decision.timestamp,
                    side=Side.BUY if recommendation == REC_BUY else Side.SELL,
                    decision=decision,
                    previous_recommendation=previous,
                )
            )
        previous = recommendation
    return events


# ---------------------------------------------------------------------------
# 中文理由（dashboard/monitor 通知共用）
# ---------------------------------------------------------------------------

def build_reason(
    component_votes: Sequence[ComponentVote],
    recommendation: str,
    vote_threshold: int,
) -> str:
    fragments = [
        fragment
        for fragment in (_component_reason(vote) for vote in component_votes)
        if fragment
    ]
    total = len(component_votes)
    buy_count = sum(1 for vote in component_votes if vote.side is Side.BUY)
    sell_count = sum(1 for vote in component_votes if vote.side is Side.SELL)

    if recommendation == REC_BUY:
        head = f"{buy_count}/{total} 票看涨且无反向票（阈值 {vote_threshold}）"
    elif recommendation == REC_SELL:
        head = f"{sell_count}/{total} 票看跌且无反向票（阈值 {vote_threshold}）"
    elif recommendation == REC_CONFLICT:
        if buy_count and sell_count:
            head = (
                f"信号冲突：{buy_count} 票看涨、{sell_count} 票看跌"
                f"（需 {vote_threshold} 票同向且无反向票）"
            )
        else:
            direction = "看涨" if buy_count else "看跌"
            head = f"票数不足：{buy_count or sell_count} 票{direction}，未达阈值 {vote_threshold}"
    else:
        return "指标未形成有效投票"

    return "；".join([head, *fragments]) if fragments else head


def _component_reason(vote: ComponentVote) -> Optional[str]:
    if vote.side is None:
        return None
    detail = vote.detail
    buy = vote.side is Side.BUY

    def _fmt(key: str) -> str:
        value = detail.get(key)
        return f"{value:.2f}" if isinstance(value, (int, float)) else "?"

    if vote.kind == "ma":
        relation = "高于" if buy else "低于"
        return f"{vote.name}: 快线 {_fmt('fast_ma')} {relation}慢线 {_fmt('slow_ma')}"
    if vote.kind == "rsi":
        zone = "超卖" if buy else "超买"
        return f"{vote.name}: RSI {_fmt('rsi')} 进入{zone}区"
    if vote.kind == "bollinger":
        band = "下轨" if buy else "上轨"
        return f"{vote.name}: 价格触及布林带{band}"
    if vote.kind == "macd":
        state = "多头（DIF 高于 DEA）" if buy else "空头（DIF 低于 DEA）"
        return f"{vote.name}: MACD {state}"
    if vote.kind == "trend":
        alignment = "多头排列（短>中>长）" if buy else "空头排列（短<中<长）"
        return f"{vote.name}: 均线{alignment}"
    if vote.kind == "donchian":
        window = detail.get("window")
        window_text = f"{int(window)}" if isinstance(window, (int, float)) else "?"
        point = "高点" if buy else "低点"
        return f"{vote.name}: 收盘突破 {window_text} 日通道{point}"
    return f"{vote.name}: {'看涨' if buy else '看跌'}"
