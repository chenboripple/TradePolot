"""A3 可选价格预警（与信号链路解耦，默认关）。

监控统一切日线后，分钟线不再进信号投票；但"日内急拉/急跌、逼近涨跌停、穿越参考价"这类
**规则化、非投票**的即时提醒仍有价值，遂降级为独立的价格预警通道：

- 与信号通道**分开去重**：信号去重键 ``(symbol, side, trade_date, provisional)``，价格预警去重键
  ``(symbol, kind, trade_date)``，互不串扰（一方发过不影响另一方）；
- 消息前缀 ``⚡价格预警（非交易信号）``，明确不是投票信号、不含买卖建议；
- 默认关（``monitor.price_alert.enabled=false``），开启不改变任何信号/通知行为；
- 涨跌停距离用 A7 ``price_limit_for_symbol`` 分板块推导（创业板/科创板 20%、北交所 30%、主板
  10%），取代旧 ±9.8% 一刀切。

纯函数 ``evaluate_price_alerts`` 只吃 ``stock_quotes`` 快照行，不触网、不触库、不碰 ``signals``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..backtest.rules import price_limit_for_symbol

__all__ = [
    "ALERT_PCT_MOVE",
    "ALERT_NEAR_LIMIT_UP",
    "ALERT_NEAR_LIMIT_DOWN",
    "ALERT_CROSS_REF",
    "PriceAlert",
    "PriceAlertConfig",
    "parse_price_alert_config",
    "evaluate_price_alerts",
]

ALERT_PCT_MOVE = "pct_move"
ALERT_NEAR_LIMIT_UP = "near_limit_up"
ALERT_NEAR_LIMIT_DOWN = "near_limit_down"
ALERT_CROSS_REF = "cross_ref"

_PREFIX = "⚡价格预警（非交易信号）"


@dataclass(frozen=True)
class PriceAlertConfig:
    """价格预警规则参数（默认关；阈值均为比例，如 0.05=5%）。"""

    enabled: bool = False
    pct_threshold: float = 0.05       # |日内涨跌幅| ≥ 此值 → pct_move
    near_limit_pct: float = 0.02      # 距涨/跌停 ≤ 此值 → near_limit_*
    reference_prices: Dict[str, float] = field(default_factory=dict)  # symbol → 参考价（上穿触发）


@dataclass(frozen=True)
class PriceAlert:
    symbol: str
    kind: str
    price: float
    pct_chg: float
    message: str


def parse_price_alert_config(monitor_cfg: Optional[Mapping[str, Any]]) -> PriceAlertConfig:
    """从 ``monitor.price_alert`` 节解析配置；缺失/非法值回退默认（关）。"""
    section = (monitor_cfg or {}).get("price_alert", {}) or {}
    refs_raw = section.get("reference_prices", {}) or {}
    refs: Dict[str, float] = {}
    if isinstance(refs_raw, Mapping):
        for key, value in refs_raw.items():
            try:
                refs[str(key).upper()] = float(value)
            except (TypeError, ValueError):
                continue
    return PriceAlertConfig(
        enabled=bool(section.get("enabled", False)),
        pct_threshold=float(section.get("pct_threshold", 0.05)),
        near_limit_pct=float(section.get("near_limit_pct", 0.02)),
        reference_prices=refs,
    )


def _to_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _pct_chg(price: float, pre_close: Optional[float], change_pct: Optional[float]) -> Optional[float]:
    """日内涨跌幅（小数）。优先用 price/pre_close−1（原始锚更可靠），回退快照 change_pct/100。"""
    if pre_close and pre_close > 0:
        return price / pre_close - 1.0
    if change_pct is not None:
        return change_pct / 100.0
    return None


def evaluate_price_alerts(
    quote_rows: Sequence[Mapping[str, Any]],
    *,
    config: PriceAlertConfig,
    names: Optional[Mapping[str, str]] = None,
) -> List[PriceAlert]:
    """对一批 ``stock_quotes`` 快照行应用规则，返回触发的价格预警（纯函数）。

    ``config.enabled=False`` → 直接返回空（默认关，零行为变化）。每条快照可触发多类预警
    （如既急拉又逼近涨停）；去重由调用方按 ``(symbol, kind, trade_date)`` 处理。
    """
    if not config.enabled:
        return []
    names = names or {}
    alerts: List[PriceAlert] = []
    for row in quote_rows:
        symbol = str(row.get("symbol", "")).upper()
        price = _to_float(row.get("price"))
        if not symbol or not price or price <= 0:
            continue
        pre_close = _to_float(row.get("pre_close"))
        pct = _pct_chg(price, pre_close, _to_float(row.get("change_pct")))
        name = names.get(symbol, symbol)
        pct_text = f"{pct:+.2%}" if pct is not None else "—"

        # 1) 日内涨跌幅超阈值
        if pct is not None and abs(pct) >= config.pct_threshold:
            direction = "急拉" if pct > 0 else "急跌"
            alerts.append(PriceAlert(
                symbol=symbol, kind=ALERT_PCT_MOVE, price=price, pct_chg=pct,
                message=f"{_PREFIX}：{name}({symbol}) 日内{direction} {pct_text}，现价 {price:.2f}",
            ))

        # 2) 逼近涨/跌停（分板块 limit）
        if pre_close and pre_close > 0:
            limit = price_limit_for_symbol(symbol)
            limit_up = pre_close * (1 + limit)
            limit_down = pre_close * (1 - limit)
            band = config.near_limit_pct
            if price >= limit_up * (1 - band):
                alerts.append(PriceAlert(
                    symbol=symbol, kind=ALERT_NEAR_LIMIT_UP, price=price, pct_chg=pct or 0.0,
                    message=(f"{_PREFIX}：{name}({symbol}) 逼近涨停（{limit:.0%}，"
                             f"涨停价≈{limit_up:.2f}），现价 {price:.2f} {pct_text}"),
                ))
            elif price <= limit_down * (1 + band):
                alerts.append(PriceAlert(
                    symbol=symbol, kind=ALERT_NEAR_LIMIT_DOWN, price=price, pct_chg=pct or 0.0,
                    message=(f"{_PREFIX}：{name}({symbol}) 逼近跌停（-{limit:.0%}，"
                             f"跌停价≈{limit_down:.2f}），现价 {price:.2f} {pct_text}"),
                ))

        # 3) 上穿参考价（每日每标的去重后只提醒一次）
        ref = config.reference_prices.get(symbol)
        if ref and ref > 0 and price >= ref:
            alerts.append(PriceAlert(
                symbol=symbol, kind=ALERT_CROSS_REF, price=price, pct_chg=pct or 0.0,
                message=f"{_PREFIX}：{name}({symbol}) 上穿参考价 {ref:.2f}，现价 {price:.2f} {pct_text}",
            ))

    return alerts
