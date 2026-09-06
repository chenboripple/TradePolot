"""Web/CLI 回测的策略解析链（A5）：symbol/strategy/params/profile → 可回测 Strategy + provenance。

回测历史上对 5 个单策略**无参默认构造**（``api/app.py`` 与 ``cli.py`` 各写一份注册表），
既不读 ``config.strategy_profiles``，也不读用户在 Web 保存的系统策略——监控实际跑的画像
从未被回测验证过（plan A 节"周期与参数分裂"）。本模块把"解析哪个策略、用哪套参数、来源是
什么"收敛成单一函数 :func:`resolve_backtest_strategy`，Web 端点与 CLI 共同消费，并输出
provenance（``profile_source`` + ``strategy_params``）进响应与 ``backtest_results``（v12 列）。

解析优先级（与 monitor 口径对齐）：

1. **显式 params**：按 strategy 键白名单校验后构造（单策略 ``cls(**params)``；combo_vote →
   ProfileSpec）；``profile_source="explicit"``。
2. **profile="system"**：``user_store.get_system_strategy(symbol)`` 的参数 → combo_vote；
   ``profile_source="system"``。
3. **profile=名字**：``config.strategy_profiles[名字]`` → combo_vote；``profile_source="config:名字"``。
4. **缺省**（无 params 无 profile）：
   - strategy 为单策略 → 类默认参数构造（与改造前 ``cls()`` 完全一致）；``profile_source="default"``；
   - strategy 为 combo_vote → 复用 monitor 解析链（config symbols 绑定 → DB system → 全局默认
     画像 :data:`DEFAULT_PROFILE`）；``profile_source`` 随命中环节为 ``"config:名字"``/``"system"``/``"default"``。

:data:`DEFAULT_PROFILE`（全局默认三件套）抽成此处单一常量，monitor 的 ``_default_profile``
改为引用它，消除双份定义（plan A5"与 monitor ``_default_profile`` 抽成同一常量"）。

profile 比 strategy 下拉更强：给定 profile 时一律解析为 combo_vote 投票画像（一个 profile
本就是完整的投票规格）；params 最显式，优先级最高。
"""
from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..config_loader import get_vote_threshold
from ..indicators import DEFAULT_VOTE_THRESHOLD
from ..strategies.base import Strategy
from .combo_strategy import ComboVoteStateStrategy
from .profile import (
    BOLLINGER_DEFAULTS,
    MA_DEFAULTS,
    ProfileSpec,
    RSI_DEFAULTS,
    parse_profile,
)

__all__ = [
    "COMBO_VOTE",
    "BACKTEST_STRATEGIES",
    "DEFAULT_PROFILE",
    "PARAM_WHITELIST",
    "ResolvedStrategy",
    "IllegalParamError",
    "UnknownProfileError",
    "default_profile",
    "load_single_strategy_class",
    "build_strategy",
    "params_schema",
    "resolve_backtest_strategy",
]

# ---------------------------------------------------------------------------
# 策略键注册表（单一来源：app.py 的 Literal/标签表、cli.py 的 Choice、meta 的
# params_schema 全部以此为准，四方一致性由 tests/test_backtest_profile.py 钉死）
# ---------------------------------------------------------------------------
COMBO_VOTE = "combo_vote"

# 单策略键 → (strategies 子模块, 类名)；懒加载避免回测无关路径付出导入成本
SINGLE_STRATEGY_CLASSES: Dict[str, Tuple[str, str]] = {
    "ma": ("moving_average", "MovingAverageCross"),
    "rsi": ("rsi", "RSI"),
    "macd": ("macd", "MACD"),
    "bollinger": ("bollinger", "BollingerBands"),
    "donchian": ("donchian", "DonchianBreakout"),
}

# 回测可选策略键（有序）：5 个单策略 + combo_vote
BACKTEST_STRATEGIES: Tuple[str, ...] = (*SINGLE_STRATEGY_CLASSES, COMBO_VOTE)

# 每个策略**可经 params 调**的白名单（单策略用类构造器形参名；combo_vote 用 parse_profile
# 认得的扁平三件套键 + vote_threshold）。白名单外的键 → IllegalParamError（Web 转 422）。
_SINGLE_PARAM_ORDER: Dict[str, Tuple[str, ...]] = {
    "ma": ("fast", "slow"),
    "rsi": ("period", "oversold", "overbought"),
    "macd": ("fast", "slow", "signal"),
    "bollinger": ("period", "std_dev"),
    "donchian": ("window",),
}
_COMBO_PARAM_ORDER: Tuple[str, ...] = (
    "vote_threshold",
    "ma_fast", "ma_slow",
    "rsi_period", "rsi_oversold", "rsi_overbought",
    "bb_period", "bb_std",
)
PARAM_WHITELIST: Dict[str, frozenset] = {
    key: frozenset(names) for key, names in _SINGLE_PARAM_ORDER.items()
}
PARAM_WHITELIST[COMBO_VOTE] = frozenset(_COMBO_PARAM_ORDER)

# 全局默认三件套画像（MA + RSI + 布林带）。**不含** vote_threshold——阈值由调用方按
# config（get_vote_threshold）注入，缺省回落到 DEFAULT_VOTE_THRESHOLD。monitor 的
# ``_default_profile`` 引用 :func:`default_profile` 而非各自硬编码，消除双份。
DEFAULT_PROFILE: Dict[str, Any] = {
    "kind": COMBO_VOTE,
    "ma_fast": MA_DEFAULTS["fast"], "ma_slow": MA_DEFAULTS["slow"],
    "rsi_period": RSI_DEFAULTS["period"],
    "rsi_oversold": RSI_DEFAULTS["oversold"], "rsi_overbought": RSI_DEFAULTS["overbought"],
    "bb_period": BOLLINGER_DEFAULTS["period"], "bb_std": BOLLINGER_DEFAULTS["num_std"],
}


def default_profile(vote_threshold: Optional[int] = None) -> Dict[str, Any]:
    """全局默认三件套画像副本；给定 ``vote_threshold`` 时注入（否则交给 parse_profile 回落默认）。"""
    profile = dict(DEFAULT_PROFILE)
    if vote_threshold is not None:
        profile["vote_threshold"] = vote_threshold
    return profile


class IllegalParamError(ValueError):
    """params 含策略白名单外的键，或值非法（Web 端转 422，CLI 打印后退出）。"""


class UnknownProfileError(ValueError):
    """请求了 config.strategy_profiles 中不存在的画像名。"""

    def __init__(self, name: str, available: Optional[List[str]] = None):
        available = available or []
        hint = f"，可选：{', '.join(available)}" if available else "（config 未配置任何 strategy_profiles）"
        super().__init__(f"未知策略画像 {name!r}{hint}")
        self.name = name
        self.available = available


@dataclass(frozen=True)
class ResolvedStrategy:
    """解析结果：可喂 ``run_backtest`` 的 Strategy + provenance（来源/参数/投票规格）。

    - ``strategy``：构造好的 Strategy 实例（单策略类或 ComboVoteStateStrategy）；
    - ``strategy_key``：实际生效的策略键（profile 命中时强制为 ``combo_vote``）；
    - ``profile_source``：``"explicit"``/``"system"``/``"config:名字"``/``"default"``；
    - ``strategy_params``：扁平参数摘要（单策略 = 构造参数；combo = vote_threshold + 组件参数）；
    - ``spec``：combo_vote 的 ProfileSpec（单策略为 ``None``）。
    """

    strategy: Strategy
    strategy_key: str
    profile_source: str
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    spec: Optional[ProfileSpec] = None


# ---------------------------------------------------------------------------
# 策略构造
# ---------------------------------------------------------------------------

def load_single_strategy_class(strategy_key: str):
    """懒加载单策略类（``cls(**params)`` 构造用）。未知键 → ValueError。"""
    if strategy_key not in SINGLE_STRATEGY_CLASSES:
        raise ValueError(f"未知单策略键：{strategy_key!r}（可选 {', '.join(SINGLE_STRATEGY_CLASSES)}）")
    module_name, class_name = SINGLE_STRATEGY_CLASSES[strategy_key]
    module = importlib.import_module(f"ripple_tradePilot.strategies.{module_name}")
    return getattr(module, class_name)


def _single_defaults(strategy_key: str) -> Dict[str, Any]:
    """单策略类构造器默认参数（provenance 展示 + params_schema default 的同一来源）。"""
    signature = inspect.signature(load_single_strategy_class(strategy_key).__init__)
    return {
        name: signature.parameters[name].default
        for name in _SINGLE_PARAM_ORDER[strategy_key]
    }


def build_strategy(
    strategy_key: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    default_threshold: int = DEFAULT_VOTE_THRESHOLD,
    source: str = "explicit",
) -> Strategy:
    """显式 params 构造策略实例（walk-forward 工厂与 :func:`resolve_backtest_strategy` 共用）。

    - combo_vote：``{"kind": "combo_vote", **params}`` → parse_profile → ComboVoteStateStrategy；
    - 单策略：``cls(**params)``（params 须为构造器形参名，已由白名单校验）。

    参数值合法性由 parse_profile / 各策略构造器负责（非法值抛 ValueError）。
    """
    clean = {key: value for key, value in (params or {}).items() if value is not None}
    if strategy_key == COMBO_VOTE:
        spec = parse_profile(
            {"kind": COMBO_VOTE, **clean}, default_threshold=default_threshold, source=source
        )
        return ComboVoteStateStrategy(spec=spec)
    return load_single_strategy_class(strategy_key)(**clean)


def _validate_whitelist(strategy_key: str, params: Mapping[str, Any]) -> None:
    """params 键必须落在该策略白名单内，否则 IllegalParamError（列出越界键与允许集）。"""
    allowed = PARAM_WHITELIST.get(strategy_key)
    if allowed is None:
        raise ValueError(f"未知回测策略：{strategy_key!r}（可选 {', '.join(BACKTEST_STRATEGIES)}）")
    illegal = sorted(key for key in params if key not in allowed)
    if illegal:
        raise IllegalParamError(
            f"策略 {strategy_key!r} 不接受参数 {', '.join(illegal)}；可用参数：{', '.join(sorted(allowed))}"
        )


# ---------------------------------------------------------------------------
# params_schema（meta 端点下发，前端动态渲染参数表单）
# ---------------------------------------------------------------------------

def _json_type(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    return "float"


def single_param_schema(strategy_key: str) -> List[Dict[str, Any]]:
    """单策略参数字段表；``default`` 取自类构造器 ``inspect.signature``（一致性测试钉死）。"""
    signature = inspect.signature(load_single_strategy_class(strategy_key).__init__)
    fields: List[Dict[str, Any]] = []
    for name in _SINGLE_PARAM_ORDER[strategy_key]:
        default = signature.parameters[name].default
        fields.append({"name": name, "type": _json_type(default), "default": default})
    return fields


def combo_param_schema() -> List[Dict[str, Any]]:
    """combo_vote 参数字段表：vote_threshold（1~3）+ 三件套组件参数（默认取自 profile.py 常量）。"""
    return [
        {"name": "vote_threshold", "type": "int", "default": DEFAULT_VOTE_THRESHOLD, "min": 1, "max": 3},
        {"name": "ma_fast", "type": "int", "default": MA_DEFAULTS["fast"], "min": 1},
        {"name": "ma_slow", "type": "int", "default": MA_DEFAULTS["slow"], "min": 2},
        {"name": "rsi_period", "type": "int", "default": RSI_DEFAULTS["period"], "min": 2},
        {"name": "rsi_oversold", "type": "float", "default": RSI_DEFAULTS["oversold"]},
        {"name": "rsi_overbought", "type": "float", "default": RSI_DEFAULTS["overbought"]},
        {"name": "bb_period", "type": "int", "default": BOLLINGER_DEFAULTS["period"], "min": 2},
        {"name": "bb_std", "type": "float", "default": BOLLINGER_DEFAULTS["num_std"]},
    ]


def params_schema() -> Dict[str, List[Dict[str, Any]]]:
    """策略键 → 参数字段表（``/api/meta/backtest-options`` 下发；前端据此动态渲染）。"""
    return {
        key: (combo_param_schema() if key == COMBO_VOTE else single_param_schema(key))
        for key in BACKTEST_STRATEGIES
    }


# ---------------------------------------------------------------------------
# 缺省解析链（与 monitor 同口径）
# ---------------------------------------------------------------------------

def _safe_system_parameters(symbol: str) -> Optional[Dict[str, Any]]:
    """读 symbol 的系统策略参数（``user_store.get_system_strategy``）；无/异常 → None（离线安全）。"""
    try:
        from ..storage.user_store import get_system_strategy

        override = get_system_strategy(symbol, "stock")
    except Exception:
        return None
    if override and override.get("parameters"):
        return dict(override["parameters"])
    return None


def _resolve_default_chain(
    symbol: str, config: Mapping[str, Any], default_threshold: int
) -> Tuple[Dict[str, Any], str]:
    """缺省 combo_vote 解析链：config symbols 绑定 → DB system → 全局默认画像。

    与 monitor ``_collect_symbols``/``_refresh_strategy_profiles``/``_resolve_profile`` 同口径：
    config 里给该 symbol 绑了画像名且该名在 strategy_profiles → 用它（DB system 参数优先覆盖，
    覆盖时来源记 ``system`` 以诚实反映参数实际来自 DB）；否则查 DB system；再否则全局默认三件套。
    返回 ``(profile_dict, source_label)``。
    """
    profiles: Mapping[str, Any] = (config.get("strategy_profiles") or {}) if config else {}
    bound_name: Optional[str] = None
    for item in (config.get("symbols") or []) if config else []:
        if str(item.get("code", "")).upper() == str(symbol).upper():
            bound_name = item.get("strategy_profile")
            break

    if bound_name and bound_name in profiles:
        base = dict(profiles[bound_name])
        override = _safe_system_parameters(symbol)
        if override:
            return {**override, "kind": base.get("kind", COMBO_VOTE)}, "system"
        return base, f"config:{bound_name}"

    override = _safe_system_parameters(symbol)
    if override:
        return {**override, "kind": COMBO_VOTE}, "system"

    return default_profile(default_threshold), "default"


def _combo_result(
    profile_dict: Mapping[str, Any], source: str, default_threshold: int
) -> ResolvedStrategy:
    """profile_dict → combo_vote ResolvedStrategy（解析一次 ProfileSpec，构造器复用之）。"""
    spec = parse_profile(dict(profile_dict), default_threshold=default_threshold, source=source)
    return ResolvedStrategy(
        strategy=ComboVoteStateStrategy(spec=spec),
        strategy_key=COMBO_VOTE,
        profile_source=source,
        strategy_params={
            "vote_threshold": spec.vote_threshold,
            "components": spec.params_summary(),
        },
        spec=spec,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def resolve_backtest_strategy(
    *,
    symbol: str,
    strategy: str = COMBO_VOTE,
    params: Optional[Mapping[str, Any]] = None,
    profile: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    default_threshold: Optional[int] = None,
) -> ResolvedStrategy:
    """按优先级解析回测应跑的策略与参数，返回带 provenance 的 :class:`ResolvedStrategy`。

    优先级：显式 ``params`` → ``profile="system"`` → ``profile=名字`` → 缺省链
    （单策略走类默认；combo_vote 走 config 绑定 → DB system → 全局默认画像）。

    - ``symbol``：标的代码（DB system 策略与 config symbols 绑定按它查）；
    - ``strategy``：策略键（``BACKTEST_STRATEGIES`` 之一）；profile 命中时强制 combo_vote；
    - ``params``：显式参数（按 ``strategy`` 白名单校验，越界 → IllegalParamError）；
    - ``profile``：``"system"`` | config 画像名 | ``None``；
    - ``config``：已加载的 config（缺省 ``{}``，离线安全）；
    - ``default_threshold``：combo 投票阈值缺省（None → ``get_vote_threshold(config)``）。

    非法 params / 未知画像名分别抛 :class:`IllegalParamError` / :class:`UnknownProfileError`
    （均为 ValueError 子类），调用方（Web 422 / CLI 退出）负责转译。
    """
    config = dict(config or {})
    if default_threshold is None:
        default_threshold = get_vote_threshold(config)
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}

    # 1. 显式 params 最优先（strategy 键决定构造器）
    if clean_params:
        _validate_whitelist(strategy, clean_params)
        if strategy == COMBO_VOTE:
            result = _combo_result({"kind": COMBO_VOTE, **clean_params}, "explicit", default_threshold)
            return result
        built = build_strategy(strategy, clean_params, default_threshold=default_threshold, source="explicit")
        return ResolvedStrategy(
            strategy=built,
            strategy_key=strategy,
            profile_source="explicit",
            strategy_params=dict(clean_params),
            spec=None,
        )

    # 2. profile="system"：DB 系统策略 → combo_vote
    if profile == "system":
        override = _safe_system_parameters(symbol)
        if override:
            return _combo_result({**override, "kind": COMBO_VOTE}, "system", default_threshold)
        # 未配置系统策略 → 落回缺省链（诚实标记真实来源）
        prof, source = _resolve_default_chain(symbol, config, default_threshold)
        return _combo_result(prof, source, default_threshold)

    # 3. profile=名字：config.strategy_profiles → combo_vote
    if profile:
        profiles: Mapping[str, Any] = config.get("strategy_profiles") or {}
        if profile not in profiles:
            raise UnknownProfileError(profile, sorted(profiles))
        return _combo_result(dict(profiles[profile]), f"config:{profile}", default_threshold)

    # 4. 缺省
    if strategy == COMBO_VOTE:
        prof, source = _resolve_default_chain(symbol, config, default_threshold)
        return _combo_result(prof, source, default_threshold)

    # 单策略缺省 → 类默认参数构造（与改造前 cls() 完全一致）
    if strategy not in SINGLE_STRATEGY_CLASSES:
        raise ValueError(f"未知回测策略：{strategy!r}（可选 {', '.join(BACKTEST_STRATEGIES)}）")
    return ResolvedStrategy(
        strategy=build_strategy(strategy, {}, default_threshold=default_threshold, source="default"),
        strategy_key=strategy,
        profile_source="default",
        strategy_params=_single_defaults(strategy),
        spec=None,
    )
