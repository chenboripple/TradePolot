"""
观察池联动与飞书通知链路的回归测试。
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier
from ripple_tradePilot.storage import user_store


class WatchlistAggregationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "test.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lists_distinct_watched_symbols_across_users(self):
        u1 = user_store.create_user("alice", "pw-alice-1", self.db)
        u2 = user_store.create_user("bob", "pw-bob-123", self.db)
        user_store.add_watchlist_item(u1["id"], "000001.SZ", "平安银行", self.db)
        user_store.add_watchlist_item(u1["id"], "600309.SH", "万华化学", self.db)
        user_store.add_watchlist_item(u2["id"], "000001.SZ", "平安银行", self.db)
        user_store.add_watchlist_item(u2["id"], "002022.SZ", "科华生物", self.db)
        user_store.delete_watchlist_item(u2["id"], "002022.SZ", self.db)  # 移出观察池

        watched = user_store.list_all_watched_symbols(self.db)

        self.assertEqual(
            {item["symbol"] for item in watched},
            {"000001.SZ", "600309.SH"},  # 去重且不含已移出的标的
        )


class FeishuSignatureTest(unittest.TestCase):
    def test_signature_is_deterministic(self):
        notifier = FeishuWebhookNotifier("http://example.com/hook", secret="s3cret")
        self.assertEqual(
            notifier._generate_signature("1700000000"),
            notifier._generate_signature("1700000000"),
        )
        self.assertNotEqual(
            notifier._generate_signature("1700000000"),
            notifier._generate_signature("1700000001"),
        )

    def test_no_signature_without_secret(self):
        notifier = FeishuWebhookNotifier("http://example.com/hook")
        self.assertEqual(notifier._generate_signature("1700000000"), "")


class _CapturingNotifier(FeishuWebhookNotifier):
    """截住 ``_post``：只检查卡片装配，绝不发网络请求。"""

    def __init__(self):
        super().__init__("http://example.com/hook")
        self.payloads = []

    def _post(self, payload):
        self.payloads.append(payload)
        return True


def _forecast(**overrides):
    """:class:`ml.scoring.ScoreResult` ``to_dict()`` 的形态。"""
    payload = {
        "model_id": "logreg-win5-abc1234567",
        "target": "win5",
        "horizon_days": 5,
        "as_of": "20260904",
        "p_win": 0.5812,
        "expected_net_return": 0.0123,
        "downside_mae": -0.031,
        "status": "promoted",
        "stale": False,
        "return_basis": "主模型 OOS 历史中 p≥0.581 子集的已实现平均净收益",
        "oos_n_signals": 120,
        "oos_coverage": 0.16,
        "oos_win_rate": 0.58,
        "warnings": [],
    }
    payload.update(overrides)
    return payload


class FeishuForecastCardTest(unittest.TestCase):
    """D5：飞书信号卡片的模型预估块（monitor 收盘例程经 ``extra_info['forecast']`` 传入）。"""

    def setUp(self):
        self.notifier = _CapturingNotifier()
        self.bar = Bar(
            timestamp=datetime(2026, 9, 4, 15, 0),
            open=10.0, high=11.0, low=9.0, close=10.5, volume=1_000_000.0,
        )

    def _send(self, extra_info):
        self.notifier.send(
            symbol="600000.SH", name="浦发银行", side=Side.BUY, price=10.5,
            strategy="股票策略", bar=self.bar, extra_info=extra_info,
        )
        payload = self.notifier.payloads[-1]
        texts = [
            element["text"]["content"]
            for element in payload["card"]["elements"]
            if element.get("tag") == "div"
        ]
        return "\n".join(texts)

    def test_forecast_block_rendered(self):
        text = self._send({"provisional": False, "forecast": _forecast()})
        self.assertIn("🧠 **模型预估**", text)
        self.assertIn("胜率（5日）：**58.1%**", text)
        self.assertIn("期望净收益：**+1.23%**", text)
        self.assertIn("参考下行：**-3.10%**", text)
        self.assertIn("口径：主模型 OOS 历史中 p≥0.581 子集的已实现平均净收益", text)
        self.assertIn("基准日 20260904 · 模型 logreg-win5-abc1234567", text)
        # 收盘确认状态行照旧（预估块是追加，不替换既有内容）
        self.assertIn("✅ **收盘确认**", text)

    def test_demo_model_is_flagged(self):
        text = self._send({"forecast": _forecast(status="demo")})
        self.assertIn("演示模型，门禁未过，不可作为交易依据", text)

    def test_stale_model_is_flagged(self):
        text = self._send({"forecast": _forecast(stale=True)})
        self.assertIn("数据滞后，模型已标 stale", text)

    def test_unavailable_segments_omitted_not_zeroed(self):
        # 无 ret5/mae5 在位模型 → 整行省略；补 0.00% 会被读成"预测不涨不跌"
        text = self._send(
            {"forecast": _forecast(expected_net_return=None, downside_mae=None)}
        )
        self.assertIn("胜率（5日）：**58.1%**", text)
        self.assertNotIn("期望净收益", text)
        self.assertNotIn("参考下行", text)
        self.assertNotIn("0.00%", text)

    def test_no_block_without_forecast(self):
        text = self._send({"provisional": False})
        self.assertNotIn("模型预估", text)

    def test_no_block_when_p_win_missing(self):
        text = self._send({"forecast": _forecast(p_win=None)})
        self.assertNotIn("模型预估", text)
        self.assertNotIn("None", text)


if __name__ == "__main__":
    unittest.main()
