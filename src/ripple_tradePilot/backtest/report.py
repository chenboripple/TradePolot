"""
回测指标计算

所有指标只接受引擎输出的权益曲线/成交明细，
保证与统一撮合引擎（backtest/engine.py）一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from ripple_tradePilot.models.types import Fill, Side

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Metrics:
    total_return: float
    max_drawdown: float
    sharpe: float
    annual_return: float = 0.0


@dataclass
class BenchmarkComparison:
    strategy_return: float            # 策略总收益
    benchmark_return: float           # 基准总收益
    excess_return: float              # 超额收益（策略 - 基准）
    strategy_max_drawdown: float      # 策略最大回撤（≤0）
    benchmark_max_drawdown: float     # 基准最大回撤（≤0）
    drawdown_improvement: float       # 相对回撤改善（>0 表示策略回撤更小）
    correlation: float = 0.0          # 日收益相关系数（可选，无法计算时为 0）
    beta: float = 0.0                 # 相对基准的 beta（可选，无法计算时为 0）


@dataclass
class TradeStats:
    num_trades: int = 0          # 完整买卖回合数
    win_rate: float = 0.0        # 胜率（按回合净盈亏）
    avg_return_per_trade: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    total_fees: float = field(default=0.0)


def compute_metrics(
    equity_curve: List[float],
    trading_days_per_year: int = TRADING_DAYS_PER_YEAR,
    positions: Optional[Sequence[int]] = None,
) -> Metrics:
    """从逐 bar 权益曲线计算收益/回撤/夏普/年化。

    夏普口径：传入 ``positions``（引擎逐 bar 收盘后的持仓股数）时，只用**持仓日**
    的收益计算夏普。空仓日的 0 收益会同时稀释均值与波动率，把全样本夏普向 0
    拉低并混入仓位管理的影响；持仓日口径衡量的是策略"在场内"的每持仓日风险
    调整收益，便于横向比较信号质量。不传 ``positions`` 时退回全样本口径（向后兼容）。
    """
    eq = np.array(equity_curve, dtype=float)
    if len(eq) < 2:
        return Metrics(total_return=0.0, max_drawdown=0.0, sharpe=0.0, annual_return=0.0)

    returns = np.diff(eq) / eq[:-1]
    total_return = eq[-1] / eq[0] - 1

    peak = np.maximum.accumulate(eq)
    drawdown = (eq - peak) / peak
    max_drawdown = float(drawdown.min())

    # 夏普用的收益序列：默认全样本；给了持仓序列则只取持仓日（returns[i] 对应 positions[i]）
    sharpe_returns = returns
    if positions is not None:
        held = np.asarray(list(positions), dtype=float)[: len(returns)] > 0
        if held.any():
            sharpe_returns = returns[held]

    if len(sharpe_returns) >= 2 and sharpe_returns.std() != 0:
        sharpe = float(
            sharpe_returns.mean() / sharpe_returns.std() * np.sqrt(trading_days_per_year)
        )
    else:
        sharpe = 0.0

    periods = len(eq) - 1
    if eq[0] > 0 and eq[-1] > 0 and periods > 0:
        annual_return = float(
            (eq[-1] / eq[0]) ** (trading_days_per_year / periods) - 1
        )
    else:
        annual_return = 0.0

    return Metrics(
        total_return=float(total_return),
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        annual_return=annual_return,
    )


def compare_with_benchmark(
    equity_curve: List[float],
    benchmark_prices: List[float],
    trading_days_per_year: int = TRADING_DAYS_PER_YEAR,
) -> BenchmarkComparison:
    """把策略权益曲线与基准（如沪深300）价格序列做对比。

    语义说明：
    - 输入均为数值 list（权益/价格，要求为正数）；
    - 两者长度不一致时按较短一方截断对齐，多余的 bar 不参与计算；
    - 任一输入为空时抛 ValueError；
    - 对齐后长度不足 2 时无法计算收益/回撤，返回全零结果；
    - 超额收益为算术差：策略总收益 - 基准总收益；
    - 回撤改善 = 策略最大回撤 - 基准最大回撤（回撤均为非正数，
      结果为正表示策略回撤更小）；
    - 相关系数与 beta 基于逐日收益计算；样本不足或基准收益方差为 0 时置 0。

    Args:
        equity_curve: 策略逐 bar 权益曲线
        benchmark_prices: 基准收盘价序列
        trading_days_per_year: 年化交易日数（预留参数，与接口保持一致）

    Returns:
        BenchmarkComparison
    """
    eq = np.asarray(list(equity_curve), dtype=float)
    bm = np.asarray(list(benchmark_prices), dtype=float)
    if len(eq) == 0 or len(bm) == 0:
        raise ValueError("权益曲线与基准价格均不能为空")

    # 长度不一致时按较短一方对齐
    n = min(len(eq), len(bm))
    eq = eq[:n]
    bm = bm[:n]

    if n < 2:
        return BenchmarkComparison(
            strategy_return=0.0,
            benchmark_return=0.0,
            excess_return=0.0,
            strategy_max_drawdown=0.0,
            benchmark_max_drawdown=0.0,
            drawdown_improvement=0.0,
        )

    strategy_return = float(eq[-1] / eq[0] - 1)
    benchmark_return = float(bm[-1] / bm[0] - 1)

    def _max_drawdown(series: np.ndarray) -> float:
        peak = np.maximum.accumulate(series)
        return float(((series - peak) / peak).min())

    strategy_dd = _max_drawdown(eq)
    benchmark_dd = _max_drawdown(bm)

    s_ret = np.diff(eq) / eq[:-1]
    b_ret = np.diff(bm) / bm[:-1]

    correlation = 0.0
    beta = 0.0
    if len(s_ret) >= 2:
        b_var = float(np.var(b_ret, ddof=1))
        if b_var > 0:
            beta = float(np.cov(s_ret, b_ret, ddof=1)[0, 1] / b_var)
            if float(np.var(s_ret, ddof=1)) > 0:
                correlation = float(np.corrcoef(s_ret, b_ret)[0, 1])

    return BenchmarkComparison(
        strategy_return=strategy_return,
        benchmark_return=benchmark_return,
        excess_return=strategy_return - benchmark_return,
        strategy_max_drawdown=strategy_dd,
        benchmark_max_drawdown=benchmark_dd,
        drawdown_improvement=strategy_dd - benchmark_dd,
        correlation=correlation,
        beta=beta,
    )


def pair_trades(fills: List[Fill]) -> List[dict]:
    """把 BUY/SELL 成对匹配为完整回合（单标的满仓进出模型）。"""
    trades: List[dict] = []
    entry: Fill | None = None
    for fill in fills:
        if fill.side == Side.BUY:
            entry = fill
        elif fill.side == Side.SELL and entry is not None:
            quantity = min(entry.quantity, fill.quantity)
            cost = entry.price * quantity + entry.fee + fill.fee
            proceeds = fill.price * quantity
            net_return = proceeds / cost - 1 if cost > 0 else 0.0
            trades.append(
                {
                    "entry_time": entry.timestamp,
                    "exit_time": fill.timestamp,
                    "entry_price": entry.price,
                    "exit_price": fill.price,
                    "quantity": quantity,
                    "return": net_return,
                }
            )
            entry = None
    return trades


def compute_trade_stats(fills: List[Fill]) -> TradeStats:
    trades = pair_trades(fills)
    if not trades:
        return TradeStats(total_fees=sum(f.fee for f in fills))
    returns = [t["return"] for t in trades]
    wins = sum(1 for r in returns if r > 0)
    return TradeStats(
        num_trades=len(trades),
        win_rate=wins / len(trades),
        avg_return_per_trade=float(np.mean(returns)),
        best_trade=max(returns),
        worst_trade=min(returns),
        total_fees=sum(f.fee for f in fills),
    )
