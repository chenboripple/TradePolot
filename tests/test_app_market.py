"""C4 市场/行业历史只读端点的离线测试。

端点纯读 DB、不触发网络（``/api/market/history`` 用 ``fetch=False``）。测试用 tmp DB +
TestClient + 注册登录拿会话 cookie，seed 各 v14 表后断言响应形状/值、日期过滤、limit、
登录门控、非法日期 422，并证明空库时 market/history 仍不触发补拉（零网络）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.storage.database import (
    init_database,
    record_market_daily,
    upsert_index_daily,
    upsert_industry_board_bars,
    upsert_industry_boards,
    upsert_industry_membership,
    upsert_stock_catalog,
)


class MarketHistoryApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database = root / "tradepilot.db"
        self.config = root / "config.yaml"
        self.config.write_text("symbols: []\nfutures: []\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "TRADEPILOT_BACKTEST_DB": str(self.database),
                "TRADEPILOT_CONFIG": str(self.config),
            },
        )
        self.environment.start()
        init_database(self.database)
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self._tmp.cleanup()

    def register(self, username="alice"):
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "strong-pass-123"},
        )
        self.assertEqual(response.status_code, 201, response.text)

    # --- /api/market/history ---
    def test_history_requires_login(self):
        self.assertEqual(self.client.get("/api/market/history").status_code, 401)

    def test_history_returns_seeded_index_rows(self):
        self.register()
        upsert_index_daily(
            "000300.SH",
            [
                {"trade_date": "20260102", "open": 3000, "high": 3050, "low": 2990,
                 "close": 3040, "pct_chg": 1.33, "amount": 1.2e11, "vol": 9.5e9},
                {"trade_date": "20260103", "open": 3040, "high": 3060, "low": 3020,
                 "close": 3055, "pct_chg": 0.49, "amount": 1.1e11, "vol": 9.1e9},
            ],
            "tushare",
            self.database,
        )
        data = self.client.get("/api/market/history").json()["data"]
        self.assertEqual(data["index_code"], "000300.SH")
        self.assertEqual(data["name"], "沪深300")  # INDEX_CODES 名称映射
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["rows"][0]["trade_date"], "20260102")  # 升序
        self.assertEqual(data["rows"][1]["close"], 3055)

    def test_history_never_fetches_on_empty_db(self):
        # 空库 + fetch=False：即使补拉会抛错也不应被调用（只读端点零网络）
        self.register()
        from ripple_tradePilot.data import market_service as ms

        with patch.object(
            ms.MarketDataService, "refresh_index_daily", side_effect=AssertionError("不应补拉")
        ):
            response = self.client.get("/api/market/history?index_code=000001.SH")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["count"], 0)

    def test_history_date_filter_and_limit(self):
        self.register()
        upsert_index_daily(
            "000300.SH",
            [{"trade_date": f"202601{i:02d}", "close": 3000 + i} for i in range(1, 11)],
            "tushare",
            self.database,
        )
        data = self.client.get(
            "/api/market/history?start=20260103&end=20260106&limit=2"
        ).json()["data"]
        # 闭区间 [03,06] 共 4 行，limit=2 取最近 2 行
        self.assertEqual([r["trade_date"] for r in data["rows"]], ["20260105", "20260106"])

    def test_history_bad_date_returns_422(self):
        self.register()
        self.assertEqual(
            self.client.get("/api/market/history?start=2026-01-02").status_code, 422
        )

    # --- /api/market/breadth ---
    def test_breadth_requires_login(self):
        self.assertEqual(self.client.get("/api/market/breadth").status_code, 401)

    def test_breadth_returns_seeded_rows(self):
        self.register()
        record_market_daily(
            "20260102",
            {"advancers": 2800, "decliners": 1900, "unchanged": 300, "limit_up": 45,
             "limit_down": 12, "total_amount": 9.8e11, "up_ratio": 0.56},
            "snapshot",
            self.database,
        )
        data = self.client.get("/api/market/breadth").json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rows"][0]["trade_date"], "20260102")
        self.assertEqual(data["rows"][0]["advancers"], 2800)
        self.assertEqual(data["rows"][0]["limit_up"], 45)

    def test_breadth_empty_db_returns_empty(self):
        # 增量积累制：新库无宽度历史 → 诚实返回空（不报错）
        self.register()
        data = self.client.get("/api/market/breadth").json()["data"]
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["rows"], [])

    # --- /api/industry/boards ---
    def test_industry_boards_requires_login(self):
        self.assertEqual(self.client.get("/api/industry/boards").status_code, 401)

    def test_industry_boards_lists_seeded(self):
        self.register()
        upsert_industry_boards(
            [{"board_code": "BK0475", "board_name": "银行"},
             {"board_code": "BK1027", "board_name": "小金属"}],
            "em", self.database,
        )
        data = self.client.get("/api/industry/boards").json()["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["boards"][0]["board_code"], "BK0475")
        self.assertEqual(data["boards"][0]["board_name"], "银行")

    # --- /api/industry/boards/{code}/bars ---
    def test_industry_board_bars(self):
        self.register()
        upsert_industry_board_bars(
            "BK0475",
            [{"trade_date": "20260102", "open": 1000, "high": 1020, "low": 990,
              "close": 1015, "pct_chg": 1.5, "amount": 8e9, "turnover_rate": 1.1},
             {"trade_date": "20260103", "open": 1015, "high": 1030, "low": 1010,
              "close": 1025, "pct_chg": 0.99, "amount": 7.5e9, "turnover_rate": 1.0}],
            "em", self.database,
        )
        data = self.client.get("/api/industry/boards/BK0475/bars").json()["data"]
        self.assertEqual(data["board_code"], "BK0475")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["rows"][1]["close"], 1025)
        self.assertAlmostEqual(data["rows"][1]["turnover_rate"], 1.0)

    def test_industry_board_bars_date_filter(self):
        self.register()
        upsert_industry_board_bars(
            "BK0475",
            [{"trade_date": f"202601{i:02d}", "close": 1000 + i} for i in range(1, 8)],
            "em", self.database,
        )
        data = self.client.get(
            "/api/industry/boards/BK0475/bars?start=20260103&end=20260105"
        ).json()["data"]
        self.assertEqual([r["trade_date"] for r in data["rows"]],
                         ["20260103", "20260104", "20260105"])

    def test_industry_unknown_board_returns_empty(self):
        self.register()
        data = self.client.get("/api/industry/boards/BK9999/bars").json()["data"]
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["rows"], [])

    # --- /api/industry/boards/{code}/members ---
    def test_industry_board_members_with_names(self):
        self.register()
        upsert_stock_catalog(
            [{"symbol": "600000.SH", "name": "浦发银行", "industry": "银行"}],
            "synth", self.database,
        )
        upsert_industry_membership("BK0475", ["600000.SH", "000001.SZ"], "20260102", "em", self.database)
        data = self.client.get("/api/industry/boards/BK0475/members").json()["data"]
        self.assertEqual(data["board_code"], "BK0475")
        self.assertEqual(data["count"], 2)
        by_symbol = {m["symbol"]: m for m in data["members"]}
        self.assertEqual(by_symbol["600000.SH"]["name"], "浦发银行")  # catalog 名称
        self.assertEqual(by_symbol["000001.SZ"]["name"], "000001.SZ")  # 无名回退 symbol
        self.assertEqual(by_symbol["600000.SH"]["as_of"], "20260102")


if __name__ == "__main__":
    unittest.main()
