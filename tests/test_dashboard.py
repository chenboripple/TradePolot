import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from ripple_tradePilot.api.dashboard import DashboardService
from ripple_tradePilot.storage.database import upsert_daily_bars


class _DashboardFixture(unittest.TestCase):
    """共享夹具：tmp config.yaml（股票/期货各一 + combo_vote 画像）+ 60 根线性上涨 CSV。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "symbols": [
                        {
                            "code": "000001.SZ",
                            "name": "测试股票",
                            "asset_class": "stock",
                            "strategy_profile": "股票策略",
                        }
                    ],
                    "futures": [
                        {
                            "code": "IF2609.CFFEX",
                            "name": "测试期货",
                            "strategy_profile": "期货策略",
                        }
                    ],
                    "strategy_profiles": {"股票策略": self._profile()},
                    "futures_strategy_profiles": {"期货策略": self._profile()},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self._write_bars("000001.SZ", 10.0)
        self._write_bars("IF2609.CFFEX", 3800.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _profile():
        return {
            "kind": "combo_vote",
            "ma_fast": 5,
            "ma_slow": 20,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "bb_period": 20,
            "bb_std": 2,
        }

    def _write_bars(self, symbol, starting_price):
        path = self.data_dir / f"{symbol}.csv"
        first_day = date.today() - timedelta(days=59)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["trade_date", "open", "high", "low", "close", "vol"])
            writer.writeheader()
            for index in range(60):
                close = starting_price + index * 0.2
                writer.writerow(
                    {
                        "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                        "open": close - 0.1,
                        "high": close + 0.3,
                        "low": close - 0.3,
                        "close": close,
                        "vol": 1000 + index,
                    }
                )

    def _service(self, scorer=None):
        return DashboardService(
            data_dir=self.data_dir,
            config_path=self.config_path,
            backtest_db=self.root / "missing.db",
            scorer=scorer,
        )

class DashboardServiceTest(_DashboardFixture):
    def test_dashboard_separates_stocks_and_futures(self):
        dashboard = self._service().dashboard()

        self.assertEqual(dashboard["summary"]["by_asset"]["stock"]["configured"], 1)
        self.assertEqual(dashboard["summary"]["by_asset"]["future"]["configured"], 1)
        self.assertEqual({item["asset_class"] for item in dashboard["markets"]}, {"stock", "future"})

    def test_system_strategy_owner_comes_from_config(self):
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["strategy_owner"] = "chenboripple@gmail.com"
        self.config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
        )

        strategies = self._service().strategy_catalog()

        self.assertEqual(
            {item["owner"] for item in strategies},
            {"chenboripple@gmail.com"},
        )

    def test_future_market_detail_contains_chart_and_strategy(self):
        detail = self._service().market_detail("IF2609.CFFEX", limit=40)

        self.assertEqual(detail["asset_class"], "future")
        self.assertEqual(detail["exchange"], "CFFEX")
        self.assertEqual(len(detail["bars"]), 40)
        # 统一口径新增 CONFLICT 第四态
        self.assertIn(detail["recommendation"], {"BUY", "SELL", "HOLD", "CONFLICT"})
        self.assertIsNotNone(detail["indicators"]["ma_slow"])

    def test_market_detail_conflict_on_linear_rise(self):
        # 线性上涨：MA 多头(BUY)、RSI=100 超买(SELL)、布林不触轨 → 阈值 2 下 strict 判冲突。
        # 旧 dashboard majority 规则会把它误判成 BUY（buy>sell），统一口径修正为 CONFLICT。
        detail = self._service().market_detail("000001.SZ")
        self.assertEqual(detail["recommendation"], "CONFLICT")
        self.assertTrue(detail["is_conflict"])
        self.assertEqual(detail["buy_count"], 1)
        self.assertEqual(detail["sell_count"], 1)
        self.assertEqual(detail["votes"], {"ma": "BUY", "rsi": "SELL", "bollinger": "HOLD"})
        self.assertIn("信号冲突", detail["reason"])

    def test_confidence_removed_vote_ratio_present(self):
        detail = self._service().market_detail("000001.SZ")
        # 递归查（序列化整棵树）：D5 的 forecast 块是嵌套结构，只查顶层会让
        # forecast.confidence 这种"伪概率换个地方回潮"悄悄溜过去
        self.assertNotIn("confidence", json.dumps(detail, ensure_ascii=False, default=str))
        self.assertIn("vote_ratio", detail)
        # 1 票 / 3 组件 → 33%
        self.assertEqual(detail["vote_ratio"], 33)
        self.assertEqual(detail["profile_source"], "config")

    def test_dashboard_honors_strict_voting_rule(self):
        """A2 黄金一致性：dashboard 输出严格遵守 signals 的 strict 规则与 vote_ratio 契约。"""
        for code in ("000001.SZ", "IF2609.CFFEX"):
            for threshold in (1, 2, 3):
                detail = self._service().market_detail(
                    code,
                    profile_override={**self._profile(), "vote_threshold": threshold},
                )
                votes = list(detail["votes"].values())
                buy = sum(v == "BUY" for v in votes)
                sell = sum(v == "SELL" for v in votes)
                if buy >= threshold and sell == 0:
                    expected = "BUY"
                elif sell >= threshold and buy == 0:
                    expected = "SELL"
                elif buy or sell:
                    expected = "CONFLICT"
                else:
                    expected = "HOLD"
                self.assertEqual(detail["recommendation"], expected, (code, threshold))
                self.assertEqual(detail["buy_count"], buy)
                self.assertEqual(detail["sell_count"], sell)
                self.assertEqual(
                    detail["vote_ratio"], round(max(buy, sell) / len(votes) * 100)
                )
                self.assertEqual(detail["is_conflict"], expected == "CONFLICT")

    def test_market_detail_prefers_database_daily_bars(self):
        database = self.root / "market.db"
        first_day = date.today() - timedelta(days=59)
        rows = []
        for index in range(60):
            close = 50 + index
            rows.append(
                {
                    "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                    "open": close - 1,
                    "high": close + 1,
                    "low": close - 2,
                    "close": close,
                    "vol": 2000 + index,
                }
            )
        upsert_daily_bars("000001.SZ", rows, "test", database)

        detail = DashboardService(
            data_dir=self.data_dir,
            config_path=self.config_path,
            backtest_db=database,
        ).market_detail("000001.SZ")

        self.assertEqual(detail["price"], 109)
        self.assertEqual(detail["total_rows"], 60)

    def test_market_detail_recalculates_with_selected_strategy(self):
        service = self._service()
        default = service.market_detail("000001.SZ")
        selected = service.market_detail(
            "000001.SZ",
            profile_override={
                "ma_fast": 2,
                "ma_slow": 8,
                "rsi_period": 6,
                "rsi_oversold": 20,
                "rsi_overbought": 101,
                "bb_period": 10,
                "bb_std": 2,
                "vote_threshold": 1,
            },
            strategy_profile="敏感趋势策略",
        )

        self.assertEqual(selected["strategy_profile"], "敏感趋势策略")
        self.assertEqual(selected["parameters"]["ma_fast"], 2)
        self.assertNotEqual(selected["indicators"]["ma_fast"], default["indicators"]["ma_fast"])
        self.assertEqual(selected["recommendation"], "BUY")


_FORECAST_PAYLOAD = {
    "model_id": "logreg-win5-abc1234567",
    "target": "win5",
    "horizon_days": 5,
    "as_of": "20260801",
    "p_win": 0.5812,
    "expected_net_return": 0.012345,
    "downside_mae": -0.031,
    "status": "promoted",
    "stale": False,
    "return_basis": "主模型 OOS 历史中 p≥0.581 子集的已实现平均净收益（n=120，覆盖率 16.0%）",
    "oos_n_signals": 120,
    "oos_coverage": 0.16,
    "oos_win_rate": 0.58,
    "warnings": ["（预估基准日 20260801 = 库内最新已收盘交易日，非盘中实时）"],
}


class _StubForecast:
    """替身 ScoreResult：只提供 dashboard 用到的 ``to_dict()``。"""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


class _StubScorer:
    """替身 SignalScorer（本仓测试不用 mock 库，手写鸭子类型替身更直白）。

    ``payload=None`` 模拟"该标的无数据/无在位模型"，``error`` 模拟打分抛错。
    """

    def __init__(self, payload=None, error=None):
        self.calls = []
        self._payload = payload
        self._error = error

    def score_symbol(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        if self._error is not None:
            raise self._error
        return None if self._payload is None else _StubForecast(self._payload)


class ForecastBlockTest(_DashboardFixture):
    """D5：``market_detail`` 的 ``forecast`` 块（注入替身打分器，零 sklearn 依赖）。"""

    def test_no_scorer_forecast_is_null(self):
        detail = self._service().market_detail("000001.SZ")
        # 键必须存在（前端不判 undefined），值为 null → 只显示 vote_ratio
        self.assertIn("forecast", detail)
        self.assertIsNone(detail["forecast"])
        self.assertEqual(detail["vote_ratio"], 33)

    def test_forecast_payload_passthrough(self):
        scorer = _StubScorer(_FORECAST_PAYLOAD)
        detail = self._service(scorer=scorer).market_detail("000001.SZ")
        self.assertEqual(detail["forecast"], _FORECAST_PAYLOAD)
        # 打分走看板同一个库文件（与其余读库口径一致）
        self.assertEqual(scorer.calls, [("000001.SZ", {"db_path": self.root / "missing.db"})])
        # 规则票与模型概率并列，不互相冒充
        self.assertEqual(detail["vote_ratio"], 33)
        self.assertEqual(detail["forecast"]["p_win"], 0.5812)

    def test_scorer_returning_none_degrades_quietly(self):
        detail = self._service(scorer=_StubScorer(None)).market_detail("000001.SZ")
        self.assertIsNone(detail["forecast"])
        self.assertEqual(detail["recommendation"], "CONFLICT")

    def test_scorer_error_cannot_break_dashboard(self):
        scorer = _StubScorer(error=RuntimeError("joblib 炸了"))
        detail = self._service(scorer=scorer).market_detail("000001.SZ")
        self.assertEqual(scorer.calls[0][0], "000001.SZ")
        self.assertIsNone(detail["forecast"])
        self.assertIn("recommendation", detail)
        self.assertEqual(len(detail["bars"]), 60)  # 夹具只有 60 根，详情其余部分完好

    def test_list_endpoints_skip_scoring(self):
        # 列表型调用逐标的打分是 N 倍读库开销，而列表视图不展示预估 → 必须关掉
        scorer = _StubScorer(_FORECAST_PAYLOAD)
        service = self._service(scorer=scorer)
        dashboard = service.dashboard()
        catalog = service.strategy_catalog()
        self.assertEqual(scorer.calls, [])
        self.assertTrue(dashboard["markets"])
        self.assertTrue(catalog)

    def test_with_forecast_false_on_detail(self):
        scorer = _StubScorer(_FORECAST_PAYLOAD)
        detail = self._service(scorer=scorer).market_detail("000001.SZ", with_forecast=False)
        self.assertIsNone(detail["forecast"])
        self.assertEqual(scorer.calls, [])


if __name__ == "__main__":
    unittest.main()
