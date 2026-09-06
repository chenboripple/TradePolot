"""signals/ 统一投票语义测试（A1）。

覆盖：三种 profile 结构解析等价、strict 投票规则、状态票手算核对、
事件 = recommendation 转移点、facade 端到端。
"""
from __future__ import annotations

import unittest

import synth

from ripple_tradePilot.indicators import rsi_series
from ripple_tradePilot.models.types import Side
from ripple_tradePilot.signals import (
    REC_BUY,
    REC_CONFLICT,
    REC_HOLD,
    REC_SELL,
    ComponentSpec,
    ComponentVote,
    UnsupportedProfileKindError,
    component_state_series,
    decide,
    decision_events,
    evaluate_symbol,
    parse_profile,
    warmup_bars,
)

FLAT_PROFILE = {
    "kind": "combo_vote",
    "ma_fast": 5,
    "ma_slow": 20,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "bb_period": 20,
    "bb_std": 2.0,
    "vote_threshold": 2,
}
GRID_PROFILE = {
    "kind": "grid_combo",
    "ma": {"fast": 5, "slow": 20},
    "rsi": {"period": 14, "oversold": 30, "overbought": 70},
    "bb": {"period": 20, "std_dev": 2.0},
    "vote_threshold": 2,
}
COMPONENTS_PROFILE = {
    "kind": "combo_vote",
    "components": [
        {"kind": "ma", "name": "ma", "ma": {"fast": 5, "slow": 20}},
        {"kind": "rsi", "name": "rsi", "rsi": {"period": 14, "oversold": 30, "overbought": 70}},
        {"kind": "bollinger", "name": "bollinger", "bb": {"period": 20, "std_dev": 2.0}},
    ],
    "vote_threshold": 2,
}


def fake_vote(name: str, side, kind: str = "ma") -> ComponentVote:
    return ComponentVote(name=name, kind=kind, side=side)


class ProfileParsingTest(unittest.TestCase):
    def test_three_structures_parse_equivalent(self):
        specs = [
            parse_profile(profile)
            for profile in (FLAT_PROFILE, GRID_PROFILE, COMPONENTS_PROFILE)
        ]
        normalized = [
            [(c.kind, c.name, tuple(sorted(c.params.items()))) for c in spec.components]
            for spec in specs
        ]
        self.assertEqual(normalized[0], normalized[1])
        self.assertEqual(normalized[1], normalized[2])
        for spec in specs:
            self.assertEqual(spec.vote_threshold, 2)
            self.assertEqual(spec.component_names, ("ma", "rsi", "bollinger"))

    def test_defaults_and_global_threshold(self):
        spec = parse_profile({"kind": "combo_vote"}, default_threshold=3)
        self.assertEqual(spec.vote_threshold, 3)
        params = spec.params_summary()
        self.assertEqual(params["ma"], {"fast": 5, "slow": 20})
        self.assertEqual(
            params["rsi"], {"period": 14, "oversold": 30.0, "overbought": 70.0}
        )
        self.assertEqual(params["bollinger"], {"period": 20, "num_std": 2.0})

    def test_threshold_clamped_to_component_count(self):
        self.assertEqual(parse_profile({**FLAT_PROFILE, "vote_threshold": 99}).vote_threshold, 3)
        self.assertEqual(parse_profile({**FLAT_PROFILE, "vote_threshold": 0}).vote_threshold, 1)
        self.assertEqual(parse_profile({**FLAT_PROFILE, "vote_threshold": "2"}).vote_threshold, 2)
        # 非法值回退默认
        self.assertEqual(
            parse_profile({**FLAT_PROFILE, "vote_threshold": "abc"}, default_threshold=2).vote_threshold,
            2,
        )

    def test_single_kind_profile(self):
        spec = parse_profile(
            {"kind": "rsi", "rsi": {"period": 6, "oversold": 25, "overbought": 75}},
            default_threshold=2,
        )
        self.assertEqual(len(spec.components), 1)
        self.assertEqual(spec.components[0].kind, "rsi")
        self.assertEqual(
            spec.components[0].params, {"period": 6, "oversold": 25.0, "overbought": 75.0}
        )
        # 单组件画像阈值自然退化为 1
        self.assertEqual(spec.vote_threshold, 1)

    def test_components_direct_keys_and_alias(self):
        spec = parse_profile(
            {"components": [{"kind": "ma", "fast": 8, "slow": 21}]}
        )
        self.assertEqual(spec.components[0].params, {"fast": 8, "slow": 21})
        self.assertEqual(spec.kind, "combo_vote")  # 无 kind 的 components 画像归入 combo_vote
        bb = parse_profile({"components": [{"kind": "bollinger", "bb": {"std_dev": 3.0}}]})
        self.assertEqual(bb.components[0].params["num_std"], 3.0)

    def test_breakout_raises_unsupported(self):
        with self.assertRaises(UnsupportedProfileKindError) as ctx:
            parse_profile({"kind": "breakout", "breakout_window": 20})
        self.assertEqual(ctx.exception.kind, "breakout")

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            parse_profile({"kind": "moon_phase"})

    def test_invalid_params_raise(self):
        with self.assertRaises(ValueError):
            parse_profile({"kind": "ma", "ma": {"fast": 20, "slow": 5}})
        with self.assertRaises(ValueError):
            parse_profile({"kind": "rsi", "rsi": {"oversold": 70, "overbought": 30}})
        with self.assertRaises(ValueError):
            parse_profile({"kind": "trend", "trend": {"short": 30, "medium": 20, "long": 10}})
        with self.assertRaises(ValueError):
            parse_profile({"kind": "donchian", "donchian": {"window": 1}})

    def test_macd_zero_cross_ignored(self):
        spec = parse_profile(
            {"kind": "macd", "macd": {"fast": 12, "slow": 26, "signal": 9, "zero_cross": True}}
        )
        self.assertEqual(spec.components[0].params, {"fast": 12, "slow": 26, "signal": 9})
        self.assertNotIn("zero_cross", spec.components[0].params)

    def test_flat_keys_win_over_nested(self):
        spec = parse_profile({**GRID_PROFILE, "ma_fast": 8})
        self.assertEqual(spec.components[0].params["fast"], 8)

    def test_source_provenance(self):
        spec = parse_profile(FLAT_PROFILE, source="system")
        self.assertEqual(spec.source, "system")
        self.assertEqual(spec.raw["kind"], "combo_vote")


class ComponentStateTest(unittest.TestCase):
    def test_linear_series_hand_computed(self):
        bars = synth.bars_from_closes(synth.linear_closes(40))
        spec = parse_profile(FLAT_PROFILE)

        ma_votes = component_state_series(spec.components[0], bars)
        for index, vote in enumerate(ma_votes):
            if index < 19:  # slow=20：前 19 根不投票
                self.assertIsNone(vote.side, index)
            else:
                self.assertIs(vote.side, Side.BUY, index)  # 等差上涨快线恒在慢线上
        self.assertAlmostEqual(ma_votes[-1].detail["fast_ma"], sum(b.close for b in bars[-5:]) / 5, places=10)
        self.assertAlmostEqual(ma_votes[-1].detail["slow_ma"], sum(b.close for b in bars[-20:]) / 20, places=10)

        rsi_votes = component_state_series(spec.components[1], bars)
        for index, vote in enumerate(rsi_votes):
            if index < 14:
                self.assertIsNone(vote.side, index)
            else:
                self.assertEqual(vote.detail["rsi"], 100.0, index)  # losses=0 → RSI=100
                self.assertIs(vote.side, Side.SELL, index)
                self.assertGreater(vote.strength, 1.0)  # 超买深度加成

        bb_votes = component_state_series(spec.components[2], bars)
        # 缓坡直线永不触轨（±2σ ≈ ±0.577 > 收盘价偏离均值 0.475）
        self.assertTrue(all(vote.side is None for vote in bb_votes))
        self.assertAlmostEqual(bb_votes[-1].detail["middle"], sum(b.close for b in bars[-20:]) / 20, places=10)

    def test_constant_series_ma_neutral_bollinger_degenerate(self):
        bars = synth.bars_from_closes([10.0] * 30)
        spec = parse_profile(FLAT_PROFILE)
        ma_votes = component_state_series(spec.components[0], bars)
        # 快慢线相等 → 不投票（改变旧 dashboard 相等给 SELL 的行为）
        self.assertTrue(all(vote.side is None for vote in ma_votes[19:]))
        bb_votes = component_state_series(spec.components[2], bars)
        # 带宽为 0 的退化布林带不投票
        self.assertTrue(all(vote.side is None for vote in bb_votes[19:]))
        self.assertEqual(bb_votes[-1].detail["middle"], 10.0)

    def test_sine_rsi_matches_closed_interval_rule(self):
        closes = synth.sine_closes(80)
        bars = synth.bars_from_closes(closes)
        spec = ComponentSpec(kind="rsi", name="rsi", params={"period": 14, "oversold": 30.0, "overbought": 70.0})
        votes = component_state_series(spec, bars)
        expected = rsi_series(closes, 14)
        saw_buy = saw_sell = False
        for index, (vote, rsi) in enumerate(zip(votes, expected)):
            if rsi is None:
                self.assertIsNone(vote.side, index)
                continue
            if rsi <= 30:
                self.assertIs(vote.side, Side.BUY, index)
                saw_buy = True
            elif rsi >= 70:
                self.assertIs(vote.side, Side.SELL, index)
                saw_sell = True
            else:
                self.assertIsNone(vote.side, index)
        self.assertTrue(saw_buy and saw_sell)

    def test_donchian_breakout_uses_previous_window(self):
        closes = [10.0] * 25 + [12.0] + [12.0] * 5
        bars = synth.bars_from_closes(closes)
        spec = ComponentSpec(kind="donchian", name="donchian", params={"window": 20})
        votes = component_state_series(spec, bars)
        self.assertIsNone(votes[24].side)  # 前 25 根平顶：close == channel_high 不突破
        self.assertIs(votes[25].side, Side.BUY)  # 12 > 10.05（前一窗口最高 high）
        self.assertIsNone(votes[26].side)  # 12 < 12.06（窗口已纳入突破日高点）
        down = synth.bars_from_closes([10.0] * 25 + [8.0])
        down_votes = component_state_series(spec, down)
        self.assertIs(down_votes[25].side, Side.SELL)

    def test_trend_alignment(self):
        bars = synth.bars_from_closes(synth.linear_closes(80))
        spec = ComponentSpec(
            kind="trend", name="trend", params={"short": 5, "medium": 20, "long": 60}
        )
        votes = component_state_series(spec, bars)
        self.assertTrue(all(vote.side is None for vote in votes[:59]))
        self.assertTrue(all(vote.side is Side.BUY for vote in votes[59:]))

    def test_warmup_bars(self):
        self.assertEqual(warmup_bars(parse_profile(FLAT_PROFILE)), 20)
        self.assertEqual(warmup_bars(parse_profile({"kind": "macd"})), 34)
        self.assertEqual(
            warmup_bars(parse_profile({"kind": "donchian", "donchian": {"window": 40}})), 41
        )


class VotingRuleTest(unittest.TestCase):
    def test_strict_buy_and_sell(self):
        from datetime import datetime

        ts = datetime(2024, 1, 2)
        buy = decide(ts, [fake_vote("ma", Side.BUY), fake_vote("rsi", Side.BUY), fake_vote("bb", None)], 2)
        self.assertEqual(buy.recommendation, REC_BUY)
        self.assertEqual(buy.buy_count, 2)
        self.assertAlmostEqual(buy.vote_ratio, 2 / 3)
        sell = decide(ts, [fake_vote("ma", Side.SELL), fake_vote("rsi", Side.SELL)], 2)
        self.assertEqual(sell.recommendation, REC_SELL)

    def test_conflict_rules(self):
        from datetime import datetime

        ts = datetime(2024, 1, 2)
        # 2 买 1 卖：旧 monitor 判买入，统一口径判冲突
        mixed = decide(ts, [fake_vote("ma", Side.BUY), fake_vote("rsi", Side.BUY), fake_vote("bb", Side.SELL)], 2)
        self.assertEqual(mixed.recommendation, REC_CONFLICT)
        self.assertTrue(mixed.is_conflict)
        self.assertIn("信号冲突", mixed.reason)
        # 2 卖 1 买同样冲突
        self.assertEqual(
            decide(ts, [fake_vote("a", Side.SELL), fake_vote("b", Side.SELL), fake_vote("c", Side.BUY)], 2).recommendation,
            REC_CONFLICT,
        )
        # 票数不足（无反向票）也是 CONFLICT，不是 HOLD
        insufficient = decide(ts, [fake_vote("ma", Side.BUY), fake_vote("rsi", None)], 2)
        self.assertEqual(insufficient.recommendation, REC_CONFLICT)
        self.assertIn("票数不足", insufficient.reason)

    def test_hold_only_when_no_votes(self):
        from datetime import datetime

        hold = decide(datetime(2024, 1, 2), [fake_vote("ma", None), fake_vote("rsi", None)], 2)
        self.assertEqual(hold.recommendation, REC_HOLD)
        self.assertEqual(hold.reason, "指标未形成有效投票")
        self.assertEqual(hold.vote_ratio, 0.0)
        self.assertEqual(hold.votes, {"ma": "HOLD", "rsi": "HOLD"})

    def test_threshold_one_single_component(self):
        from datetime import datetime

        decision = decide(datetime(2024, 1, 2), [fake_vote("rsi", Side.BUY, kind="rsi")], 1)
        self.assertEqual(decision.recommendation, REC_BUY)
        self.assertEqual(decision.vote_ratio, 1.0)


class EventTest(unittest.TestCase):
    def _decisions_from_recs(self, recs):
        """用假组件票拼出指定 recommendation 序列。"""
        from datetime import datetime, timedelta

        recipes = {
            REC_HOLD: [None, None],
            REC_BUY: [Side.BUY, Side.BUY],
            REC_SELL: [Side.SELL, Side.SELL],
            REC_CONFLICT: [Side.BUY, Side.SELL],
        }
        base = datetime(2024, 1, 2)
        return [
            decide(
                base + timedelta(days=index),
                [fake_vote(f"c{i}", side) for i, side in enumerate(recipes[rec])],
                2,
            )
            for index, rec in enumerate(recs)
        ]

    def test_events_are_transitions(self):
        decisions = self._decisions_from_recs(
            [REC_HOLD, REC_BUY, REC_BUY, REC_CONFLICT, REC_SELL, REC_HOLD, REC_SELL]
        )
        events = decision_events(decisions)
        self.assertEqual([event.index for event in events], [1, 4, 6])
        self.assertEqual([event.side for event in events], [Side.BUY, Side.SELL, Side.SELL])
        self.assertEqual(
            [event.previous_recommendation for event in events],
            [REC_HOLD, REC_CONFLICT, REC_HOLD],
        )

    def test_consecutive_same_recommendation_single_event(self):
        events = decision_events(self._decisions_from_recs([REC_BUY, REC_BUY, REC_BUY]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].previous_recommendation, REC_HOLD)  # 首根 BUY 也是转移

    def test_conflict_and_hold_never_event(self):
        events = decision_events(
            self._decisions_from_recs([REC_CONFLICT, REC_HOLD, REC_CONFLICT])
        )
        self.assertEqual(events, [])


class FacadeTest(unittest.TestCase):
    def test_evaluate_symbol_shapes(self):
        bars = synth.daily_bars(120, seed=5)
        evaluation = evaluate_symbol(bars, FLAT_PROFILE)
        self.assertEqual(len(evaluation.decisions), 120)
        self.assertEqual(evaluation.latest, evaluation.decisions[-1])
        if evaluation.events:
            self.assertEqual(evaluation.latest_event, evaluation.events[-1])
        for event in evaluation.events:
            self.assertIn(event.decision.recommendation, (REC_BUY, REC_SELL))
        # 预热完成前（RSI 需 15 根）必然无票
        self.assertTrue(all(d.recommendation == REC_HOLD for d in evaluation.decisions[:14]))

    def test_dict_rows_equal_bar_objects(self):
        bars = synth.daily_bars(90, seed=6)
        rows = synth.daily_rows(bars)
        from_bars = evaluate_symbol(bars, FLAT_PROFILE)
        from_rows = evaluate_symbol(rows, FLAT_PROFILE)
        self.assertEqual(list(from_bars.decisions), list(from_rows.decisions))
        self.assertEqual(
            [event.index for event in from_bars.events],
            [event.index for event in from_rows.events],
        )

    def test_empty_bars_raise(self):
        with self.assertRaises(ValueError):
            evaluate_symbol([], FLAT_PROFILE)

    def test_profilespec_passthrough(self):
        bars = synth.daily_bars(60, seed=7)
        spec = parse_profile(FLAT_PROFILE, source="system")
        evaluation = evaluate_symbol(bars, spec)
        self.assertIs(evaluation.spec, spec)
        self.assertEqual(evaluation.spec.source, "system")

    def test_events_recomputed_from_sine_series(self):
        # 注意：正弦序列上 strict 阈值 2 恒为 CONFLICT（均值回归组件 rsi/bb 与
        # 趋势组件 ma 在波谷/波峰永远反向）——这正是统一口径要暴露的冲突信息，
        # 旧 dashboard majority 规则会把这些全部误判成 BUY/SELL。
        # 事件转移测试用阈值 1（单票且无反向即过）。
        bars = synth.bars_from_closes(synth.sine_closes(120))
        profile = {**FLAT_PROFILE, "vote_threshold": 1}
        evaluation = evaluate_symbol(bars, profile, default_threshold=1)
        expected = []
        previous = REC_HOLD
        for index, decision in enumerate(evaluation.decisions):
            if decision.recommendation in (REC_BUY, REC_SELL) and decision.recommendation != previous:
                expected.append(index)
            previous = decision.recommendation
        self.assertEqual([event.index for event in evaluation.events], expected)
        self.assertTrue(expected, "阈值 1 下正弦序列应产生转移事件")
        sides = {event.side for event in evaluation.events}
        self.assertEqual(sides, {Side.BUY, Side.SELL})

        # strict 语义钉死：同序列阈值 2 时无任何 BUY/SELL 决策，全是冲突或观望
        strict = evaluate_symbol(bars, FLAT_PROFILE, default_threshold=2)
        self.assertTrue(
            all(
                decision.recommendation in (REC_CONFLICT, REC_HOLD)
                for decision in strict.decisions
            )
        )
        self.assertEqual(strict.events, ())


if __name__ == "__main__":
    unittest.main()
