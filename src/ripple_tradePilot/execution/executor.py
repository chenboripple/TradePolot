"""
纸面执行（历史回放）

直接复用统一回测引擎的撮合逻辑，不再维护第二份撮合循环。
真实的券商下单尚未实现（见 live_stub.py 的 BrokerClient 协议），
在补齐订单幂等、持仓对账、审计日志之前，不得接入真实下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List
from uuid import uuid4

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.rules import MarketRules, price_limit_for_symbol
from ripple_tradePilot.models.types import Bar, Fill
from ripple_tradePilot.risk.manager import RiskConfig
from ripple_tradePilot.storage import paper_ledger
from ripple_tradePilot.strategies.base import Strategy


@dataclass
class ExecutionResult:
    fills: List[Fill]
    equity_curve: List[float]


def paper_trade(
    strategy: Strategy,
    bars: Iterable[Bar],
    starting_cash: float = 100000.0,
    fee_rate: float = 0.0003,
    risk_config: RiskConfig | None = None,
    ledger: bool = False,
    run_id: str | None = None,
    symbol: str = "",
) -> ExecutionResult:
    result = run_backtest(
        strategy=strategy,
        bars=bars,
        initial_cash=starting_cash,
        fee_rate=fee_rate,
        risk_config=risk_config,
        # 按标的板块推导涨跌停（symbol 为空时 price_limit_for_symbol 返回主板 10%=引擎默认）
        market_rules=MarketRules(price_limit_pct=price_limit_for_symbol(symbol)),
    )
    if ledger:
        resolved_run_id = run_id or str(uuid4())
        final_equity = (
            result.equity_curve[-1] if result.equity_curve else starting_cash
        )
        paper_ledger.record_run(
            run_id=resolved_run_id,
            symbol=symbol,
            strategy=str(getattr(strategy, "name", strategy.__class__.__name__)),
            initial_cash=starting_cash,
            final_equity=final_equity,
            fills=result.fills,
        )
    return ExecutionResult(fills=result.fills, equity_curve=result.equity_curve)
