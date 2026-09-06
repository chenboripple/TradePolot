from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import akshare as ak
import httpx
import pandas as pd

from ripple_tradePilot.config_loader import load_config
from ripple_tradePilot.data.adjustment import detect_rebase
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.storage.database import (
    database_path,
    load_daily_bars,
    stock_catalog_name,
    upsert_daily_bars,
    upsert_stock_catalog,
    upsert_stock_quotes,
)


_DATA_LOCK = threading.Lock()
_CATALOG_LOCK = threading.Lock()
_QUOTE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

# 市场总览固定展示的三支指数
MARKET_INDEXES: Tuple[Dict[str, str], ...] = (
    {"name": "上证指数", "code": "sh000001"},
    {"name": "深证成指", "code": "sz399001"},
    {"name": "创业板指", "code": "sz399006"},
)

# 指数行情模块级内存缓存：{来源: (monotonic 时间, 行情列表)}，60 秒有效，
# 缓存键含来源，避免频繁请求把上游刷爆。
_INDEX_QUOTE_CACHE: Dict[str, Tuple[float, list]] = {}
_INDEX_QUOTE_TTL = 60.0


def _cached_index_quotes() -> Optional[Tuple[list, str]]:
    """返回 60 秒内的指数行情缓存 (items, source)，无可用缓存返回 None。"""
    now = time.monotonic()
    for source, (cached_at, items) in list(_INDEX_QUOTE_CACHE.items()):
        if now - cached_at < _INDEX_QUOTE_TTL:
            return items, source
        _INDEX_QUOTE_CACHE.pop(source, None)
    return None


class StockDataError(RuntimeError):
    pass


class InvalidStockSymbolError(StockDataError):
    pass


class StockDataUnavailableError(StockDataError):
    pass


class StockDataService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
        database: Optional[Path] = None,
    ):
        self.data_dir = data_dir or Path(
            os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data")
        )
        self.config_path = config_path or Path(
            os.getenv("TRADEPILOT_CONFIG", Path.cwd() / "config.yaml")
        )
        self.database = database or database_path()

    @staticmethod
    def normalize_symbol(value: str) -> str:
        raw = value.strip().upper()
        match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", raw)
        if not match:
            raise InvalidStockSymbolError("请输入 6 位股票代码，如 600309 或 000001.SZ")
        code, exchange = match.groups()
        if exchange is None:
            if code.startswith(("4", "8", "92")):
                exchange = "BJ"
            elif code.startswith(("5", "6", "9")):
                exchange = "SH"
            else:
                exchange = "SZ"
        return f"{code}.{exchange}"

    def _configured_name(self, symbol: str) -> Optional[str]:
        config = load_config(str(self.config_path))
        for item in config.get("symbols", []):
            if str(item.get("code", "")).upper() == symbol:
                return item.get("name")
        return None

    @staticmethod
    def _valid_name(value: Any, symbol: str) -> Optional[str]:
        name = str(value or "").strip()
        if not name or name.lower() in {"nan", symbol.lower(), symbol.split(".", 1)[0]}:
            return None
        return name

    @staticmethod
    def _catalog_records(frame: pd.DataFrame) -> list[Dict[str, str]]:
        if not len(frame) or not {"ts_code", "name"}.issubset(frame.columns):
            return []
        records = []
        for _, row in frame.iterrows():
            symbol = str(row.get("ts_code") or "").upper().strip()
            name = StockDataService._valid_name(row.get("name"), symbol)
            if not symbol or not name:
                continue
            derived = StockDataService._market_metadata(symbol)
            board = str(row.get("market") or derived["board"]).strip()
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": board,
                    "exchange": str(
                        row.get("exchange") or derived["exchange"]
                    ).strip(),
                    "board": board,
                    "industry": str(row.get("industry") or "").strip(),
                    "area": str(row.get("area") or "").strip(),
                    "list_status": str(row.get("list_status") or "L").strip(),
                    "list_date": str(row.get("list_date") or "").strip(),
                }
            )
        return records

    @staticmethod
    def _market_metadata(symbol: str) -> Dict[str, str]:
        code, suffix = symbol.upper().split(".", 1)
        if suffix == "BJ":
            return {"exchange": "BSE", "board": "北交所"}
        if suffix == "SH":
            board = "科创板" if code.startswith(("688", "689")) else "主板"
            return {"exchange": "SSE", "board": board}
        board = "创业板" if code.startswith(("300", "301")) else "主板"
        return {"exchange": "SZSE", "board": board}

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(str(value).replace(",", "").replace("%", ""))
        except (TypeError, ValueError):
            return None
        return None if pd.isna(number) else number

    @staticmethod
    def _realtime_records(frame: pd.DataFrame) -> list[Dict[str, Any]]:
        if frame is None or len(frame) == 0 or "代码" not in frame.columns:
            return []
        quote_time = datetime.now().isoformat(timespec="seconds")
        records = []
        for _, row in frame.iterrows():
            code = str(row.get("代码") or "").strip().zfill(6)
            if not re.fullmatch(r"\d{6}", code):
                continue
            try:
                symbol = StockDataService.normalize_symbol(code)
            except InvalidStockSymbolError:
                continue
            price = StockDataService._optional_float(row.get("最新价"))
            if price is None:
                continue
            volume = StockDataService._optional_float(row.get("成交量"))
            records.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "pre_close": StockDataService._optional_float(row.get("昨收")),
                    "change": StockDataService._optional_float(row.get("涨跌额")),
                    "change_pct": StockDataService._optional_float(row.get("涨跌幅")),
                    "open": StockDataService._optional_float(row.get("今开")),
                    "high": StockDataService._optional_float(row.get("最高")),
                    "low": StockDataService._optional_float(row.get("最低")),
                    "volume": volume * 100 if volume is not None else 0,
                    "amount": StockDataService._optional_float(row.get("成交额")),
                    "turnover_rate": StockDataService._optional_float(
                        row.get("换手率")
                    ),
                    "quote_time": quote_time,
                }
            )
        return records

    @staticmethod
    def _eastmoney_catalog_records() -> list[Dict[str, str]]:
        params = {
            "pn": 1,
            "pz": 10000,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14",
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        endpoints = (
            "https://82.push2.eastmoney.com/api/qt/clist/get",
            "https://push2.eastmoney.com/api/qt/clist/get",
            "https://88.push2.eastmoney.com/api/qt/clist/get",
        )
        last_error: Optional[Exception] = None
        rows = []
        for endpoint in endpoints:
            try:
                response = httpx.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=30,
                    follow_redirects=True,
                )
                response.raise_for_status()
                rows = (response.json().get("data") or {}).get("diff") or []
                if rows:
                    break
                last_error = StockDataUnavailableError("东方财富返回了空股票清单")
            except Exception as error:
                last_error = error
        if not rows:
            raise StockDataUnavailableError("东方财富股票清单请求失败") from last_error
        records = []
        for row in rows:
            code = str(row.get("f12") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            symbol = StockDataService.normalize_symbol(code)
            name = StockDataService._valid_name(row.get("f14"), symbol)
            if not name:
                continue
            metadata = StockDataService._market_metadata(symbol)
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": metadata["board"],
                    **metadata,
                    "industry": "",
                    "area": "",
                    "list_status": "L",
                    "list_date": "",
                }
            )
        return records

    @staticmethod
    def _sina_market_rows() -> list[Dict[str, Any]]:
        count_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeStockCount"
        )
        data_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://finance.sina.com.cn/stock/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        rows = []
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
            count_response = client.get(count_url, params={"node": "hs_a"})
            count_response.raise_for_status()
            total = int(count_response.json())
            for page in range(1, (total + 99) // 100 + 1):
                response = client.get(
                    data_url,
                    params={
                        "page": page,
                        "num": 100,
                        "sort": "symbol",
                        "asc": 1,
                        "node": "hs_a",
                    },
                )
                response.raise_for_status()
                page_rows = response.json()
                if not isinstance(page_rows, list):
                    raise StockDataUnavailableError("新浪返回了无效的市场数据")
                rows.extend(page_rows)
        return rows

    @staticmethod
    def _sina_catalog_records() -> list[Dict[str, str]]:
        rows = StockDataService._sina_market_rows()

        records = []
        exchange_names = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
        for row in rows:
            code = str(row.get("code") or "").strip()
            raw_symbol = str(row.get("symbol") or "").lower().strip()
            exchange = exchange_names.get(raw_symbol[:2])
            if not re.fullmatch(r"\d{6}", code) or not exchange:
                continue
            symbol = f"{code}.{exchange}"
            name = StockDataService._valid_name(row.get("name"), symbol)
            if not name:
                continue
            metadata = StockDataService._market_metadata(symbol)
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": metadata["board"],
                    **metadata,
                    "industry": "",
                    "area": "",
                    "list_status": "L",
                    "list_date": "",
                }
            )
        return records

    @staticmethod
    def _sina_realtime_records() -> list[Dict[str, Any]]:
        rows = StockDataService._sina_market_rows()
        quote_date = datetime.now().date().isoformat()
        exchange_names = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
        records = []
        for row in rows:
            code = str(row.get("code") or "").strip()
            raw_symbol = str(row.get("symbol") or "").lower().strip()
            exchange = exchange_names.get(raw_symbol[:2])
            if not re.fullmatch(r"\d{6}", code) or not exchange:
                continue
            pre_close = StockDataService._optional_float(row.get("settlement"))
            price = StockDataService._optional_float(row.get("trade"))
            if price == 0 and pre_close not in (None, 0):
                price = pre_close
            if price is None:
                continue
            tick_time = str(row.get("ticktime") or "").strip()
            quote_time = (
                f"{quote_date}T{tick_time}"
                if re.fullmatch(r"\d{2}:\d{2}:\d{2}", tick_time)
                else datetime.now().isoformat(timespec="seconds")
            )
            records.append(
                {
                    "symbol": f"{code}.{exchange}",
                    "price": price,
                    "pre_close": pre_close,
                    "change": StockDataService._optional_float(
                        row.get("pricechange")
                    ),
                    "change_pct": StockDataService._optional_float(
                        row.get("changepercent")
                    ),
                    "open": StockDataService._optional_float(row.get("open")),
                    "high": StockDataService._optional_float(row.get("high")),
                    "low": StockDataService._optional_float(row.get("low")),
                    "volume": (
                        StockDataService._optional_float(row.get("volume")) or 0
                    ),
                    "amount": StockDataService._optional_float(row.get("amount")),
                    "turnover_rate": StockDataService._optional_float(
                        row.get("turnoverratio")
                    ),
                    "quote_time": quote_time,
                }
            )
        return records

    def refresh_catalog(self) -> Dict[str, Any]:
        config = load_config(str(self.config_path))
        tushare = config.get("tushare", {})
        token = tushare.get("token")
        with _CATALOG_LOCK:
            records = []
            source = ""
            if token:
                try:
                    loader = TushareDataLoader(
                        token,
                        rate_limit_delay=float(tushare.get("rate_limit_delay", 1.5)),
                    )
                    records = self._catalog_records(loader.get_stock_list())
                    source = "tushare"
                except Exception:
                    records = []
            if not records:
                try:
                    records = self._eastmoney_catalog_records()
                    source = "eastmoney"
                except Exception:
                    try:
                        records = self._sina_catalog_records()
                        source = "sina"
                    except Exception as error:
                        raise StockDataUnavailableError(
                            "暂时无法获取股票清单，请稍后重试"
                        ) from error
            if not records:
                raise StockDataUnavailableError("未获取到有效的股票清单")
            upsert_stock_catalog(records, source, self.database)
        return {"count": len(records), "source": source}

    def refresh_quotes(self) -> Dict[str, Any]:
        with _QUOTE_LOCK:
            records = []
            source = ""
            errors = []
            try:
                frame = ak.stock_zh_a_spot_em()
            except Exception as error:
                errors.append(("AkShare/东方财富", error))
                logger.exception("AkShare realtime snapshot request failed")
            else:
                records = self._realtime_records(frame)
                if records:
                    source = "akshare"
                else:
                    error = StockDataUnavailableError("返回了空数据")
                    errors.append(("AkShare/东方财富", error))
                    logger.warning("AkShare realtime snapshot returned no valid records")
            if not records:
                try:
                    records = self._sina_realtime_records()
                except Exception as error:
                    errors.append(("新浪财经", error))
                    logger.exception("Sina realtime snapshot request failed")
                else:
                    if records:
                        source = "sina"
                    else:
                        error = StockDataUnavailableError("返回了空数据")
                        errors.append(("新浪财经", error))
                        logger.warning("Sina realtime snapshot returned no valid records")
            if not records:
                attempted = "、".join(name for name, _ in errors)
                cause = errors[-1][1] if errors else None
                raise StockDataUnavailableError(
                    f"全市场实时行情刷新失败（已尝试：{attempted}），原有行情数据未受影响"
                ) from cause
            upsert_stock_quotes(records, source, self.database)
        return {
            "count": len(records),
            "source": source,
            "quote_time": records[0]["quote_time"],
        }

    def _mx_loader(self):
        """创建东方财富妙想加载器；未配置 key 或依赖缺失时返回 None（静默降级）。

        API key 取自 load_config() 的 mx.api_key（config_loader 会把环境变量
        MX_APIKEY 合并进该配置项）。
        """
        api_key = (
            load_config(str(self.config_path)).get("mx", {}).get("api_key")
            or os.getenv("MX_APIKEY")
        )
        if not api_key:
            return None
        try:
            from ripple_tradePilot.data.mx_loader import MXDataLoader

            return MXDataLoader(api_key=api_key)
        except Exception as error:
            logger.warning("妙想数据源不可用：%s", error)
            return None

    def _mx_index_quotes(self) -> list:
        """一级来源：妙想自然语言查询三支指数的最新点位（失败返回 []）。"""
        loader = self._mx_loader()
        if loader is None:
            return []
        items = []
        for index in MARKET_INDEXES:
            # 妙想查询有延迟和不确定性：任何异常都静默降级，绝不上抛
            try:
                result = loader._query(f"{index['name']} 最新价 涨跌幅")
                frame = loader._extract_price_data(result)
            except Exception as error:
                logger.warning("妙想指数查询失败（%s）：%s", index["name"], error)
                return []
            if frame is None or len(frame) == 0 or "close" not in frame.columns:
                return []
            row = frame.iloc[-1]  # 妙想对"最新"查询通常返回当日数据，取最后一根
            price = self._optional_float(row.get("close"))
            if price is None:
                return []
            change = None
            change_pct = None
            for column in frame.columns:
                label = str(column)
                if change_pct is None and "涨跌幅" in label:
                    change_pct = self._optional_float(row.get(column))
                elif change is None and "涨跌额" in label:
                    change = self._optional_float(row.get(column))
            if change is None and change_pct is not None and change_pct > -100:
                change = round(price * change_pct / (100 + change_pct), 2)
            items.append(
                {
                    "name": index["name"],
                    "code": index["code"],
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                }
            )
        return items

    @staticmethod
    def _sina_index_quotes() -> list:
        """二级来源：新浪简版指数行情接口（GBK 编码，需 Referer 头）。"""
        codes = ",".join(f"s_{index['code']}" for index in MARKET_INDEXES)
        response = httpx.get(
            f"https://hq.sinajs.cn/list={codes}",
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
                ),
            },
            timeout=10,
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="replace")
        parsed: Dict[str, Dict[str, Any]] = {}
        for line in text.splitlines():
            match = re.fullmatch(
                r'var hq_str_s_([a-z]{2}\d{6})="([^"]*)";?', line.strip()
            )
            if not match:
                continue
            code, payload = match.groups()
            # 简版字段：名称,点位,涨跌,涨跌幅,成交量(手),成交额(万)
            fields = [field.strip() for field in payload.split(",")]
            if len(fields) < 4:
                continue
            price = StockDataService._optional_float(fields[1])
            if price is None:
                continue
            parsed[code] = {
                "name": fields[0],
                "price": price,
                "change": StockDataService._optional_float(fields[2]),
                "change_pct": StockDataService._optional_float(fields[3]),
            }
        items = []
        for index in MARKET_INDEXES:
            quote = parsed.get(index["code"])
            if quote is None:
                continue
            items.append(
                {
                    "name": quote["name"] or index["name"],
                    "code": index["code"],
                    "price": quote["price"],
                    "change": quote["change"],
                    "change_pct": quote["change_pct"],
                }
            )
        return items

    @staticmethod
    def _akshare_index_quotes() -> list:
        """三级来源：AkShare 东财指数列表里挑出三支目标指数。"""
        frame = ak.stock_zh_index_spot_em()
        if frame is None or len(frame) == 0 or "代码" not in frame.columns:
            return []
        by_digits = {index["code"][2:]: index for index in MARKET_INDEXES}
        found: Dict[str, Dict[str, Any]] = {}
        for _, row in frame.iterrows():
            digits = str(row.get("代码") or "").strip()
            index = by_digits.get(digits)
            if index is None or digits in found:
                continue
            price = StockDataService._optional_float(row.get("最新价"))
            if price is None:
                continue
            found[digits] = {
                "name": str(row.get("名称") or "").strip() or index["name"],
                "code": index["code"],
                "price": price,
                "change": StockDataService._optional_float(row.get("涨跌额")),
                "change_pct": StockDataService._optional_float(row.get("涨跌幅")),
            }
        return [
            found[index["code"][2:]]
            for index in MARKET_INDEXES
            if index["code"][2:] in found
        ]

    def fetch_index_quotes(self) -> Dict[str, Any]:
        """获取三大指数行情：妙想 → 新浪 → AkShare 依次降级，都失败返回 []。

        返回 {"indices": [...], "source": "mx"/"sina"/"akshare"/""}，
        成功结果带 60 秒模块级内存缓存（缓存键含来源）。
        """
        cached = _cached_index_quotes()
        if cached is not None:
            items, source = cached
            return {"indices": items, "source": source}
        tiers = (
            ("mx", self._mx_index_quotes),
            ("sina", self._sina_index_quotes),
            ("akshare", self._akshare_index_quotes),
        )
        for source, fetch in tiers:
            try:
                items = fetch()
            except Exception as error:
                logger.warning("指数行情获取失败（%s）：%s", source, error)
                items = []
            if items:
                _INDEX_QUOTE_CACHE[source] = (time.monotonic(), items)
                return {"indices": items, "source": source}
        return {"indices": [], "source": ""}

    def fetch_market_breadth_mx(self) -> Optional[Dict[str, int]]:
        """用妙想查询"A股上涨/下跌/平盘家数"；结构不保证，解析失败返回 None。

        返回 None 时，调用方应退回本地 stock_quotes 快照统计。
        """
        loader = self._mx_loader()
        if loader is None:
            return None
        try:
            result = loader._query("今日A股上涨家数 下跌家数 平盘家数")
            frame = loader._extract_price_data(result)
        except Exception as error:
            logger.warning("妙想市场宽度查询失败：%s", error)
            return None
        if frame is None or len(frame) == 0:
            return None
        row = frame.iloc[-1]
        breadth: Dict[str, int] = {}
        for column in frame.columns:
            if column == "datetime":
                continue
            label = str(column)
            value = self._optional_float(row.get(column))
            if value is None or value < 0 or value != int(value):
                continue
            if "涨" in label or "up" in label.lower():
                breadth["up"] = int(value)
            elif "跌" in label or "down" in label.lower():
                breadth["down"] = int(value)
            elif "平" in label or "flat" in label.lower():
                breadth["flat"] = int(value)
        # 三项家数必须齐全才可用，否则放弃（回到快照统计）
        if {"up", "down", "flat"} - breadth.keys():
            return None
        return breadth

    @staticmethod
    def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy().rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "昨收": "pre_close",
                "涨跌额": "change",
                "涨跌幅": "pct_chg",
                "成交量": "vol",
                "成交额": "amount",
                "datetime": "trade_date",
                "timestamp": "trade_date",
                "volume": "vol",
            }
        )
        if "trade_date" not in data.columns:
            raise StockDataUnavailableError("行情数据缺少交易日期")
        for column in ("open", "high", "low", "close"):
            if column not in data.columns:
                raise StockDataUnavailableError(f"行情数据缺少 {column} 字段")
            data[column] = pd.to_numeric(data[column], errors="coerce")
        for column in ("pre_close", "change", "pct_chg"):
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce")
        if "vol" not in data.columns:
            data["vol"] = 0
        data["vol"] = pd.to_numeric(data["vol"], errors="coerce").fillna(0)
        parsed_dates = pd.to_datetime(
            data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True),
            errors="coerce",
        )
        numeric_dates = data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
        numeric_mask = numeric_dates.str.fullmatch(r"\d{8}")
        parsed_dates.loc[numeric_mask] = pd.to_datetime(
            numeric_dates.loc[numeric_mask], format="%Y%m%d", errors="coerce"
        )
        data["trade_date"] = parsed_dates.dt.strftime("%Y%m%d")
        columns = ["trade_date", "open", "high", "low", "close", "vol"]
        columns.extend(
            column
            for column in ("pre_close", "change", "pct_chg")
            if column in data.columns
        )
        if "amount" in data.columns:
            data["amount"] = pd.to_numeric(data["amount"], errors="coerce")
            columns.append("amount")
        return (
            data[columns]
            .dropna(subset=["trade_date", "open", "high", "low", "close"])
            .drop_duplicates(subset=["trade_date"], keep="last")
            .sort_values("trade_date")
            .reset_index(drop=True)
        )

    def _fetch_tushare(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        config = load_config(str(self.config_path))
        tushare = config.get("tushare", {})
        token = tushare.get("token")
        if not token:
            return pd.DataFrame()
        loader = TushareDataLoader(
            token,
            rate_limit_delay=float(tushare.get("rate_limit_delay", 1.5)),
        )
        return loader.get_daily_bars(
            symbol, start_date=start_date, end_date=end_date
        )

    @staticmethod
    def _fetch_akshare(
        symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        code = symbol.split(".", 1)[0]
        frame = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            # 前复权：不复权序列在除权日有假跳空，会污染信号与盈亏
            adjust="qfq",
        )
        return frame

    def _fetch_daily(
        self, symbol: str, start_date: str, end_date: str
    ) -> Tuple[pd.DataFrame, str]:
        """tushare → akshare 降级拉取并规范化、做价格合理性校验。

        返回 ``(frame, source)``；frame 为空表示两源都无有效数据。
        """
        frame = pd.DataFrame()
        source = "tushare"
        try:
            frame = self._fetch_tushare(symbol, start_date, end_date)
        except Exception:
            frame = pd.DataFrame()
        if len(frame) == 0:
            source = "akshare"
            try:
                frame = self._fetch_akshare(symbol, start_date, end_date)
            except Exception as error:
                raise StockDataUnavailableError(
                    f"无法获取 {symbol} 的日线数据，请检查行情源配置"
                ) from error
        fetched = self._normalize_frame(frame)
        if len(fetched):
            # 价格合理性校验：丢弃非正价/高低倒置的脏行
            fetched = fetched[
                (fetched[["open", "high", "low", "close"]] > 0).all(axis=1)
                & (fetched["high"] >= fetched["low"])
            ].reset_index(drop=True)
        return fetched, source

    def refresh(self, value: str, initial_days: int = 365) -> Dict[str, Any]:
        symbol = self.normalize_symbol(value)
        now = datetime.now()
        config = load_config(str(self.config_path))
        data_cfg = config.get("data", {}) if isinstance(config, dict) else {}
        # A9：回看窗口 10→可配置（默认 30），给复权检测留足重叠样本
        overlap_days = int(data_cfg.get("refresh_overlap_days", 30))
        full_refetch_days = int(data_cfg.get("full_refetch_days", 1095))
        with _DATA_LOCK:
            existing = pd.DataFrame(load_daily_bars(symbol, self.database))
            if len(existing):
                existing = self._normalize_frame(existing)
            if len(existing):
                latest = datetime.strptime(existing.iloc[-1]["trade_date"], "%Y%m%d")
                start = latest - timedelta(days=overlap_days)
            else:
                start = now - timedelta(days=initial_days)
            start_date = start.strftime("%Y%m%d")
            end_date = now.strftime("%Y%m%d")

            name = stock_catalog_name(symbol, self.database) or self._configured_name(
                symbol
            )

            fetched, source = self._fetch_daily(symbol, start_date, end_date)
            if len(fetched) == 0:
                raise StockDataUnavailableError(f"未获取到 {symbol} 的有效日线数据")

            # A9 复权基准漂移检测：库内存量 vs 新拉取的重叠日 close 比值。
            # 命中（连续≥2日方向一致、幅度恒定的乘法偏差）→ 判上游 qfq 重算，
            # 全量重拉整段覆盖，杜绝新旧复权基准混接。
            stored_rows = existing.to_dict(orient="records") if len(existing) else []
            rebase_report = detect_rebase(stored_rows, fetched.to_dict(orient="records"))
            rebased = rebase_report.rebased
            if rebased:
                # 扩窗覆盖全部存量历史（含 initial_days/full_refetch_days/实际跨度），
                # 确保旧锚行全部被新锚数据覆盖，不在更早处残留混接点。
                span_days = 0
                if len(existing):
                    earliest = datetime.strptime(
                        existing.iloc[0]["trade_date"], "%Y%m%d"
                    )
                    span_days = (now - earliest).days
                refetch_days = max(initial_days, full_refetch_days, span_days)
                full_start = (now - timedelta(days=refetch_days)).strftime("%Y%m%d")
                logger.warning(
                    "检测到 %s 复权基准漂移（factor≈%.4f，连续 %d 日）：全量重拉 %s~%s",
                    symbol,
                    rebase_report.factor,
                    rebase_report.max_run,
                    full_start,
                    end_date,
                )
                full_frame, full_source = self._fetch_daily(
                    symbol, full_start, end_date
                )
                if len(full_frame):
                    fetched, source = full_frame, full_source

            anchor_date = str(fetched.iloc[-1]["trade_date"]) if len(fetched) else ""
            data_version = f"{source}|{anchor_date}|{now.strftime('%Y%m%dT%H%M%SZ')}"
            merged = self._normalize_frame(
                pd.concat([existing, fetched], ignore_index=True)
            )
            upsert_daily_bars(
                symbol,
                fetched.to_dict(orient="records"),
                source,
                self.database,
                adjust="qfq",
                adj_anchor_date=anchor_date,
                data_version=data_version,
            )

        return {
            "symbol": symbol,
            "name": name or symbol,
            "source": source,
            "fetched_rows": len(fetched),
            "total_rows": len(merged),
            "latest_date": datetime.strptime(
                merged.iloc[-1]["trade_date"], "%Y%m%d"
            ).date().isoformat(),
            "rebased": rebased,
            "rebase_factor": rebase_report.factor,
            "rebase_reason": rebase_report.reason,
            "data_version": data_version,
        }
