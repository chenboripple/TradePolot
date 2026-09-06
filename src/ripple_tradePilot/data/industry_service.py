"""C3：东财行业板块基建（akshare 为唯一主源）。

探查确认 tushare 120 积分档无免费行业指数历史，故行业数据以东财（akshare）为唯一主源：
``stock_board_industry_name_em``（板块登记）/ ``stock_board_industry_hist_em``（板块日线，
注意入参是板块**名称**而非代码）/ ``stock_board_industry_cons_em``（成分股，返回 6 位裸代码）。

设计要点（与 ``market_service`` 一致的离线可测原则）：
- **部分成功 + 显式 RefreshReport**：逐板块独立 try/except，单个板块失败不阻断其余；失败
  板块的行业特征在 D 阶段自动 NaN 降级（绝不造假）。
- **只为股票池所属板块拉数据**：``refresh_for_symbols`` 先解析 symbol→板块（DB 成分快照优先，
  兜底用 ``stock_catalog.industry`` 静态标签按名称模糊匹配），板块去重后再逐板块拉，限流 sleep
  可配（东财易触发频控）。
- **列名映射集中模块顶部常量表**：东财接口列名易变，变化只改这里一处。

全程离线可测：网络来源是模块级 ``ak.*`` 函数，测试 patch 即可；归一化与 DB 读为纯函数/纯 DB。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import akshare as ak
import pandas as pd

from ripple_tradePilot.config_loader import load_config
# 复用 market_service 的归一化纯函数（同一 data/ 包内、稳定无副作用，避免重复实现）
from ripple_tradePilot.data.market_service import _normalize_trade_date, _to_float
from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
    StockDataUnavailableError,
)
from ripple_tradePilot.storage.database import (
    industry_board_for_symbol,
    load_industry_boards,
    stock_catalog_industries,
    upsert_industry_board_bars,
    upsert_industry_boards,
    upsert_industry_membership,
)

logger = logging.getLogger(__name__)


# 东财列名映射集中一处（接口易变，变化只改这里）。每个 canonical 键对应一组候选列名，
# 取首个命中的列；全部缺失则该字段为 None（板块日线缺 trade_date/close 时整帧判无效）。
_BOARD_LIST_ALIASES: Dict[str, Tuple[str, ...]] = {
    "board_code": ("板块代码",),
    "board_name": ("板块名称",),
}
_BOARD_HIST_ALIASES: Dict[str, Tuple[str, ...]] = {
    "trade_date": ("日期",),
    "open": ("开盘",),
    "high": ("最高",),
    "low": ("最低",),
    "close": ("收盘",),
    "pct_chg": ("涨跌幅",),
    "amount": ("成交额",),
    "turnover_rate": ("换手率",),
}
_BOARD_CONS_ALIASES: Dict[str, Tuple[str, ...]] = {
    "code": ("代码",),
}


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------
def _pick(frame: pd.DataFrame, aliases: Tuple[str, ...]) -> Optional[str]:
    for alias in aliases:
        if alias in frame.columns:
            return alias
    return None


def _is_empty(frame: Any) -> bool:
    return frame is None or not isinstance(frame, pd.DataFrame) or len(frame) == 0


def _normalize_board_list(frame: Any) -> List[Dict[str, str]]:
    """东财板块列表 → ``[{"board_code", "board_name"}]``（丢弃缺代码/名称的行）。"""
    if _is_empty(frame):
        return []
    code_col = _pick(frame, _BOARD_LIST_ALIASES["board_code"])
    name_col = _pick(frame, _BOARD_LIST_ALIASES["board_name"])
    if not code_col or not name_col:
        return []
    records: List[Dict[str, str]] = []
    for _, raw in frame.iterrows():
        code = str(raw.get(code_col) or "").strip()
        name = str(raw.get(name_col) or "").strip()
        if code and name:
            records.append({"board_code": code, "board_name": name})
    return records


def _normalize_board_hist(frame: Any) -> List[Dict[str, Any]]:
    """东财板块日线 → 规范行（升序）。缺 trade_date 或 close 的帧返回 []（判该板块失败）。

    规范行键：trade_date(YYYYMMDD)/open/high/low/close/pct_chg/amount/turnover_rate。
    """
    if _is_empty(frame):
        return []
    cols = {canonical: _pick(frame, aliases) for canonical, aliases in _BOARD_HIST_ALIASES.items()}
    if not cols["trade_date"] or not cols["close"]:
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
                "turnover_rate": (
                    _to_float(raw.get(cols["turnover_rate"])) if cols["turnover_rate"] else None
                ),
            }
        )
    rows.sort(key=lambda row: row["trade_date"])
    return rows


def _normalize_board_cons(frame: Any) -> List[str]:
    """东财板块成分股 → 规范 symbol 列表（``600000`` → ``600000.SH``；malformed 跳过）。"""
    if _is_empty(frame):
        return []
    code_col = _pick(frame, _BOARD_CONS_ALIASES["code"])
    if not code_col:
        return []
    symbols: List[str] = []
    for _, raw in frame.iterrows():
        code = str(raw.get(code_col) or "").strip()
        if not code:
            continue
        try:
            symbols.append(StockDataService.normalize_symbol(code))
        except InvalidStockSymbolError:
            logger.debug("跳过无法规范化的成分股代码：%s", code)
    return symbols


def _match_board_by_industry(
    industry: str, name_to_code: Mapping[str, str]
) -> Optional[str]:
    """兜底：用 ``stock_catalog.industry`` 静态标签按板块名模糊匹配（精确优先，再子串）。

    匹配不上返回 None（该股票行业特征缺失，绝不造假）。
    """
    if not industry:
        return None
    if industry in name_to_code:
        return name_to_code[industry]
    for name, code in name_to_code.items():
        if industry in name or name in industry:
            return code
    return None


# ---------------------------------------------------------------------------
# C3：行业板块刷新服务
# ---------------------------------------------------------------------------
class IndustryDataService:
    """东财行业板块刷新：板块登记 / 板块日线 / 成分股快照，部分成功 + 显式报告。"""

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        path: Optional[Path] = None,
        rate_limit_delay: Optional[float] = None,
    ):
        self.config: Mapping[str, Any] = config if config is not None else load_config()
        self.path = path
        data_cfg = self.config.get("data", {}) if isinstance(self.config, Mapping) else {}
        self.rate_limit_delay = float(
            rate_limit_delay
            if rate_limit_delay is not None
            else data_cfg.get("industry_rate_limit_delay", 0.5)
        )

    # --- 板块登记表（一次网络调用）---
    def refresh_boards(self) -> Dict[str, Any]:
        """拉东财行业板块列表 → ``industry_boards``。

        Returns:
            ``{"count", "boards": [{"board_code", "board_name"}, ...]}``

        Raises:
            StockDataUnavailableError: 接口失败或返回空列表。
        """
        try:
            frame = ak.stock_board_industry_name_em()
        except Exception as error:
            raise StockDataUnavailableError(f"东财行业板块列表请求失败：{error}") from error
        records = _normalize_board_list(frame)
        if not records:
            raise StockDataUnavailableError("东财行业板块列表为空或列名不匹配")
        count = upsert_industry_boards(records, "em", self.path)
        return {"count": count, "boards": records}

    # --- 单板块日线 ---
    def refresh_board_bars(self, board_name: str, board_code: str, days: int = 750) -> Dict[str, Any]:
        """拉某板块近 ``days`` 天日线 → ``industry_board_bars``（入参用板块**名称**）。

        Raises:
            StockDataUnavailableError: 接口失败或返回空。
        """
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        try:
            frame = ak.stock_board_industry_hist_em(
                symbol=board_name,
                period="日k",
                adjust="",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=end_dt.strftime("%Y%m%d"),
            )
        except Exception as error:
            raise StockDataUnavailableError(f"板块 {board_name} 日线请求失败：{error}") from error
        rows = _normalize_board_hist(frame)
        if not rows:
            raise StockDataUnavailableError(f"板块 {board_name} 日线为空或列名不匹配")
        count = upsert_industry_board_bars(board_code, rows, "em", self.path)
        return {"board_code": board_code, "board_name": board_name, "rows": count}

    # --- 单板块成分股快照 ---
    def refresh_membership(
        self, board_name: str, board_code: str, as_of: Optional[str] = None
    ) -> Dict[str, Any]:
        """拉某板块成分股 → ``industry_membership`` 最新快照（入参用板块**名称**）。

        Raises:
            StockDataUnavailableError: 接口失败或返回空。
        """
        try:
            frame = ak.stock_board_industry_cons_em(symbol=board_name)
        except Exception as error:
            raise StockDataUnavailableError(f"板块 {board_name} 成分股请求失败：{error}") from error
        symbols = _normalize_board_cons(frame)
        if not symbols:
            raise StockDataUnavailableError(f"板块 {board_name} 成分股为空或列名不匹配")
        observation = as_of or datetime.now().strftime("%Y%m%d")
        count = upsert_industry_membership(board_code, symbols, observation, "em", self.path)
        return {"board_code": board_code, "board_name": board_name, "symbols": count}

    # --- 编排：只为股票池所属板块拉数据 ---
    def refresh_for_symbols(
        self, symbols: Iterable[str], days: int = 750
    ) -> Dict[str, Any]:
        """解析股票池所属板块 → 板块去重 → 逐板块拉成分股 + 日线（部分成功 + 报告）。

        symbol→板块解析：DB 成分快照（``industry_board_for_symbol``）优先，兜底用
        ``stock_catalog.industry`` 静态标签按名称模糊匹配。两者都解析不出的 symbol 记入
        ``symbols_unresolved``（其行业特征在 D 阶段缺失，绝不造假）。

        Returns:
            RefreshReport（dict）::

                {
                  "boards_total": int,
                  "boards_refreshed": [board_code, ...],
                  "boards_failed": [{"board_code", "board_name", "error"}, ...],
                  "symbols_total": int, "symbols_resolved": int,
                  "symbols_unresolved": [symbol, ...],
                  "membership_rows": int, "bar_rows": int,
                }
        """
        normalized = self._normalize_symbols(symbols)
        report: Dict[str, Any] = {
            "boards_total": 0,
            "boards_refreshed": [],
            "boards_failed": [],
            "symbols_total": len(normalized),
            "symbols_resolved": 0,
            "symbols_unresolved": [],
            "membership_rows": 0,
            "bar_rows": 0,
        }
        if not normalized:
            return report

        # 1) 板块登记表（一次网络）；失败不致命——仍可用 DB 既有 boards 解析
        try:
            self.refresh_boards()
        except StockDataUnavailableError as error:
            logger.warning("行业板块登记表刷新失败（用 DB 既有登记解析）：%s", error)
        boards = load_industry_boards(self.path)
        name_to_code = {b["board_name"]: b["board_code"] for b in boards}
        code_to_name = {b["board_code"]: b["board_name"] for b in boards}
        catalog_industries = stock_catalog_industries(self.path)

        # 2) 解析每个 symbol 的板块（membership 先行 → 板块去重）
        target_codes: set = set()
        resolved = 0
        for symbol in normalized:
            code = industry_board_for_symbol(symbol, self.path)
            if not code:
                code = _match_board_by_industry(catalog_industries.get(symbol, ""), name_to_code)
            if code:
                target_codes.add(code)
                resolved += 1
            else:
                report["symbols_unresolved"].append(symbol)
        report["symbols_resolved"] = resolved
        report["boards_total"] = len(target_codes)

        # 3) 逐板块：成分股 + 日线（独立 try/except，限流 sleep）
        for code in sorted(target_codes):
            name = code_to_name.get(code, "")
            if not name:
                report["boards_failed"].append(
                    {"board_code": code, "board_name": "", "error": "板块名缺失，无法调用东财接口"}
                )
                continue
            ok = False
            errors: List[str] = []
            try:
                membership = self.refresh_membership(name, code)
                report["membership_rows"] += membership["symbols"]
                ok = True
            except StockDataUnavailableError as error:
                errors.append(f"成分股：{error}")
                logger.warning("板块 %s 成分股刷新失败：%s", name, error)
            try:
                bars = self.refresh_board_bars(name, code, days=days)
                report["bar_rows"] += bars["rows"]
                ok = True
            except StockDataUnavailableError as error:
                errors.append(f"日线：{error}")
                logger.warning("板块 %s 日线刷新失败：%s", name, error)
            if ok:
                report["boards_refreshed"].append(code)
            else:
                report["boards_failed"].append(
                    {"board_code": code, "board_name": name, "error": "；".join(errors)}
                )
            if self.rate_limit_delay:
                time.sleep(self.rate_limit_delay)
        return report

    @staticmethod
    def _normalize_symbols(symbols: Iterable[str]) -> List[str]:
        """规范化 + 去重 + 跳过 malformed，保序。"""
        seen: set = set()
        out: List[str] = []
        for raw in symbols or []:
            if not raw:
                continue
            try:
                symbol = StockDataService.normalize_symbol(str(raw))
            except InvalidStockSymbolError:
                logger.debug("跳过无法规范化的股票代码：%s", raw)
                continue
            if symbol not in seen:
                seen.add(symbol)
                out.append(symbol)
        return out
