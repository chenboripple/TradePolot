"""行情数据清洗工具（数据源无关，供各 loader 复用）。

主要解决量纲脏数据：某些数据源（如自然语言查询接口）偶发返回与真实价格
相差数个数量级的价格（例：真实约 40 元的标的混入 0.19 元），这类 bar 一旦
进入回测会严重扭曲收益与撮合。此处以序列中位数为基准做稳健过滤。
"""

from __future__ import annotations

from typing import Iterable, List

from ripple_tradePilot.models.types import Bar


def is_valid_ohlc(open_price: float, high: float, low: float, close: float) -> bool:
    """单根 bar 的基本合理性：四个价格均为正、非 NaN、且 high >= low。"""
    values = (open_price, high, low, close)
    return all(v == v and v > 0 for v in values) and high >= low


def reject_price_outliers(bars: Iterable[Bar], factor: float = 10.0) -> List[Bar]:
    """剔除价格量纲异常的脏 bar。

    以收盘价**中位数**为基准（中位数对离群值稳健），丢弃收盘价偏离超过
    ``factor`` 倍的 bar，同时丢弃未通过 ``is_valid_ohlc`` 的 bar。
    样本过少（<3 根有效收盘）时不做离群过滤，仅做基本合理性校验，
    避免因数据太少误杀。

    Args:
        bars: 待清洗的 Bar 序列。
        factor: 允许的价格偏离倍数，默认 10 倍。

    Returns:
        清洗后的 Bar 列表（保持原顺序）。
    """
    bar_list = list(bars)
    valid = [b for b in bar_list if is_valid_ohlc(b.open, b.high, b.low, b.close)]
    closes = sorted(b.close for b in valid)
    if len(closes) < 3:
        return valid

    median = closes[len(closes) // 2]
    if median <= 0:
        return valid

    low_bound = median / factor
    high_bound = median * factor
    return [b for b in valid if low_bound <= b.close <= high_bound]
