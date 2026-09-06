"""市场交易规则（分板块涨跌停 / T+1 / 成交量参与率 / 最小成交单位）。

旧引擎把涨跌幅限制写死成主板 ±10%（``PRICE_LIMIT_PCT``），对创业板/科创板（±20%）
与北交所（±30%）一刀切，且 ``close`` 撮合模式完全不拦截涨跌停、无显式 T+1、无成交量
约束。本模块把这些规则抽成可注入的 ``MarketRules``，由调用方（CLI/Web）按标的推导
``price_limit_pct`` 后传入引擎；默认值在日线 next_open 场景下与旧行为一致，唯有
``close`` 模式补上涨跌停拦截（修复历史缺口）。

局限（显式声明，不静默造假）：
- ``price_limit_for_symbol`` **不识别 ST/*ST**（应为 ±5%）与新股上市首日等特殊安排；
  如需精确，调用方显式构造 ``MarketRules(price_limit_pct=...)``。
- T+1 按 bar 的**日历日**比较；日线下一根 bar 即一日，天然满足，约束只对分钟线
  （同一交易日多根 bar）实际生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_LOT_SIZE = 100

# 板块涨跌幅限制
LIMIT_MAIN_BOARD = 0.10    # 沪深主板
LIMIT_GEM_STAR = 0.20      # 创业板(300/301)、科创板(688/689)
LIMIT_BSE = 0.30           # 北交所(.BJ)

# 创业板/科创板代码前缀
_GEM_STAR_PREFIXES = ("300", "301", "688", "689")


@dataclass(frozen=True)
class MarketRules:
    """单标的回测适用的市场微观结构规则。

    Attributes:
        price_limit_pct: 涨跌幅限制（0.10=±10%）。涨跌停判定带 0.2% 容差，
            容忍复权/四舍五入造成的微小偏差。
        enforce_limit_on_close: ``close`` 撮合模式是否也拦截涨跌停（默认 True，
            修复旧引擎 close 模式不拦截的缺口）。``next_open`` 模式恒拦截。
        t_plus_1: 是否强制 T+1（当日买入禁止当日卖出，默认 True）。按 bar 日历日
            比较，日线天然满足，分钟线实际生效。
        lot_size: 最小成交单位（股），A 股默认 100。
        max_volume_participation: 单根 bar 最大成交量参与率（如 0.1=不超过该 bar
            成交量的 10%）。``None``（默认）= 不限，与旧行为一致；超出部分截断并
            记入 ``skipped_fills``。
    """

    price_limit_pct: float = LIMIT_MAIN_BOARD
    enforce_limit_on_close: bool = True
    t_plus_1: bool = True
    lot_size: int = DEFAULT_LOT_SIZE
    max_volume_participation: Optional[float] = None


def price_limit_for_symbol(symbol: str) -> float:
    """按证券代码推导涨跌幅限制比例。

    规则（优先级从上到下）：
    - ``.BJ`` 后缀（北交所）→ 30%；
    - 代码前缀 300/301（创业板）、688/689（科创板）→ 20%；
    - 其余（沪深主板）→ 10%。

    不识别 ST/*ST（±5%）与新股首日等特殊安排——见模块 docstring 局限声明。

    Args:
        symbol: 证券代码，可带交易所后缀（如 ``300750.SZ``、``600519.SH``、
            ``920001.BJ``）。

    Returns:
        涨跌幅限制比例（0.10 / 0.20 / 0.30）。
    """
    if not symbol:
        return LIMIT_MAIN_BOARD
    parts = symbol.split(".")
    code = parts[0]
    suffix = parts[-1].upper() if len(parts) > 1 else ""
    if suffix == "BJ":
        return LIMIT_BSE
    if code[:3] in _GEM_STAR_PREFIXES:
        return LIMIT_GEM_STAR
    return LIMIT_MAIN_BOARD
