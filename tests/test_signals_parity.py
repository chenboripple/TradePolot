"""黄金一致性测试（A1 第一层）：增量引擎 vs indicators.py 批量序列位级相等。

本文件是"三套实现不再分裂"的钉子，随后续阶段扩展：
- A2 接入后：dashboard 响应 == evaluate_symbol（同 bars+profile）；
- A3 接入后：monitor daily_eval == evaluate_symbol；
- A4 接入后：ComboVoteStateStrategy 信号 bar 集合 == decision_events 索引集合。

当前层：每种组件引擎的 detail 序列与 indicators.py 对应函数**逐位 ==**
（float 精确相等而非 assertAlmostEqual——运算顺序被刻意镜像，任何漂移都
说明实现分叉，必须当场抓住）。
"""
from __future__ import annotations

import unittest

import synth

from ripple_tradePilot.indicators import (
    atr_series,
    bollinger,
    ema_series,
    macd_series,
    rolling_max,
    rolling_mean,
    rolling_min,
    rsi_series,
)
from ripple_tradePilot.signals import (
    ComponentSpec,
    component_state_series,
    decide,
    evaluate_symbol,
    make_engine,
    parse_profile,
)

BARS = synth.daily_bars(150, seed=42)
CLOSES = [bar.close for bar in BARS]
HIGHS = [bar.high for bar in BARS]
LOWS = [bar.low for bar in BARS]


def detail_column(votes, key):
    return [vote.detail[key] for vote in votes]


class EngineIndicatorParityTest(unittest.TestCase):
    def test_ma_parity_bitwise(self):
        spec = ComponentSpec("ma", "ma", {"fast": 5, "slow": 20})
        votes = component_state_series(spec, BARS)
        self.assertEqual(detail_column(votes, "fast_ma"), rolling_mean(CLOSES, 5))
        self.assertEqual(detail_column(votes, "slow_ma"), rolling_mean(CLOSES, 20))

    def test_rsi_parity_bitwise(self):
        spec = ComponentSpec("rsi", "rsi", {"period": 14, "oversold": 30.0, "overbought": 70.0})
        votes = component_state_series(spec, BARS)
        self.assertEqual(detail_column(votes, "rsi"), rsi_series(CLOSES, 14))

    def test_bollinger_parity_bitwise(self):
        spec = ComponentSpec("bollinger", "bollinger", {"period": 20, "num_std": 2.0})
        votes = component_state_series(spec, BARS)
        middle, upper, lower = bollinger(CLOSES, 20, 2.0)
        self.assertEqual(detail_column(votes, "middle"), middle)
        self.assertEqual(detail_column(votes, "upper"), upper)
        self.assertEqual(detail_column(votes, "lower"), lower)

    def test_macd_parity_bitwise(self):
        for params in ({"fast": 12, "slow": 26, "signal": 9}, {"fast": 8, "slow": 17, "signal": 5}):
            spec = ComponentSpec("macd", "macd", dict(params))
            votes = component_state_series(spec, BARS)
            dif, dea, hist = macd_series(CLOSES, **params)
            self.assertEqual(detail_column(votes, "dif"), dif, params)
            self.assertEqual(detail_column(votes, "dea"), dea, params)
            self.assertEqual(detail_column(votes, "hist"), hist, params)

    def test_trend_parity_bitwise(self):
        spec = ComponentSpec("trend", "trend", {"short": 5, "medium": 20, "long": 60})
        votes = component_state_series(spec, BARS)
        self.assertEqual(detail_column(votes, "short_ma"), rolling_mean(CLOSES, 5))
        self.assertEqual(detail_column(votes, "medium_ma"), rolling_mean(CLOSES, 20))
        self.assertEqual(detail_column(votes, "long_ma"), rolling_mean(CLOSES, 60))

    def test_donchian_parity_bitwise(self):
        window = 20
        spec = ComponentSpec("donchian", "donchian", {"window": window})
        votes = component_state_series(spec, BARS)
        # 通道 = 前一窗口（不含当前 bar）：等于 rolling_max/rolling_min 右移一位
        expected_high = [None] + rolling_max(HIGHS, window)[:-1]
        expected_low = [None] + rolling_min(LOWS, window)[:-1]
        self.assertEqual(detail_column(votes, "channel_high"), expected_high)
        self.assertEqual(detail_column(votes, "channel_low"), expected_low)

    def test_reset_replay_identical(self):
        spec = ComponentSpec("macd", "macd", {"fast": 12, "slow": 26, "signal": 9})
        engine = make_engine(spec)
        first = [engine.push(bar) for bar in BARS]
        engine.reset()
        second = [engine.push(bar) for bar in BARS]
        self.assertEqual(first, second)


class IndicatorsSelfTest(unittest.TestCase):
    """新增 indicators 函数自身的确定性核对（parity 的基准侧）。"""

    def test_ema_span_one_is_identity(self):
        values = [1.0, 2.5, 3.0, 4.25]
        self.assertEqual(ema_series(values, 1), values)

    def test_ema_seed_and_recursion(self):
        values = [float(i) for i in range(1, 11)]
        result = ema_series(values, 5)
        self.assertTrue(all(v is None for v in result[:4]))
        self.assertEqual(result[4], 3.0)  # SMA(1..5)
        alpha = 2 / 6
        expected = 3.0
        for value in values[5:]:
            expected = alpha * value + (1 - alpha) * expected
        self.assertEqual(result[-1], expected)

    def test_macd_relations(self):
        dif, dea, hist = macd_series(CLOSES, 12, 26, 9)
        ema_fast = ema_series(CLOSES, 12)
        ema_slow = ema_series(CLOSES, 26)
        for index in range(25, len(CLOSES)):
            self.assertEqual(dif[index], ema_fast[index] - ema_slow[index])
        # DEA 种子 = 前 9 个 DIF 的 SMA
        first_difs = dif[25:34]
        self.assertEqual(dea[33], sum(first_difs) / 9)
        for index in range(33, len(CLOSES)):
            self.assertEqual(hist[index], dif[index] - dea[index])
        self.assertTrue(all(v is None for v in dea[:33]))

    def test_atr_hand_computed(self):
        highs = [11.0, 12.0, 13.0, 12.5, 14.0]
        lows = [9.0, 10.0, 11.0, 11.5, 12.0]
        closes = [10.0, 11.0, 12.5, 12.0, 13.0]
        result = atr_series(highs, lows, closes, period=3)
        tr0 = 11.0 - 9.0  # 首根无前收
        tr1 = max(12 - 10, abs(12 - 10), abs(10 - 10))
        tr2 = max(13 - 11, abs(13 - 11), abs(11 - 11))
        self.assertEqual(result[:2], [None, None])
        self.assertEqual(result[2], (tr0 + tr1 + tr2) / 3)


class DashboardParityTest(unittest.TestCase):
    """A2 层黄金一致性：dashboard.market_detail == evaluate_symbol（同 bars + profile）。"""

    def _service_and_bars(self, root, profile):
        import csv

        import yaml

        from ripple_tradePilot.api.dashboard import DashboardService

        symbol = "000001.SZ"
        bars = synth.daily_bars(120, seed=99)
        data_dir = root / "data"
        data_dir.mkdir()
        rows = synth.daily_rows(bars)
        with (data_dir / f"{symbol}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["trade_date", "open", "high", "low", "close", "vol"]
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "trade_date": row["trade_date"],
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "vol": row["vol"],
                    }
                )
        config_path = root / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "symbols": [
                        {"code": symbol, "name": " parity", "asset_class": "stock", "strategy_profile": "p"}
                    ],
                    "strategy_profiles": {"p": profile},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        service = DashboardService(
            data_dir=data_dir, config_path=config_path, backtest_db=root / "missing.db"
        )
        return service, symbol, bars

    def test_detail_matches_evaluate_symbol(self):
        import tempfile
        from pathlib import Path

        # 阈值 1：seed 99 随机游走上产生 16 个转移事件，信号列表比对非空
        profile = {"kind": "combo_vote", "vote_threshold": 1}
        with tempfile.TemporaryDirectory() as tmp:
            service, symbol, bars = self._service_and_bars(Path(tmp), profile)
            detail = service.market_detail(symbol)
            expected = evaluate_symbol(bars, profile, default_threshold=1)
            latest = expected.latest
            self.assertTrue(expected.events, "parity 夹具应产生转移事件")

            self.assertEqual(detail["recommendation"], latest.recommendation)
            self.assertEqual(detail["votes"], latest.votes)
            self.assertEqual(detail["buy_count"], latest.buy_count)
            self.assertEqual(detail["sell_count"], latest.sell_count)
            self.assertEqual(detail["vote_threshold"], latest.vote_threshold)
            self.assertEqual(detail["reason"], latest.reason)
            self.assertEqual(detail["is_conflict"], latest.is_conflict)
            self.assertEqual(detail["vote_ratio"], round(latest.vote_ratio * 100))

            # 信号列表 = 决策转移点（尾部 8 条逆序），与 evaluate_symbol.events 同源
            tail = expected.events[-8:][::-1]
            self.assertEqual(len(detail["signals"]), len(tail))
            for signal, event in zip(detail["signals"], tail):
                self.assertEqual(signal["side"], event.side.value)
                self.assertEqual(
                    signal["date"], bars[event.index].timestamp.date().isoformat()
                )
                self.assertEqual(signal["reason"], event.decision.reason)

    def test_detail_tracks_threshold_sweep(self):
        import tempfile
        from pathlib import Path

        for threshold in (1, 2, 3):
            profile = {"kind": "combo_vote", "vote_threshold": threshold}
            with tempfile.TemporaryDirectory() as tmp:
                service, symbol, bars = self._service_and_bars(Path(tmp), profile)
                detail = service.market_detail(symbol)
                expected = evaluate_symbol(bars, profile, default_threshold=threshold)
                self.assertEqual(
                    detail["recommendation"], expected.latest.recommendation, threshold
                )
                self.assertEqual(
                    [s["side"] for s in detail["signals"]],
                    [e.side.value for e in expected.events[-8:][::-1]],
                    threshold,
                )


class ComboStrategyParityTest(unittest.TestCase):
    """A4 层黄金一致性：ComboVoteStateStrategy 信号 == decision_events。"""

    def test_signal_indices_equal_event_indices(self):
        from ripple_tradePilot.signals import ComboVoteStateStrategy

        fixtures = [
            (synth.bars_from_closes(synth.sine_closes(120)), {"kind": "combo_vote", "vote_threshold": 1}),
            (synth.daily_bars(120, seed=99), {"kind": "combo_vote", "vote_threshold": 1}),
            (synth.daily_bars(150, seed=7), {"kind": "combo_vote", "vote_threshold": 2}),
            (synth.bars_from_closes(synth.linear_closes(60)), {"kind": "ma", "ma": {"fast": 5, "slow": 20}}),
        ]
        for bars, profile in fixtures:
            strategy = ComboVoteStateStrategy(profile)
            signal_idx = strategy.signal_indices(bars)
            event_idx = [event.index for event in evaluate_symbol(bars, profile).events]
            self.assertEqual(signal_idx, event_idx, profile)

    def test_run_backtest_produces_fills(self):
        from ripple_tradePilot.backtest.engine import run_backtest
        from ripple_tradePilot.signals import ComboVoteStateStrategy

        bars = synth.daily_bars(120, seed=99)
        strategy = ComboVoteStateStrategy({"kind": "combo_vote", "vote_threshold": 1})
        result = run_backtest(strategy, bars)
        self.assertEqual(len(result.equity_curve), len(bars))
        self.assertTrue(result.fills, "阈值 1 随机游走应产生成交")
        # 成交方向交替合法（BUY 开仓 / SELL 平仓），无非法 side
        for fill in result.fills:
            self.assertIn(fill.side.value, ("BUY", "SELL"))

    def test_warmup_matches_cold_full_run_in_suffix(self):
        """预热不改变评估窗信号：warmup(prefix)+suffix == 冷启动全程的 suffix 段。"""
        from ripple_tradePilot.signals import ComboVoteStateStrategy

        bars = synth.daily_bars(120, seed=99)
        spec = {"kind": "combo_vote", "vote_threshold": 1}
        prefix, suffix = bars[:60], bars[60:]

        full = ComboVoteStateStrategy(spec)
        full_signals = [i for i, bar in enumerate(bars) if full.on_bar(bar).side is not None]
        expected_suffix = [i for i in full_signals if i >= 60]

        warm = ComboVoteStateStrategy(spec)
        warm.warmup(prefix)
        warm_suffix = [60 + j for j, bar in enumerate(suffix) if warm.on_bar(bar).side is not None]
        self.assertEqual(warm_suffix, expected_suffix)

    def test_warmup_enables_first_bar_vote_vs_cold_suffix(self):
        """预热的价值：直接在 suffix 上冷启动会因历史不足漏掉前若干根的信号。"""
        from ripple_tradePilot.signals import ComboVoteStateStrategy

        bars = synth.daily_bars(120, seed=99)
        spec = {"kind": "combo_vote", "ma_slow": 20, "vote_threshold": 1}
        prefix, suffix = bars[:60], bars[60:]

        warm = ComboVoteStateStrategy(spec)
        warm.warmup(prefix)
        warm_signals = [j for j, bar in enumerate(suffix) if warm.on_bar(bar).side is not None]

        cold = ComboVoteStateStrategy(spec)
        cold_signals = [j for j, bar in enumerate(suffix) if cold.on_bar(bar).side is not None]

        # 冷启动 suffix 在 MA 预热满（前 19 根）内无法出 MA 票，信号集合必然不同
        self.assertNotEqual(warm_signals, cold_signals)
        self.assertTrue(any(j < 19 for j in warm_signals), "预热后评估段早期即可出票")

    def test_reset_clears_state(self):
        from ripple_tradePilot.signals import ComboVoteStateStrategy

        bars = synth.daily_bars(80, seed=5)
        strategy = ComboVoteStateStrategy({"kind": "combo_vote", "vote_threshold": 1})
        first = [i for i, bar in enumerate(bars) if strategy.on_bar(bar).side is not None]
        strategy.reset()
        second = [i for i, bar in enumerate(bars) if strategy.on_bar(bar).side is not None]
        self.assertEqual(first, second)


class FacadeParityTest(unittest.TestCase):
    def test_evaluate_equals_manual_engine_loop(self):
        spec = parse_profile({"kind": "combo_vote"}, default_threshold=2)
        engines = [make_engine(component) for component in spec.components]
        manual = []
        for bar in BARS:
            manual.append(
                decide(bar.timestamp, [engine.push(bar) for engine in engines], spec.vote_threshold)
            )
        evaluation = evaluate_symbol(BARS, spec)
        self.assertEqual(list(evaluation.decisions), manual)

    def test_batch_series_equals_push_loop(self):
        # component_state_series 与手动 push 是同一实现的两条调用路径，钉死防分叉
        spec = ComponentSpec("bollinger", "bollinger", {"period": 20, "num_std": 2.0})
        engine = make_engine(spec)
        manual = [engine.push(bar) for bar in BARS]
        self.assertEqual(component_state_series(spec, BARS), manual)


if __name__ == "__main__":
    unittest.main()
