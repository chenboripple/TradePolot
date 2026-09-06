"""组件状态票：增量引擎 + 批量序列（批量 = 逐根喂增量，天然一致）。

语义约定（统一口径，取代旧 dashboard/monitor 三套实现）：
- 组件在每根 bar 上输出**状态票**（Side.BUY / Side.SELL / None=不投票），
  不是边沿触发的交叉事件；事件语义由 voting.decision_events 在决策层派生。
- ma: fast > slow → BUY，fast < slow → SELL，**相等 → 不投票**
  （改变旧 dashboard "相等给 SELL" 的行为）；
- rsi: 闭区间 rsi <= oversold → BUY，rsi >= overbought → SELL；
- bollinger: close <= 下轨 → BUY，close >= 上轨 → SELL；带宽为 0（常数序列）不投票；
- macd: dif > dea → BUY，dif < dea → SELL（状态化；zero_cross 参数忽略）；
- trend: 短 > 中 > 长 → BUY，短 < 中 < 长 → SELL，其余不投票；
- donchian: close 突破**前一窗口**（不含当前 bar）高点 → BUY / 低点 → SELL。

数值一致性：每个引擎逐位镜像 indicators.py 对应序列函数的运算顺序
（rolling_mean 的 running-total 加减次序、rsi/bollinger 的窗口求和次序、
EMA 的 SMA 种子 + 递推），tests/test_signals_parity.py 断言位级相等（==）。

strength 约定：基础 1.0；rsi/bollinger 按越界深度加成至多 1.5
（仅用于展示与通知排序，回测撮合不消费——见 engine.py 文档）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import sqrt
from typing import Deque, Dict, List, Mapping, Optional, Sequence

from ..models.types import Bar, Side
from .profile import ComponentSpec

# 每类组件出首票所需的最少 bar 数（预热长度）
_WARMUP_FORMULAS = {
    "ma": lambda p: p["slow"],
    "rsi": lambda p: p["period"] + 1,
    "bollinger": lambda p: p["period"],
    "macd": lambda p: p["slow"] + p["signal"] - 1,
    "trend": lambda p: p["long"],
    "donchian": lambda p: p["window"] + 1,
}


@dataclass(frozen=True)
class ComponentVote:
    """单根 bar 上一个组件的状态票。"""

    name: str
    kind: str
    side: Optional[Side]
    strength: float = 1.0
    detail: Dict[str, Optional[float]] = field(default_factory=dict)

    @property
    def votes(self) -> bool:
        return self.side is not None


def required_bars(spec: ComponentSpec) -> int:
    """该组件出首票所需的最少 bar 数。"""
    return _WARMUP_FORMULAS[spec.kind](spec.params)


def warmup_bars(spec_or_components) -> int:
    """整个画像出满票所需的最少 bar 数（各组件预热长度取最大）。"""
    components = getattr(spec_or_components, "components", spec_or_components)
    return max(required_bars(component) for component in components)


def make_engine(spec: ComponentSpec) -> "ComponentStateEngine":
    """按组件类型构造增量状态引擎。"""
    engine_cls = _ENGINES[spec.kind]
    return engine_cls(spec)


def component_state_series(
    spec: ComponentSpec, bars: Sequence[Bar]
) -> List[ComponentVote]:
    """批量口径 = 逐根喂增量引擎（同一实现，不存在两套公式）。"""
    engine = make_engine(spec)
    return [engine.push(bar) for bar in bars]


class ComponentStateEngine:
    """增量组件状态引擎基类：push(bar) -> ComponentVote，O(1)~O(window)/根。"""

    def __init__(self, spec: ComponentSpec):
        self.spec = spec
        self.params = dict(spec.params)
        self.reset()

    @property
    def required_bars(self) -> int:
        return _WARMUP_FORMULAS[self.spec.kind](self.params)

    def reset(self) -> None:  # pragma: no cover - 子类必须实现
        raise NotImplementedError

    def push(self, bar: Bar) -> ComponentVote:  # pragma: no cover - 子类必须实现
        raise NotImplementedError

    def _vote(
        self,
        side: Optional[Side],
        detail: Dict[str, Optional[float]],
        strength: float = 1.0,
    ) -> ComponentVote:
        return ComponentVote(
            name=self.spec.name,
            kind=self.spec.kind,
            side=side,
            strength=strength if side is not None else 1.0,
            detail=detail,
        )


class _RunningMean:
    """逐位镜像 indicators.rolling_mean 的 running-total 运算顺序。"""

    def __init__(self, window: int):
        self.window = window
        self._values: Deque[float] = deque(maxlen=window)
        self._total = 0.0

    def reset(self) -> None:
        self._values.clear()
        self._total = 0.0

    def push(self, value: float) -> Optional[float]:
        # 运算顺序与 rolling_mean 完全一致：先加新值，窗口满时再减出窗的旧值。
        # 注意必须显式 popleft——deque(maxlen=...) 的静默淘汰会跳过减法。
        self._total += value
        if len(self._values) == self.window:
            self._total -= self._values.popleft()
        self._values.append(value)
        if len(self._values) == self.window:
            return self._total / self.window
        return None


class _EmaState:
    """逐位镜像 indicators.ema_series：SMA 种子 + alpha 递推。"""

    def __init__(self, span: int):
        self.span = span
        self.alpha = 2.0 / (span + 1)
        self.reset()

    def reset(self) -> None:
        self._count = 0
        self._seed_sum = 0.0
        self.value: Optional[float] = None

    def push(self, value: float) -> Optional[float]:
        if self.value is None:
            self._count += 1
            self._seed_sum += value
            if self._count == self.span:
                self.value = self._seed_sum / self.span
        else:
            self.value = self.alpha * value + (1 - self.alpha) * self.value
        return self.value


class MaEngine(ComponentStateEngine):
    def reset(self) -> None:
        self._fast = _RunningMean(int(self.params["fast"]))
        self._slow = _RunningMean(int(self.params["slow"]))

    def push(self, bar: Bar) -> ComponentVote:
        fast_ma = self._fast.push(bar.close)
        slow_ma = self._slow.push(bar.close)
        side: Optional[Side] = None
        if fast_ma is not None and slow_ma is not None:
            if fast_ma > slow_ma:
                side = Side.BUY
            elif fast_ma < slow_ma:
                side = Side.SELL
        return self._vote(side, {"fast_ma": fast_ma, "slow_ma": slow_ma})


class RsiEngine(ComponentStateEngine):
    def reset(self) -> None:
        self._period = int(self.params["period"])
        self._oversold = float(self.params["oversold"])
        self._overbought = float(self.params["overbought"])
        self._changes: Deque[float] = deque(maxlen=self._period)
        self._previous_close: Optional[float] = None

    def push(self, bar: Bar) -> ComponentVote:
        if self._previous_close is not None:
            self._changes.append(bar.close - self._previous_close)
        self._previous_close = bar.close
        rsi: Optional[float] = None
        if len(self._changes) == self._period:
            gains = sum(max(change, 0) for change in self._changes) / self._period
            losses = sum(max(-change, 0) for change in self._changes) / self._period
            rsi = 100.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
        side: Optional[Side] = None
        strength = 1.0
        if rsi is not None:
            if rsi <= self._oversold:
                side = Side.BUY
                depth = (self._oversold - rsi) / self._oversold
                strength = 1.0 + min(max(depth, 0.0), 1.0) * 0.5
            elif rsi >= self._overbought:
                side = Side.SELL
                depth = (rsi - self._overbought) / (100.0 - self._overbought)
                strength = 1.0 + min(max(depth, 0.0), 1.0) * 0.5
        return self._vote(side, {"rsi": rsi}, strength)


class BollingerEngine(ComponentStateEngine):
    def reset(self) -> None:
        self._period = int(self.params["period"])
        self._num_std = float(self.params["num_std"])
        self._closes: Deque[float] = deque(maxlen=self._period)

    def push(self, bar: Bar) -> ComponentVote:
        self._closes.append(bar.close)
        middle: Optional[float] = None
        upper: Optional[float] = None
        lower: Optional[float] = None
        side: Optional[Side] = None
        strength = 1.0
        if len(self._closes) == self._period:
            average = sum(self._closes) / self._period
            deviation = sqrt(
                sum((value - average) ** 2 for value in self._closes) / self._period
            )
            middle = average
            upper = average + self._num_std * deviation
            lower = average - self._num_std * deviation
            if deviation > 0:
                # 触轨判定与旧 dashboard 相同（闭区间）；带宽为 0 的常数序列不投票
                if bar.close <= lower:
                    side = Side.BUY
                    depth = (lower - bar.close) / (middle - lower)
                    strength = 1.0 + min(max(depth, 0.0), 1.0) * 0.5
                elif bar.close >= upper:
                    side = Side.SELL
                    depth = (bar.close - upper) / (upper - middle)
                    strength = 1.0 + min(max(depth, 0.0), 1.0) * 0.5
        return self._vote(side, {"middle": middle, "upper": upper, "lower": lower}, strength)


class MacdEngine(ComponentStateEngine):
    """DIF/DEA 状态票：镜像 indicators.macd_series（DEA = DIF 有值段的 EMA，SMA 种子）。"""

    def reset(self) -> None:
        self._ema_fast = _EmaState(int(self.params["fast"]))
        self._ema_slow = _EmaState(int(self.params["slow"]))
        self._ema_signal = _EmaState(int(self.params["signal"]))

    def push(self, bar: Bar) -> ComponentVote:
        fast = self._ema_fast.push(bar.close)
        slow = self._ema_slow.push(bar.close)
        dif: Optional[float] = None
        dea: Optional[float] = None
        hist: Optional[float] = None
        if fast is not None and slow is not None:
            dif = fast - slow
            dea = self._ema_signal.push(dif)
            if dea is not None:
                hist = dif - dea
        side: Optional[Side] = None
        if dif is not None and dea is not None:
            if dif > dea:
                side = Side.BUY
            elif dif < dea:
                side = Side.SELL
        return self._vote(side, {"dif": dif, "dea": dea, "hist": hist})


class TrendEngine(ComponentStateEngine):
    def reset(self) -> None:
        self._short = _RunningMean(int(self.params["short"]))
        self._medium = _RunningMean(int(self.params["medium"]))
        self._long = _RunningMean(int(self.params["long"]))

    def push(self, bar: Bar) -> ComponentVote:
        short_ma = self._short.push(bar.close)
        medium_ma = self._medium.push(bar.close)
        long_ma = self._long.push(bar.close)
        side: Optional[Side] = None
        if short_ma is not None and medium_ma is not None and long_ma is not None:
            if short_ma > medium_ma > long_ma:
                side = Side.BUY
            elif short_ma < medium_ma < long_ma:
                side = Side.SELL
        return self._vote(
            side, {"short_ma": short_ma, "medium_ma": medium_ma, "long_ma": long_ma}
        )


class DonchianEngine(ComponentStateEngine):
    """收盘突破前一窗口（不含当前 bar）高/低点。"""

    def reset(self) -> None:
        self._window = int(self.params["window"])
        self._highs: Deque[float] = deque(maxlen=self._window)
        self._lows: Deque[float] = deque(maxlen=self._window)

    def push(self, bar: Bar) -> ComponentVote:
        channel_high: Optional[float] = None
        channel_low: Optional[float] = None
        side: Optional[Side] = None
        if len(self._highs) == self._window:
            channel_high = max(self._highs)
            channel_low = min(self._lows)
            if bar.close > channel_high:
                side = Side.BUY
            elif bar.close < channel_low:
                side = Side.SELL
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        return self._vote(
            side,
            {
                "channel_high": channel_high,
                "channel_low": channel_low,
                "window": float(self._window),
            },
        )


_ENGINES: Mapping[str, type] = {
    "ma": MaEngine,
    "rsi": RsiEngine,
    "bollinger": BollingerEngine,
    "macd": MacdEngine,
    "trend": TrendEngine,
    "donchian": DonchianEngine,
}
