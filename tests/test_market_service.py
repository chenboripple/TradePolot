"""C1/C2 ``data/market_service.py`` 的离线测试。

覆盖：指数日线归一化（tushare 英文 / akshare 东财中文 / 新浪英文三种列名）、
refresh_index_daily 三级降级链（逐层失败注入 + 部分成功报告 + upsert 幂等）、
load_index_bars DB 优先（命中零网络、缺失有界补拉、补拉失败优雅降级）、
aggregate_breadth 分板块涨跌停口径手算（含"创业板 +9.85% 不再误判涨停"的修复）。

全程 mock 模块级 ``ak.*`` 函数与 ``TushareDataLoader``，零网络。
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import synth
from ripple_tradePilot.data import market_service as ms
from ripple_tradePilot.data.market_service import (
    MarketDataService,
    _normalize_index_frame,
    _normalize_trade_date,
    _to_sina_code,
    aggregate_breadth,
    load_index_bars,
    record_market_breadth,
)
from ripple_tradePilot.data.stock_service import StockDataUnavailableError
from ripple_tradePilot.storage.database import (
    init_database,
    load_index_bars as db_load_index_bars,
    load_market_daily,
    upsert_index_daily,
)

CONFIG_WITH_TOKEN = {"tushare": {"token": "TESTTOKEN", "rate_limit_delay": 0}}
CONFIG_NO_TOKEN: dict = {}
CODE = "000300.SH"


def _tushare_frame(count=5, start_close=3000.0):
    """tushare pro.index_daily 风格：英文列、trade_date=YYYYMMDD、含 vol/amount/pct_chg。"""
    base = date(2026, 1, 1)
    return pd.DataFrame(
        {
            "trade_date": [(base + timedelta(days=i)).strftime("%Y%m%d") for i in range(count)],
            "open": [start_close + i for i in range(count)],
            "high": [start_close + i + 5 for i in range(count)],
            "low": [start_close + i - 5 for i in range(count)],
            "close": [start_close + i * 2 for i in range(count)],
            "pct_chg": [0.1 * i for i in range(count)],
            "vol": [1e8] * count,
            "amount": [1e10] * count,
        }
    )


def _akshare_hist_frame(count=5, start_close=3000.0):
    """akshare index_zh_a_hist 风格：中文列、日期=YYYY-MM-DD。"""
    base = date(2026, 1, 1)
    return pd.DataFrame(
        {
            "日期": [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)],
            "开盘": [start_close + i for i in range(count)],
            "收盘": [start_close + i * 2 for i in range(count)],
            "最高": [start_close + i + 5 for i in range(count)],
            "最低": [start_close + i - 5 for i in range(count)],
            "成交量": [1e8] * count,
            "成交额": [1e10] * count,
            "涨跌幅": [0.1 * i for i in range(count)],
        }
    )


def _sina_frame(count=5, start_close=3000.0):
    """akshare stock_zh_index_daily 风格：英文列、date=YYYY-MM-DD、无 pct_chg/amount。"""
    base = date(2026, 1, 1)
    return pd.DataFrame(
        {
            "date": [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)],
            "open": [start_close + i for i in range(count)],
            "high": [start_close + i + 5 for i in range(count)],
            "low": [start_close + i - 5 for i in range(count)],
            "close": [start_close + i * 2 for i in range(count)],
            "volume": [1e8] * count,
        }
    )


class _TmpDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "market.db"
        init_database(self.db)


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------
class NormalizeTest(unittest.TestCase):
    def test_normalize_trade_date_variants(self):
        self.assertEqual(_normalize_trade_date("20260102"), "20260102")
        self.assertEqual(_normalize_trade_date("2026-01-02"), "20260102")
        self.assertEqual(_normalize_trade_date("2026/01/02"), "20260102")
        self.assertEqual(_normalize_trade_date(datetime(2026, 1, 2)), "20260102")
        self.assertEqual(_normalize_trade_date(date(2026, 1, 2)), "20260102")
        self.assertEqual(_normalize_trade_date(pd.Timestamp("2026-01-02")), "20260102")

    def test_normalize_trade_date_bad_input(self):
        self.assertEqual(_normalize_trade_date(""), "")
        self.assertEqual(_normalize_trade_date(None), "")
        self.assertEqual(_normalize_trade_date("not-a-date"), "")

    def test_to_sina_code(self):
        self.assertEqual(_to_sina_code("000300.SH"), "sh000300")
        self.assertEqual(_to_sina_code("000001.SH"), "sh000001")
        self.assertEqual(_to_sina_code("399001.SZ"), "sz399001")
        self.assertEqual(_to_sina_code("399006.SZ"), "sz399006")

    def test_normalize_index_frame_chinese_columns(self):
        rows = _normalize_index_frame(_akshare_hist_frame())
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["trade_date"], "20260101")  # 升序、YYYYMMDD
        self.assertEqual(rows[0]["close"], 3000.0)
        self.assertEqual(rows[0]["vol"], 1e8)  # 成交量 → vol
        self.assertEqual(rows[0]["amount"], 1e10)
        self.assertAlmostEqual(rows[1]["pct_chg"], 0.1)

    def test_normalize_index_frame_english_columns(self):
        rows = _normalize_index_frame(_tushare_frame())
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["trade_date"], "20260101")
        self.assertEqual(rows[2]["close"], 3004.0)

    def test_normalize_index_frame_sina_no_pct(self):
        rows = _normalize_index_frame(_sina_frame())
        self.assertEqual(len(rows), 5)
        self.assertIsNone(rows[0]["pct_chg"])  # 新浪源无涨跌幅
        self.assertIsNone(rows[0]["amount"])
        self.assertEqual(rows[0]["vol"], 1e8)

    def test_normalize_index_frame_skips_bad_rows(self):
        frame = _tushare_frame(3)
        frame.loc[1, "close"] = float("nan")  # 无效收盘 → 丢弃
        frame.loc[2, "trade_date"] = "garbage"  # 无效日期 → 丢弃
        rows = _normalize_index_frame(frame)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "20260101")

    def test_normalize_index_frame_empty_or_none(self):
        self.assertEqual(_normalize_index_frame(None), [])
        self.assertEqual(_normalize_index_frame(pd.DataFrame()), [])
        # 缺 close 列 → 无法用，返回 []（触发降级）
        self.assertEqual(_normalize_index_frame(pd.DataFrame({"trade_date": ["20260101"]})), [])


# ---------------------------------------------------------------------------
# refresh_index_daily 三级降级链
# ---------------------------------------------------------------------------
class RefreshIndexDailyTest(_TmpDbTestCase):
    def test_tushare_first_when_token_present(self):
        with patch.object(ms, "TushareDataLoader") as MockLoader:
            MockLoader.return_value.get_index_bars.return_value = _tushare_frame()
            report = MarketDataService(config=CONFIG_WITH_TOKEN, path=self.db).refresh_index_daily(CODE)
        self.assertEqual(report["source"], "tushare")
        self.assertEqual(report["rows"], 5)
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(db_load_index_bars(CODE, self.db)), 5)

    def test_no_token_skips_tushare_cleanly(self):
        # 无 token：get_tushare_token 抛 ValueError → _fetch_tushare 返回 []（非错误），
        # 不构造 loader、不计入 errors，直接降级到 akshare 东财。
        with patch.object(ms, "TushareDataLoader") as MockLoader, patch.object(
            ms.ak, "index_zh_a_hist", return_value=_akshare_hist_frame()
        ):
            report = MarketDataService(config=CONFIG_NO_TOKEN, path=self.db).refresh_index_daily(CODE)
        MockLoader.assert_not_called()
        self.assertEqual(report["source"], "akshare")
        self.assertEqual(report["errors"], [])  # 无 token 是干净跳过，不是错误

    def test_tushare_error_falls_to_akshare(self):
        with patch.object(ms, "TushareDataLoader") as MockLoader, patch.object(
            ms.ak, "index_zh_a_hist", return_value=_akshare_hist_frame()
        ):
            MockLoader.return_value.get_index_bars.side_effect = RuntimeError("tushare 502")
            report = MarketDataService(config=CONFIG_WITH_TOKEN, path=self.db).refresh_index_daily(CODE)
        self.assertEqual(report["source"], "akshare")
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["source"], "tushare")

    def test_akshare_hist_empty_falls_to_sina(self):
        with patch.object(ms, "TushareDataLoader") as MockLoader, patch.object(
            ms.ak, "index_zh_a_hist", return_value=pd.DataFrame()
        ), patch.object(ms.ak, "stock_zh_index_daily", return_value=_sina_frame()) as mock_sina:
            MockLoader.return_value.get_index_bars.return_value = pd.DataFrame()
            report = MarketDataService(config=CONFIG_WITH_TOKEN, path=self.db).refresh_index_daily(CODE)
        self.assertEqual(report["source"], "sina")
        # 新浪代码转换正确
        self.assertEqual(mock_sina.call_args.kwargs["symbol"], "sh000300")
        self.assertEqual(report["rows"], 5)

    def test_all_tiers_fail_raises(self):
        with patch.object(ms, "TushareDataLoader") as MockLoader, patch.object(
            ms.ak, "index_zh_a_hist", side_effect=RuntimeError("ak down")
        ), patch.object(ms.ak, "stock_zh_index_daily", side_effect=RuntimeError("sina down")):
            MockLoader.return_value.get_index_bars.side_effect = RuntimeError("tushare down")
            with self.assertRaises(StockDataUnavailableError):
                MarketDataService(config=CONFIG_WITH_TOKEN, path=self.db).refresh_index_daily(CODE)

    def test_refresh_is_idempotent(self):
        with patch.object(ms, "TushareDataLoader") as MockLoader:
            MockLoader.return_value.get_index_bars.return_value = _tushare_frame()
            service = MarketDataService(config=CONFIG_WITH_TOKEN, path=self.db)
            service.refresh_index_daily(CODE)
            service.refresh_index_daily(CODE)  # 第二次 upsert 覆盖，不产生重复行
        self.assertEqual(len(db_load_index_bars(CODE, self.db)), 5)


class RefreshIndexesBatchTest(_TmpDbTestCase):
    def test_partial_success_report(self):
        # 000300 成功（tushare），000001 全失败 → refreshed 1 条、failed 1 条，互不阻断
        def fake_get_index_bars(index_code, start_date, end_date):
            if index_code == CODE:
                return _tushare_frame()
            raise RuntimeError("no data for 000001")

        with patch.object(ms, "TushareDataLoader") as MockLoader, patch.object(
            ms.ak, "index_zh_a_hist", side_effect=RuntimeError("ak down")
        ), patch.object(ms.ak, "stock_zh_index_daily", side_effect=RuntimeError("sina down")):
            MockLoader.return_value.get_index_bars.side_effect = fake_get_index_bars
            report = MarketDataService(config=CONFIG_WITH_TOKEN, path=self.db).refresh_indexes(
                [CODE, "000001.SH"]
            )
        self.assertEqual(len(report["refreshed"]), 1)
        self.assertEqual(report["refreshed"][0]["index_code"], CODE)
        self.assertEqual(len(report["failed"]), 1)
        self.assertEqual(report["failed"][0]["index_code"], "000001.SH")


# ---------------------------------------------------------------------------
# load_index_bars：DB 优先 + 有界补拉 + 优雅降级
# ---------------------------------------------------------------------------
class LoadIndexBarsTest(_TmpDbTestCase):
    def test_db_hit_does_not_touch_network(self):
        upsert_index_daily(CODE, synth.index_rows(10), "test", self.db)
        with patch.object(
            ms.MarketDataService,
            "refresh_index_daily",
            side_effect=AssertionError("DB 命中不应触发补拉/网络"),
        ):
            rows = load_index_bars(CODE, path=self.db)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["trade_date"], synth.index_rows(10)[0]["trade_date"])

    def test_filters_by_date_range(self):
        upsert_index_daily(CODE, synth.index_rows(10), "test", self.db)
        all_rows = db_load_index_bars(CODE, self.db)
        start = all_rows[2]["trade_date"]
        end = all_rows[5]["trade_date"]
        with patch.object(
            ms.MarketDataService, "refresh_index_daily", side_effect=AssertionError("不应补拉")
        ):
            rows = load_index_bars(CODE, start_date=start, end_date=end, path=self.db)
        self.assertEqual([r["trade_date"] for r in rows], [r["trade_date"] for r in all_rows[2:6]])

    def test_fetches_when_db_empty(self):
        def fake_refresh(self, index_code, days=750):
            upsert_index_daily(index_code, synth.index_rows(6), "tushare", self.path)
            return {"index_code": index_code, "source": "tushare", "rows": 6}

        with patch.object(ms.MarketDataService, "refresh_index_daily", fake_refresh):
            rows = load_index_bars(CODE, config=CONFIG_NO_TOKEN, path=self.db)
        self.assertEqual(len(rows), 6)

    def test_degrades_to_empty_on_fetch_failure(self):
        with patch.object(
            ms.MarketDataService,
            "refresh_index_daily",
            side_effect=StockDataUnavailableError("all sources down"),
        ):
            rows = load_index_bars(CODE, config=CONFIG_NO_TOKEN, path=self.db)
        self.assertEqual(rows, [])  # 诚实返回空，绝不抛错

    def test_fetch_false_never_touches_network(self):
        with patch.object(
            ms.MarketDataService,
            "refresh_index_daily",
            side_effect=AssertionError("fetch=False 不应补拉"),
        ):
            rows = load_index_bars(CODE, fetch=False, path=self.db)
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# aggregate_breadth：分板块涨跌停口径
# ---------------------------------------------------------------------------
class AggregateBreadthTest(unittest.TestCase):
    def test_basic_counts(self):
        quotes = [
            {"symbol": "600000.SH", "change_pct": 3.0, "amount": 1e9},
            {"symbol": "600001.SH", "change_pct": -2.0, "amount": 2e9},
            {"symbol": "600002.SH", "change_pct": 0.0, "amount": None},  # 平盘、无成交额
            {"symbol": "600003.SH", "change_pct": None, "amount": 5e8},  # 停牌等 → 平盘
        ]
        b = aggregate_breadth(quotes)
        self.assertEqual(b["advancers"], 1)
        self.assertEqual(b["decliners"], 1)
        self.assertEqual(b["unchanged"], 2)  # 0.0 与 None 都算平盘
        self.assertEqual(b["limit_up"], 0)
        self.assertEqual(b["limit_down"], 0)
        self.assertEqual(b["total"], 4)
        self.assertEqual(b["total_amount"], 1e9 + 2e9 + 5e8)  # None 跳过
        self.assertEqual(b["up_ratio"], 0.25)

    def test_per_board_limit_classification(self):
        # 关键修复：涨跌停按板块判定，不再是 ±9.8% 一刀切。
        quotes = [
            {"symbol": "600000.SH", "change_pct": 9.85},   # 主板 ≥9.8 → 涨停
            {"symbol": "300750.SZ", "change_pct": 9.85},   # 创业板阈值 19.8，9.85 → 非涨停（旧口径会误判！）
            {"symbol": "300751.SZ", "change_pct": 19.9},   # 创业板 ≥19.8 → 涨停
            {"symbol": "688001.SH", "change_pct": 19.95},  # 科创板 ≥19.8 → 涨停
            {"symbol": "920001.BJ", "change_pct": 29.9},   # 北交所 ≥29.8 → 涨停
            {"symbol": "000001.SZ", "change_pct": -9.85},  # 主板跌停
            {"symbol": "300752.SZ", "change_pct": -19.9},  # 创业板跌停
        ]
        b = aggregate_breadth(quotes)
        self.assertEqual(b["limit_up"], 4)    # 600000 / 300751 / 688001 / 920001
        self.assertEqual(b["limit_down"], 2)  # 000001 / 300752
        self.assertEqual(b["advancers"], 5)
        self.assertEqual(b["decliners"], 2)
        self.assertEqual(b["total"], 7)

    def test_main_board_boundary_preserved(self):
        # 主板恰好 9.8（旧 MARKET_LIMIT_PCT）仍判涨停，口径向后兼容
        b = aggregate_breadth([{"symbol": "600000.SH", "change_pct": 9.8}])
        self.assertEqual(b["limit_up"], 1)
        # 9.79 不判
        b2 = aggregate_breadth([{"symbol": "600000.SH", "change_pct": 9.79}])
        self.assertEqual(b2["limit_up"], 0)

    def test_empty_quotes(self):
        b = aggregate_breadth([])
        self.assertEqual(b["total"], 0)
        self.assertEqual(b["up_ratio"], 0.0)
        self.assertEqual(b["advancers"], 0)


# ---------------------------------------------------------------------------
# record_market_breadth：聚合 + 落库
# ---------------------------------------------------------------------------
class RecordMarketBreadthTest(_TmpDbTestCase):
    def test_round_trip(self):
        quotes = [
            {"symbol": "600000.SH", "change_pct": 9.85, "amount": 1e9},
            {"symbol": "000001.SZ", "change_pct": -1.0, "amount": 2e9},
        ]
        breadth = record_market_breadth("20260102", quotes, "snapshot", self.db)
        self.assertEqual(breadth["advancers"], 1)
        self.assertEqual(breadth["limit_up"], 1)

        rows = load_market_daily(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "20260102")
        self.assertEqual(rows[0]["advancers"], 1)
        self.assertEqual(rows[0]["limit_up"], 1)
        self.assertEqual(rows[0]["total_amount"], 3e9)
        self.assertEqual(rows[0]["source"], "snapshot")

    def test_same_day_overwrites(self):
        # 盘中 provisional → 收盘 final：同日重复写覆盖为终态
        record_market_breadth(
            "20260102", [{"symbol": "600000.SH", "change_pct": 1.0}], "snapshot", self.db
        )
        record_market_breadth(
            "20260102",
            [{"symbol": "600000.SH", "change_pct": 9.9}, {"symbol": "000001.SZ", "change_pct": -1.0}],
            "mx",
            self.db,
        )
        rows = load_market_daily(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["advancers"], 1)
        self.assertEqual(rows[0]["limit_up"], 1)
        self.assertEqual(rows[0]["source"], "mx")


if __name__ == "__main__":
    unittest.main()
