"""统一投票决策模块（A 阶段唯一口径）。

- profile.py        三种画像结构单一解析器 → ProfileSpec
- components.py     组件状态票（增量引擎 + 批量序列，位级镜像 indicators.py）
- voting.py         strict 投票规则、决策序列、边沿事件
- facade.py         evaluate_symbol()：dashboard / monitor / 回测共同入口
- combo_strategy.py ComboVoteStateStrategy：回测流式适配器（信号=转移点）
- backtest_profile.py Web/CLI 回测策略解析链（A5）：params/profile/缺省 → Strategy + provenance
"""
from __future__ import annotations

from .backtest_profile import (
    BACKTEST_STRATEGIES,
    COMBO_VOTE,
    DEFAULT_PROFILE,
    PARAM_WHITELIST,
    IllegalParamError,
    ResolvedStrategy,
    UnknownProfileError,
    build_strategy,
    default_profile,
    load_single_strategy_class,
    params_schema,
    resolve_backtest_strategy,
)
from .combo_strategy import ComboVoteStateStrategy
from .components import (
    ComponentStateEngine,
    ComponentVote,
    component_state_series,
    make_engine,
    required_bars,
    warmup_bars,
)
from .facade import (
    SymbolEvaluation,
    coerce_bars,
    evaluate_incremental,
    evaluate_symbol,
)
from .profile import (
    ComponentSpec,
    ProfileSpec,
    UnsupportedProfileKindError,
    parse_profile,
)
from .voting import (
    REC_BUY,
    REC_CONFLICT,
    REC_HOLD,
    REC_SELL,
    VoteDecision,
    VoteEvent,
    build_reason,
    decide,
    decision_events,
    vote_series,
)

__all__ = [
    "BACKTEST_STRATEGIES",
    "COMBO_VOTE",
    "ComboVoteStateStrategy",
    "ComponentSpec",
    "ComponentStateEngine",
    "ComponentVote",
    "DEFAULT_PROFILE",
    "IllegalParamError",
    "PARAM_WHITELIST",
    "ProfileSpec",
    "REC_BUY",
    "REC_CONFLICT",
    "REC_HOLD",
    "REC_SELL",
    "ResolvedStrategy",
    "SymbolEvaluation",
    "UnknownProfileError",
    "UnsupportedProfileKindError",
    "VoteDecision",
    "VoteEvent",
    "build_reason",
    "build_strategy",
    "coerce_bars",
    "component_state_series",
    "decide",
    "decision_events",
    "default_profile",
    "evaluate_incremental",
    "evaluate_symbol",
    "load_single_strategy_class",
    "make_engine",
    "params_schema",
    "parse_profile",
    "required_bars",
    "resolve_backtest_strategy",
    "vote_series",
    "warmup_bars",
]
