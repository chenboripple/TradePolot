"""A3 monitor 日线化回归：通知门控 + provisional/final 去重 + 分钟零调用 + 收盘幂等 + 预警通道隔离。

隔离策略：通知门控/去重/台账直接喂手工构造的 ``DailyEvaluation`` 给 ``_consume_evaluation``
（绕开取数与网络）；日线路由用临时库灌合成日线后跑 ``check_symbol``；收盘幂等把
``_finalize_after_close`` 换成记录用 async 桩，验证 ``kv_store`` 防重；价格预警用纯快照行驱动
``_dispatch_price_alerts``。全部离线、确定性。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import synth

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.monitor.daily_eval import DailyEvaluation, today_str
from ripple_tradePilot.monitor.main import FINALIZE_KV_KEY, REC_BUY, MarketMonitor
from ripple_tradePilot.monitor.price_alert import PriceAlertConfig
from ripple_tradePilot.signals.components import ComponentVote
from ripple_tradePilot.signals.voting import VoteDecision, VoteEvent
from ripple_tradePilot.storage.database import (
    init_database,
    load_market_daily,
    upsert_daily_bars,
    upsert_stock_quotes,
)
from ripple_tradePilot.storage.signal_ledger import kv_get, list_signals

CONFIG = """
symbols:
  - code: "600000.SH"
    name: "浦发银行"
futures: []
tushare:
  token: "test-token"
monitor:
  interval_seconds: 300
  report_interval_seconds: 3600
notifiers:
  console:
    enabled: false
  feishu:
    enabled: false
risk:
  stop_loss_pct: 0.1
  take_profit_pct: 0.25
"""

TS = datetime(2026, 9, 4)
SYMBOL = "600000.SH"


class FakeFeishu:
    """记录 send/send_card/_send_text 的假飞书通知器。"""

    def __init__(self):
        self.signals = []   # send() 的 kwargs
        self.posted = []    # send_card()
        self.texts = []     # _send_text()

    def send(self, **kwargs):
        self.signals.append(kwargs)
        return True

    def send_card(self, content):
        self.posted.append(content)
        return True

    def _send_text(self, content):
        self.texts.append(content)
        return True


class FakeStockService:
    """记录调用的假数据服务（替代 StockDataService，避免收盘例程/快照刷新触网）。"""

    def __init__(self):
        self.refresh_quotes_calls = 0
        self.refreshed = []  # refresh() 收到的 (symbol, initial_days)

    def refresh_quotes(self):
        self.refresh_quotes_calls += 1
        return {"updated": 0}

    def refresh(self, symbol, initial_days=365):
        self.refreshed.append((symbol, initial_days))
        return {"symbol": symbol, "data_version": "synth|20260904|now", "rebased": False}


# ---------------------------------------------------------------------------
# 手工构造的评估结果（隔离门控逻辑，不依赖取数）
# ---------------------------------------------------------------------------
def _bar() -> Bar:
    return Bar(timestamp=TS, open=10.0, high=11.0, low=9.0, close=10.5, volume=1000.0)


def _buy_decision() -> VoteDecision:
    return VoteDecision(
        timestamp=TS, recommendation="BUY", buy_count=2, sell_count=0, vote_threshold=2,
        components=(
            ComponentVote(name="ma", kind="ma", side=Side.BUY),
            ComponentVote(name="macd", kind="macd", side=Side.BUY),
        ),
    )


def _conflict_decision() -> VoteDecision:
    return VoteDecision(
        timestamp=TS, recommendation="CONFLICT", buy_count=2, sell_count=1, vote_threshold=2,
        components=(
            ComponentVote(name="ma", kind="ma", side=Side.BUY),
            ComponentVote(name="macd", kind="macd", side=Side.BUY),
            ComponentVote(name="rsi", kind="rsi", side=Side.SELL),
        ),
    )


def _eval(decision, *, provisional, events, n_bars=1, trade_date="20260904") -> DailyEvaluation:
    return DailyEvaluation(
        symbol=SYMBOL, n_bars=n_bars, provisional=provisional, rebased=False,
        rebase_factor=1.0, trade_date=trade_date, insufficient=False,
        decision=decision, events=tuple(events), bars=(_bar(),) * n_bars,
    )


def _buy_event(decision, index=0) -> VoteEvent:
    return VoteEvent(index=index, timestamp=TS, side=Side.BUY, decision=decision,
                     previous_recommendation="HOLD")


class _MonitorBase(unittest.TestCase):
    """临时 config + 临时库 + 离线 MarketMonitor（注入 FakeFeishu）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config_path = root / "config.yaml"
        self.config_path.write_text(CONFIG, encoding="utf-8")
        self.db_path = root / "tradepilot.db"
        self._env = patch.dict(os.environ, {
            "TRADEPILOT_BACKTEST_DB": str(self.db_path),
            "TRADEPILOT_CONFIG": str(self.config_path),
        })
        self._env.start()
        init_database(self.db_path)
        self.monitor = MarketMonitor(str(self.config_path))
        self.fake = FakeFeishu()
        self.monitor.feishu_notifier = self.fake
        self.monitor.notifier.feishu = self.fake

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _seed(self, symbol, count, seed=5):
        bars = synth.daily_bars(count, seed=seed)
        upsert_daily_bars(symbol, synth.daily_rows(bars), "synth", self.db_path)

    def _consume(self, evaluation, *, results=None, write_ledger=False, profile_name=None,
                 forecast=None):
        out = [] if results is None else results
        self.monitor._consume_evaluation(
            SYMBOL, "浦发银行", profile_name, evaluation, out,
            write_ledger=write_ledger, data_version=None, forecast=forecast,
        )
        return out


class NotificationGatingTest(_MonitorBase):
    def test_conflict_does_not_notify(self):
        # 2 买 1 卖 → strict 投票判 CONFLICT，无边沿事件 → 不推送（修复旧"买入分支不查 sell_count"）
        results = self._consume(_eval(_conflict_decision(), provisional=True, events=()))
        self.assertEqual(len(self.fake.signals), 0)
        self.assertEqual(len(results), 1)
        self.assertIn("观望", results[0]["recommendation"])
        self.assertIn("冲突", results[0]["recommendation"])

    def test_stale_event_does_not_notify(self):
        # 事件落在历史 bar（index 2 < n_bars-1=4）→ 非当日新事件 → 不推送
        dec = _buy_decision()
        stale = _eval(dec, provisional=False, events=(_buy_event(dec, index=2),), n_bars=5)
        self._consume(stale)
        self.assertEqual(len(self.fake.signals), 0)

    def test_new_buy_event_notifies_once_with_annotations(self):
        dec = _buy_decision()
        ev = _eval(dec, provisional=True, events=(_buy_event(dec, index=0),), n_bars=1)
        results = self._consume(ev)
        self.assertEqual(len(self.fake.signals), 1)
        sent = self.fake.signals[0]
        # SignalNotifier.send 把 provisional/投票/触发组件折进 extra_info 再调 feishu.send
        info = sent["extra_info"]
        self.assertEqual(sent["side"], Side.BUY)
        self.assertTrue(info["provisional"])
        self.assertIn("ma", info["note"])
        self.assertIn("macd", info["note"])
        self.assertIn("2涨", info["note"])
        self.assertIn("阈值 2", info["note"])
        self.assertEqual(results[0]["recommendation"], REC_BUY)


class DedupTest(_MonitorBase):
    def test_provisional_and_final_each_notify_once(self):
        dec = _buy_decision()
        prov = _eval(dec, provisional=True, events=(_buy_event(dec, index=0),), n_bars=1)
        final = _eval(dec, provisional=False, events=(_buy_event(dec, index=0),), n_bars=1)

        self._consume(prov)                       # 盘中首次 → 发
        self.assertEqual(len(self.fake.signals), 1)
        self._consume(prov)                       # 盘中重复 → 去重不发
        self.assertEqual(len(self.fake.signals), 1)

        self._consume(final)                      # 收盘确认（provisional=False）→ 另一去重键 → 发
        self.assertEqual(len(self.fake.signals), 2)
        self._consume(final)                      # 收盘重复 → 不发
        self.assertEqual(len(self.fake.signals), 2)

        self.assertTrue(self.fake.signals[0]["extra_info"]["provisional"])
        self.assertFalse(self.fake.signals[1]["extra_info"]["provisional"])


class LedgerWriteTest(_MonitorBase):
    def test_write_ledger_true_records_monitor_row(self):
        dec = _buy_decision()
        ev = _eval(dec, provisional=True, events=(_buy_event(dec, index=0),), n_bars=1)
        self._consume(ev, write_ledger=True)
        rows = list_signals(source="monitor")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recommendation"], "BUY")
        self.assertEqual(rows[0]["provisional"], 1)
        self.assertEqual(rows[0]["trade_date"], "20260904")

    def test_write_ledger_false_records_nothing(self):
        dec = _buy_decision()
        ev = _eval(dec, provisional=True, events=(_buy_event(dec, index=0),), n_bars=1)
        self._consume(ev, write_ledger=False)
        self.assertEqual(list_signals(source="monitor"), [])


class DailyRoutingTest(_MonitorBase):
    def test_check_symbol_uses_daily_and_never_touches_minute(self):
        self._seed(SYMBOL, 200)
        minute_mock = Mock()
        self.monitor.data_loader.load_minute_bars = minute_mock
        results = []
        asyncio.run(self.monitor.check_symbol(
            {"code": SYMBOL, "name": "浦发银行", "strategy_profile": None},
            results, quotes=None, write_ledger=False,
        ))
        minute_mock.assert_not_called()           # 分钟线已退出信号链路
        self.assertEqual(len(results), 1)         # 日线路径产出一行
        self.assertEqual(results[0]["code"], SYMBOL)

    def test_check_symbol_skips_insufficient_history(self):
        self._seed("000001.SZ", 10)               # < MIN_DAILY_BARS(60)
        results = []
        asyncio.run(self.monitor.check_symbol(
            {"code": "000001.SZ", "name": "平安银行", "strategy_profile": None},
            results, quotes=None, write_ledger=False,
        ))
        self.assertEqual(results, [])             # 数据不足 → 跳过，不产出行、不抛错


class FinalizeIdempotenceTest(_MonitorBase):
    def test_finalize_runs_once_per_day_via_kv(self):
        calls = []

        async def fake_finalize(today):
            calls.append(today)

        self.monitor._finalize_after = time(0, 0)            # now >= 00:00 恒成立
        self.monitor.data_loader.is_trade_day = lambda d=None: True
        self.monitor._finalize_after_close = fake_finalize

        asyncio.run(self.monitor._maybe_finalize())
        asyncio.run(self.monitor._maybe_finalize())          # 第二次应被 kv 短路

        self.assertEqual(len(calls), 1)
        self.assertEqual(kv_get(FINALIZE_KV_KEY), today_str())


class PriceAlertDispatchTest(_MonitorBase):
    def _quotes(self, price=10.6, pre_close=10.0):
        return {SYMBOL: {"symbol": SYMBOL, "price": price, "pre_close": pre_close,
                         "change_pct": (price / pre_close - 1) * 100}}

    def test_dispatch_and_same_day_dedup(self):
        self.monitor._price_alert_config = PriceAlertConfig(
            enabled=True, pct_threshold=0.05, near_limit_pct=0.02, reference_prices={}
        )
        spy = Mock()
        self.monitor.notifier.send_price_alert = spy

        self.monitor._dispatch_price_alerts(self._quotes(price=10.6))   # +6% → pct_move
        first = spy.call_count
        self.assertGreaterEqual(first, 1)
        self.monitor._dispatch_price_alerts(self._quotes(price=10.6))   # 同日同 kind → 去重
        self.assertEqual(spy.call_count, first)

    def test_disabled_config_dispatches_nothing(self):
        # 默认 config 无 price_alert 节 → enabled False
        self.assertFalse(self.monitor._price_alert_config.enabled)
        spy = Mock()
        self.monitor.notifier.send_price_alert = spy
        self.monitor._dispatch_price_alerts(self._quotes(price=10.6))
        spy.assert_not_called()


class ChannelIsolationTest(_MonitorBase):
    def test_signal_and_alert_dedup_are_independent(self):
        notifier = self.monitor.notifier
        # 记录一个信号后，同 (symbol, trade_date) 的价格预警仍可发
        notifier.record(SYMBOL, Side.BUY, "20260904", True)
        self.assertTrue(notifier.should_alert(SYMBOL, "pct_move", "20260904"))
        # 记录一个预警后，信号通道不受影响（不同 side / 不同 provisional 仍可发）
        notifier.record_alert(SYMBOL, "pct_move", "20260904")
        self.assertTrue(notifier.should_notify(SYMBOL, Side.SELL, "20260904", True))
        self.assertTrue(notifier.should_notify(SYMBOL, Side.BUY, "20260904", False))
        # 但完全相同的信号键已被去重
        self.assertFalse(notifier.should_notify(SYMBOL, Side.BUY, "20260904", True))


class FinalizeBodyTest(_MonitorBase):
    """收盘例程真实主体：refresh 个股日线 → 确认 bar 重评 → 写台账 provisional=0（B2 闭环第二写入方）。"""

    def test_finalize_refreshes_and_writes_confirmed_ledger(self):
        self._seed(SYMBOL, 200)
        svc = FakeStockService()
        self.monitor._stock_service = svc

        asyncio.run(self.monitor._finalize_after_close("20260904"))

        # 逐标的走 StockDataService.refresh（A9 复权防护入口）
        self.assertIn(SYMBOL, [symbol for symbol, _ in svc.refreshed])
        # 确认 bar（quote=None → provisional False）重评后写台账 provisional=0
        rows = list_signals(source="monitor")
        self.assertTrue(rows)
        self.assertTrue(all(row["provisional"] == 0 for row in rows))
        self.assertIn(rows[0]["recommendation"], ("BUY", "SELL", "HOLD", "CONFLICT"))


class FinalizeMarketDataTest(_MonitorBase):
    """C5：收盘例程市场级落库（C2 宽度 → C1 指数 → 可选 C3 行业），全部独立 try/except。"""

    def _seed_quotes(self):
        upsert_stock_quotes(
            [
                {"symbol": "600000.SH", "price": 10.0, "change_pct": 9.85, "amount": 1e9},
                {"symbol": "000001.SZ", "price": 12.0, "change_pct": -1.0, "amount": 2e9},
            ],
            "synth",
            self.db_path,
        )

    def test_writes_breadth_and_refreshes_indexes(self):
        self._seed_quotes()
        self.monitor._stock_service = FakeStockService()
        with patch(
            "ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
            return_value={"refreshed": [{"index_code": "000300.SH"}], "failed": []},
        ) as mock_idx:
            self.monitor._finalize_market_data("20260904", [{"code": SYMBOL}])
        mock_idx.assert_called_once()
        rows = load_market_daily(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "20260904")
        self.assertEqual(rows[0]["limit_up"], 1)    # 600000 主板 +9.85 ≥9.8 → 涨停
        self.assertEqual(rows[0]["advancers"], 1)
        self.assertEqual(rows[0]["decliners"], 1)

    def test_index_failure_does_not_block_breadth(self):
        self._seed_quotes()
        self.monitor._stock_service = FakeStockService()
        with patch(
            "ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
            side_effect=RuntimeError("index down"),
        ):
            self.monitor._finalize_market_data("20260904", [{"code": SYMBOL}])
        # 指数失败被独立 try/except 吞掉，宽度仍落库
        self.assertEqual(len(load_market_daily(self.db_path)), 1)

    def test_no_snapshot_skips_breadth_but_still_refreshes_indexes(self):
        self.monitor._stock_service = FakeStockService()  # 不 seed 快照
        with patch(
            "ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
            return_value={"refreshed": [], "failed": []},
        ) as mock_idx:
            self.monitor._finalize_market_data("20260904", [{"code": SYMBOL}])
        mock_idx.assert_called_once()
        self.assertEqual(load_market_daily(self.db_path), [])  # 无快照 → 无宽度行

    def test_industry_skipped_by_default(self):
        self._seed_quotes()
        self.monitor._stock_service = FakeStockService()
        with patch(
            "ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
            return_value={"refreshed": [], "failed": []},
        ), patch(
            "ripple_tradePilot.data.industry_service.IndustryDataService.refresh_for_symbols"
        ) as mock_ind:
            self.monitor._finalize_market_data("20260904", [{"code": SYMBOL}])
        mock_ind.assert_not_called()  # monitor.refresh_industry 默认 false

    def test_industry_runs_when_enabled(self):
        self._seed_quotes()
        self.monitor._stock_service = FakeStockService()
        self.monitor.config["monitor"]["refresh_industry"] = True
        report = {"boards_total": 1, "boards_refreshed": ["BK0475"], "boards_failed": []}
        with patch(
            "ripple_tradePilot.data.market_service.MarketDataService.refresh_indexes",
            return_value={"refreshed": [], "failed": []},
        ), patch(
            "ripple_tradePilot.data.industry_service.IndustryDataService.refresh_for_symbols",
            return_value=report,
        ) as mock_ind:
            self.monitor._finalize_market_data("20260904", [{"code": SYMBOL}])
        mock_ind.assert_called_once()
        self.assertEqual(mock_ind.call_args[0][0], [SYMBOL])  # 股票池传入


class BreakoutExemptionTest(_MonitorBase):
    """breakout 是唯一豁免：parse_profile 不支持 → 旧路径 _check_breakout，仍走日线、不碰分钟。"""

    def test_breakout_profile_routes_to_legacy_path(self):
        self._seed(SYMBOL, 200)
        self.monitor.strategy_profiles["bo"] = {
            "kind": "breakout", "breakout_window": 20, "rsi_period": 6,
            "buy_rsi_min": 55, "exit_ma": 10, "sell_rsi_max": 45,
        }
        minute_mock = Mock()
        self.monitor.data_loader.load_minute_bars = minute_mock
        results = []

        asyncio.run(self.monitor.check_symbol(
            {"code": SYMBOL, "name": "浦发银行", "strategy_profile": "bo"},
            results, quotes=None, write_ledger=False,
        ))

        minute_mock.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["code"], SYMBOL)
        # 旧路径仍产展示层 REC_* 文案（🔴/🟢/⚪/🟡 之一）
        self.assertTrue(results[0]["recommendation"][:1] in ("🔴", "🟢", "⚪", "🟡"))


class SnapshotStalenessTest(_MonitorBase):
    """每轮一次性快照刷新的陈旧门控：新鲜/空/陈旧 → refresh_quotes 调用次数（N 次/轮 → 1 次/轮）。"""

    def _quote_row(self, quote_time):
        return {"symbol": SYMBOL, "price": 10.5, "pre_close": 10.0, "open": 10.1,
                "high": 10.6, "low": 9.9, "volume": 1000.0, "change_pct": 5.0,
                "quote_time": quote_time}

    def test_fresh_snapshot_skips_refresh(self):
        svc = FakeStockService()
        self.monitor._stock_service = svc
        fresh = datetime.now().isoformat(timespec="seconds")
        with patch("ripple_tradePilot.monitor.main.load_stock_quotes",
                   return_value=[self._quote_row(fresh)]):
            quotes = self.monitor._refresh_snapshot()
        self.assertEqual(svc.refresh_quotes_calls, 0)   # 新鲜 → 不真拉
        self.assertIn(SYMBOL, quotes)

    def test_stale_snapshot_triggers_one_refresh(self):
        svc = FakeStockService()
        self.monitor._stock_service = svc
        old = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        with patch("ripple_tradePilot.monitor.main.load_stock_quotes",
                   return_value=[self._quote_row(old)]):
            self.monitor._refresh_snapshot()
        self.assertEqual(svc.refresh_quotes_calls, 1)   # 陈旧 → 全市场一次性刷新

    def test_empty_snapshot_triggers_refresh(self):
        svc = FakeStockService()
        self.monitor._stock_service = svc
        with patch("ripple_tradePilot.monitor.main.load_stock_quotes", return_value=[]):
            self.monitor._refresh_snapshot()
        self.assertEqual(svc.refresh_quotes_calls, 1)   # 无快照（latest None）→ 视为陈旧


class _StubForecast:
    """替身 ``ml.scoring.ScoreResult``：monitor 只读这几个属性 + ``to_dict()``。"""

    def __init__(self, *, p_win=0.5812, expected=0.0123, downside=-0.031,
                 horizon=5, as_of="20260904", model_id="logreg-win5-abc1234567",
                 status="promoted"):
        self.p_win = p_win
        self.expected_net_return = expected
        self.downside_mae = downside
        self.horizon_days = horizon
        self.as_of = as_of
        self.model_id = model_id
        self.status = status

    def to_dict(self):
        return {
            "model_id": self.model_id, "p_win": self.p_win,
            "expected_net_return": self.expected_net_return,
            "downside_mae": self.downside_mae, "horizon_days": self.horizon_days,
            "as_of": self.as_of, "status": self.status,
        }


class _StubScorer:
    def __init__(self, forecast=None, error=None):
        self.calls = []
        self._forecast = forecast
        self._error = error

    def score_symbol(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        if self._error is not None:
            raise self._error
        return self._forecast


class ForecastPlumbingTest(_MonitorBase):
    """D5：预估块贯穿收盘通知 → 飞书卡片 → 台账四列，且任何失败都不影响监控。"""

    def _buy_eval(self, provisional=False):
        dec = _buy_decision()
        return _eval(dec, provisional=provisional, events=(_buy_event(dec),), n_bars=1)

    # --- 文本块 ---
    def test_forecast_text_renders_all_segments(self):
        text = self.monitor.notifier._forecast_text(_StubForecast())
        self.assertIn("🧠 模型预估（5日 · 基准 20260904）", text)
        self.assertIn("胜率 58.1%", text)
        self.assertIn("期望净收益 +1.23%", text)
        self.assertIn("参考下行 -3.10%", text)

    def test_forecast_text_omits_unavailable_segments(self):
        # 缺 ret5/mae5 在位模型 → 整段省略。写 0.00% 会被读成"预测不涨不跌"，是撒谎
        text = self.monitor.notifier._forecast_text(
            _StubForecast(expected=None, downside=None)
        )
        self.assertIn("胜率 58.1%", text)
        self.assertNotIn("期望净收益", text)
        self.assertNotIn("参考下行", text)
        self.assertNotIn("0.00%", text)

    def test_forecast_text_flags_demo_model(self):
        text = self.monitor.notifier._forecast_text(_StubForecast(status="demo"))
        self.assertIn("演示模型", text)

    def test_forecast_text_empty_without_p_win(self):
        self.assertEqual(self.monitor.notifier._forecast_text(None), "")
        self.assertEqual(self.monitor.notifier._forecast_text(_StubForecast(p_win=None)), "")

    # --- 通知 ---
    def test_notification_carries_forecast_to_feishu(self):
        forecast = _StubForecast()
        self._consume(self._buy_eval(), forecast=forecast)
        self.assertEqual(len(self.fake.signals), 1)
        extra = self.fake.signals[0]["extra_info"]
        self.assertEqual(extra["forecast"], forecast.to_dict())
        self.assertIs(extra["provisional"], False)  # 收盘确认路径

    def test_broken_forecast_cannot_suppress_notification(self):
        class _BrokenForecast(_StubForecast):
            def to_dict(self):
                raise RuntimeError("序列化炸了")

        self._consume(self._buy_eval(), forecast=_BrokenForecast())
        # 预估是增强项：它出错时通知照发，只是不带 forecast 键
        self.assertEqual(len(self.fake.signals), 1)
        self.assertNotIn("forecast", self.fake.signals[0]["extra_info"])

    def test_notification_without_forecast_has_no_key(self):
        self._consume(self._buy_eval())
        self.assertNotIn("forecast", self.fake.signals[0]["extra_info"])

    # --- 台账 ---
    def test_ledger_records_forecast_columns(self):
        self._consume(self._buy_eval(), write_ledger=True,
                      forecast=_StubForecast(horizon=10))
        rows = list_signals(source="monitor")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["model_id"], "logreg-win5-abc1234567")
        self.assertAlmostEqual(row["p_win"], 0.5812, places=6)
        self.assertAlmostEqual(row["expected_ret"], 0.0123, places=6)
        self.assertAlmostEqual(row["downside_mae"], -0.031, places=6)
        self.assertEqual(row["horizon"], 10)  # 跟随模型口径，不再硬编码 5

    def test_ledger_defaults_without_forecast(self):
        self._consume(self._buy_eval(), write_ledger=True)
        row = list_signals(source="monitor")[0]
        self.assertIsNone(row["model_id"])
        self.assertIsNone(row["p_win"])
        self.assertIsNone(row["expected_ret"])
        self.assertIsNone(row["downside_mae"])
        self.assertEqual(row["horizon"], 5)

    # --- 打分器获取（ML 支线绝不影响收盘例程）---
    def test_forecast_for_uses_cached_scorer(self):
        scorer = _StubScorer(_StubForecast())
        with patch("ripple_tradePilot.ml.scoring.get_scorer", return_value=scorer):
            result = self.monitor._forecast_for(SYMBOL)
        self.assertIsNotNone(result)
        self.assertEqual(scorer.calls[0][0], SYMBOL)

    def test_forecast_for_none_when_no_incumbent(self):
        with patch("ripple_tradePilot.ml.scoring.get_scorer", return_value=None):
            self.assertIsNone(self.monitor._forecast_for(SYMBOL))

    def test_forecast_for_swallows_errors(self):
        with patch("ripple_tradePilot.ml.scoring.get_scorer",
                   side_effect=ImportError("no sklearn")):
            self.assertIsNone(self.monitor._forecast_for(SYMBOL))
        scorer = _StubScorer(error=RuntimeError("joblib 炸了"))
        with patch("ripple_tradePilot.ml.scoring.get_scorer", return_value=scorer):
            self.assertIsNone(self.monitor._forecast_for(SYMBOL))

    # --- 收盘例程全链路 ---
    def test_finalize_writes_forecast_into_ledger(self):
        self._seed(SYMBOL, 200)
        self.monitor._stock_service = FakeStockService()
        forecast = _StubForecast(as_of="20260903")
        with patch.object(MarketMonitor, "_forecast_for", return_value=forecast):
            asyncio.run(self.monitor._finalize_after_close("20260904"))
        rows = list_signals(source="monitor")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["model_id"], forecast.model_id)
        self.assertAlmostEqual(rows[0]["p_win"], forecast.p_win, places=6)
        self.assertTrue(all(row["provisional"] == 0 for row in rows))

    def test_finalize_without_incumbent_model_still_records(self):
        # 无在位模型（sklearn 未装 / 门禁全拒）→ 收盘例程照常写台账，四列为空
        self._seed(SYMBOL, 200)
        self.monitor._stock_service = FakeStockService()
        with patch("ripple_tradePilot.ml.scoring.get_scorer", return_value=None):
            asyncio.run(self.monitor._finalize_after_close("20260904"))
        rows = list_signals(source="monitor")
        self.assertTrue(rows)
        self.assertIsNone(rows[0]["model_id"])
        self.assertIsNone(rows[0]["p_win"])


if __name__ == "__main__":
    unittest.main()
