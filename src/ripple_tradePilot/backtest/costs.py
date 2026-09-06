"""回测成本模型（撮合引擎与 ML 标签共用，避免两套成本口径分裂）。

引擎 ``run_backtest`` 与 B1 的前瞻收益标签 ``ml/labels.py`` 都必须用**同一**成本
模型计算净收益，否则"回测收益"与"标签收益"口径不一致，模型学到的目标与回测展示
的绩效对不上。本模块把成本常量与公式抽成单一来源：

- ``LOT_SIZE``：A 股一手 = 100 股（成交单位整数倍）；
- ``MIN_FEE``：单笔最低佣金（元）；
- ``CostModel``：佣金率 / 印花税 / 滑点 / 最低佣金 / 一手股数的不可变组合，
  提供买卖成本与含滑点成交价的纯函数。

默认值与历史引擎逐位一致（佣金万三、印花税万五、滑点千一），故 ``CostModel()``
 reproduces 旧 ``run_backtest`` 的成本，现有回测结果不变。
"""

from __future__ import annotations

from dataclasses import dataclass

LOT_SIZE = 100            # A 股一手
MIN_FEE = 5.0             # 单笔最低佣金（元）
DEFAULT_FEE_RATE = 0.0003     # 佣金率（买卖双边，万三）
DEFAULT_STAMP_DUTY = 0.0005   # 印花税率（仅卖出，万五）
DEFAULT_SLIPPAGE = 0.001      # 滑点率（买入加价/卖出降价，千一）


@dataclass(frozen=True)
class CostModel:
    """交易成本的不可变参数组与纯函数。

    所有方法均为无状态纯函数，给定 (quantity, price) 即得确定结果，
    便于引擎逐笔调用、也便于标签函数向量化复用。
    """

    fee_rate: float = DEFAULT_FEE_RATE
    stamp_duty: float = DEFAULT_STAMP_DUTY
    slippage: float = DEFAULT_SLIPPAGE
    min_fee: float = MIN_FEE
    lot_size: int = LOT_SIZE

    def buy_fill_price(self, price: float) -> float:
        """买入成交价：基准价加滑点。"""
        return price * (1 + self.slippage)

    def sell_fill_price(self, price: float) -> float:
        """卖出成交价：基准价减滑点。"""
        return price * (1 - self.slippage)

    def buy_cost(self, quantity: int, price: float) -> float:
        """买入费用 = max(最低佣金, 成交额 × 佣金率)。"""
        return max(self.min_fee, quantity * price * self.fee_rate)

    def sell_cost(self, quantity: int, price: float) -> float:
        """卖出费用 = max(最低佣金, 成交额 × 佣金率) + 成交额 × 印花税率。"""
        return max(self.min_fee, quantity * price * self.fee_rate) + quantity * price * self.stamp_duty

    def round_lots(self, quantity: float) -> int:
        """把股数向下取整到一手整数倍。"""
        return int(quantity // self.lot_size) * self.lot_size

    def buy_quantity(self, budget: float, price: float) -> int:
        """给定预算与（含滑点）成交价，返回可买入的一手整数倍股数。

        预留佣金空间：``budget / (price × (1 + fee_rate))`` 再向下取整到手，
        与历史引擎的 ``int(budget / (fill_price * (1 + fee_rate)) // LOT_SIZE) * LOT_SIZE``
        逐位一致。
        """
        return self.round_lots(budget / (price * (1 + self.fee_rate)))
