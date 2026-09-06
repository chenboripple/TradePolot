"""策略画像统一解析器：三种 profile 结构 → ProfileSpec（唯一口径）。

历史上同一份 config 画像在三处被分别解析且语义互异：
- dashboard `_profile_parameters`（扁平键优先，嵌套 ma/rsi/bb 兜底）；
- monitor `_run_combo_vote_profile`（仅扁平键）与 `_run_grid_combo_profile`（仅嵌套键）；
- monitor `_build_combo_components`（components 列表，嵌套节在组件内）。

parse_profile 一次吃掉全部结构，输出带来源标注的 ProfileSpec，
供 dashboard / monitor / 回测 / 台账共同消费，杜绝再次分裂。

canonical 参数名（与 indicators.py 函数签名一致）：
- ma:        fast, slow                    （兼容 ma_fast/ma_slow、ma.{fast,slow}）
- rsi:       period, oversold, overbought  （兼容 rsi_* 扁平键、rsi.{...}）
- bollinger: period, num_std               （兼容 bb_period/bb_std、bb.{period,std_dev}）
- macd:      fast, slow, signal            （兼容 macd.{...}；zero_cross 忽略——状态票语义）
- trend:     short, medium, long
- donchian:  window

kind == "breakout" 是唯一豁免：保留 monitor 旧路径（TODO: 后续以
donchian+rsi 组件表达后移除），parse_profile 对其抛 UnsupportedProfileKindError，
调用方捕获后回退。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..indicators import DEFAULT_VOTE_THRESHOLD

# 三件套画像（MA + RSI + 布林带）的 kind 别名
TRIO_KINDS = ("combo_vote", "grid_combo")
# 单策略画像：kind 本身即组件类型
SINGLE_KINDS = ("ma", "rsi", "bollinger", "macd", "trend", "donchian")

MA_DEFAULTS: Dict[str, Any] = {"fast": 5, "slow": 20}
RSI_DEFAULTS: Dict[str, Any] = {"period": 14, "oversold": 30.0, "overbought": 70.0}
BOLLINGER_DEFAULTS: Dict[str, Any] = {"period": 20, "num_std": 2.0}
MACD_DEFAULTS: Dict[str, Any] = {"fast": 12, "slow": 26, "signal": 9}
TREND_DEFAULTS: Dict[str, Any] = {"short": 5, "medium": 20, "long": 60}
DONCHIAN_DEFAULTS: Dict[str, Any] = {"window": 20}


class UnsupportedProfileKindError(ValueError):
    """profile kind 暂不支持统一投票（目前仅 breakout），调用方应回退旧路径。"""

    def __init__(self, kind: str):
        super().__init__(f"画像 kind {kind!r} 暂不支持统一投票口径，请沿用旧评估路径")
        self.kind = kind


@dataclass(frozen=True)
class ComponentSpec:
    """单个投票组件的类型与 canonical 参数。"""

    kind: str
    name: str
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_component(self.kind, self.params)


@dataclass(frozen=True)
class ProfileSpec:
    """解析后的完整画像：组件有序元组 + 投票阈值 + 来源标注。"""

    kind: str
    components: Tuple[ComponentSpec, ...]
    vote_threshold: int
    source: str = "config"
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def component_names(self) -> Tuple[str, ...]:
        return tuple(component.name for component in self.components)

    def params_summary(self) -> Dict[str, Dict[str, Any]]:
        """组件名 → 参数副本（provenance 展示 / 落库用）。"""
        return {
            component.name: dict(component.params) for component in self.components
        }


def parse_profile(
    profile: Optional[Mapping[str, Any]],
    default_threshold: int = DEFAULT_VOTE_THRESHOLD,
    source: str = "config",
) -> ProfileSpec:
    """把扁平 / grid 嵌套 / components 列表 / 单策略四种结构解析为 ProfileSpec。

    - default_threshold：画像未写 vote_threshold 时的全局默认（config_loader.get_vote_threshold）；
    - source：来源标注（"config" / "system" / "explicit" / …），进台账与回测 provenance；
    - 阈值夹取到 [1, 组件数]；非法参数抛 ValueError。
    """
    if isinstance(profile, ProfileSpec):
        return profile
    profile_dict: Dict[str, Any] = dict(profile or {})
    kind = str(profile_dict.get("kind") or "combo_vote").strip().lower()

    if kind == "breakout":
        raise UnsupportedProfileKindError(kind)

    if kind in TRIO_KINDS:
        if "components" in profile_dict:
            components = _parse_components_list(profile_dict["components"])
        else:
            components = _trio_components(profile_dict)
    elif kind in SINGLE_KINDS:
        components = [_single_component(kind, profile_dict)]
    elif "components" in profile_dict:
        # 无 kind 但带 components 的画像（monitor 旧路由靠 components 键识别）
        components = _parse_components_list(profile_dict["components"])
    else:
        raise ValueError(f"未知画像 kind: {kind!r}")

    if not components:
        raise ValueError("画像至少需要一个投票组件")

    threshold = _parse_threshold(
        profile_dict.get("vote_threshold", default_threshold),
        default_threshold,
        len(components),
    )
    return ProfileSpec(
        kind=kind,
        components=tuple(components),
        vote_threshold=threshold,
        source=source,
        raw=profile_dict,
    )


# ---------------------------------------------------------------------------
# 内部解析
# ---------------------------------------------------------------------------

def _trio_components(profile: Mapping[str, Any]) -> List[ComponentSpec]:
    """MA + RSI + 布林带三件套；扁平键优先、嵌套节兜底（dashboard 旧口径）。"""
    ma_section = profile.get("ma") or {}
    rsi_section = profile.get("rsi") or {}
    bb_section = profile.get("bb") or {}
    return [
        ComponentSpec(
            kind="ma",
            name="ma",
            params={
                "fast": _pick_int(profile, ma_section, "ma_fast", "fast", MA_DEFAULTS["fast"]),
                "slow": _pick_int(profile, ma_section, "ma_slow", "slow", MA_DEFAULTS["slow"]),
            },
        ),
        ComponentSpec(
            kind="rsi",
            name="rsi",
            params={
                "period": _pick_int(profile, rsi_section, "rsi_period", "period", RSI_DEFAULTS["period"]),
                "oversold": _pick_float(profile, rsi_section, "rsi_oversold", "oversold", RSI_DEFAULTS["oversold"]),
                "overbought": _pick_float(profile, rsi_section, "rsi_overbought", "overbought", RSI_DEFAULTS["overbought"]),
            },
        ),
        ComponentSpec(
            kind="bollinger",
            name="bollinger",
            params={
                "period": _pick_int(profile, bb_section, "bb_period", "period", BOLLINGER_DEFAULTS["period"]),
                "num_std": _pick_float(profile, bb_section, "bb_std", "std_dev", BOLLINGER_DEFAULTS["num_std"]),
            },
        ),
    ]


def _parse_components_list(raw_components: Any) -> List[ComponentSpec]:
    """components 列表结构（monitor _build_combo_components 旧格式 + 直接键便利写法）。"""
    if not isinstance(raw_components, (list, tuple)):
        raise ValueError("components 必须是列表")
    components: List[ComponentSpec] = []
    for item in raw_components:
        if not isinstance(item, Mapping):
            raise ValueError(f"组件必须是映射: {item!r}")
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if kind not in SINGLE_KINDS:
            raise ValueError(f"不支持的组件类型: {kind or '(缺失)'}")
        name = str(item.get("name") or kind)
        components.append(ComponentSpec(kind=kind, name=name, params=_component_params(kind, item)))
    return components


def _single_component(kind: str, profile: Mapping[str, Any]) -> ComponentSpec:
    """单策略画像（kind = ma/rsi/bollinger/macd/...）表达为单组件 ProfileSpec。"""
    return ComponentSpec(kind=kind, name=kind, params=_component_params(kind, profile))


def _component_params(kind: str, container: Mapping[str, Any]) -> Dict[str, Any]:
    """从容器（profile 或组件字典）提取 canonical 参数：
    嵌套节 → ``params`` 子节 → 容器内直接键 → 扁平前缀键 → 默认值。"""
    # canonical 的组件字典形态是 {"name","kind","params":{...}}（ml/dataset 的训练画像快照、
    # 系统画像序列化都用它）。不抬平就会整组静默回落默认值——D5 serve 端拿默认 MA5/20 去喂
    # 一个按 MA5/10 训练的模型，是典型的 train/serve 偏斜，且没有任何报错。
    nested_params = container.get("params")
    if isinstance(nested_params, Mapping):
        container = {**container, **nested_params}
    section: Mapping[str, Any] = {}
    for section_key in _SECTION_KEYS.get(kind, ()):
        nested = container.get(section_key)
        if isinstance(nested, Mapping):
            section = nested
            break
    defaults = _DEFAULTS[kind]
    flat_prefix = _FLAT_PREFIX.get(kind)
    params: Dict[str, Any] = {}
    for canonical, default in defaults.items():
        value: Any = default
        # 嵌套节里的旧键名（std_dev → num_std）
        for section_alias in _SECTION_ALIASES.get((kind, canonical), (canonical,)):
            if section_alias in section:
                value = section[section_alias]
                break
        else:
            if canonical in container:
                value = container[canonical]
            elif flat_prefix:
                for flat_key in _FLAT_ALIASES.get((kind, canonical), (f"{flat_prefix}_{canonical}",)):
                    if flat_key in container:
                        value = container[flat_key]
                        break
        params[canonical] = value
    # 数值化
    int_keys = _INT_KEYS[kind]
    for key, value in params.items():
        try:
            params[key] = int(value) if key in int_keys else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{kind} 组件参数 {key}={value!r} 不是数值")
    return params


_SECTION_KEYS: Dict[str, Tuple[str, ...]] = {
    "ma": ("ma",),
    "rsi": ("rsi",),
    "bollinger": ("bb", "bollinger"),
    "macd": ("macd",),
    "trend": ("trend",),
    "donchian": ("donchian",),
}
_SECTION_ALIASES: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("bollinger", "num_std"): ("num_std", "std_dev", "std"),
}
_FLAT_PREFIX: Dict[str, str] = {
    "ma": "ma",
    "rsi": "rsi",
    "bollinger": "bb",
    "macd": "macd",
    "trend": "trend",
    "donchian": "donchian",
}
_FLAT_ALIASES: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("bollinger", "num_std"): ("bb_std", "bb_num_std"),
}
_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "ma": MA_DEFAULTS,
    "rsi": RSI_DEFAULTS,
    "bollinger": BOLLINGER_DEFAULTS,
    "macd": MACD_DEFAULTS,
    "trend": TREND_DEFAULTS,
    "donchian": DONCHIAN_DEFAULTS,
}
_INT_KEYS: Dict[str, frozenset] = {
    "ma": frozenset({"fast", "slow"}),
    "rsi": frozenset({"period"}),
    "bollinger": frozenset({"period"}),
    "macd": frozenset({"fast", "slow", "signal"}),
    "trend": frozenset({"short", "medium", "long"}),
    "donchian": frozenset({"window"}),
}


def _validate_component(kind: str, params: Mapping[str, Any]) -> None:
    if kind == "ma":
        if params["fast"] < 1 or params["slow"] <= params["fast"]:
            raise ValueError(f"ma 参数要求 slow > fast >= 1，实际 fast={params['fast']}, slow={params['slow']}")
    elif kind == "rsi":
        if params["period"] < 1:
            raise ValueError("rsi period 必须 >= 1")
        # 只要求 oversold < overbought：阈值越界（如 overbought=101 / oversold=-1）
        # 是合法的"禁用该侧投票"惯用法（RSI 恒在 0-100，越界侧永不触发），
        # 旧 dashboard 即如此使用，不能拒。
        if not params["oversold"] < params["overbought"]:
            raise ValueError(
                f"rsi 参数要求 oversold < overbought，实际 "
                f"{params['oversold']}/{params['overbought']}"
            )
    elif kind == "bollinger":
        if params["period"] < 2 or params["num_std"] <= 0:
            raise ValueError("bollinger 参数要求 period >= 2 且 num_std > 0")
    elif kind == "macd":
        if params["fast"] < 1 or params["slow"] <= params["fast"] or params["signal"] < 1:
            raise ValueError(
                f"macd 参数要求 slow > fast >= 1 且 signal >= 1，实际 "
                f"{params['fast']}/{params['slow']}/{params['signal']}"
            )
    elif kind == "trend":
        if not 1 <= params["short"] < params["medium"] < params["long"]:
            raise ValueError(
                f"trend 参数要求 short < medium < long，实际 "
                f"{params['short']}/{params['medium']}/{params['long']}"
            )
    elif kind == "donchian":
        if params["window"] < 2:
            raise ValueError("donchian window 必须 >= 2")


def _pick_int(
    container: Mapping[str, Any],
    section: Mapping[str, Any],
    flat_key: str,
    section_key: str,
    default: int,
) -> int:
    value = container.get(flat_key, section.get(section_key, default))
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"画像参数 {flat_key}={value!r} 不是整数")


def _pick_float(
    container: Mapping[str, Any],
    section: Mapping[str, Any],
    flat_key: str,
    section_key: str,
    default: float,
) -> float:
    value = container.get(flat_key, section.get(section_key, default))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"画像参数 {flat_key}={value!r} 不是数值")


def _parse_threshold(raw: Any, default_threshold: int, component_count: int) -> int:
    """阈值夹取到 [1, 组件数]（单组件画像自然退化为 1）。"""
    try:
        threshold = int(raw)
    except (TypeError, ValueError):
        threshold = int(default_threshold)
    return max(1, min(threshold, component_count))
