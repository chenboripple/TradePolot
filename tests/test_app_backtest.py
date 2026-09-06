"""POST /api/backtest 端点的离线测试（tmp DB + mock 行情，不联网）。"""
import importlib
import json
import math
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.data.stock_service import StockDataUnavailableError
from ripple_tradePilot.storage.database import database_path, upsert_daily_bars

api_module = importlib.import_module("ripple_tradePilot.api.app")

SYMBOL = "002022.SZ"
PASSWORD = "strong-pass-123"


def _seed_rows(count=140, start_day=None):
    """正弦震荡的合法日线：日波动小，不会触发涨跌停拦截。"""
    first_day = start_day or (date.today() - timedelta(days=count + 10))
    rows = []
    for index in range(count):
        close = 10.0 + 1.5 * math.sin(index / 8.0)
        rows.append(
            {
                "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "vol": 1000 + index,
            }
        )
    return rows


class WebBacktestApiTest(unittest.TestCase):
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
                # 测试机不配置 tushare token，保证基准对比走优雅降级、不联网
                "TUSHARE_TOKEN": "",
            },
        )
        self.environment.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temp_dir.cleanup()

    def register(self, username="alice"):
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_requires_login(self):
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma"}
        )
        self.assertEqual(response.status_code, 401)

    def _seed_system_strategy(self, parameters, symbol=SYMBOL):
        """在 tmp DB 预置一条系统策略（system_key=stock:<symbol>），供 profile=system 解析。"""
        with sqlite3.connect(database_path()) as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO strategies (
                    user_id, name, asset_class, symbol, profile,
                    parameters_json, visibility, system_key
                ) VALUES (?, ?, 'stock', ?, 'combo_vote', ?, 'public', ?)
                """,
                (
                    user_id,
                    f"系统:{symbol}",
                    symbol,
                    json.dumps(parameters, ensure_ascii=False),
                    f"stock:{symbol}",
                ),
            )

    def test_backtest_options_metadata_matches_registry(self):
        """/api/meta/backtest-options 是前端下拉的单一来源（A5 四方一致）：
        注册表 == meta strategies == 标签表 == 请求模型 Literal == params_schema 键集；匿名可用。"""
        from typing import get_args

        from ripple_tradePilot.signals.backtest_profile import (
            BACKTEST_STRATEGIES,
            params_schema,
        )

        response = self.client.get("/api/meta/backtest-options")  # 未注册未登录
        self.assertEqual(response.status_code, 200)
        data = response.json()

        strategy_values = [item["value"] for item in data["strategies"]]
        # 四方对齐：共享注册表 == meta 下发 == 标签表 == 请求模型 Literal
        self.assertEqual(set(strategy_values), set(BACKTEST_STRATEGIES))
        self.assertEqual(set(strategy_values), set(api_module._BACKTEST_STRATEGY_LABELS))
        self.assertEqual(
            set(strategy_values),
            set(get_args(api_module.BacktestRequest.model_fields["strategy"].annotation)),
        )
        # 第四方：params_schema 键集 == 注册表，且 meta 逐策略下发的 schema 与单一来源一致
        schema = params_schema()
        self.assertEqual(set(schema), set(BACKTEST_STRATEGIES))
        for item in data["strategies"]:
            self.assertEqual(
                item["params_schema"],
                schema[item["value"]],
                f"{item['value']} 的 params_schema 与单一来源不一致",
            )
        # A5 新增 combo_vote 进注册表与下拉
        self.assertIn("combo_vote", strategy_values)
        self.assertIn(data["default_strategy"], strategy_values)
        self.assertTrue(all(item["label"] for item in data["strategies"]))
        # profiles 下拉至少含 system 项（config 画像名在无 config 时为空）
        self.assertIn("system", [profile["value"] for profile in data["profiles"]])

        execution_values = [item["value"] for item in data["executions"]]
        self.assertEqual(
            set(execution_values),
            set(get_args(api_module.BacktestRequest.model_fields["execution"].annotation)),
        )
        self.assertEqual(set(execution_values), set(api_module._BACKTEST_EXECUTION_LABELS))
        self.assertIn(data["default_execution"], execution_values)

    def test_backtest_returns_metrics_and_curve(self):
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        response = self.client.post(
            "/api/backtest",
            json={
                "symbol": SYMBOL,
                "strategy": "ma",
                "bars": 100,
                "cash": 100000,
                "execution": "next_open",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]

        self.assertEqual(data["symbol"], SYMBOL)
        self.assertEqual(data["strategy"], "ma")
        self.assertEqual(data["execution"], "next_open")
        self.assertEqual(data["bar_count"], 100)
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe"):
            self.assertIn(key, data["metrics"])
        for key in ("num_trades", "win_rate", "total_fees"):
            self.assertIn(key, data["trades"])
        self.assertIsInstance(data["halted_by_drawdown"], bool)
        self.assertEqual(data["skipped_fills"], 0)
        self.assertEqual(len(data["equity_curve"]), 100)
        self.assertIn("disclaimer", data)
        # 未请求 benchmark 时不返回该字段，保持默认回测轻量
        self.assertNotIn("benchmark", data)
        for fill in data["fills"]:
            self.assertIn(fill["side"], ("BUY", "SELL"))

    def test_insufficient_data_returns_503_without_network(self):
        self.register()
        # 只有 30 根，不足 60 根门槛；refresh 被 mock，保证不联网
        upsert_daily_bars(SYMBOL, _seed_rows(30), "test", database_path())
        with patch.object(
            api_module.StockDataService,
            "refresh",
            side_effect=StockDataUnavailableError("行情源不可用"),
        ):
            response = self.client.post(
                "/api/backtest", json={"symbol": SYMBOL, "strategy": "rsi"}
            )
        self.assertEqual(response.status_code, 503, response.text)

    def test_backtest_result_recorded_for_ledger_page(self):
        """Web 回测成功后应写入回测记录，/api/backtests 能列出。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma", "bars": 100}
        )
        self.assertEqual(response.status_code, 200, response.text)

        listing = self.client.get("/api/backtests")
        self.assertEqual(listing.status_code, 200, listing.text)
        items = listing.json()["items"]
        self.assertEqual(len(items), 1)
        record = items[0]
        self.assertEqual(record["symbol"], SYMBOL)
        self.assertEqual(record["asset_class"], "stock")
        self.assertEqual(record["start_date"][:2], "20")
        self.assertTrue(record["end_date"])
        self.assertIsInstance(record["total_return"], float)
        self.assertIsInstance(record["win_rate"], float)
        # 扩展字段供前端"重跑"：记录回测入参
        self.assertIsInstance(record["id"], int)
        self.assertEqual(record["strategy_key"], "ma")
        self.assertEqual(record["bar_count"], 100)
        self.assertEqual(record["execution"], "next_open")

    def test_rejects_unknown_strategy(self):
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "bogus"}
        )
        self.assertEqual(response.status_code, 422)

    def test_backtest_benchmark_degrades_without_token(self):
        """benchmark=true 但无 tushare token：200 + available=false，不联网。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        with patch.object(
            api_module,
            "TushareDataLoader",
            side_effect=AssertionError("无 token 时不应构造 TushareDataLoader"),
        ):
            response = self.client.post(
                "/api/backtest",
                json={
                    "symbol": SYMBOL,
                    "strategy": "ma",
                    "bars": 100,
                    "benchmark": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        # 回测主体照常返回
        self.assertEqual(data["symbol"], SYMBOL)
        self.assertEqual(len(data["equity_curve"]), 100)
        # 基准优雅降级：available=false、空曲线、收益为 null
        self.assertEqual(
            data["benchmark"],
            {
                "code": "000300.SH",
                "name": "沪深300",
                "available": False,
                "return": None,
                "curve": [],
            },
        )

    def test_backtest_benchmark_curve_normalized(self):
        """benchmark=true 且指数数据可用：归一化曲线首点 1.0，全程离线（mock）。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        index_df = pd.DataFrame(
            {
                "trade_date": [
                    (date(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d")
                    for i in range(100)
                ],
                "close": [3000.0 + i * 5 for i in range(100)],
            }
        )

        class FakeLoader:
            def __init__(self, token, rate_limit_delay=1.5):
                self.token = token

            def get_index_bars(self, index_code, start_date, end_date):
                return index_df

        with patch.object(api_module, "load_config", return_value={}), patch.object(
            api_module, "get_tushare_token", return_value="fake-token"
        ), patch.object(api_module, "TushareDataLoader", FakeLoader):
            response = self.client.post(
                "/api/backtest",
                json={
                    "symbol": SYMBOL,
                    "strategy": "ma",
                    "bars": 100,
                    "benchmark": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        benchmark = response.json()["data"]["benchmark"]
        self.assertTrue(benchmark["available"])
        self.assertEqual(benchmark["code"], "000300.SH")
        self.assertEqual(benchmark["name"], "沪深300")
        self.assertEqual(len(benchmark["curve"]), 100)
        self.assertEqual(benchmark["curve"][0], {"date": "2026-01-01", "value": 1.0})
        self.assertAlmostEqual(
            benchmark["curve"][-1]["value"], (3000.0 + 99 * 5) / 3000.0, places=4
        )
        self.assertAlmostEqual(
            benchmark["return"], benchmark["curve"][-1]["value"] - 1, places=6
        )

    def test_delete_backtest_record_flow(self):
        """DELETE /api/backtests/{id}：本人 204，重复删/删他人记录均 404。"""
        self.register("alice")
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma", "bars": 100}
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = self.client.get("/api/backtests").json()["items"]
        self.assertEqual(len(items), 1)
        alice_record = items[0]

        # 注册第二个用户后，无权删除 alice 的记录
        self.register("bob")
        response = self.client.delete(f"/api/backtests/{alice_record['id']}")
        self.assertEqual(response.status_code, 404, response.text)

        # 切回 alice：首次删除 204，同一 id 再删 404
        login = self.client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.delete(f"/api/backtests/{alice_record['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        response = self.client.delete(f"/api/backtests/{alice_record['id']}")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("不存在", response.json()["detail"])
        self.assertEqual(self.client.get("/api/backtests").json()["items"], [])

    def test_delete_backtest_requires_login(self):
        response = self.client.delete("/api/backtests/1")
        self.assertEqual(response.status_code, 401)

    def test_backtest_detail_replay(self):
        """GET /api/backtests/{id} 返回保存的完整结果，供历史回放。"""
        self.register("alice")
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        created = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma", "bars": 100}
        )
        self.assertEqual(created.status_code, 200, created.text)
        live = created.json()["data"]
        record_id = self.client.get("/api/backtests").json()["items"][0]["id"]

        replay = self.client.get(f"/api/backtests/{record_id}")
        self.assertEqual(replay.status_code, 200, replay.text)
        data = replay.json()["data"]
        # 回放数据与即时回测一致：曲线、成交、指标齐全
        self.assertEqual(data["symbol"], SYMBOL)
        self.assertEqual(data["strategy"], "ma")
        self.assertEqual(len(data["equity_curve"]), 100)
        self.assertEqual(data["metrics"], live["metrics"])
        self.assertEqual(data["fills"], live["fills"])

    def test_backtest_detail_forbidden_for_other_user(self):
        """他人记录回放返回 404；不存在的 id 也返回 404。"""
        self.register("alice")
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma", "bars": 100}
        )
        record_id = self.client.get("/api/backtests").json()["items"][0]["id"]

        self.register("bob")
        response = self.client.get(f"/api/backtests/{record_id}")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.client.get("/api/backtests/999999").status_code, 404)

    def test_backtest_detail_requires_login(self):
        response = self.client.get("/api/backtests/1")
        self.assertEqual(response.status_code, 401)

    # -- A5：combo_vote 接入 + 参数传递 + provenance -------------------------

    def test_explicit_params_provenance_and_effect(self):
        """显式 params 生效：provenance 标 explicit + 参数回显，且不同参数产出不同成交。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        def run(params):
            response = self.client.post(
                "/api/backtest",
                json={"symbol": SYMBOL, "strategy": "ma", "bars": 120, "params": params},
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["data"]

        fast = run({"fast": 3, "slow": 8})
        slow = run({"fast": 30, "slow": 60})
        # provenance：来源 explicit + 实际参数原样回显
        self.assertEqual(fast["profile_source"], "explicit")
        self.assertEqual(fast["strategy_params"], {"fast": 3, "slow": 8})
        self.assertEqual(fast["strategy"], "ma")
        # 参数确实驱动回测：两组均线参数在同序列上成交明细不同
        self.assertTrue(fast["fills"], "MA(3,8) 在正弦序列上应产生成交")
        self.assertNotEqual(fast["fills"], slow["fills"], "不同均线参数应产出不同成交")

    def test_combo_vote_runs_with_default_profile(self):
        """combo_vote 无 params/profile：走缺省链（config 无绑定 → 全局默认画像），source=default。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest",
            json={"symbol": SYMBOL, "strategy": "combo_vote", "bars": 120},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["strategy"], "combo_vote")
        self.assertEqual(data["profile_source"], "default")
        self.assertIn("vote_threshold", data["strategy_params"])
        self.assertEqual(
            sorted(data["strategy_params"]["components"]), ["bollinger", "ma", "rsi"]
        )

    def test_combo_vote_explicit_threshold(self):
        """combo_vote 显式参数：vote_threshold + 组件参数生效并回显。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest",
            json={
                "symbol": SYMBOL, "strategy": "combo_vote", "bars": 120,
                "params": {"vote_threshold": 3, "ma_fast": 3, "ma_slow": 8},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["profile_source"], "explicit")
        self.assertEqual(data["strategy_params"]["vote_threshold"], 3)
        self.assertEqual(data["strategy_params"]["components"]["ma"], {"fast": 3, "slow": 8})

    def test_system_profile_resolution(self):
        """profile=system：读 tmp DB 预置的系统策略参数 → combo_vote，source=system。"""
        self.register()
        self._seed_system_strategy({"ma_fast": 3, "ma_slow": 9, "vote_threshold": 1})
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest",
            json={"symbol": SYMBOL, "strategy": "combo_vote", "bars": 120, "profile": "system"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["profile_source"], "system")
        self.assertEqual(data["strategy_params"]["vote_threshold"], 1)
        self.assertEqual(data["strategy_params"]["components"]["ma"], {"fast": 3, "slow": 9})

    def test_config_profile_resolution(self):
        """profile=名字：读 config.strategy_profiles → combo_vote，source=config:名字。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        config = {
            "symbols": [],
            "strategy_profiles": {
                "aggro": {"kind": "combo_vote", "ma_fast": 3, "ma_slow": 8, "vote_threshold": 1}
            },
        }
        with patch.object(api_module, "load_config", return_value=config):
            response = self.client.post(
                "/api/backtest",
                json={"symbol": SYMBOL, "strategy": "combo_vote", "bars": 120, "profile": "aggro"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["profile_source"], "config:aggro")
        self.assertEqual(data["strategy_params"]["vote_threshold"], 1)

    def test_illegal_param_rejected_422(self):
        """params 含策略白名单外的键 → 422（ma 不接受 window）。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest",
            json={"symbol": SYMBOL, "strategy": "ma", "bars": 100, "params": {"window": 5}},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_param_value_out_of_range_422(self):
        """params 值越界 → 422（vote_threshold le=3；fast ge=1）。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        over = self.client.post(
            "/api/backtest",
            json={"symbol": SYMBOL, "strategy": "combo_vote", "bars": 100,
                  "params": {"vote_threshold": 9}},
        )
        self.assertEqual(over.status_code, 422, over.text)
        zero = self.client.post(
            "/api/backtest",
            json={"symbol": SYMBOL, "strategy": "ma", "bars": 100, "params": {"fast": 0}},
        )
        self.assertEqual(zero.status_code, 422, zero.text)

    def test_unknown_profile_rejected_422(self):
        """profile 指向 config 中不存在的画像名 → 422。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        with patch.object(
            api_module, "load_config", return_value={"symbols": [], "strategy_profiles": {}}
        ):
            response = self.client.post(
                "/api/backtest",
                json={"symbol": SYMBOL, "strategy": "combo_vote", "bars": 100, "profile": "ghost"},
            )
        self.assertEqual(response.status_code, 422, response.text)

    def test_provenance_persisted_to_db(self):
        """A5 provenance 落库：backtest_results 写 run_kind/profile_source/params_json（v12 列）。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest",
            json={"symbol": SYMBOL, "strategy": "ma", "bars": 100,
                  "params": {"fast": 4, "slow": 12}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with sqlite3.connect(database_path()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT run_kind, profile_source, params_json, strategy_key, user_id "
                "FROM backtest_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["run_kind"], "backtest")
        self.assertEqual(row["profile_source"], "explicit")
        self.assertEqual(row["strategy_key"], "ma")
        self.assertIsNotNone(row["user_id"])  # Web 回测仍归属用户（记录页可见）
        params = json.loads(row["params_json"])
        self.assertEqual(params["strategy"], "ma")
        self.assertEqual(params["params"], {"fast": 4, "slow": 12})


if __name__ == "__main__":
    unittest.main()
