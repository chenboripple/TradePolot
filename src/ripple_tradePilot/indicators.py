"""
通用技术指标计算（纯函数，基于 Python list[float]）

dashboard、监控与回测共用同一套指标实现，避免各处手写重复。
约定：序列前部数据不足以计算的位置以 None 占位。
"""

from __future__ import annotations

from math import sqrt
from typing import List, Optional, Sequence, Tuple

# combo_vote 类策略的默认投票阈值（dashboard 与监控共用）
DEFAULT_VOTE_THRESHOLD = 2


def rolling_mean(values: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动均线：前 window-1 个位置为 None，之后为窗口内均值。"""
    result: List[Optional[float]] = [None] * len(values)
    running_total = 0.0
    for index, value in enumerate(values):
        running_total += value
        if index >= window:
            running_total -= values[index - window]
        if index >= window - 1:
            result[index] = running_total / window
    return result


def rsi_series(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """RSI 序列：简单窗口平均口径（与 dashboard 旧实现一致），前 period 个位置为 None。"""
    result: List[Optional[float]] = [None] * len(values)
    for index in range(period, len(values)):
        changes = [
            values[position] - values[position - 1]
            for position in range(index - period + 1, index + 1)
        ]
        gains = sum(max(change, 0) for change in changes) / period
        losses = sum(max(-change, 0) for change in changes) / period
        result[index] = 100.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
    return result


def bollinger(
    values: Sequence[float],
    window: int = 20,
    num_std: float = 2.0,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """布林带：返回 (中轨, 上轨, 下轨)，前 window-1 个位置为 None；标准差按总体口径计算。"""
    middle: List[Optional[float]] = [None] * len(values)
    upper: List[Optional[float]] = [None] * len(values)
    lower: List[Optional[float]] = [None] * len(values)
    for index in range(window - 1, len(values)):
        window_values = values[index - window + 1:index + 1]
        average = sum(window_values) / window
        deviation = sqrt(sum((value - average) ** 2 for value in window_values) / window)
        middle[index] = average
        upper[index] = average + num_std * deviation
        lower[index] = average - num_std * deviation
    return middle, upper, lower


def rolling_max(values: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动最大值：前 window-1 个位置为 None（唐奇安通道上轨用）。"""
    result: List[Optional[float]] = [None] * len(values)
    for index in range(window - 1, len(values)):
        result[index] = max(values[index - window + 1:index + 1])
    return result


def rolling_min(values: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动最小值：前 window-1 个位置为 None（唐奇安通道下轨用）。"""
    result: List[Optional[float]] = [None] * len(values)
    for index in range(window - 1, len(values)):
        result[index] = min(values[index - window + 1:index + 1])
    return result


def ema_series(values: Sequence[float], span: int) -> List[Optional[float]]:
    """指数移动平均：前 span-1 个位置为 None，第 span-1 位用 SMA 种子，其后按
    alpha = 2/(span+1) 递推。signals/ 的增量引擎逐位镜像该运算顺序，保证位级一致。"""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < span or span < 1:
        return result
    alpha = 2.0 / (span + 1)
    seed = sum(values[:span]) / span
    result[span - 1] = seed
    previous = seed
    for index in range(span, len(values)):
        previous = alpha * values[index] + (1 - alpha) * previous
        result[index] = previous
    return result


def macd_series(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """MACD：返回 (DIF, DEA, HIST)。DIF = EMA(fast) − EMA(slow)（自 slow-1 位起有值）；
    DEA = 对 DIF 有值段做 EMA(signal)（SMA 种子）；HIST = DIF − DEA。
    前 slow+signal-2 个位置 DEA 为 None。"""
    n = len(values)
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    dif: List[Optional[float]] = [None] * n
    for index in range(n):
        if ema_fast[index] is not None and ema_slow[index] is not None:
            dif[index] = ema_fast[index] - ema_slow[index]
    dea: List[Optional[float]] = [None] * n
    hist: List[Optional[float]] = [None] * n
    defined = [(index, value) for index, value in enumerate(dif) if value is not None]
    if len(defined) >= signal:
        compact = [value for _, value in defined]
        compact_dea = ema_series(compact, signal)
        for position, (index, _) in enumerate(defined):
            dea[index] = compact_dea[position]
            if compact_dea[position] is not None:
                hist[index] = dif[index] - compact_dea[position]
    return dif, dea, hist


def atr_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> List[Optional[float]]:
    """ATR：真实波幅 TR 的 period 简单均值（首根 TR = high-low，无前收）。
    前 period-1 个位置为 None。"""
    n = len(closes)
    tr: List[float] = []
    for index in range(n):
        if index == 0:
            tr.append(highs[index] - lows[index])
        else:
            previous_close = closes[index - 1]
            tr.append(
                max(
                    highs[index] - lows[index],
                    abs(highs[index] - previous_close),
                    abs(lows[index] - previous_close),
                )
            )
    return rolling_mean(tr, period)
