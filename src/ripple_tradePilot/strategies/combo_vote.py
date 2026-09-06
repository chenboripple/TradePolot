from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class ComboVoteStrategy(Strategy):
    """Lightweight multi-strategy voting wrapper.

    .. deprecated:: 2026-09
        统一投票口径已迁移至 :mod:`ripple_tradePilot.signals`（状态票 +
        strict 规则 + 事件=决策转移）。本类保留原行为不动，仅供
        ``experiments/`` 历史脚本兼容；生产链路（dashboard / monitor /
        Web·CLI 回测）请改用 ``signals.evaluate_symbol`` 与回测适配器
        ``signals.ComboVoteStateStrategy``。

    语义差异警示（勿混用两者的结论）：本类对**边沿信号**计票——子策略
    只在交叉/触发的当根返回 side，票数统计的是"同一根 bar 上恰好同时
    发生的事件"；signals/ 模块对**状态**计票（如 MA 快线在慢线上方期间
    持续记 BUY 票），事件由决策序列的转移点派生。同一份数据两者产出的
    信号 bar 集合通常不同。

    Supports combining 2+ sub-strategies and triggering when buy/sell votes
    reach the configured threshold and are not contradicted by the other side.
    """

    name = "combo_vote"

    def __init__(self, strategies: Iterable[Tuple[str, Strategy]], vote_threshold: int = 1):
        strategies = list(strategies)
        if not strategies:
            raise ValueError("at least one sub-strategy is required")
        if vote_threshold < 1:
            raise ValueError("vote_threshold must be >= 1")
        if vote_threshold > len(strategies):
            raise ValueError("vote_threshold cannot exceed number of sub-strategies")

        self.strategies: List[Tuple[str, Strategy]] = strategies
        self.vote_threshold = vote_threshold

    def on_bar(self, bar: Bar) -> Signal:
        signals = self.get_component_signals(bar)
        buy_count = sum(1 for signal in signals.values() if signal.side == Side.BUY)
        sell_count = sum(1 for signal in signals.values() if signal.side == Side.SELL)

        if buy_count >= self.vote_threshold and sell_count == 0:
            strength = max((signal.strength for signal in signals.values() if signal.side == Side.BUY), default=1.0)
            return Signal(timestamp=bar.timestamp, side=Side.BUY, strength=strength)
        if sell_count >= self.vote_threshold and buy_count == 0:
            strength = max((signal.strength for signal in signals.values() if signal.side == Side.SELL), default=1.0)
            return Signal(timestamp=bar.timestamp, side=Side.SELL, strength=strength)
        return Signal(timestamp=bar.timestamp, side=None)

    def get_component_signals(self, bar: Bar) -> Dict[str, Signal]:
        return {name: strategy.on_bar(bar) for name, strategy in self.strategies}

    def reset(self) -> None:
        for _, strategy in self.strategies:
            strategy.reset()
