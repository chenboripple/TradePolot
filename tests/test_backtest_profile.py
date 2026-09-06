"""A5 核心解析链 ``signals/backtest_profile.py`` 的单元测试（离线，无 DB / 无网络）。

覆盖四步优先级（显式 params > profile=system > profile=名字 > 缺省链）、白名单校验、
``params_schema`` 与策略类 ``inspect.signature`` 默认值一致、``build_strategy`` 构造、
``default_profile`` 单一常量。DB system 路径用 patch ``_safe_system_parameters`` 隔离
（端到端落库见 ``test_app_backtest.py`` / ``test_cli_backtest.py``）。
"""

import inspect
import unittest
from unittest.mock import patch

from ripple_tradePilot.indicators import DEFAULT_VOTE_THRESHOLD
from ripple_tradePilot.signals import backtest_profile as bp
from ripple_tradePilot.signals.backtest_profile import (
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
from ripple_tradePilot.signals.combo_strategy import ComboVoteStateStrategy
from ripple_tradePilot.signals.profile import (
    BOLLINGER_DEFAULTS,
    MA_DEFAULTS,
    RSI_DEFAULTS,
)

SINGLE_KEYS = ("ma", "rsi", "macd", "bollinger", "donchian")
SYMBOL = "600000.SH"

# 一个合法的 combo 画像（config.strategy_profiles 与 DB system 参数共用此形状）
_COMBO_PROFILE = {
    "kind": COMBO_VOTE,
    "ma_fast": 8, "ma_slow": 21,
    "rsi_period": 10, "rsi_oversold": 25.0, "rsi_overbought": 75.0,
    "bb_period": 18, "bb_std": 2.5,
    "vote_threshold": 3,
}


class _NoDbTestCase(unittest.TestCase):
    """默认把 ``_safe_system_parameters`` patch 成 None，确保任何用例都不触真实 DB。

    需要 DB system 命中的用例在自身内层再 patch 覆盖（with 块优先于 setUp 的 patch）。
    """

    def setUp(self):
        patcher = patch.object(bp, "_safe_system_parameters", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _system_override(self, params):
        """临时让 _safe_system_parameters 返回给定参数（模拟 DB 里存了系统策略）。"""
        return patch.object(bp, "_safe_system_parameters", return_value=dict(params))


# ---------------------------------------------------------------------------
# 注册表 / 常量
# ---------------------------------------------------------------------------
class RegistryTest(unittest.TestCase):
    def test_backtest_strategies_ordered(self):
        self.assertEqual(
            BACKTEST_STRATEGIES,
            ("ma", "rsi", "macd", "bollinger", "donchian", "combo_vote"),
        )

    def test_whitelist_keys_match_registry(self):
        self.assertEqual(set(PARAM_WHITELIST), set(BACKTEST_STRATEGIES))

    def test_whitelist_contents(self):
        self.assertEqual(PARAM_WHITELIST["ma"], frozenset({"fast", "slow"}))
        self.assertEqual(PARAM_WHITELIST["rsi"], frozenset({"period", "oversold", "overbought"}))
        self.assertEqual(PARAM_WHITELIST["macd"], frozenset({"fast", "slow", "signal"}))
        self.assertEqual(PARAM_WHITELIST["bollinger"], frozenset({"period", "std_dev"}))
        self.assertEqual(PARAM_WHITELIST["donchian"], frozenset({"window"}))
        self.assertIn("vote_threshold", PARAM_WHITELIST[COMBO_VOTE])
        self.assertIn("ma_fast", PARAM_WHITELIST[COMBO_VOTE])

    def test_default_profile_has_no_threshold(self):
        # 阈值由调用方按 config 注入，常量本身不含 vote_threshold
        self.assertNotIn("vote_threshold", DEFAULT_PROFILE)
        self.assertEqual(DEFAULT_PROFILE["kind"], COMBO_VOTE)

    def test_default_profile_values_match_component_constants(self):
        self.assertEqual(DEFAULT_PROFILE["ma_fast"], MA_DEFAULTS["fast"])
        self.assertEqual(DEFAULT_PROFILE["ma_slow"], MA_DEFAULTS["slow"])
        self.assertEqual(DEFAULT_PROFILE["rsi_period"], RSI_DEFAULTS["period"])
        self.assertEqual(DEFAULT_PROFILE["rsi_oversold"], RSI_DEFAULTS["oversold"])
        self.assertEqual(DEFAULT_PROFILE["rsi_overbought"], RSI_DEFAULTS["overbought"])
        self.assertEqual(DEFAULT_PROFILE["bb_period"], BOLLINGER_DEFAULTS["period"])
        self.assertEqual(DEFAULT_PROFILE["bb_std"], BOLLINGER_DEFAULTS["num_std"])

    def test_default_profile_injects_threshold(self):
        self.assertEqual(default_profile(3)["vote_threshold"], 3)
        self.assertNotIn("vote_threshold", default_profile(None))
        self.assertNotIn("vote_threshold", default_profile())

    def test_default_profile_returns_copy(self):
        profile = default_profile()
        profile["ma_fast"] = 999
        self.assertEqual(DEFAULT_PROFILE["ma_fast"], MA_DEFAULTS["fast"])  # 常量未被污染


# ---------------------------------------------------------------------------
# build_strategy / load_single_strategy_class
# ---------------------------------------------------------------------------
class BuildStrategyTest(unittest.TestCase):
    def test_load_single_class(self):
        from ripple_tradePilot.strategies.rsi import RSI

        self.assertIs(load_single_strategy_class("rsi"), RSI)

    def test_load_single_unknown_raises(self):
        with self.assertRaises(ValueError):
            load_single_strategy_class("combo_vote")  # 非单策略键
        with self.assertRaises(ValueError):
            load_single_strategy_class("nope")

    def test_build_single_with_params(self):
        from ripple_tradePilot.strategies.moving_average import MovingAverageCross

        strategy = build_strategy("ma", {"fast": 3, "slow": 9})
        self.assertIsInstance(strategy, MovingAverageCross)
        self.assertEqual(strategy.fast, 3)
        self.assertEqual(strategy.slow, 9)

    def test_build_single_filters_none(self):
        from ripple_tradePilot.strategies.rsi import RSI

        strategy = build_strategy("rsi", {"period": None})
        self.assertIsInstance(strategy, RSI)
        self.assertEqual(strategy.period, RSI_DEFAULTS["period"])  # None 被丢弃 → 用默认

    def test_build_combo(self):
        strategy = build_strategy(COMBO_VOTE, {"vote_threshold": 3, "ma_fast": 8})
        self.assertIsInstance(strategy, ComboVoteStateStrategy)

    def test_build_unknown_raises(self):
        with self.assertRaises(ValueError):
            build_strategy("nope", {})


# ---------------------------------------------------------------------------
# 优先级 1：显式 params
# ---------------------------------------------------------------------------
class ExplicitParamsTest(_NoDbTestCase):
    def test_single_explicit(self):
        from ripple_tradePilot.strategies.rsi import RSI

        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy="rsi", params={"period": 7})
        self.assertIsInstance(resolved, ResolvedStrategy)
        self.assertEqual(resolved.strategy_key, "rsi")
        self.assertEqual(resolved.profile_source, "explicit")
        self.assertEqual(resolved.strategy_params, {"period": 7})
        self.assertIsNone(resolved.spec)
        self.assertIsInstance(resolved.strategy, RSI)
        self.assertEqual(resolved.strategy.period, 7)

    def test_single_explicit_partial_keeps_only_given(self):
        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy="ma", params={"fast": 3})
        self.assertEqual(resolved.strategy_params, {"fast": 3})  # 只记显式给的
        self.assertEqual(resolved.strategy.fast, 3)
        self.assertEqual(resolved.strategy.slow, MA_DEFAULTS["slow"])  # 未给的用类默认

    def test_combo_explicit_threshold(self):
        resolved = resolve_backtest_strategy(
            symbol=SYMBOL, strategy=COMBO_VOTE, params={"vote_threshold": 1}
        )
        self.assertEqual(resolved.strategy_key, COMBO_VOTE)
        self.assertEqual(resolved.profile_source, "explicit")
        self.assertIsInstance(resolved.strategy, ComboVoteStateStrategy)
        self.assertEqual(resolved.strategy_params["vote_threshold"], 1)
        self.assertIn("components", resolved.strategy_params)
        self.assertIsNotNone(resolved.spec)

    def test_none_params_fall_through_to_default(self):
        # params 全 None → 视作无显式参数 → 单策略走缺省
        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy="rsi", params={"period": None})
        self.assertEqual(resolved.profile_source, "default")

    def test_illegal_param_single_raises(self):
        with self.assertRaises(IllegalParamError) as ctx:
            resolve_backtest_strategy(symbol=SYMBOL, strategy="rsi", params={"bogus": 1})
        self.assertIn("bogus", str(ctx.exception))
        self.assertIn("period", str(ctx.exception))  # 提示信息列出可用参数

    def test_illegal_param_combo_raises(self):
        # "fast" 是单策略 ma 的参数，不在 combo 白名单（combo 用 ma_fast）
        with self.assertRaises(IllegalParamError):
            resolve_backtest_strategy(symbol=SYMBOL, strategy=COMBO_VOTE, params={"fast": 5})

    def test_params_beat_profile(self):
        # 同时给 params 与 profile → params 最显式，胜出
        config = {"strategy_profiles": {"aggr": dict(_COMBO_PROFILE)}}
        resolved = resolve_backtest_strategy(
            symbol=SYMBOL, strategy=COMBO_VOTE,
            params={"vote_threshold": 1}, profile="aggr", config=config,
        )
        self.assertEqual(resolved.profile_source, "explicit")
        self.assertEqual(resolved.strategy_params["vote_threshold"], 1)


# ---------------------------------------------------------------------------
# 优先级 2：profile="system"
# ---------------------------------------------------------------------------
class SystemProfileTest(_NoDbTestCase):
    def test_system_profile_uses_db_params(self):
        with self._system_override(_COMBO_PROFILE):
            resolved = resolve_backtest_strategy(
                symbol=SYMBOL, strategy=COMBO_VOTE, profile="system"
            )
        self.assertEqual(resolved.profile_source, "system")
        self.assertEqual(resolved.strategy_key, COMBO_VOTE)
        self.assertEqual(resolved.strategy_params["vote_threshold"], 3)
        self.assertIsInstance(resolved.strategy, ComboVoteStateStrategy)

    def test_system_profile_without_db_falls_to_default_chain(self):
        # _safe_system_parameters 返回 None（setUp 默认）→ 诚实落回缺省链
        resolved = resolve_backtest_strategy(
            symbol=SYMBOL, strategy=COMBO_VOTE, profile="system", config={}
        )
        self.assertEqual(resolved.profile_source, "default")

    def test_system_profile_on_single_strategy_still_combo(self):
        # profile 比 strategy 下拉更强：给定 profile 一律解析为 combo_vote
        with self._system_override(_COMBO_PROFILE):
            resolved = resolve_backtest_strategy(
                symbol=SYMBOL, strategy="rsi", profile="system"
            )
        self.assertEqual(resolved.strategy_key, COMBO_VOTE)
        self.assertEqual(resolved.profile_source, "system")


# ---------------------------------------------------------------------------
# 优先级 3：profile=名字（config.strategy_profiles）
# ---------------------------------------------------------------------------
class ConfigProfileTest(_NoDbTestCase):
    def test_named_profile(self):
        config = {"strategy_profiles": {"aggr": dict(_COMBO_PROFILE)}}
        resolved = resolve_backtest_strategy(
            symbol=SYMBOL, strategy=COMBO_VOTE, profile="aggr", config=config
        )
        self.assertEqual(resolved.profile_source, "config:aggr")
        self.assertEqual(resolved.strategy_key, COMBO_VOTE)
        self.assertEqual(resolved.strategy_params["vote_threshold"], 3)

    def test_named_profile_beats_single_strategy_dropdown(self):
        config = {"strategy_profiles": {"aggr": dict(_COMBO_PROFILE)}}
        resolved = resolve_backtest_strategy(
            symbol=SYMBOL, strategy="ma", profile="aggr", config=config
        )
        self.assertEqual(resolved.strategy_key, COMBO_VOTE)
        self.assertEqual(resolved.profile_source, "config:aggr")

    def test_unknown_profile_raises(self):
        config = {"strategy_profiles": {"aggr": dict(_COMBO_PROFILE)}}
        with self.assertRaises(UnknownProfileError) as ctx:
            resolve_backtest_strategy(
                symbol=SYMBOL, strategy=COMBO_VOTE, profile="nope", config=config
            )
        self.assertEqual(ctx.exception.name, "nope")
        self.assertEqual(ctx.exception.available, ["aggr"])
        self.assertIn("nope", str(ctx.exception))

    def test_unknown_profile_empty_config(self):
        with self.assertRaises(UnknownProfileError) as ctx:
            resolve_backtest_strategy(
                symbol=SYMBOL, strategy=COMBO_VOTE, profile="nope", config={}
            )
        self.assertEqual(ctx.exception.available, [])


# ---------------------------------------------------------------------------
# 优先级 4：缺省链
# ---------------------------------------------------------------------------
class DefaultChainTest(_NoDbTestCase):
    def test_single_strategy_default(self):
        from ripple_tradePilot.strategies.bollinger import BollingerBands

        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy="bollinger", config={})
        self.assertEqual(resolved.profile_source, "default")
        self.assertEqual(resolved.strategy_key, "bollinger")
        self.assertIsNone(resolved.spec)
        self.assertIsInstance(resolved.strategy, BollingerBands)
        # 缺省参数 == 类构造器默认（与改造前 cls() 完全一致）
        self.assertEqual(
            resolved.strategy_params,
            {"period": BOLLINGER_DEFAULTS["period"], "std_dev": BOLLINGER_DEFAULTS["num_std"]},
        )

    def test_all_single_defaults_match_signature(self):
        for key in SINGLE_KEYS:
            with self.subTest(strategy=key):
                resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy=key, config={})
                cls = load_single_strategy_class(key)
                signature = inspect.signature(cls.__init__)
                expected = {
                    name: signature.parameters[name].default
                    for name in resolved.strategy_params
                }
                self.assertEqual(resolved.strategy_params, expected)
                self.assertEqual(resolved.profile_source, "default")

    def test_combo_default_global(self):
        # 无 config 绑定、无 DB system → 全局默认三件套
        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy=COMBO_VOTE, config={})
        self.assertEqual(resolved.profile_source, "default")
        self.assertEqual(resolved.strategy_key, COMBO_VOTE)
        self.assertEqual(resolved.strategy_params["vote_threshold"], DEFAULT_VOTE_THRESHOLD)
        self.assertIsInstance(resolved.strategy, ComboVoteStateStrategy)

    def test_combo_default_threshold_override(self):
        resolved = resolve_backtest_strategy(
            symbol=SYMBOL, strategy=COMBO_VOTE, config={}, default_threshold=3
        )
        self.assertEqual(resolved.strategy_params["vote_threshold"], 3)

    def test_combo_default_config_symbols_binding(self):
        config = {
            "symbols": [{"code": SYMBOL, "strategy_profile": "aggr"}],
            "strategy_profiles": {"aggr": dict(_COMBO_PROFILE)},
        }
        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy=COMBO_VOTE, config=config)
        self.assertEqual(resolved.profile_source, "config:aggr")
        self.assertEqual(resolved.strategy_params["vote_threshold"], 3)

    def test_combo_default_binding_db_override_wins(self):
        # config 绑了画像名，但 DB system 有参数覆盖 → 诚实标记来源为 system
        config = {
            "symbols": [{"code": SYMBOL, "strategy_profile": "aggr"}],
            "strategy_profiles": {"aggr": dict(_COMBO_PROFILE)},
        }
        override = dict(_COMBO_PROFILE, vote_threshold=1)
        with self._system_override(override):
            resolved = resolve_backtest_strategy(
                symbol=SYMBOL, strategy=COMBO_VOTE, config=config
            )
        self.assertEqual(resolved.profile_source, "system")
        self.assertEqual(resolved.strategy_params["vote_threshold"], 1)

    def test_combo_default_db_system_no_binding(self):
        with self._system_override(_COMBO_PROFILE):
            resolved = resolve_backtest_strategy(
                symbol=SYMBOL, strategy=COMBO_VOTE, config={}
            )
        self.assertEqual(resolved.profile_source, "system")

    def test_combo_default_binding_for_other_symbol_ignored(self):
        # config 给别的 symbol 绑了画像，本 symbol 未绑 → 不命中，落全局默认
        config = {
            "symbols": [{"code": "000001.SZ", "strategy_profile": "aggr"}],
            "strategy_profiles": {"aggr": dict(_COMBO_PROFILE)},
        }
        resolved = resolve_backtest_strategy(symbol=SYMBOL, strategy=COMBO_VOTE, config=config)
        self.assertEqual(resolved.profile_source, "default")

    def test_unknown_single_strategy_raises(self):
        with self.assertRaises(ValueError):
            resolve_backtest_strategy(symbol=SYMBOL, strategy="nope", config={})


# ---------------------------------------------------------------------------
# params_schema（meta 端点下发，前端动态渲染）
# ---------------------------------------------------------------------------
class ParamsSchemaTest(unittest.TestCase):
    def test_schema_keys_match_registry(self):
        self.assertEqual(set(params_schema()), set(BACKTEST_STRATEGIES))

    def test_schema_field_names_match_whitelist(self):
        schema = params_schema()
        for key in BACKTEST_STRATEGIES:
            with self.subTest(strategy=key):
                names = {field["name"] for field in schema[key]}
                self.assertEqual(names, set(PARAM_WHITELIST[key]))

    def test_single_schema_defaults_match_signature(self):
        # 关键一致性：schema 的 default 必须 == 策略类构造器 inspect.signature 默认值
        schema = params_schema()
        for key in SINGLE_KEYS:
            cls = load_single_strategy_class(key)
            signature = inspect.signature(cls.__init__)
            for field in schema[key]:
                with self.subTest(strategy=key, param=field["name"]):
                    self.assertEqual(
                        field["default"],
                        signature.parameters[field["name"]].default,
                    )

    def test_single_schema_field_shape(self):
        schema = params_schema()
        for key in SINGLE_KEYS:
            for field in schema[key]:
                self.assertIn(field["type"], ("int", "float"))
                self.assertIn("default", field)

    def test_combo_schema_shape(self):
        schema = params_schema()[COMBO_VOTE]
        names = [field["name"] for field in schema]
        self.assertEqual(names[0], "vote_threshold")
        self.assertEqual(
            names,
            ["vote_threshold", "ma_fast", "ma_slow", "rsi_period",
             "rsi_oversold", "rsi_overbought", "bb_period", "bb_std"],
        )
        threshold = schema[0]
        self.assertEqual(threshold["default"], DEFAULT_VOTE_THRESHOLD)
        self.assertEqual(threshold["min"], 1)
        self.assertEqual(threshold["max"], 3)
        by_name = {field["name"]: field for field in schema}
        self.assertEqual(by_name["ma_fast"]["default"], MA_DEFAULTS["fast"])
        self.assertEqual(by_name["ma_slow"]["default"], MA_DEFAULTS["slow"])
        self.assertEqual(by_name["rsi_period"]["default"], RSI_DEFAULTS["period"])
        self.assertEqual(by_name["rsi_oversold"]["default"], RSI_DEFAULTS["oversold"])
        self.assertEqual(by_name["rsi_overbought"]["default"], RSI_DEFAULTS["overbought"])
        self.assertEqual(by_name["bb_period"]["default"], BOLLINGER_DEFAULTS["period"])
        self.assertEqual(by_name["bb_std"]["default"], BOLLINGER_DEFAULTS["num_std"])

    def test_combo_schema_types(self):
        by_name = {field["name"]: field for field in params_schema()[COMBO_VOTE]}
        self.assertEqual(by_name["vote_threshold"]["type"], "int")
        self.assertEqual(by_name["ma_fast"]["type"], "int")
        self.assertEqual(by_name["rsi_oversold"]["type"], "float")
        self.assertEqual(by_name["bb_std"]["type"], "float")


if __name__ == "__main__":
    unittest.main()
