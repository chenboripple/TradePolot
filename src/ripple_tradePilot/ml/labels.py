"""前瞻收益标签（B1 · 纯函数）。

把"可验证的预测目标"钉死成与回测引擎自洽的口径：

    T 日收盘后决策 → T+1 开盘买入 → 持有 horizon 个交易日 → T+1+horizon 开盘卖出

即 ``entry = open[i+1]``、``exit = open[i+1+horizon]``（horizon=5 时 exit=open[T+6]），
与引擎的 ``next_open`` 撮合、T+1 约束一致——标签学到的目标正是回测/实盘能复现的动作。

净收益含完整往返成本，**镜像** :class:`~ripple_tradePilot.backtest.costs.CostModel`
的滑点/佣金/印花税口径，避免"标签收益"与"回测收益"两套成本对不上。标签是
**单位资金的无量纲收益**（per-share），故只计比例成本，不含 ``min_fee``（最低佣金是
与持仓规模相关的固定下限，属仓位/引擎层，放进 per-share 标签会引入规模依赖）。

下行风险标签 ``mae``（持有期最低价相对 entry 的最大回撤）是 D 阶段回归目标，
与"预期收益 + 下行风险"的决策口径对应。

bars 不足以走完整 horizon 时返回 ``None`` 丢弃——**绝不用尾部截断价冒充完整标签**，
否则最后几根 bar 的标签会系统性偏短、污染训练集。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from ripple_tradePilot.backtest.costs import CostModel
from ripple_tradePilot.models.types import Bar

__all__ = [
    "HorizonLabel",
    "horizon_label",
    "label_series",
    "round_trip_cost_factor",
]


@dataclass(frozen=True)
class HorizonLabel:
    """单个决策点（T 日收盘）的前瞻收益标签。"""

    entry: float  # open[T+1]，买入基准价
    exit: float  # open[T+1+horizon]，卖出基准价
    gross_return: float  # exit/entry - 1，不含成本
    net_return: float  # 含完整往返成本的净收益
    mae: float  # max adverse excursion：持有期最低价/entry - 1（≤0，下行风险）
    mfe: float  # max favorable excursion：持有期最高价/entry - 1（≥0）
    win: bool  # net_return > 0
    # 本函数只在 horizon 完整时返回标签，故 complete 恒为 True；不完整返回 None。
    # 保留该字段以明示契约、便于下游过滤。
    complete: bool = True


def round_trip_cost_factor(costs: Optional[CostModel] = None) -> float:
    """平价（entry==exit）往返的净收益损失（正数）。

    即 ``-net_return`` 当价格不变时的取值，等于滑点+佣金+印花税的合成成本因子。
    供测试与"成本是否吃掉信号"的快速判断复用。
    """
    model = costs or CostModel()
    buy_cost_ps = model.buy_fill_price(1.0) * (1 + model.fee_rate)
    sell_net_ps = model.sell_fill_price(1.0) * (1 - model.fee_rate - model.stamp_duty)
    return 1.0 - sell_net_ps / buy_cost_ps


def _net_return(entry: float, exit_price: float, costs: CostModel) -> float:
    """含成本净收益：每 share 买入成本（含滑点+佣金）vs 卖出净得（扣滑点+佣金+印花税）。"""
    buy_cost_ps = costs.buy_fill_price(entry) * (1 + costs.fee_rate)
    sell_net_ps = costs.sell_fill_price(exit_price) * (
        1 - costs.fee_rate - costs.stamp_duty
    )
    if buy_cost_ps <= 0:
        return 0.0
    return sell_net_ps / buy_cost_ps - 1.0


def horizon_label(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    i: int,
    horizon: int = 5,
    costs: Optional[CostModel] = None,
) -> Optional[HorizonLabel]:
    """第 ``i`` 根 bar（T 日收盘）决策的 horizon 日前瞻标签；数据不足返回 ``None``。

    需要 ``open[i+1]``（entry）与 ``open[i+1+horizon]``（exit），以及持有期
    ``[i+1, i+horizon]`` 的 high/low（MAE/MFE）。任一越界 → ``None``。

    ``closes`` 仅为接口对齐/未来扩展保留（当前口径用 open 进出，不用 close）。
    """
    if horizon < 1:
        raise ValueError("horizon 必须 ≥ 1")
    model = costs or CostModel()
    n = len(opens)
    entry_index = i + 1
    exit_index = i + 1 + horizon
    if i < 0 or exit_index >= n:
        return None
    # 持有期 high/low 切片 [i+1, i+horizon]（共 horizon 根，卖出发生在 i+1+horizon 的开盘）
    if len(highs) <= i + horizon or len(lows) <= i + horizon:
        return None

    entry = float(opens[entry_index])
    exit_price = float(opens[exit_index])
    if entry <= 0:
        return None

    holding_lows = [float(x) for x in lows[entry_index : i + 1 + horizon]]
    holding_highs = [float(x) for x in highs[entry_index : i + 1 + horizon]]
    # 持有期低点/高点至少为正才有意义；异常数据回退到 entry
    valid_lows = [x for x in holding_lows if x > 0]
    valid_highs = [x for x in holding_highs if x > 0]
    worst_low = min(valid_lows) if valid_lows else entry
    best_high = max(valid_highs) if valid_highs else entry

    gross_return = exit_price / entry - 1.0
    net_return = _net_return(entry, exit_price, model)
    return HorizonLabel(
        entry=entry,
        exit=exit_price,
        gross_return=gross_return,
        net_return=net_return,
        mae=worst_low / entry - 1.0,
        mfe=best_high / entry - 1.0,
        win=net_return > 0.0,
        complete=True,
    )


def label_series(
    bars: Sequence[Bar],
    horizon: int = 5,
    costs: Optional[CostModel] = None,
) -> List[Optional[HorizonLabel]]:
    """对每根 bar 生成 horizon 日前瞻标签，返回与 ``bars`` 等长的列表。

    ``labels[i]`` 对应"在第 i 根 bar 收盘决策"的标签；尾部不足以走完整 horizon 的
    位置为 ``None``（调用方据此丢弃，绝不截断冒充）。同一组 bars 可用不同 horizon
    各调一次，得到主标签（5 日）与辅助标签（10 日）。
    """
    opens = [bar.open for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    closes = [bar.close for bar in bars]
    return [
        horizon_label(opens, highs, lows, closes, i, horizon=horizon, costs=costs)
        for i in range(len(bars))
    ]
