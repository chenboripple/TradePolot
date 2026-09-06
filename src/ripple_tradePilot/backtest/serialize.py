"""回测结果序列化（A8）。

Web 端与 CLI 共用同一 ``result_json`` 结构，使 CLI 落库的记录可被前端历史回放
直接消费，杜绝两端格式分裂。结构由本模块单点定义，app.py 与 cli.py 均调此函数。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ripple_tradePilot.backtest.engine import BacktestResult
from ripple_tradePilot.backtest.walkforward import WalkForwardReport
from ripple_tradePilot.models.types import Bar

__all__ = [
    "DISCLAIMER",
    "serialize_backtest_result",
    "serialize_walkforward_report",
]


DISCLAIMER = "样本内回测仅供参考，未经样本外验证的收益不可作为预期收益。"


def serialize_backtest_result(
    symbol: str,
    strategy_key: str,
    execution: str,
    bars: Sequence[Bar],
    result: BacktestResult,
    metrics: Any,
    stats: Any,
    benchmark: Optional[Dict[str, Any]] = None,
    disclaimer: str = DISCLAIMER,
    profile_source: Optional[str] = None,
    strategy_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把回测产物组装成前端可回放的 ``data`` 字典（与 Web 端逐字段一致）。

    ``metrics`` 为 compute_metrics 结果，``stats`` 为 compute_trade_stats 结果；
    ``benchmark`` 给定时附加（取不到则不附加，调用方负责优雅降级）。

    A5 provenance：``profile_source``（"explicit"/"system"/"config:名字"/"default"）与
    ``strategy_params``（实际生效参数摘要）给定时附加，供前端结果卡片显示"这次回测到底跑了
    哪套参数、来自哪里"，并随 ``result_json`` 落库供历史回放核对。
    """
    data: Dict[str, Any] = {
        "symbol": symbol,
        "strategy": strategy_key,
        "execution": execution,
        "bar_count": len(bars),
        "metrics": {
            "total_return": metrics.total_return,
            "annual_return": metrics.annual_return,
            "max_drawdown": metrics.max_drawdown,
            "sharpe": metrics.sharpe,
        },
        "trades": {
            "num_trades": stats.num_trades,
            "win_rate": stats.win_rate,
            "avg_return_per_trade": stats.avg_return_per_trade,
            "best_trade": stats.best_trade,
            "worst_trade": stats.worst_trade,
            "total_fees": stats.total_fees,
        },
        "halted_by_drawdown": result.halted_by_drawdown,
        "skipped_fills": len(result.skipped_fills),
        "equity_curve": [
            {"date": bars[index].timestamp.strftime("%Y-%m-%d"), "equity": value}
            for index, value in enumerate(result.equity_curve)
            if index < len(bars)
        ],
        "fills": [
            {
                "date": fill.timestamp.strftime("%Y-%m-%d"),
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
            }
            for fill in result.fills
        ],
        "disclaimer": disclaimer,
    }
    if benchmark is not None:
        data["benchmark"] = benchmark
    if profile_source is not None:
        data["profile_source"] = profile_source
    if strategy_params is not None:
        data["strategy_params"] = strategy_params
    return data


def _metrics_dict(metrics: Any) -> Dict[str, float]:
    return {
        "total_return": metrics.total_return,
        "annual_return": metrics.annual_return,
        "max_drawdown": metrics.max_drawdown,
        "sharpe": metrics.sharpe,
    }


def serialize_walkforward_report(report: WalkForwardReport) -> Dict[str, Any]:
    """把 walk-forward 报告全量序列化为可 JSON 化、可反序列化比对的结构。

    每段的训练/测试下标、预热起点、最佳参数、样本内/外指标（持仓日夏普）与
    全样本对照夏普都保留，使 ``report_json`` 落库后能逐字段还原 splits。
    """
    return {
        "splits": [
            {
                "split_index": split.split_index,
                "train_start": split.train_start,
                "train_end": split.train_end,
                "test_start": split.test_start,
                "test_end": split.test_end,
                "warmup_start": split.warmup_start,
                "best_params": dict(split.best_params),
                "is_metrics": _metrics_dict(split.is_metrics),
                "oos_metrics": _metrics_dict(split.oos_metrics),
                "is_sharpe_full": split.is_sharpe_full,
                "oos_sharpe_full": split.oos_sharpe_full,
            }
            for split in report.splits
        ],
        "oos_total_return": report.oos_total_return,
        "avg_is_return": report.avg_is_return,
        "avg_oos_return": report.avg_oos_return,
        "avg_oos_sharpe": report.avg_oos_sharpe,
        "overfit_gap": report.overfit_gap,
    }
