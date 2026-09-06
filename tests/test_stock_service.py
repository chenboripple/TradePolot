import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pandas as pd

import synth

from ripple_tradePilot.data.adjustment import audit_series
from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
    StockDataUnavailableError,
)
from ripple_tradePilot.storage.database import (
    list_stock_catalog,
    load_daily_bars,
    stock_catalog_name,
    upsert_stock_catalog,
    upsert_stock_quotes,
)


class FakeStockDataService(StockDataService):
    def _fetch_tushare(self, symbol, start_date, end_date):
        self.requested_range = (start_date, end_date)
        return pd.DataFrame(
            [
                {
                    "trade_date": "20260831",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 100,
                },
                {
                    "trade_date": "20260901",
                    "open": 10.5,
                    "high": 12,
                    "low": 10,
                    "close": 11.8,
                    "vol": 120,
                },
            ]
        )


class RebaseFakeService(StockDataService):
    """第二次起把整段序列 ×0.9，模拟上游 qfq 重算（复权基准漂移）。

    与 FakeStockDataService 一样忽略请求窗口、返回全量行——detect_rebase 只比对
    重叠交易日，窗口仅用于断言"全量重拉确实扩窗"。
    """

    def __init__(self, base_rows, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_rows = base_rows
        self.rebased_mode = False
        self.calls = []  # [(start_date, end_date), ...]

    def _fetch_tushare(self, symbol, start_date, end_date):
        self.calls.append((start_date, end_date))
        rows = (
            synth.scale_rows(self.base_rows, 0.9)
            if self.rebased_mode
            else self.base_rows
        )
        # synth.daily_rows 同时带 vol/volume；源帧只应有一种成交量列名，
        # 否则 _normalize_frame 的 volume→vol 重命名会产生重复列。
        frame_rows = [{k: v for k, v in row.items() if k != "volume"} for row in rows]
        return pd.DataFrame(frame_rows)


class FakeTushareLoader:
    catalog_requests = 0

    def __init__(self, token, rate_limit_delay=1.5):
        self.token = token

    def get_daily_bars(self, symbol, start_date, end_date):
        return pd.DataFrame(
            [
                {
                    "trade_date": "20260901",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 100,
                }
            ]
        )

    def get_stock_list(self):
        type(self).catalog_requests += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "600418.SH",
                    "name": "江淮汽车",
                    "market": "主板",
                    "exchange": "SSE",
                    "industry": "汽车整车",
                    "area": "安徽",
                    "list_status": "L",
                    "list_date": "20010930",
                }
            ]
        )


class StockDataServiceTest(unittest.TestCase):
    def test_normalizes_stock_exchange_suffix(self):
        self.assertEqual(StockDataService.normalize_symbol("600000"), "600000.SH")
        self.assertEqual(StockDataService.normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(StockDataService.normalize_symbol("830001"), "830001.BJ")
        self.assertEqual(StockDataService.normalize_symbol("920855"), "920855.BJ")
        with self.assertRaises(InvalidStockSymbolError):
            StockDataService.normalize_symbol("abc")

    def test_refresh_stores_daily_bars_in_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text(
                "symbols:\n  - code: 600000.SH\n    name: 测试银行\n",
                encoding="utf-8",
            )
            database = root / "market.db"
            service = FakeStockDataService(config_path=config, database=database)

            result = service.refresh("600000", initial_days=365)
            rows = load_daily_bars("600000.SH", database)

            requested_start = datetime.strptime(service.requested_range[0], "%Y%m%d")
            requested_end = datetime.strptime(service.requested_range[1], "%Y%m%d")
            self.assertGreaterEqual((requested_end - requested_start).days, 364)
            self.assertEqual(result["name"], "测试银行")
            self.assertEqual(result["total_rows"], 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["close"], 11.8)

    def test_refresh_detects_rebase_and_full_refetches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text(
                "symbols:\n  - code: 600000.SH\n    name: 测试银行\n",
                encoding="utf-8",
            )
            database = root / "market.db"
            base_rows = synth.daily_rows(synth.daily_bars(60, seed=11))
            service = RebaseFakeService(base_rows, config_path=config, database=database)

            # 第一次刷新：库空，正常落库（anchor A）
            first = service.refresh("600000", initial_days=365)
            self.assertFalse(first["rebased"])
            self.assertEqual(len(load_daily_bars("600000.SH", database)), 60)
            self.assertEqual(len(service.calls), 1)

            # 第二次刷新：上游整段 ×0.9（anchor B）→ 检测命中 → 全量重拉
            service.rebased_mode = True
            second = service.refresh("600000", initial_days=365)

            self.assertTrue(second["rebased"])
            self.assertAlmostEqual(second["rebase_factor"], 0.9, places=3)
            # 调用 3 次：初次 + 重叠窗 + 全量重拉
            self.assertEqual(len(service.calls), 3)
            overlap_start, full_start = service.calls[1][0], service.calls[2][0]
            self.assertLess(full_start, overlap_start)  # 全量重拉窗口更宽

            rows = load_daily_bars("600000.SH", database)
            self.assertEqual(len(rows), 60)
            # 库内整段被新锚覆盖：close ≈ 原 ×0.9，且 data_version/anchor 已更新
            for original, stored in zip(base_rows, rows):
                self.assertAlmostEqual(
                    stored["close"], round(original["close"] * 0.9, 4), places=4
                )
                self.assertEqual(stored["data_version"], second["data_version"])
                self.assertEqual(stored["adj_anchor_date"], base_rows[-1]["trade_date"])
                self.assertEqual(stored["adjust"], "qfq")
            # 覆盖后序列内部自洽（无新旧锚混接点）
            self.assertTrue(audit_series(rows).clean)

    def test_refresh_overlap_window_is_configurable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text(
                "symbols:\n  - code: 600000.SH\n    name: 测试银行\n"
                "data:\n  refresh_overlap_days: 5\n",
                encoding="utf-8",
            )
            database = root / "market.db"
            base_rows = synth.daily_rows(synth.daily_bars(40, seed=13))
            service = RebaseFakeService(base_rows, config_path=config, database=database)
            service.refresh("600000", initial_days=365)  # 落库
            service.refresh("600000", initial_days=365)  # 增量：回看窗按配置
            # 第二次（增量）请求的 start 应为 latest-5d，而非默认 30d
            overlap_start = datetime.strptime(service.calls[1][0], "%Y%m%d")
            latest = datetime.strptime(base_rows[-1]["trade_date"], "%Y%m%d")
            self.assertEqual((latest - overlap_start).days, 5)

    def test_catalog_refresh_persists_latest_names_and_basic_info(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("tushare:\n  token: test-token\nsymbols: []\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            FakeTushareLoader.catalog_requests = 0

            with patch(
                "ripple_tradePilot.data.stock_service.TushareDataLoader",
                FakeTushareLoader,
            ):
                result = service.refresh_catalog()

            self.assertEqual(result["count"], 1)
            self.assertEqual(stock_catalog_name("600418.SH", root / "market.db"), "江淮汽车")
            self.assertEqual(FakeTushareLoader.catalog_requests, 1)
            item = list_stock_catalog(root / "market.db")[0]
            self.assertEqual(item["exchange"], "SSE")
            self.assertEqual(item["board"], "主板")
            self.assertEqual(item["industry"], "汽车整车")
            self.assertEqual(item["area"], "安徽")

    def test_full_market_realtime_snapshot_is_fetched_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600418.SH", "name": "江淮汽车"}],
                "test",
                database,
            )
            frame = pd.DataFrame(
                [
                    {
                        "代码": "600418",
                        "最新价": 42.6,
                        "昨收": 41.8,
                        "涨跌额": 0.8,
                        "涨跌幅": 1.9139,
                        "今开": 42,
                        "最高": 43,
                        "最低": 41.9,
                        "成交量": 1234,
                        "成交额": 5300000,
                        "换手率": 1.2,
                    }
                ]
            )
            service = StockDataService(database=database)
            with patch(
                "ripple_tradePilot.data.stock_service.ak.stock_zh_a_spot_em",
                return_value=frame,
            ) as snapshot:
                result = service.refresh_quotes()

            item = list_stock_catalog(database)[0]
            self.assertEqual(snapshot.call_count, 1)
            self.assertEqual(result["count"], 1)
            self.assertEqual(item["price"], 42.6)
            self.assertEqual(item["pre_close"], 41.8)
            self.assertEqual(item["change_pct"], 1.9139)
            self.assertEqual(item["quote_volume"], 123400)
            self.assertEqual(item["price_kind"], "realtime")

    def test_sina_realtime_snapshot_maps_official_quote_fields(self):
        rows = [
            {
                "symbol": "bj920000",
                "code": "920000",
                "trade": "14.760",
                "settlement": "14.190",
                "pricechange": 0.57,
                "changepercent": 4.017,
                "open": "14.160",
                "high": "15.990",
                "low": "14.160",
                "volume": 2394472,
                "amount": 36274538,
                "turnoverratio": 4.15751,
                "ticktime": "15:30:00",
            }
        ]
        with patch.object(StockDataService, "_sina_market_rows", return_value=rows):
            records = StockDataService._sina_realtime_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["symbol"], "920000.BJ")
        self.assertEqual(records[0]["price"], 14.76)
        self.assertEqual(records[0]["pre_close"], 14.19)
        self.assertEqual(records[0]["change"], 0.57)
        self.assertEqual(records[0]["change_pct"], 4.017)
        self.assertEqual(records[0]["volume"], 2394472)
        self.assertTrue(records[0]["quote_time"].endswith("T15:30:00"))

    def test_realtime_refresh_falls_back_to_sina_and_persists_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "market.db"
            upsert_stock_catalog(
                [{"symbol": "920000.BJ", "name": "安徽凤凰"}],
                "test",
                database,
            )
            records = [
                {
                    "symbol": "920000.BJ",
                    "price": 14.76,
                    "pre_close": 14.19,
                    "change": 0.57,
                    "change_pct": 4.017,
                    "open": 14.16,
                    "high": 15.99,
                    "low": 14.16,
                    "volume": 2394472,
                    "amount": 36274538,
                    "turnover_rate": 4.15751,
                    "quote_time": "2026-09-02T15:30:00",
                }
            ]
            service = StockDataService(database=database)
            with (
                patch(
                    "ripple_tradePilot.data.stock_service.ak.stock_zh_a_spot_em",
                    side_effect=RuntimeError("eastmoney disconnected"),
                ),
                patch.object(
                    StockDataService,
                    "_sina_realtime_records",
                    return_value=records,
                ),
            ):
                result = service.refresh_quotes()

            item = list_stock_catalog(database)[0]
            self.assertEqual(result["source"], "sina")
            self.assertEqual(result["count"], 1)
            self.assertEqual(item["price"], 14.76)
            self.assertEqual(item["price_source"], "sina")

    def test_failed_realtime_refresh_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600418.SH", "name": "江淮汽车"}],
                "test",
                database,
            )
            upsert_stock_quotes(
                [
                    {
                        "symbol": "600418.SH",
                        "price": 42.6,
                        "quote_time": "2026-09-02T10:30:00",
                    }
                ],
                "existing",
                database,
            )
            service = StockDataService(database=database)
            with (
                patch(
                    "ripple_tradePilot.data.stock_service.ak.stock_zh_a_spot_em",
                    side_effect=RuntimeError("eastmoney disconnected"),
                ),
                patch.object(
                    StockDataService,
                    "_sina_realtime_records",
                    side_effect=RuntimeError("sina disconnected"),
                ),
            ):
                with self.assertRaisesRegex(
                    StockDataUnavailableError,
                    "AkShare/东方财富、新浪财经",
                ):
                    service.refresh_quotes()

            item = list_stock_catalog(database)[0]
            self.assertEqual(item["price"], 42.6)
            self.assertEqual(item["price_source"], "existing")

    def test_catalog_refresh_falls_back_to_full_eastmoney_list(self):
        class LimitedTushareLoader(FakeTushareLoader):
            def get_stock_list(self):
                raise RuntimeError("stock_basic rate limited")

        class FakeResponse:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "data": {
                        "diff": [
                            {"f12": "600418", "f14": "江淮汽车"},
                            {"f12": "920855", "f14": "浙江大农"},
                        ]
                    }
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("tushare:\n  token: test-token\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            with (
                patch(
                    "ripple_tradePilot.data.stock_service.TushareDataLoader",
                    LimitedTushareLoader,
                ),
                patch("ripple_tradePilot.data.stock_service.httpx.get", return_value=FakeResponse()),
            ):
                result = service.refresh_catalog()

            self.assertEqual(result, {"count": 2, "source": "eastmoney"})
            self.assertEqual(stock_catalog_name("600418.SH", root / "market.db"), "江淮汽车")
            self.assertEqual(stock_catalog_name("920855.BJ", root / "market.db"), "浙江大农")

    def test_catalog_refresh_without_tushare_token_uses_fallback_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("symbols: []\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            with (
                patch.object(
                    StockDataService,
                    "_eastmoney_catalog_records",
                    return_value=[
                        {
                            "symbol": "600418.SH",
                            "name": "江淮汽车",
                            "exchange": "SSE",
                            "board": "主板",
                        }
                    ],
                ),
                patch(
                    "ripple_tradePilot.data.stock_service.TushareDataLoader"
                ) as loader,
            ):
                result = service.refresh_catalog()

            loader.assert_not_called()
            self.assertEqual(result, {"count": 1, "source": "eastmoney"})

    def test_eastmoney_catalog_retries_an_alternate_endpoint_after_disconnect(self):
        class FakeResponse:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"data": {"diff": [{"f12": "600418", "f14": "江淮汽车"}]}}

        with patch(
            "ripple_tradePilot.data.stock_service.httpx.get",
            side_effect=[httpx.RemoteProtocolError("disconnected"), FakeResponse()],
        ) as get:
            records = StockDataService._eastmoney_catalog_records()

        self.assertEqual(records[0]["name"], "江淮汽车")
        self.assertEqual(get.call_count, 2)

    def test_sina_catalog_reads_all_exchange_names(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data

            @staticmethod
            def raise_for_status():
                return None

            def json(self):
                return self.data

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, url, params):
                if "StockCount" in url:
                    return FakeResponse("3")
                return FakeResponse(
                    [
                        {"symbol": "sh600418", "code": "600418", "name": "江淮汽车"},
                        {"symbol": "sz300750", "code": "300750", "name": "宁德时代"},
                        {"symbol": "bj920855", "code": "920855", "name": "浙江大农"},
                    ]
                )

        with patch("ripple_tradePilot.data.stock_service.httpx.Client", return_value=FakeClient()):
            records = StockDataService._sina_catalog_records()

        self.assertEqual(
            [(item["symbol"], item["name"]) for item in records],
            [
                ("600418.SH", "江淮汽车"),
                ("300750.SZ", "宁德时代"),
                ("920855.BJ", "浙江大农"),
            ],
        )

    def test_catalog_refresh_falls_back_to_sina_when_eastmoney_is_unavailable(self):
        class LimitedTushareLoader(FakeTushareLoader):
            def get_stock_list(self):
                raise RuntimeError("stock_basic rate limited")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("tushare:\n  token: test-token\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            with (
                patch(
                    "ripple_tradePilot.data.stock_service.TushareDataLoader",
                    LimitedTushareLoader,
                ),
                patch.object(
                    StockDataService,
                    "_eastmoney_catalog_records",
                    side_effect=RuntimeError("unavailable"),
                ),
                patch.object(
                    StockDataService,
                    "_sina_catalog_records",
                    return_value=[{"symbol": "600418.SH", "name": "江淮汽车"}],
                ),
            ):
                result = service.refresh_catalog()

        self.assertEqual(result, {"count": 1, "source": "sina"})

    def test_single_stock_refresh_uses_catalog_name_without_refreshing_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text(
                "tushare:\n  token: test-token\nsymbols: []\n",
                encoding="utf-8",
            )
            database = root / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600000.SH", "name": "ST测试"}],
                "test",
                database,
            )
            FakeTushareLoader.catalog_requests = 0
            with patch(
                "ripple_tradePilot.data.stock_service.TushareDataLoader",
                FakeTushareLoader,
            ):
                result = StockDataService(
                    config_path=config, database=database
                ).refresh("600000.SH")

        self.assertEqual(result["name"], "ST测试")
        self.assertEqual(FakeTushareLoader.catalog_requests, 0)
