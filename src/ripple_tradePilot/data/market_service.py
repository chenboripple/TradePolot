"""C1/C2：指数日线落库 + 市场宽度聚合。

C1（``index_daily``）：指数历史此前从不落库，benchmark 每次回测都实时拉 Tushare，
离线即降级。本模块把四大指数日线落库，并提供 DB 优先 + 有界补拉的 ``load_index_bars``
供 app.py 基准对比、CLI ``--benchmark``、（D 阶段）市场特征复用。降级链：
tushare ``pro.index_daily`` → akshare ``index_zh_a_hist`` → akshare ``stock_zh_index_daily``
（新浪），复刻 ``stock_service.refresh_quotes`` 的 tier 模式，全部失败才 raise。

C2（``market_daily``）：无免费历史宽度 API，故走"增量积累制"——monitor 收盘例程 /
``tradepilot data refresh-market`` 用当日 ``stock_quotes`` 终态快照经 ``aggregate_breadth``
聚合写入。涨跌停家数用 A7 ``price_limit_for_symbol`` 分板块判定（替代旧的 ±9.8% 一刀切）。

全程离线可测：网络来源是模块级 ``ak.*`` 函数与 ``TushareDataLoader``，测试 patch 即可；
``aggregate_breadth`` / ``load_index_bars``（DB 命中时）为纯函数 / 纯 DB 读，零网络。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import akshare as ak
import pandas as pd

from ripple_tradePilot.backtest.rules import price_limit_for_symbol
from ripple_tradePilot.config_loader import get_tushare_token, load_config
from ripple_tradePilot.data.stock_service import StockDataUnavailableError
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.storage.database import (
    load_index_bars as _db_load_index_bars,
    record_market_daily,
    upsert_index_daily,
)

logger = logging.getLogger(__name__)


# C1：默认落库的指数（tushare 风格代码）。000300=基准，其余三大指数供市场特征/总览。
INDEX_CODES: Tuple[Tuple[str, str], ...] = (
    ("000300.SH", "沪深300"),
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
)

# C2：涨跌停判定的"绝对"容差（百分点）。用 change_pct 分类时，A 股价格四舍五入到
# 0.01 元会让低价股的真实涨停显示成 ~9.95%，故用绝对 0.2pp 容差（主板阈值 9.8、
# 创业板/科创板 19.8、北交所 29.8）：既修掉旧的 ±9.8% 一刀切，又精确保留主板原口径。
# 注意：与引擎撮合的"乘性"容差（engine._LIMIT_TOLERANCE=0.998，作用于 price）上下文
# 不同——那是价格比较，这是 change_pct 比较，刻意不强行统一（详见 STRATEGIES.md）。
_LIMIT_HIT_TOLERANCE_PP = 0.2

# 指数日线列名别名：tushare 英文 / akshare index_zh_a_hist 中文 / stock_zh_index_daily 英文。
# 集中在一处，上游列名变动只改这里。
_INDEX_COL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "trade_date": ("trade_date", "日期", "date"),
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价"),
    "pct_chg": ("pct_chg", "涨跌幅"),
    "amount": ("amount", "成交额"),
    "vol": ("vol", "volume", "成交量"),
}


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> Optional[float]:
    """安全转 float：None / 非数 / NaN 一律返回 None。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _normalize_trade_date(value: Any) -> str:
    """统一交易日为 ``YYYYMMDD`` 字符串；无法解析返回 ``''``。

    吃 tushare 的 ``YYYYMMDD``、akshare 的 ``YYYY-MM-DD``、``date``/``datetime``/
    ``pandas.Timestamp`` 等多种形态。
    """
    if value is None:
        return ""
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y%m%d")
    strftime = getattr(value, "strftime", None)
    if callable(strftime):  # datetime.date 等
        try:
            return strftime("%Y%m%d")
        except Exception:  # pragma: no cover - 防御性
            pass
    text = str(value).strip()
    if not text:
        return ""
    digits = text.replace("-", "").replace("/", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return digits[:8]
    return ""


def _pick_column(frame: pd.DataFrame, canonical: str) -> Optional[str]:
    for alias in _INDEX_COL_ALIASES[canonical]:
        if alias in frame.columns:
            return alias
    return None


def _normalize_index_frame(frame: Any) -> List[Dict[str, Any]]:
    """把任意来源的指数日线 DataFrame 归一化成规范行（升序）。

    规范行键：``trade_date``(YYYYMMDD)/``open``/``high``/``low``/``close``/``pct_chg``/
    ``amount``/``vol``。缺 trade_date 或 close 的帧返回 []（触发降级到下一来源）。
    """
    if frame is None or not isinstance(frame, pd.DataFrame) or len(frame) == 0:
        return []
    cols = {canonical: _pick_column(frame, canonical) for canonical in _INDEX_COL_ALIASES}
    if cols["trade_date"] is None or cols["close"] is None:
        return []
    rows: List[Dict[str, Any]] = []
    for _, raw in frame.iterrows():
        trade_date = _normalize_trade_date(raw.get(cols["trade_date"]))
        if not trade_date:
            continue
        close = _to_float(raw.get(cols["close"]))
        if close is None:
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "open": _to_float(raw.get(cols["open"])) if cols["open"] else None,
                "high": _to_float(raw.get(cols["high"])) if cols["high"] else None,
                "low": _to_float(raw.get(cols["low"])) if cols["low"] else None,
                "close": close,
                "pct_chg": _to_float(raw.get(cols["pct_chg"])) if cols["pct_chg"] else None,
                "amount": _to_float(raw.get(cols["amount"])) if cols["amount"] else None,
                "vol": _to_float(raw.get(cols["vol"])) if cols["vol"] else 0.0,
            }
        )
    rows.sort(key=lambda row: row["trade_date"])
    return rows


def _to_sina_code(index_code: str) -> str:
    """``000300.SH`` → ``sh000300``；``399001.SZ`` → ``sz399001``（新浪指数代码）。"""
    parts = index_code.split(".")
    digits = parts[0]
    suffix = parts[-1].lower() if len(parts) > 1 else ""
    prefix = "sz" if suffix == "sz" else "sh"
    return f"{prefix}{digits}"


def _filter_range(
    rows: List[Mapping[str, Any]],
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Mapping[str, Any]]:
    """按 ``YYYYMMDD`` 闭区间过滤（升序行，字典序比较即可）。"""
    out: Iterable[Mapping[str, Any]] = rows
    if start_date:
        out = (row for row in out if row["trade_date"] >= start_date)
    if end_date:
        out = (row for row in out if row["trade_date"] <= end_date)
    return list(out)


# ---------------------------------------------------------------------------
# C1：指数日线落库
# ---------------------------------------------------------------------------
class MarketDataService:
    """指数日线刷新（tushare → akshare 东财 → akshare 新浪 三级降级）。"""

    def __init__(self, config: Optional[Mapping[str, Any]] = None, path: Optional[Path] = None):
        self.config: Mapping[str, Any] = config if config is not None else load_config()
        self.path = path

    # --- 三个来源，各自返回归一化行（空列表 = 该来源不可用，降级到下一个）---
    def _fetch_tushare(self, index_code: str, start: str, end: str) -> List[Dict[str, Any]]:
        try:
            token = get_tushare_token(self.config)
        except ValueError:
            return []  # 无 token：诚实跳过（非错误，不计入 errors），降级到 akshare
        loader = TushareDataLoader(
            token,
            rate_limit_delay=float(
                self.config.get("tushare", {}).get("rate_limit_delay", 1.5)
            ),
        )
        return _normalize_index_frame(loader.get_index_bars(index_code, start, end))

    def _fetch_akshare_hist(self, index_code: str, start: str, end: str) -> List[Dict[str, Any]]:
        digits = index_code.split(".")[0]
        frame = ak.index_zh_a_hist(
            symbol=digits, period="daily", start_date=start, end_date=end
        )
        return _normalize_index_frame(frame)

    def _fetch_akshare_daily(self, index_code: str, start: str, end: str) -> List[Dict[str, Any]]:
        # 新浪 stock_zh_index_daily 返回全历史、无日期参数，本地按区间裁剪
        frame = ak.stock_zh_index_daily(symbol=_to_sina_code(index_code))
        return _filter_range(_normalize_index_frame(frame), start, end)

    def refresh_index_daily(self, index_code: str, days: int = 750) -> Dict[str, Any]:
        """刷新单个指数近 ``days`` 天日线并落库。

        Returns:
            ``{"index_code", "source", "rows", "start", "end", "errors"}``；``errors``
            记录降级途中各失败来源（部分成功可溯源）。

        Raises:
            StockDataUnavailableError: 三级来源全部失败/为空。
        """
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        start = start_dt.strftime("%Y%m%d")
        end = end_dt.strftime("%Y%m%d")
        tiers = (
            ("tushare", self._fetch_tushare),
            ("akshare", self._fetch_akshare_hist),
            ("sina", self._fetch_akshare_daily),
        )
        errors: List[Dict[str, str]] = []
        for source, fetch in tiers:
            try:
                rows = fetch(index_code, start, end)
            except Exception as error:  # 网络/接口/列名变动一律降级
                logger.warning("指数日线获取失败（%s/%s）：%s", index_code, source, error)
                errors.append({"source": source, "error": str(error)})
                rows = []
            if rows:
                count = upsert_index_daily(index_code, rows, source, self.path)
                return {
                    "index_code": index_code,
                    "source": source,
                    "rows": count,
                    "start": rows[0]["trade_date"],
                    "end": rows[-1]["trade_date"],
                    "errors": errors,
                }
        attempted = "、".join(name for name, _ in tiers)
        raise StockDataUnavailableError(
            f"指数 {index_code} 日线全部来源失败（尝试：{attempted}）"
        )

    def refresh_indexes(
        self, index_codes: Optional[Iterable[str]] = None, days: int = 750
    ) -> Dict[str, Any]:
        """批量刷新多个指数：部分成功 + 显式报告（单个失败不阻断其余）。

        monitor 收盘例程 / ``data refresh --market`` 复用。返回
        ``{"refreshed": [...], "failed": [{"index_code", "error"}, ...]}``。
        """
        codes = list(index_codes) if index_codes else [code for code, _ in INDEX_CODES]
        refreshed: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        for code in codes:
            try:
                refreshed.append(self.refresh_index_daily(code, days=days))
            except StockDataUnavailableError as error:
                logger.warning("指数 %s 刷新失败：%s", code, error)
                failed.append({"index_code": code, "error": str(error)})
        return {"refreshed": refreshed, "failed": failed}


def load_index_bars(
    index_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    *,
    days: int = 750,
    fetch: bool = True,
    config: Optional[Mapping[str, Any]] = None,
    path: Optional[Path] = None,
) -> List[Mapping[str, Any]]:
    """DB 优先读取指数日线；DB 缺该区间且 ``fetch=True`` 时有界补拉一次。

    离线安全：补拉失败/无来源 → 诚实返回 DB 现有（可能为空），**绝不抛错**，
    保证 benchmark 等消费方优雅降级。``start_date``/``end_date`` 为 ``YYYYMMDD``。

    返回 ``list[dict]``（升序，键见 ``database.load_index_bars``：trade_date/open/
    high/low/close/pct_chg/amount/vol/source）。
    """
    rows = _filter_range(_db_load_index_bars(index_code, path), start_date, end_date)
    if rows or not fetch:
        return rows
    try:
        MarketDataService(config=config, path=path).refresh_index_daily(index_code, days=days)
    except Exception as error:  # StockDataUnavailableError 及其它一律降级
        logger.warning("指数 %s 补拉失败，降级为 DB 现有数据：%s", index_code, error)
        return []
    return _filter_range(_db_load_index_bars(index_code, path), start_date, end_date)


# ---------------------------------------------------------------------------
# C2：市场宽度聚合
# ---------------------------------------------------------------------------
def aggregate_breadth(quote_rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """从 ``stock_quotes`` 终态快照聚合市场宽度（纯函数，无网络/无 DB）。

    涨跌停分类用 A7 ``price_limit_for_symbol`` 分板块判定（主板 10%、创业板/科创板
    20%、北交所 30%），带 ``_LIMIT_HIT_TOLERANCE_PP`` 绝对容差，替代旧的 ±9.8% 一刀切。

    Args:
        quote_rows: ``load_stock_quotes()`` 风格的行，需含 ``symbol``/``change_pct``，
            可选 ``amount``。``change_pct`` 为百分比数值（如 9.98 表示 +9.98%）。

    Returns:
        规范键 ``advancers``/``decliners``/``unchanged``/``limit_up``/``limit_down``/
        ``total``/``total_amount``/``up_ratio``，可直接喂 ``database.record_market_daily``。
    """
    advancers = decliners = unchanged = limit_up = limit_down = 0
    total = 0
    total_amount = 0.0
    for row in quote_rows:
        total += 1
        change_pct = row.get("change_pct")
        if change_pct is None:
            unchanged += 1
        elif change_pct > 0:
            advancers += 1
        elif change_pct < 0:
            decliners += 1
        else:
            unchanged += 1
        if change_pct is not None:
            limit = price_limit_for_symbol(row.get("symbol", "")) * 100.0
            threshold = limit - _LIMIT_HIT_TOLERANCE_PP
            if change_pct >= threshold:
                limit_up += 1
            elif change_pct <= -threshold:
                limit_down += 1
        amount = _to_float(row.get("amount"))
        if amount is not None:
            total_amount += amount
    up_ratio = round(advancers / total, 4) if total else 0.0
    return {
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "total": total,
        "total_amount": total_amount,
        "up_ratio": up_ratio,
    }


def record_market_breadth(
    trade_date: str,
    quote_rows: Iterable[Mapping[str, Any]],
    source: str = "snapshot",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """聚合 + 落库某交易日市场宽度（C2 写入入口）。

    monitor 收盘例程（C5）与 ``tradepilot data refresh-market``（C4）复用：传入当日
    ``stock_quotes`` 终态快照即可。``trade_date`` 为 ``YYYYMMDD``。按 trade_date upsert
    幂等（盘中 provisional → 收盘 final 覆盖为终态）。返回 ``aggregate_breadth`` 结果。
    """
    breadth = aggregate_breadth(quote_rows)
    record_market_daily(trade_date, breadth, source, path)
    return breadth
