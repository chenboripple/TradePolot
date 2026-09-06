"""
滚动前进（walk-forward）验证协议测试

用合成价格序列（等差趋势 + 正弦波动）和周期可调的均线策略做夹具，
不依赖网络与真实行情/ token，只验证协议本身的正确性：
- split 数量正确、时间切分严格有序（OOS 段必须严格位于训练段之后，
  各段之间无重叠、无前瞻泄漏）
- 报告字段齐全，最佳参数确实来自给定网格
- overfit_gap 可计算，且与定义（平均 IS 收益 − 平均 OOS 收益）一致
"""

import math
import unittest
from datetime import datetime, timedelta
from itertools import product
from typing import Dict, List

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import Metrics, compute_metrics
from ripple_tradePilot.backtest.walkforward import (
    WalkForwardReport,
    WalkForwardSplit,
    walk_forward,
)
from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy
from ripple_tradePilot.strategies.moving_average import MovingAverageCross

PARAM_GRID: Dict[str, List] = {"fast": [2, 3], "slow": [5, 8]}
N_COMBOS = len(PARAM_GRID["fast"]) * len(PARAM_GRID["slow"])


def make_bars(n: int, start: float = 10.0, trend: float = 0.02,
              amp: float = 0.8, period: int = 20) -> List[Bar]:
    """合成 bars：等差趋势叠加正弦波动，保证价格为正、时间戳唯一递增。"""
    base = datetime(2026, 1, 5)
    bars = []
    for i in range(n):
        close = start + trend * i + amp * math.sin(2 * math.pi * i / period)
        bars.append(
            Bar(
                timestamp=base + timedelta(days=i),
                open=close,
                high=close * 1.001,
                low=close * 0.999,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def ma_factory(params: dict) -> Strategy:
    """工厂模式：每次用网格参数新建一个全新策略实例。"""
    return MovingAverageCross(fast=params["fast"], slow=params["slow"])


class RecordingMACross(Strategy):
    """记录自己实际看到的每根 bar，用于检查信息泄漏。

    区分两条路径（A6 warmup 语义）：
    - ``seen``：评估窗内 ``on_bar`` 看到的 bar（由 run_backtest 调用，进权益曲线）；
    - ``warmup_seen``：预热前缀看到的 bar（由 ``warmup`` 调用，只推进指标状态、不进权益）。

    ``warmup`` 被刻意覆写为只喂 ``_inner.on_bar``、记到 ``warmup_seen``，这样
    ``seen`` 始终精确等于评估窗，可与 split 的 train/test 区间逐根比对。
    """

    name = "recording_ma_cross"

    def __init__(self, fast: int, slow: int):
        self._inner = MovingAverageCross(fast=fast, slow=slow)
        self.seen: List[datetime] = []
        self.warmup_seen: List[datetime] = []

    def warmup(self, history) -> None:
        for bar in history:
            self.warmup_seen.append(bar.timestamp)
            self._inner.on_bar(bar)

    def on_bar(self, bar: Bar) -> Signal:
        self.seen.append(bar.timestamp)
        return self._inner.on_bar(bar)


class WalkForwardPartitionTest(unittest.TestCase):
    """split 数量与时间切分：不重叠、不前瞻。"""

    def setUp(self):
        self.bars = make_bars(120)

    def test_split_count_and_contiguous_partition(self):
        report = walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=3)

        self.assertIsInstance(report, WalkForwardReport)
        self.assertEqual(len(report.splits), 3)
        # 覆盖完整时间线：首段从头开始，末段到尾结束
        self.assertEqual(report.splits[0].train_start, 0)
        self.assertEqual(report.splits[-1].test_end, len(self.bars))

        for split in report.splits:
            # OOS 段严格在训练段之后：训练段非空、测试段非空、首尾相接
            self.assertLess(split.train_start, split.train_end)
            self.assertEqual(split.train_end, split.test_start)
            self.assertLess(split.test_start, split.test_end)
            # 训练/测试比例符合设定（取整误差内）
            size = split.test_end - split.train_start
            train_len = split.train_end - split.train_start
            self.assertAlmostEqual(train_len / size, 0.7, delta=0.1)

        # 相邻段首尾相接、互不重叠
        for prev, nxt in zip(report.splits, report.splits[1:]):
            self.assertEqual(prev.test_end, nxt.train_start)

        # 时间轴上的严格先后：OOS 第一根 bar 晚于训练最后一根 bar
        for split in report.splits:
            self.assertGreater(
                self.bars[split.test_start].timestamp,
                self.bars[split.train_end - 1].timestamp,
            )

    def test_split_count_follows_n_splits(self):
        for n_splits in (2, 4):
            report = walk_forward(
                ma_factory, self.bars, PARAM_GRID, n_splits=n_splits
            )
            self.assertEqual(len(report.splits), n_splits)

    def test_no_lookahead_leakage_via_recorded_bars(self):
        """每个策略实例实际看到的 bar 必须恰好是某一个窗口：
        要么是某 split 的训练段（网格搜索），要么是某 split 的测试段
        （样本外评估），不允许跨段、更不允许训练实例看到未来数据。"""
        created: List[RecordingMACross] = []

        def factory(params: dict) -> Strategy:
            strategy = RecordingMACross(fast=params["fast"], slow=params["slow"])
            created.append(strategy)
            return strategy

        report = walk_forward(factory, self.bars, PARAM_GRID, n_splits=3)

        # 每段 = 网格组合数（训练搜索） + 1（OOS 复跑），共 3 段
        self.assertEqual(len(created), 3 * (N_COMBOS + 1))

        ts_to_index = {bar.timestamp: i for i, bar in enumerate(self.bars)}
        n_oos_instances = 0
        for strategy in created:
            self.assertGreater(len(strategy.seen), 0)
            # warmup_bars=0（默认）：绝不预热，warmup_seen 必为空 → 与旧版逐根一致
            self.assertEqual(strategy.warmup_seen, [])
            indices = [ts_to_index[ts] for ts in strategy.seen]
            # 看到的是一段连续区间，中间没有跳变
            self.assertEqual(indices, list(range(indices[0], indices[0] + len(indices))))
            # 区间恰好等于某个 split 的训练窗口或测试窗口
            matched = False
            for split in report.splits:
                if indices[0] == split.train_start and indices[-1] == split.train_end - 1:
                    matched = True
                    break
                if indices[0] == split.test_start and indices[-1] == split.test_end - 1:
                    matched = True
                    n_oos_instances += 1
                    break
            self.assertTrue(matched, f"实例看到了非法区间 {indices[0]}..{indices[-1]}")

        # 每个 split 恰好有一个实例只跑样本外段
        self.assertEqual(n_oos_instances, 3)


class WalkForwardWarmupTest(unittest.TestCase):
    """A6：warmup 预热前缀合法性 + 消除冷启动哑区的语义验证。"""

    def setUp(self):
        self.bars = make_bars(120)
        self.ts_to_index = {bar.timestamp: i for i, bar in enumerate(self.bars)}

    def _recording_factory(self, created: List[RecordingMACross]):
        def factory(params: dict) -> Strategy:
            strategy = RecordingMACross(fast=params["fast"], slow=params["slow"])
            created.append(strategy)
            return strategy
        return factory

    def test_warmup_prefix_legal_and_eval_window_exact(self):
        """每实例 = 合法预热前缀（长度≤warmup、紧邻窗口前、时间严格更早）+ 恰好评估窗。"""
        warmup = 10
        created: List[RecordingMACross] = []
        report = walk_forward(
            self._recording_factory(created), self.bars, PARAM_GRID,
            n_splits=3, warmup_bars=warmup,
        )

        n_oos = 0
        for strategy in created:
            # 评估窗 seen：连续区间，恰好等于某 split 的 train 或 test 窗
            self.assertGreater(len(strategy.seen), 0)
            idx = [self.ts_to_index[ts] for ts in strategy.seen]
            self.assertEqual(idx, list(range(idx[0], idx[0] + len(idx))))
            kind = None
            for split in report.splits:
                if idx[0] == split.train_start and idx[-1] == split.train_end - 1:
                    kind, matched = "train", split
                    break
                if idx[0] == split.test_start and idx[-1] == split.test_end - 1:
                    kind, matched = "test", split
                    n_oos += 1
                    break
            self.assertIsNotNone(kind, f"非法评估窗 {idx[0]}..{idx[-1]}")

            # 预热前缀 warmup_seen：长度 ≤ warmup，右端点 == 窗口首根前一根，时间戳严格早于窗口
            window_start = idx[0]
            w_idx = [self.ts_to_index[ts] for ts in strategy.warmup_seen]
            self.assertLessEqual(len(w_idx), warmup)
            if w_idx:
                self.assertEqual(w_idx, list(range(w_idx[0], w_idx[0] + len(w_idx))))
                self.assertEqual(w_idx[-1], window_start - 1)
                self.assertTrue(
                    all(ts < self.bars[window_start].timestamp for ts in strategy.warmup_seen)
                )
                if kind == "test":
                    # OOS 前缀严格落在训练窗内（只用过去数据，无前瞻泄漏）
                    self.assertGreaterEqual(w_idx[0], matched.train_start)
                    self.assertLessEqual(w_idx[-1], matched.train_end - 1)
        self.assertEqual(n_oos, 3)

    def test_split_warmup_start_within_train_window(self):
        """WalkForwardSplit.warmup_start：OOS 预热起点夹在训练窗内，前缀长度 ≤ warmup。"""
        warmup = 15
        report = walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=3, warmup_bars=warmup)
        for split in report.splits:
            self.assertGreaterEqual(split.warmup_start, split.train_start)
            self.assertLessEqual(split.warmup_start, split.test_start)
            self.assertLessEqual(split.test_start - split.warmup_start, warmup)

    def test_warmup_zero_keeps_empty_prefix(self):
        """warmup_bars=0：warmup_start == test_start（空前缀），完全等价旧版冷启动。"""
        report = walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=3, warmup_bars=0)
        for split in report.splits:
            self.assertEqual(split.warmup_start, split.test_start)

    def test_warmup_enables_first_bar_signal(self):
        """语义验证：warmup≥slow 时评估段首根即可出信号；冷启动前 slow 根必无信号。

        夹具：平价预热段（fast_ma==slow_ma，无信号、_last_side 仍为 None）→ 评估段
        首根跳涨，使 fast_ma>slow_ma 形成 BUY 交叉。冷启动实例首根 slow 窗未满，必返回
        None；预热实例窗口已满，首根即捕捉交叉。
        """
        fast, slow = 3, 8
        base = datetime(2026, 1, 5)

        def flat(day: int, price: float) -> Bar:
            return Bar(timestamp=base + timedelta(days=day), open=price, high=price,
                       low=price, close=price, volume=1_000_000)

        prefix = [flat(i, 100.0) for i in range(slow + 2)]   # 平价预热，窗口填满
        eval_bars = [flat(len(prefix) + j, 110.0 if j == 0 else 110.0) for j in range(6)]

        # 冷启动：评估段首根 slow 窗未满 → 无信号
        cold = MovingAverageCross(fast=fast, slow=slow)
        self.assertIsNone(cold.on_bar(eval_bars[0]).side)

        # 预热后：窗口已满，首根跳涨 → fast_ma>slow_ma → BUY 交叉
        warm = MovingAverageCross(fast=fast, slow=slow)
        warm.warmup(prefix)
        self.assertEqual(warm.on_bar(eval_bars[0]).side, Side.BUY)

    def test_select_by_return_maximizes_train_return(self):
        """select_by='return' 选出的参数确为训练期总收益最大者（独立复算交叉核对）。"""
        warmup = 10
        report = walk_forward(
            ma_factory, self.bars, PARAM_GRID, n_splits=3,
            warmup_bars=warmup, select_by="return",
        )
        split = report.splits[0]
        train_bars = self.bars[split.train_start:split.train_end]
        train_warmup = self.bars[max(0, split.train_start - warmup):split.train_start]

        def train_return(params: dict) -> float:
            strategy = ma_factory(dict(params))
            if train_warmup:
                strategy.warmup(train_warmup)
            result = run_backtest(strategy, train_bars)
            return compute_metrics(result.equity_curve, positions=result.positions).total_return

        combos = [dict(zip(PARAM_GRID.keys(), v)) for v in product(*PARAM_GRID.values())]
        best = max(train_return(p) for p in combos)
        self.assertAlmostEqual(train_return(split.best_params), best, places=10)


class WalkForwardReportTest(unittest.TestCase):
    """报告字段完整性与指标口径。"""

    def setUp(self):
        self.bars = make_bars(120)
        self.report = walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=3)

    def test_report_fields_complete(self):
        for split in self.report.splits:
            self.assertIsInstance(split, WalkForwardSplit)
            # 最佳参数必须来自给定网格
            self.assertEqual(set(split.best_params.keys()), set(PARAM_GRID.keys()))
            for key, values in PARAM_GRID.items():
                self.assertIn(split.best_params[key], values)
            # IS/OOS 指标均为完整 Metrics
            self.assertIsInstance(split.is_metrics, Metrics)
            self.assertIsInstance(split.oos_metrics, Metrics)
            for metrics in (split.is_metrics, split.oos_metrics):
                self.assertTrue(math.isfinite(metrics.total_return))
                self.assertTrue(math.isfinite(metrics.max_drawdown))
                self.assertTrue(math.isfinite(metrics.sharpe))

        for value in (
            self.report.oos_total_return,
            self.report.avg_is_return,
            self.report.avg_oos_return,
            self.report.avg_oos_sharpe,
            self.report.overfit_gap,
        ):
            self.assertIsInstance(value, float)
            self.assertTrue(math.isfinite(value))

    def test_overfit_gap_is_computable_and_consistent(self):
        report = self.report
        avg_is = sum(s.is_metrics.total_return for s in report.splits) / len(report.splits)
        avg_oos = sum(s.oos_metrics.total_return for s in report.splits) / len(report.splits)
        # 定义：平均 IS 收益 − 平均 OOS 收益
        self.assertAlmostEqual(report.overfit_gap, avg_is - avg_oos, places=10)
        self.assertAlmostEqual(report.avg_is_return, avg_is, places=10)
        self.assertAlmostEqual(report.avg_oos_return, avg_oos, places=10)
        # OOS 总收益 = 各段样本外收益的复利拼接
        compound = 1.0
        for split in report.splits:
            compound *= 1.0 + split.oos_metrics.total_return
        self.assertAlmostEqual(report.oos_total_return, compound - 1.0, places=10)

    def test_selection_is_deterministic(self):
        """相同输入重复运行，选出的参数必须一致。"""
        again = walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=3)
        self.assertEqual(
            [s.best_params for s in self.report.splits],
            [s.best_params for s in again.splits],
        )

    def test_backtest_kwargs_passthrough(self):
        """backtest_kwargs 应原样透传给 run_backtest（不报错且字段完整）。"""
        report = walk_forward(
            ma_factory,
            self.bars,
            PARAM_GRID,
            n_splits=3,
            backtest_kwargs={"initial_cash": 50_000.0, "slippage": 0.0, "execution": "close"},
        )
        self.assertEqual(len(report.splits), 3)
        self.assertTrue(math.isfinite(report.overfit_gap))

    def test_selection_uses_held_day_sharpe(self):
        """A6 口径对齐：select_by='sharpe' 用持仓日夏普选参（== compute_metrics(positions=...)），
        与 CLI/网页展示同口径，而非旧版全样本夏普。独立复算训练窗各组合核对。"""
        report = walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=3, select_by="sharpe")
        split = report.splits[0]
        train_bars = self.bars[split.train_start:split.train_end]

        def held_sharpe(params: dict) -> float:
            result = run_backtest(ma_factory(dict(params)), train_bars)
            return compute_metrics(result.equity_curve, positions=result.positions).sharpe

        combos = [dict(zip(PARAM_GRID.keys(), v)) for v in product(*PARAM_GRID.values())]
        best = max(held_sharpe(p) for p in combos)
        self.assertAlmostEqual(held_sharpe(split.best_params), best, places=10)
        # is_metrics.sharpe 即持仓日口径，与独立复算逐位一致
        self.assertAlmostEqual(split.is_metrics.sharpe, held_sharpe(split.best_params), places=10)


class WalkForwardValidationTest(unittest.TestCase):
    """非法输入与数据量不足必须显式报错，而不是静默跑出垃圾指标。"""

    def setUp(self):
        self.bars = make_bars(120)

    def test_invalid_arguments_raise(self):
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, self.bars, PARAM_GRID, n_splits=0)
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, self.bars, PARAM_GRID, train_ratio=0.0)
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, self.bars, PARAM_GRID, train_ratio=1.0)
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, self.bars, {})

    def test_insufficient_bars_raise(self):
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, make_bars(5), PARAM_GRID, n_splits=3)

    def test_invalid_warmup_and_select_by_raise(self):
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, self.bars, PARAM_GRID, warmup_bars=-1)
        with self.assertRaises(ValueError):
            walk_forward(ma_factory, self.bars, PARAM_GRID, select_by="alpha")


if __name__ == "__main__":
    unittest.main()
