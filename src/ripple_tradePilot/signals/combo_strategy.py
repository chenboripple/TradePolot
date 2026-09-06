"""ComboVoteStateStrategy：统一投票口径的回测流式适配器（A4）。

把 signals/ 的状态票 + strict 投票 + 转移事件包成一个 Strategy，可直接喂给
backtest.engine.run_backtest 与 walk-forward。

与 legacy ComboVoteStrategy 的根本区别（勿混用结论）：
- legacy 对子策略的**边沿信号**计票（只在交叉当根有票），统计的是"同一根
  bar 上恰好同时发生的事件"；
- 本类对**组件状态**计票（MA 多头期间持续记 BUY 票），并在聚合
  recommendation **转移到** BUY/SELL 的当根发出 Signal——与
  voting.decision_events 逐 bar 一致。

核心不变量（tests/test_signals_parity.py 钉死）：
    [i for i,bar in enumerate(bars) if strategy.on_bar(bar).side is not None]
    == [event.index for event in evaluate_symbol(bars, spec).events]

引擎契约：run_backtest 每根 bar 恰好调用一次 on_bar（离场分支也调用以推进
状态），故 previous_recommendation 始终与 bar 序列同步。
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Optional, Sequence, Union

from ..indicators import DEFAULT_VOTE_THRESHOLD
from ..models.types import Bar, Signal, Side
from ..strategies.base import Strategy
from .components import ComponentStateEngine, make_engine
from .profile import ProfileSpec, parse_profile
from .voting import REC_BUY, REC_HOLD, REC_SELL, decide


class ComboVoteStateStrategy(Strategy):
    """状态投票组合策略：recommendation 转移点 → 交易信号。"""

    name = "combo_vote_state"

    def __init__(
        self,
        profile: Union[Mapping[str, object], ProfileSpec, None] = None,
        *,
        spec: Optional[ProfileSpec] = None,
        default_threshold: int = DEFAULT_VOTE_THRESHOLD,
        source: str = "backtest",
    ):
        resolved = spec if spec is not None else profile
        if isinstance(resolved, ProfileSpec):
            self.spec = resolved
        else:
            self.spec = parse_profile(
                resolved, default_threshold=default_threshold, source=source
            )
        self._engines: List[ComponentStateEngine] = [
            make_engine(component) for component in self.spec.components
        ]
        self._previous_recommendation = REC_HOLD
        self._last_decision = None

    # -- Strategy 接口 ------------------------------------------------------

    def on_bar(self, bar: Bar) -> Signal:
        votes = [engine.push(bar) for engine in self._engines]
        decision = decide(bar.timestamp, votes, self.spec.vote_threshold)
        self._last_decision = decision
        recommendation = decision.recommendation

        side: Optional[Side] = None
        if recommendation in (REC_BUY, REC_SELL) and recommendation != self._previous_recommendation:
            side = Side.BUY if recommendation == REC_BUY else Side.SELL
        self._previous_recommendation = recommendation

        strength = 1.0
        if side is not None:
            strengths = [vote.strength for vote in votes if vote.side is side]
            strength = max(strengths) if strengths else 1.0
        return Signal(timestamp=bar.timestamp, side=side, strength=strength)

    def warmup(self, history: Iterable[Bar]) -> None:
        """喂前置 bars 推进组件状态与 previous_recommendation，但**不发信号**。

        基类默认实现即逐根 on_bar 丢弃返回值——对本类正好正确：预热结束时
        previous_recommendation 停在预热段最后一拍的状态，评估段首根只在发生
        真实转移时才发信号（A6 walk-forward 用它消除冷启动缺口）。
        """
        for bar in history:
            _ = self.on_bar(bar)

    def reset(self) -> None:
        for engine in self._engines:
            engine.reset()
        self._previous_recommendation = REC_HOLD
        self._last_decision = None

    # -- 便利访问器 ---------------------------------------------------------

    @property
    def last_decision(self):
        """最近一根 bar 的投票决策（monitor 通知/调试用）。"""
        return self._last_decision

    def signal_indices(self, bars: Sequence[Bar]) -> List[int]:
        """在给定 bars 上重放，返回发出信号的 bar 索引（测试/分析便利）。"""
        self.reset()
        return [
            index
            for index, bar in enumerate(bars)
            if self.on_bar(bar).side is not None
        ]
