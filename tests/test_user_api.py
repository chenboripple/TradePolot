import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.data.stock_service import StockDataService
from ripple_tradePilot.storage.database import (
    database_path,
    upsert_daily_bars,
    upsert_stock_catalog,
)

api_module = importlib.import_module("ripple_tradePilot.api.app")


class ApiStockDataService:
    stock_name = "测试银行"
    normalize_symbol = staticmethod(StockDataService.normalize_symbol)

    def refresh(self, value, initial_days=365):
        symbol = self.normalize_symbol(value)
        first_day = date.today() - timedelta(days=59)
        rows = []
        for index in range(60):
            close = 10 + index * 0.1
            rows.append(
                {
                    "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": 1000 + index,
                }
            )
        upsert_daily_bars(symbol, rows, "test", database_path())
        return {
            "symbol": symbol,
            "name": self.stock_name,
            "source": "test",
            "fetched_rows": len(rows),
            "total_rows": len(rows),
            "latest_date": date.today().isoformat(),
        }

    def refresh_catalog(self):
        upsert_stock_catalog(
            [
                {
                    "symbol": "600418.SH",
                    "name": "ST江淮",
                    "market": "主板",
                    "list_status": "L",
                    "list_date": "20010930",
                }
            ],
            "test",
            database_path(),
        )
        return {"count": 1, "source": "test"}

    def refresh_quotes(self):
        return {
            "count": 1,
            "source": "akshare",
            "quote_time": "2026-09-02T10:30:00",
        }


class UserApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "tradepilot.db"
        self.config = self.root / "config.yaml"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.config.write_text("symbols: []\nfutures: []\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "TRADEPILOT_BACKTEST_DB": str(self.database),
                "TRADEPILOT_CONFIG": str(self.config),
                "TRADEPILOT_DATA_DIR": str(self.data_dir),
            },
        )
        self.environment.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temp_dir.cleanup()

    def register(self, username):
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "strong-pass-123"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"]

    def create_strategy(self, name, visibility):
        upsert_stock_catalog(
            [{"symbol": "600309.SH", "name": "万华化学"}],
            "test",
            self.database,
        )
        response = self.client.post(
            "/api/strategies",
            json={
                "name": name,
                "asset_class": "stock",
                "symbol": "600309.SH",
                "profile": "组合投票",
                "parameters": {"ma_fast": 5, "ma_slow": 20},
                "visibility": visibility,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["item"]

    def test_stock_strategy_symbol_must_come_from_catalog(self):
        self.register("alice")

        response = self.client.post(
            "/api/strategies",
            json={
                "name": "未知股票策略",
                "asset_class": "stock",
                "symbol": "600999.SH",
                "profile": "组合投票",
                "parameters": {"ma_fast": 5, "ma_slow": 20},
                "visibility": "private",
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "股票标的必须来自全部数据池")

    def test_market_detail_uses_only_visible_strategy_for_same_symbol(self):
        self.register("alice")
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            added = self.client.post("/api/watchlist", json={"symbol": "600000"})
        self.assertEqual(added.status_code, 201, added.text)
        upsert_stock_catalog(
            [
                {"symbol": "600000.SH", "name": "测试银行"},
                {"symbol": "600309.SH", "name": "万华化学"},
            ],
            "test",
            self.database,
        )
        selected = self.client.post(
            "/api/strategies",
            json={
                "name": "敏感趋势策略",
                "asset_class": "stock",
                "symbol": "600000.SH",
                "profile": "组合投票",
                "parameters": {
                    "ma_fast": 2,
                    "ma_slow": 8,
                    "rsi_period": 6,
                    "rsi_oversold": 20,
                    "rsi_overbought": 101,
                    "bb_period": 10,
                    "bb_std": 2,
                    "vote_threshold": 1,
                },
                "visibility": "private",
            },
        ).json()["item"]
        other = self.create_strategy("其他股票策略", "private")

        detail = self.client.get(
            f"/api/markets/600000.SH?strategy_id={selected['id']}"
        )

        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["strategy_profile"], "敏感趋势策略")
        self.assertEqual(detail.json()["parameters"]["ma_fast"], 2)
        self.assertEqual(detail.json()["recommendation"], "BUY")
        mismatch = self.client.get(
            f"/api/markets/600000.SH?strategy_id={other['id']}"
        )
        self.assertEqual(mismatch.status_code, 404, mismatch.text)

        self.client.post("/api/auth/logout")
        guest = self.client.get(
            f"/api/markets/600000.SH?strategy_id={selected['id']}"
        )
        self.assertEqual(guest.status_code, 401, guest.text)

    def test_watchlist_default_strategy_drives_dashboard_and_can_be_reset(self):
        self.register("alice")
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            added = self.client.post("/api/watchlist", json={"symbol": "600000"})
        self.assertEqual(added.status_code, 201, added.text)
        upsert_stock_catalog(
            [
                {"symbol": "600000.SH", "name": "测试银行"},
                {"symbol": "600309.SH", "name": "万华化学"},
            ],
            "test",
            self.database,
        )
        selected = self.client.post(
            "/api/strategies",
            json={
                "name": "观察池默认趋势",
                "asset_class": "stock",
                "symbol": "600000.SH",
                "profile": "组合投票",
                "parameters": {
                    "ma_fast": 2,
                    "ma_slow": 8,
                    "rsi_period": 6,
                    "rsi_oversold": 20,
                    "rsi_overbought": 101,
                    "bb_period": 10,
                    "bb_std": 2,
                    "vote_threshold": 1,
                },
                "visibility": "private",
            },
        ).json()["item"]
        other = self.create_strategy("其他股票策略", "private")

        mismatch = self.client.patch(
            "/api/watchlist/600000.SH/default-strategy",
            json={"strategy_id": other["id"]},
        )
        self.assertEqual(mismatch.status_code, 404, mismatch.text)

        changed = self.client.patch(
            "/api/watchlist/600000.SH/default-strategy",
            json={"strategy_id": selected["id"]},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(
            changed.json()["item"]["default_strategy_id"], selected["id"]
        )
        watchlist = self.client.get("/api/watchlist").json()["items"]
        self.assertEqual(watchlist[0]["default_strategy_id"], selected["id"])
        market = self.client.get("/api/dashboard").json()["markets"][0]
        self.assertEqual(market["default_strategy_id"], selected["id"])
        self.assertEqual(market["strategy_profile"], "观察池默认趋势")
        self.assertEqual(market["parameters"]["ma_fast"], 2)
        self.assertEqual(market["recommendation"], "BUY")
        system_preview = self.client.get(
            "/api/markets/600000.SH?system_strategy=true"
        ).json()
        self.assertEqual(system_preview["default_strategy_id"], selected["id"])
        self.assertEqual(system_preview["strategy_profile"], "默认组合策略")
        self.assertEqual(system_preview["parameters"]["ma_fast"], 5)

        reset = self.client.patch(
            "/api/watchlist/600000.SH/default-strategy",
            json={"strategy_id": None},
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertIsNone(reset.json()["item"]["default_strategy_id"])
        reset_market = self.client.get("/api/dashboard").json()["markets"][0]
        self.assertIsNone(reset_market["default_strategy_id"])
        self.assertEqual(reset_market["strategy_profile"], "默认组合策略")

    def test_guests_only_receive_public_dashboard_sections(self):
        dashboard = self.client.get("/api/dashboard")

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn("strategies", dashboard.json())
        self.assertNotIn("backtests", dashboard.json())
        self.assertEqual(self.client.get("/api/strategies").status_code, 401)
        self.assertEqual(self.client.get("/api/backtests").status_code, 401)

    def test_guest_landing_public_data_chain(self):
        """游客首屏链路：配置观察池 + 目录 + 日线齐备时，匿名可拿到完整公开数据；
        账号功能（市场总览/策略/回测）注册墙保留。"""
        self.config.write_text(
            'symbols:\n  - code: "600000.SH"\n    name: "测试银行"\n    asset_class: stock\nfutures: []\n',
            encoding="utf-8",
        )
        upsert_stock_catalog(
            [{"symbol": "600000.SH", "name": "测试银行"}], "test", self.database
        )
        ApiStockDataService().refresh("600000")

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        markets = dashboard.json()["markets"]
        self.assertEqual([item["symbol"] for item in markets], ["600000.SH"])
        self.assertTrue(markets[0]["bars"], "游客落地页依赖 dashboard 内嵌 K 线")

        detail = self.client.get("/api/markets/600000.SH?system_strategy=true")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["bars"])

        self.assertEqual(self.client.get("/api/stocks").status_code, 200)
        self.assertEqual(self.client.get("/api/market/overview").status_code, 401)

    def test_public_strategies_are_shared_but_private_strategies_are_not(self):
        alice = self.register("alice")
        self.assertEqual(alice["role"], "admin")
        public_strategy = self.create_strategy("开放策略", "public")
        self.create_strategy("私有策略", "private")
        self.client.post("/api/auth/logout")

        bob = self.register("bob")
        self.assertEqual(bob["role"], "user")
        items = self.client.get("/api/strategies").json()["items"]

        self.assertEqual([item["name"] for item in items], ["开放策略"])
        self.assertEqual(items[0]["owner"], alice["username"])
        self.assertFalse(items[0]["is_owner"])
        forbidden = self.client.patch(
            f"/api/strategies/{public_strategy['id']}/visibility",
            json={"visibility": "private"},
        )
        self.assertEqual(forbidden.status_code, 404)

    def test_configured_owner_can_edit_system_strategies(self):
        profile = {
            "kind": "combo_vote",
            "ma_fast": 5,
            "ma_slow": 20,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "bb_period": 20,
            "bb_std": 2,
            "vote_threshold": 2,
        }
        self.config.write_text(
            yaml.safe_dump(
                {
                    "strategy_owner": "chenboripple@gmail.com",
                    "symbols": [
                        {
                            "code": "002022.SZ",
                            "name": "科华生物",
                            "strategy_profile": "科华生物策略",
                        },
                        {
                            "code": "600309.SH",
                            "name": "万华化学",
                            "strategy_profile": "万华化学策略",
                        },
                    ],
                    "strategy_profiles": {
                        "科华生物策略": profile,
                        "万华化学策略": profile,
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        service = ApiStockDataService()
        service.refresh("002022.SZ")
        service.refresh("600309.SH")

        self.register("chenboripple@gmail.com")
        items = self.client.get("/api/strategies").json()["items"]
        system_items = [item for item in items if item["is_system"]]

        self.assertEqual(
            {item["symbol"] for item in system_items},
            {"002022.SZ", "600309.SH"},
        )
        self.assertTrue(all(item["is_owner"] for item in system_items))
        kehua = next(item for item in system_items if item["symbol"] == "002022.SZ")
        updated_parameters = {**kehua["parameters"], "ma_fast": 2, "ma_slow": 8}
        changed = self.client.patch(
            f"/api/strategies/{kehua['id']}",
            json={
                "name": kehua["name"],
                "profile": kehua["profile"],
                "parameters": updated_parameters,
                "visibility": "public",
            },
        )

        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["item"]["parameters"]["ma_fast"], 2)
        market = self.client.get("/api/dashboard").json()["markets"]
        kehua_market = next(item for item in market if item["symbol"] == "002022.SZ")
        self.assertEqual(kehua_market["parameters"]["ma_fast"], 2)

        self.client.post("/api/auth/logout")
        self.register("other@example.com")
        forbidden = self.client.patch(
            f"/api/strategies/{kehua['id']}",
            json={
                "name": kehua["name"],
                "profile": kehua["profile"],
                "parameters": updated_parameters,
                "visibility": "private",
            },
        )
        self.assertEqual(forbidden.status_code, 404)

    def test_backtests_are_scoped_to_current_user(self):
        alice = self.register("alice")
        self.client.post("/api/auth/logout")
        bob = self.register("bob")
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO backtest_results (symbol, name, user_id) VALUES (?, ?, ?)",
                ("600309.SH", "Alice record", alice["id"]),
            )
            connection.execute(
                "INSERT INTO backtest_results (symbol, name, user_id) VALUES (?, ?, ?)",
                ("002022.SZ", "Bob record", bob["id"]),
            )

        items = self.client.get("/api/backtests").json()["items"]

        self.assertEqual([item["name"] for item in items], ["Bob record"])

    def test_email_can_be_used_as_case_insensitive_account_name(self):
        user = self.register("Pilot.User+cn@Example.COM")

        self.assertEqual(user["username"], "pilot.user+cn@example.com")
        self.client.post("/api/auth/logout")
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "PILOT.USER+CN@EXAMPLE.COM",
                "password": "strong-pass-123",
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["user"]["username"], "pilot.user+cn@example.com")

    def test_watchlist_is_user_scoped_and_daily_bars_are_visible(self):
        self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "600000"}).status_code, 401)
        self.register("alice")
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            added = self.client.post("/api/watchlist", json={"symbol": "600000"})
            self.assertEqual(added.status_code, 201, added.text)
            refreshed = self.client.post("/api/watchlist/600000.SH/refresh")
            self.assertEqual(refreshed.status_code, 200, refreshed.text)

        items = self.client.get("/api/watchlist").json()["items"]
        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual([item["symbol"] for item in items], ["600000.SH"])
        self.assertEqual([item["symbol"] for item in dashboard["markets"]], ["600000.SH"])
        self.assertTrue(dashboard["markets"][0]["user_added"])

        self.client.post("/api/auth/logout")
        self.register("bob")
        self.assertEqual(self.client.get("/api/watchlist").json()["items"], [])
        self.assertEqual(self.client.get("/api/dashboard").json()["markets"], [])
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            forbidden = self.client.post("/api/watchlist/600000.SH/refresh")
        self.assertEqual(forbidden.status_code, 404)

    def test_refresh_keeps_name_and_archived_stock_can_be_restored_without_refresh(self):
        self.register("alice")
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            added = self.client.post("/api/watchlist", json={"symbol": "600000"})
        self.assertEqual(added.status_code, 201, added.text)

        ApiStockDataService.stock_name = "更新后的银行"
        try:
            with patch.object(api_module, "StockDataService", ApiStockDataService):
                refreshed = self.client.post("/api/watchlist/600000.SH/refresh")
        finally:
            ApiStockDataService.stock_name = "测试银行"
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(
            self.client.get("/api/watchlist").json()["items"][0]["name"],
            "测试银行",
        )
        self.assertEqual(
            self.client.get("/api/dashboard").json()["markets"][0]["name"],
            "测试银行",
        )

        removed = self.client.delete("/api/watchlist/600000.SH")
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(self.client.get("/api/dashboard").json()["markets"], [])
        archived = self.client.get("/api/stocks").json()["items"][0]
        self.assertEqual(archived["name"], "测试银行")
        self.assertFalse(archived["is_watched"])

        with patch.object(api_module, "StockDataService", ApiStockDataService):
            forbidden = self.client.post("/api/watchlist/600000.SH/refresh")
        self.assertEqual(forbidden.status_code, 404, forbidden.text)

        class CachedOnlyStockDataService(ApiStockDataService):
            def refresh(self, value, initial_days=365):
                raise AssertionError("restoring an archived stock must not refresh data")

        with patch.object(api_module, "StockDataService", CachedOnlyStockDataService):
            restored = self.client.post(
                "/api/watchlist", json={"symbol": "600000.SH"}
            )
        self.assertEqual(restored.status_code, 201, restored.text)
        self.assertIsNone(restored.json()["data"])
        self.assertEqual(
            self.client.get("/api/dashboard").json()["markets"][0]["name"],
            "测试银行",
        )

    def test_full_catalog_refresh_updates_names_and_lists_all_stocks(self):
        self.register("alice")
        upsert_stock_catalog(
            [{"symbol": "600418.SH", "name": "江淮汽车"}],
            "test",
            self.database,
        )
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            response = self.client.post("/api/stocks/refresh")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["count"], 1)
        catalog = self.client.get("/api/stocks").json()["items"]
        self.assertEqual(
            [(item["symbol"], item["name"]) for item in catalog],
            [("600418.SH", "ST江淮")],
        )
        self.assertEqual(catalog[0]["market"], "主板")

    def test_full_market_quote_refresh_requires_login_and_returns_snapshot_time(self):
        unauthorized = self.client.post("/api/stocks/quotes/refresh")
        self.assertEqual(unauthorized.status_code, 401)
        self.register("alice")

        with patch.object(api_module, "StockDataService", ApiStockDataService):
            response = self.client.post("/api/stocks/quotes/refresh")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["count"], 1)
        self.assertEqual(
            response.json()["data"]["quote_time"], "2026-09-02T10:30:00"
        )

    def test_new_stock_is_not_saved_when_name_cannot_be_resolved(self):
        class UnnamedStockDataService(ApiStockDataService):
            stock_name = "600000.SH"

        self.register("alice")
        with patch.object(api_module, "StockDataService", UnnamedStockDataService):
            response = self.client.post("/api/watchlist", json={"symbol": "600000"})

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("无法解析股票名称", response.json()["detail"])
        self.assertEqual(self.client.get("/api/watchlist").json()["items"], [])

    def test_configured_stock_can_be_archived_and_restored(self):
        self.config.write_text(
            "symbols:\n"
            "  - code: 600001.SH\n"
            "    name: 默认银行\n"
            "    asset_class: stock\n"
            "futures: []\n",
            encoding="utf-8",
        )
        ApiStockDataService().refresh("600001")
        self.register("alice")

        before = self.client.get("/api/dashboard").json()["markets"]
        self.assertEqual([item["symbol"] for item in before], ["600001.SH"])
        removed = self.client.delete("/api/watchlist/600001.SH")
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(self.client.get("/api/dashboard").json()["markets"], [])
        catalog = self.client.get("/api/stocks").json()["items"]
        self.assertEqual(len(catalog), 1)
        self.assertTrue(catalog[0]["is_default"])
        self.assertFalse(catalog[0]["is_watched"])

        class CachedOnlyStockDataService(ApiStockDataService):
            def refresh(self, value, initial_days=365):
                raise AssertionError("restoring a configured stock must not refresh data")

        with patch.object(api_module, "StockDataService", CachedOnlyStockDataService):
            restored = self.client.post(
                "/api/watchlist", json={"symbol": "600001.SH"}
            )
        self.assertEqual(restored.status_code, 201, restored.text)
        self.assertEqual(
            [item["symbol"] for item in self.client.get("/api/dashboard").json()["markets"]],
            ["600001.SH"],
        )


if __name__ == "__main__":
    unittest.main()
