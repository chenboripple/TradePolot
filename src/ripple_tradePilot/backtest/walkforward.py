"""
滚动前进（Walk-Forward）验证协议

背景：纯样本内网格搜索调参会系统性过拟合（参数朝着历史噪声拟合），
回测收益看起来很好、实盘大概率失效。本模块提供科学的前推验证：

- 把整段历史切成 ``n_splits`` 个连续片段；
- 每个片段内，前 ``train_ratio`` 为训练集（样本内，IS），其余为测试集
  （样本外，OOS）。训练集上对 ``param_grid`` 做网格搜索，按 ``select_by``
  （持仓日夏普或总收益）选参；
- 用选出的参数在该片段的 OOS 区间跑 ``run_backtest``，
  训练与测试时间上严格隔离（OOS 段一定在训练段之后），策略实例由
  ``strategy_factory`` 每次新建，避免状态残留造成的信息泄漏；
- 拼接各分段的样本外指标，输出聚合 OOS 收益/平均夏普，以及
  ``overfit_gap``（平均 IS 收益 − 平均 OOS 收益，越大越可能过拟合）。

两处历史失真已在 A6 修复（默认参数下行为与旧版完全一致，向后兼容）：

1. **冷启动缺口**：旧版训练段与测试段都直接从窗口首根 bar 起跑，指标的滚动
   窗口（如 MA 的 ``slow``）在最初若干根内尚未填满，这段"哑区"里策略无法
   出信号，却被计入权益曲线，系统性低估 OOS 表现。现在 ``warmup_bars>0`` 时，
   每段先用窗口**之前**的 ``warmup_bars`` 根 bar 调 ``strategy.warmup()`` 预热
   指标状态——预热 bar 只推进策略内部状态、**不进** ``run_backtest``，因此不污染
   初始资金/回撤/持仓。OOS 段的预热前缀严格落在**训练窗内**（只用过去数据，
   合法且可断言），训练段前缀落在更早的历史（首段无更早数据时退化为冷启动）。
2. **夏普口径分裂**：旧版选参用全样本夏普（空仓日的 0 收益稀释），而 CLI/网页
   展示用持仓日夏普，两者可能选出不同参数。现在 walk-forward 内 ``compute_metrics``
   一律传 ``positions=result.positions``，选参口径 == 展示口径 == 持仓日夏普；
   同时保留全样本夏普（``*_sharpe_full`` 字段）供对照打印。

用法示例::

    from ripple_tradePilot.backtest.walkforward import walk_forward
    from ripple_tradePilot.strategies.moving_average import MovingAverageCross

    report = walk_forward(
        strategy_factory=lambda p: MovingAverageCross(fast=p["fast"], slow=p["slow"]),
        bars=bars,
        param_grid={"fast": [3, 5], "slow": [10, 20]},
        n_splits=3,
        train_ratio=0.7,
        warmup_bars=60,        # 预热 ≥ 最慢指标周期，消除 OOS 冷启动哑区
        select_by="sharpe",    # 持仓日夏普选参（与展示口径一致）
    )
    print(report.oos_total_return, report.overfit_gap)
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Callable, Dict, List, Optional, Sequence

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import Metrics, compute_metrics
from ripple_tradePilot.models.types import Bar
from ripple_tradePilot.strategies.base import Strategy

# 训练/测试各自最少需要的 bar 数（compute_metrics 至少需要 2 个点）
_MIN_BARS_PER_SIDE = 2


@dataclass
class WalkForwardSplit:
    """单个滚动窗口的选参与样本外评估结果。

    区间均为左闭右开的 bars 下标：``[train_start, train_end)`` 为训练集，
    ``[test_start, test_end)`` 为测试集，且 ``train_end == test_start``，
    保证测试段严格位于训练段之后、无任何重叠。

    ``is_metrics`` / ``oos_metrics`` 的夏普为**持仓日口径**（与 CLI/网页展示一致）；
    ``is_sharpe_full`` / ``oos_sharpe_full`` 为**全样本口径**夏普，仅供对照。
    ``warmup_start`` 是 OOS 段预热前缀的起点下标——前缀 ``[warmup_start, test_start)``
    严格落在训练窗内（``train_start <= warmup_start``），只喂 ``strategy.warmup()``
    预热指标、不进权益曲线。``warmup_bars=0`` 时 ``warmup_start == test_start``（空前缀）。
    """

    split_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    best_params: Dict[str, object]   # 训练集网格搜索选出的参数
    is_metrics: Metrics              # 训练集（样本内）用最佳参数回测的指标（持仓日夏普）
    oos_metrics: Metrics             # 测试集（样本外）用最佳参数回测的指标（持仓日夏普）
    warmup_start: int = 0            # OOS 预热前缀起点（落在训练窗内）；0 兼容旧构造
    is_sharpe_full: float = 0.0      # 训练集全样本夏普（对照用）
    oos_sharpe_full: float = 0.0     # 测试集全样本夏普（对照用）


@dataclass
class WalkForwardReport:
    """滚动前进验证的聚合报告。"""

    splits: List[WalkForwardSplit]
    oos_total_return: float   # 各 OOS 分段收益复利拼接后的总收益
    avg_is_return: float      # 平均样本内总收益
    avg_oos_return: float     # 平均样本外总收益
    avg_oos_sharpe: float     # 平均样本外夏普
    overfit_gap: float        # 平均 IS 收益 − 平均 OOS 收益，越大越可能过拟合


def _segment_edges(n_bars: int, n_splits: int) -> List[int]:
    """把 n_bars 根 bar 均分为 n_splits 段的边界下标（含首尾）。"""
    return [round(i * n_bars / n_splits) for i in range(n_splits + 1)]


_SELECT_BY = ("sharpe", "return")


def walk_forward(
    strategy_factory: Callable[[dict], Strategy],
    bars: Sequence[Bar],
    param_grid: Dict[str, List],
    n_splits: int = 3,
    train_ratio: float = 0.7,
    backtest_kwargs: Optional[dict] = None,
    warmup_bars: int = 0,
    select_by: str = "sharpe",
) -> WalkForwardReport:
    """滚动前进验证：训练段网格搜索选参，测试段样本外评估。

    Args:
        strategy_factory: 参数到策略实例的工厂函数。每次评估都会调用它
            新建实例，确保不同窗口/参数之间没有状态残留。
        bars: 按时间升序的 bar 序列（整段历史）。
        param_grid: 参数网格，形如 ``{"fast": [3, 5], "slow": [10, 20]}``，
            值取各列表的笛卡尔积。
        n_splits: 滚动窗口数量（把 bars 均分成几段）。
        train_ratio: 每段内训练集占比，取值 (0, 1)。
        backtest_kwargs: 透传给 ``run_backtest`` 的额外参数
            （如 ``initial_cash``、``execution``、``slippage`` 等）。
        warmup_bars: 每个评估窗口前用于预热指标状态的 bar 数（默认 0=旧版冷启动
            行为）。>0 时，训练段用窗口之前 ``warmup_bars`` 根、OOS 段用训练窗内
            末尾 ``warmup_bars`` 根，调 ``strategy.warmup()`` 预热——预热 bar 不进
            ``run_backtest``，故不污染权益/回撤/持仓。建议 ≥ 最慢指标周期。
        select_by: 训练段选参标准，``"sharpe"``（持仓日夏普，与展示口径一致，默认）
            或 ``"return"``（训练期总收益）。

    Returns:
        WalkForwardReport：每个 split 的最佳参数与 IS/OOS 指标，
        以及聚合的 OOS 总收益、平均 OOS 夏普和过拟合缺口。

    Raises:
        ValueError: 窗口数/训练比例非法、参数网格为空、``warmup_bars`` 为负、
            ``select_by`` 非法，或数据量不足以支撑所要求的切分（每段的训练与
            测试都至少需要 2 根 bar）。
    """
    if n_splits < 1:
        raise ValueError("n_splits 必须为正整数")
    if not 0.0 < train_ratio < 1.0:
        raise ValueError("train_ratio 必须在 (0, 1) 开区间内")
    if not param_grid or any(len(list(values)) == 0 for values in param_grid.values()):
        raise ValueError("param_grid 不能为空")
    if warmup_bars < 0:
        raise ValueError("warmup_bars 不能为负")
    if select_by not in _SELECT_BY:
        raise ValueError(f"select_by 必须是 {_SELECT_BY} 之一，收到 {select_by!r}")

    bar_list = list(bars)
    edges = _segment_edges(len(bar_list), n_splits)

    # 预先校验每一段的最小数据量，避免跑出无意义的指标
    for i in range(n_splits):
        size = edges[i + 1] - edges[i]
        train_len = int(round(size * train_ratio))
        if train_len < _MIN_BARS_PER_SIDE or size - train_len < _MIN_BARS_PER_SIDE:
            raise ValueError(
                f"数据量不足：第 {i} 段仅 {size} 根 bar，"
                f"无法同时满足训练/测试各至少 {_MIN_BARS_PER_SIDE} 根"
            )

    kwargs = dict(backtest_kwargs or {})
    param_names = list(param_grid.keys())
    combinations = [
        dict(zip(param_names, values))
        for values in product(*(param_grid[name] for name in param_names))
    ]

    splits: List[WalkForwardSplit] = []
    for i in range(n_splits):
        seg_start, seg_end = edges[i], edges[i + 1]
        train_end = seg_start + int(round((seg_end - seg_start) * train_ratio))
        train_bars = bar_list[seg_start:train_end]
        test_bars = bar_list[train_end:seg_end]

        # 预热前缀：均为评估窗口**之前**的 bar，只喂 strategy.warmup()、不进 run_backtest。
        # - 训练段：窗口起点之前的历史（首段 seg_start=0 时无更早数据，退化为冷启动）；
        # - OOS 段：起点夹到 seg_start，确保前缀严格落在训练窗内（只用过去，无前瞻泄漏）。
        train_warmup_start = max(0, seg_start - warmup_bars)
        train_warmup = bar_list[train_warmup_start:seg_start]
        test_warmup_start = max(seg_start, train_end - warmup_bars)
        test_warmup = bar_list[test_warmup_start:train_end]

        # ---- 训练集网格搜索：按 select_by 选参（持仓日夏普 / 总收益）----
        best_params: Optional[dict] = None
        best_score: Optional[float] = None
        best_metrics: Optional[Metrics] = None
        best_result = None
        for params in combinations:
            strategy = strategy_factory(dict(params))
            if train_warmup:
                strategy.warmup(train_warmup)
            result = run_backtest(strategy, train_bars, **kwargs)
            # 选参口径 = 展示口径：传 positions → 持仓日夏普（cli.py:87 同口径）
            metrics = compute_metrics(result.equity_curve, positions=result.positions)
            score = metrics.sharpe if select_by == "sharpe" else metrics.total_return
            # 评分相同（如全为 0）时保留先出现的组合，保证结果确定可复现
            if best_score is None or score > best_score:
                best_params, best_score, best_metrics, best_result = params, score, metrics, result

        # 全样本夏普仅供对照打印（不影响选参）
        is_sharpe_full = compute_metrics(best_result.equity_curve).sharpe

        # ---- 测试集样本外评估：同一参数、全新实例、训练窗内前缀预热 ----
        oos_strategy = strategy_factory(dict(best_params))
        if test_warmup:
            oos_strategy.warmup(test_warmup)
        oos_result = run_backtest(oos_strategy, test_bars, **kwargs)
        oos_metrics = compute_metrics(oos_result.equity_curve, positions=oos_result.positions)
        oos_sharpe_full = compute_metrics(oos_result.equity_curve).sharpe

        splits.append(
            WalkForwardSplit(
                split_index=i,
                train_start=seg_start,
                train_end=train_end,
                test_start=train_end,
                test_end=seg_end,
                best_params=dict(best_params),
                is_metrics=best_metrics,
                oos_metrics=oos_metrics,
                warmup_start=test_warmup_start,
                is_sharpe_full=is_sharpe_full,
                oos_sharpe_full=oos_sharpe_full,
            )
        )

    # ---- 聚合：OOS 收益复利拼接、IS/OOS 平均、过拟合缺口 ----
    is_returns = [s.is_metrics.total_return for s in splits]
    oos_returns = [s.oos_metrics.total_return for s in splits]
    compound = 1.0
    for ret in oos_returns:
        compound *= 1.0 + ret
    avg_is_return = sum(is_returns) / len(is_returns)
    avg_oos_return = sum(oos_returns) / len(oos_returns)

    return WalkForwardReport(
        splits=splits,
        oos_total_return=compound - 1.0,
        avg_is_return=avg_is_return,
        avg_oos_return=avg_oos_return,
        avg_oos_sharpe=sum(s.oos_metrics.sharpe for s in splits) / len(splits),
        overfit_gap=avg_is_return - avg_oos_return,
    )
