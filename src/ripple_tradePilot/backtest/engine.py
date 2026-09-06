"""
统一回测引擎（唯一撮合实现）

撮合假设（明确声明，避免隐性偏差）：
- 默认 ``execution="next_open"``：信号在第 i 根 bar 收盘后产生，
  在第 i+1 根 bar 的开盘价成交 —— 因果正确，贴近实盘。
  ``execution="close"`` 为信号当根收盘价成交（研究模式，偏乐观，
  仅用于与旧结果对照，不应作为决策依据）。
- 涨跌停约束：开盘价（next_open）或收盘价（close，A7 起也拦截）触及涨停 →
  无法买入；触及跌停 → 无法卖出。涨跌幅比例由 ``MarketRules.price_limit_pct``
  给出，调用方按标的用 ``rules.price_limit_for_symbol`` 推导（创业板/科创板 20%、
  北交所 30%、主板 10%）。
- T+1：``MarketRules.t_plus_1=True``（默认）时，当日买入的持仓禁止当日卖出，
  按 bar 日历日比较（日线天然满足，分钟线实际生效）。
- 成交量参与率：``MarketRules.max_volume_participation`` 设定后，单根 bar 的成交
  股数不超过该 bar 成交量 × 参与率（向下取整到手），超出部分截断并记入
  ``skipped_fills``；默认 ``None`` 不限制。
- 成交单位为 ``MarketRules.lot_size``（默认 100 股，A 股一手）整数倍。
- 成本模型：佣金（双边，默认万三，最低 5 元）+ 卖出印花税（默认万五）
  + 滑点（按成交金额比例，默认千一），统一由 ``costs.CostModel`` 计算。
- 风控回撤闸门触发后停止开新仓，而不是截断回测时间线；
  持仓仍按止损/止盈规则退出，权益曲线完整记录。

⚠️ ``Signal.strength`` **不参与仓位计算**：引擎一律按 ``RiskConfig.max_position_pct``
与可用现金决定买入股数，strength 仅用于展示/通知排序。把信号强度映射为仓位是
D 阶段"模型期望收益→仓位"的工作，本期刻意不做，以保持全部历史回测的可比性。

与旧实现的差异（修复项）：
- 卖出收入 ``cash += proceeds - fee``（旧版为覆盖写，部分仓位下现金被清零）
- 权益曲线逐 bar 用真实仓位计算（旧版用期末仓位重建，回撤/夏普恒为 0）
- A7：``close`` 模式补上涨跌停拦截；新增分板块涨跌幅、T+1、成交量参与率
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, List, Optional

from ripple_tradePilot.backtest.costs import LOT_SIZE, MIN_FEE, CostModel
from ripple_tradePilot.backtest.rules import MarketRules
from ripple_tradePilot.models.types import Bar, Fill, Side
from ripple_tradePilot.risk.manager import RiskConfig, RiskManager
from ripple_tradePilot.strategies.base import Strategy

# 向后兼容再导出（旧代码/测试从 engine 引这两个常量）
__all__ = ["LOT_SIZE", "MIN_FEE", "BacktestResult", "run_backtest"]

# 涨跌停判定容差：容忍复权/四舍五入造成的微小偏差（0.2%）
_LIMIT_TOLERANCE = 0.998


@dataclass
class BacktestResult:
    equity_curve: List[float]
    fills: List[Fill]
    halted_by_drawdown: bool = False
    skipped_fills: List[dict] = field(default_factory=list)  # 涨跌停/T+1/成交量等无法成交记录
    positions: List[int] = field(default_factory=list)  # 逐 bar 收盘后的持仓股数（0=空仓）


def _at_limit_up(price: float, prev_close: Optional[float], pct: float) -> bool:
    return prev_close is not None and price >= prev_close * (1 + pct) * _LIMIT_TOLERANCE


def _at_limit_down(price: float, prev_close: Optional[float], pct: float) -> bool:
    return prev_close is not None and price <= prev_close * (1 - pct) * (2 - _LIMIT_TOLERANCE)


def run_backtest(
    strategy: Strategy,
    bars: Iterable[Bar],
    initial_cash: float = 100000.0,
    fee_rate: float = 0.0003,
    stamp_duty: float = 0.0005,
    slippage: float = 0.001,
    execution: str = "next_open",
    risk_config: RiskConfig | None = None,
    market_rules: MarketRules | None = None,
) -> BacktestResult:
    """对单一标的运行回测，返回权益曲线与成交明细。

    Args:
        strategy: 流式策略实例（引擎负责逐 bar 喂入，策略只看到历史）。
        bars: 按时间升序的日线/分钟线序列。
        initial_cash: 初始资金。
        execution: ``"next_open"``（默认，因果正确）或 ``"close"``（当根收盘，偏乐观）。
        fee_rate: 佣金率（买卖双边），默认万三。
        stamp_duty: 印花税率（仅卖出），默认万五。
        slippage: 滑点率，买入加价/卖出降价，默认千一。
        risk_config: 风控参数（仓位上限/止损/止盈/回撤闸门）。
        market_rules: 市场微观结构规则（分板块涨跌停/T+1/成交量参与率/一手股数）。
            ``None`` 时用 ``MarketRules()`` 默认——主板 ±10%、close 模式拦截涨跌停、
            T+1 开启、不限成交量。调用方应按标的传 ``price_limit_for_symbol(symbol)``。
    """
    if execution not in ("next_open", "close"):
        raise ValueError(f"unsupported execution mode: {execution}")

    rules = market_rules or MarketRules()
    costs = CostModel(fee_rate=fee_rate, stamp_duty=stamp_duty, slippage=slippage,
                      lot_size=rules.lot_size)
    limit_pct = rules.price_limit_pct
    lot_size = rules.lot_size

    bar_list = list(bars)
    cash = initial_cash
    position = 0
    entry_date: Optional[date] = None   # T+1：当前持仓的买入日历日
    equity_curve: List[float] = []
    positions: List[int] = []
    fills: List[Fill] = []
    skipped: List[dict] = []
    risk = RiskManager(risk_config or RiskConfig())
    halted = False          # 回撤闸门触发后不再开新仓
    pending: Optional[Side] = None   # next_open 模式下待执行的信号

    def volume_cap(bar: Bar) -> Optional[int]:
        """该 bar 允许成交的最大股数（一手整数倍）；None=不限。"""
        if rules.max_volume_participation is None:
            return None
        return int(bar.volume * rules.max_volume_participation // lot_size) * lot_size

    def record_skip(bar: Bar, side: Side, reason: str, truncated: int = 0) -> None:
        item = {"timestamp": bar.timestamp, "side": side.value, "reason": reason}
        if truncated:
            item["truncated_shares"] = truncated
        skipped.append(item)

    def do_buy(bar: Bar, base_price: float, prev_close: Optional[float],
               check_limit: bool = True) -> bool:
        nonlocal cash, position, entry_date
        if check_limit and _at_limit_up(base_price, prev_close, limit_pct):
            record_skip(bar, Side.BUY, "涨停无法买入")
            return False
        if position != 0 or halted:
            return False
        fill_price = costs.buy_fill_price(base_price)
        budget = min(cash, risk.cap_position(cash))
        quantity = costs.buy_quantity(budget, fill_price)
        cap = volume_cap(bar)
        if cap is not None and quantity > cap:
            record_skip(bar, Side.BUY, "成交量参与率上限", truncated=quantity - cap)
            quantity = cap
        if quantity <= 0:
            return False
        fee = costs.buy_cost(quantity, fill_price)
        cash -= quantity * fill_price + fee
        position = quantity
        entry_date = bar.timestamp.date()
        fills.append(Fill(bar.timestamp, Side.BUY, quantity, fill_price, fee))
        risk.set_entry(fill_price)
        return True

    def do_sell(bar: Bar, base_price: float, prev_close: Optional[float],
                check_limit: bool = True) -> bool:
        nonlocal cash, position, entry_date
        if position <= 0:
            return False
        if check_limit and _at_limit_down(base_price, prev_close, limit_pct):
            record_skip(bar, Side.SELL, "跌停无法卖出")
            return False
        if rules.t_plus_1 and entry_date is not None and bar.timestamp.date() == entry_date:
            record_skip(bar, Side.SELL, "T+1 当日买入不可卖出")
            return False
        fill_price = costs.sell_fill_price(base_price)
        quantity = position
        cap = volume_cap(bar)
        if cap is not None and quantity > cap:
            record_skip(bar, Side.SELL, "成交量参与率上限", truncated=quantity - cap)
            quantity = cap
        if quantity <= 0:
            return False
        fee = costs.sell_cost(quantity, fill_price)
        cash += quantity * fill_price - fee
        fills.append(Fill(bar.timestamp, Side.SELL, quantity, fill_price, fee))
        position -= quantity
        if position == 0:
            entry_date = None
            risk.clear_entry()
        return True

    for index, bar in enumerate(bar_list):
        prev_close = bar_list[index - 1].close if index > 0 else None

        # ---- 1. 执行上一根 bar 产生的信号（次日开盘成交） ----
        if execution == "next_open" and pending is not None and index > 0:
            side = pending
            pending = None
            if side == Side.BUY:
                do_buy(bar, bar.open, prev_close)
            else:
                executed = do_sell(bar, bar.open, prev_close)
                # T+1 当日不可卖：保留卖出意图，下一根（次日）再试
                if (not executed and rules.t_plus_1 and entry_date is not None
                        and bar.timestamp.date() == entry_date):
                    pending = Side.SELL

        # ---- 2. 权益与风控状态 ----
        equity = cash + position * bar.close
        risk.update_equity(equity)
        if risk.check_drawdown(equity):
            halted = True

        # ---- 3. 持仓风控退出（止损/止盈，按收盘价决策） ----
        if position > 0 and (risk.should_stop_loss(bar.close) or risk.should_take_profit(bar.close)):
            if execution == "close":
                do_sell(bar, bar.close, prev_close, check_limit=rules.enforce_limit_on_close)
            else:
                pending = Side.SELL  # 次日开盘执行

        # ---- 4. 策略信号 ----
        if position == 0 or pending != Side.SELL:
            signal = strategy.on_bar(bar)
        else:
            # 已决定离场时不再叠加反向开仓信号（仍调用 on_bar 推进策略状态）
            strategy.on_bar(bar)
            signal = None

        if signal is not None and signal.side is not None:
            if execution == "close":
                if signal.side == Side.BUY and position == 0 and not halted:
                    do_buy(bar, bar.close, prev_close, check_limit=rules.enforce_limit_on_close)
                elif signal.side == Side.SELL and position > 0:
                    do_sell(bar, bar.close, prev_close, check_limit=rules.enforce_limit_on_close)
            elif signal.side == Side.BUY and position == 0 and not halted:
                pending = Side.BUY
            elif signal.side == Side.SELL and position > 0 and pending != Side.SELL:
                pending = Side.SELL

        # ---- 5. 逐 bar 记录真实权益与持仓 ----
        equity = cash + position * bar.close
        equity_curve.append(equity)
        positions.append(position)

    return BacktestResult(
        equity_curve=equity_curve,
        fills=fills,
        halted_by_drawdown=halted,
        skipped_fills=skipped,
        positions=positions,
    )
