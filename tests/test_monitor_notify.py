"""监控通知链路回归测试：A 股口径推荐语 + 定期报告发卡片 + 风控提示价 + 报告限频。"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ripple_tradePilot.monitor.main import (
    REC_BUY,
    REC_HOLD,
    REC_SELL,
    MarketMonitor,
    SignalNotifier,
)
from ripple_tradePilot.models.types import Side

CONFIG = """
symbols: []
futures: []
tushare:
  token: "test-token"
monitor:
  interval_seconds: 300
  report_interval_seconds: 3600
notifiers:
  feishu:
    enabled: false
risk:
  stop_loss_pct: 0.1
  take_profit_pct: 0.25
"""


class FakeFeishu:
    """记录发送内容的假飞书通知器（与 FeishuWebhookNotifier 接口对齐）。"""

    def __init__(self, dashboard_url=None):
        self.dashboard_url = dashboard_url
        self.posted = []

    def send_card(self, content):
        self.posted.append(content)
        return True

    def _send_text(self, content):
        self.posted.append(content)
        return True


class RecommendationConventionTest(unittest.TestCase):
    def test_constants_follow_a_share_convention(self):
        # 全站统一口径：红=买/涨，绿=卖/跌（与 Web --buy:#ba1a1a、飞书卡 red=BUY 一致）
        self.assertTrue(REC_BUY.startswith("🔴"))
        self.assertIn("买入", REC_BUY)
        self.assertTrue(REC_SELL.startswith("🟢"))
        self.assertIn("卖出", REC_SELL)
        self.assertIn("观望", REC_HOLD)


class RiskHintsTest(unittest.TestCase):
    def setUp(self):
        self.notifier = SignalNotifier({}, stop_loss_pct=0.10, take_profit_pct=0.25)

    def test_buy_signal_gets_stop_loss_and_take_profit(self):
        hints = self.notifier._risk_hints(Side.BUY, 40.0)
        self.assertAlmostEqual(hints["stop_loss"], 36.0)
        self.assertAlmostEqual(hints["take_profit"], 50.0)

    def test_sell_and_invalid_price_get_no_hints(self):
        self.assertIsNone(self.notifier._risk_hints(Side.SELL, 40.0))
        self.assertIsNone(self.notifier._risk_hints(Side.BUY, 0.0))


class PeriodicReportTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_path = root / "config.yaml"
        self.config_path.write_text(CONFIG, encoding="utf-8")
        self.db_path = root / "tradepilot.db"
        self.environment = patch.dict(
            os.environ, {"TRADEPILOT_BACKTEST_DB": str(self.db_path)}
        )
        self.environment.start()
        self.monitor = MarketMonitor(str(self.config_path))

    def tearDown(self):
        self.environment.stop()
        self.temp_dir.cleanup()

    def _results(self):
        return [
            {"code": "600309.SH", "name": "万华化学", "price": 70.5,
             "recommendation": REC_BUY, "buy_count": 2, "sell_count": 0},
            {"code": "002022.SZ", "name": "科华生物", "price": 40.1,
             "recommendation": REC_SELL, "buy_count": 0, "sell_count": 2},
            {"code": "600000.SH", "name": "浦发银行", "price": 8.2,
             "recommendation": REC_HOLD, "buy_count": 0, "sell_count": 0},
        ]

    def test_report_is_sent_as_interactive_card(self):
        feishu = FakeFeishu()
        self.monitor.feishu_notifier = feishu
        asyncio.run(self.monitor.send_periodic_report(self._results()))

        self.assertEqual(len(feishu.posted), 1)
        content = feishu.posted[0]
        self.assertEqual(content["msg_type"], "interactive")  # 不再是纯文本
        text = json.dumps(content, ensure_ascii=False)
        self.assertIn("🔴 **买入信号**", text)   # 卡片正文同为红买绿卖
        self.assertIn("🟢 **卖出信号**", text)
        self.assertIn("万华化学", text)
        self.assertIn("观望：1只", text)

    def test_report_includes_dashboard_button_when_configured(self):
        feishu = FakeFeishu(dashboard_url="http://localhost:8000")
        self.monitor.feishu_notifier = feishu
        asyncio.run(self.monitor.send_periodic_report(self._results()))

        elements = feishu.posted[0]["card"]["elements"]
        actions = [e for e in elements if e.get("tag") == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["actions"][0]["url"], "http://localhost:8000")

    def test_report_skipped_without_notifier(self):
        self.monitor.feishu_notifier = None
        asyncio.run(self.monitor.send_periodic_report(self._results()))  # 不应抛错

    def test_report_rate_limit(self):
        # 无信号 + 刚发过 → 限频不发
        self.monitor._last_report_at = datetime.now()
        hold_only = [r for r in self._results() if r["recommendation"] == REC_HOLD]
        self.assertFalse(self.monitor._should_send_report(hold_only))
        # 出现买卖信号 → 立即发
        self.assertTrue(self.monitor._should_send_report(self._results()))
        # 从未发过 → 发
        self.monitor._last_report_at = None
        self.assertTrue(self.monitor._should_send_report(hold_only))
        # 超过最短间隔 → 发
        self.monitor._last_report_at = datetime.now() - timedelta(seconds=3601)
        self.assertTrue(self.monitor._should_send_report(hold_only))


if __name__ == "__main__":
    unittest.main()
