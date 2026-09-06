"""数据清洗（量纲脏值防护）与夏普口径（剔除空仓日）的回归测试。"""

import unittest
from datetime import datetime

import numpy as np

from ripple_tradePilot.backtest.report import compute_metrics
from ripple_tradePilot.data.cleaning import is_valid_ohlc, reject_price_outliers
from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier


def make_bar(close: float, open_: float = None, high: float = None,
             low: float = None, ts: datetime = None) -> Bar:
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    return Bar(
        timestamp=ts or datetime(2026, 1, 5, 9, 31),
        open=open_, high=high, low=low, close=close, volume=10000.0,
    )


class RejectPriceOutliersTest(unittest.TestCase):
    def test_filters_order_of_magnitude_dirty_prices(self):
        # 真实价约 40 元，混入一根 0.19 元的脏 bar（妙想接口真实出现过的形态）
        bars = [make_bar(c) for c in (40.0, 41.2, 39.5, 40.3, 0.19, 40.8)]
        cleaned = reject_price_outliers(bars)
        self.assertEqual([b.close for b in cleaned],
                         [40.0, 41.2, 39.5, 40.3, 40.8])  # 脏值被剔除，顺序保持

    def test_filters_invalid_ohlc(self):
        bars = [
            make_bar(40.0), make_bar(41.0), make_bar(39.0),
            make_bar(40.0, high=38.0, low=42.0),   # high < low
            make_bar(-5.0, open_=-5.0, high=-1.0, low=-6.0),  # 非正价格
        ]
        cleaned = reject_price_outliers(bars)
        self.assertEqual([b.close for b in cleaned], [40.0, 41.0, 39.0])

    def test_too_few_samples_skips_outlier_filter(self):
        # 有效收盘 <3 根时只做基本校验，避免小样本误杀
        bars = [make_bar(40.0), make_bar(0.19)]
        cleaned = reject_price_outliers(bars)
        self.assertEqual([b.close for b in cleaned], [40.0, 0.19])

    def test_is_valid_ohlc_rejects_nan(self):
        self.assertFalse(is_valid_ohlc(float("nan"), 10.0, 9.0, 9.5))
        self.assertTrue(is_valid_ohlc(9.5, 10.0, 9.0, 9.5))


class PositionAwareSharpeTest(unittest.TestCase):
    def setUp(self):
        # 6 个 bar：第 3、4 个 bar 有持仓且产生收益，其余空仓（0 收益）
        self.equity = [100.0, 100.0, 100.0, 105.0, 105.0, 110.0]
        self.positions = [0, 0, 100, 100, 0, 200]

    def test_positions_sharpe_uses_only_held_days(self):
        returns = np.diff(self.equity) / np.array(self.equity[:-1])
        held = np.array(self.positions[: len(returns)]) > 0
        expected = float(returns[held].mean() / returns[held].std()
                         * np.sqrt(252))
        metrics = compute_metrics(self.equity, positions=self.positions)
        self.assertAlmostEqual(metrics.sharpe, expected, places=10)
        # 空仓 0 收益同时稀释均值与波动率，把全样本夏普向 0 拉低；
        # 持仓日口径剔除了这种稀释（对盈利策略恒有 held ≥ full）
        full = compute_metrics(self.equity)
        self.assertGreater(metrics.sharpe, full.sharpe)

    def test_no_positions_falls_back_to_full_sample(self):
        # 全空仓时 held.any() 为 False，退回全样本口径（不抛错、不归零）
        metrics = compute_metrics(self.equity, positions=[0] * len(self.equity))
        self.assertAlmostEqual(metrics.sharpe,
                               compute_metrics(self.equity).sharpe, places=10)

    def test_constant_held_returns_give_zero_sharpe(self):
        equity = [100.0, 100.0, 110.0, 121.0]
        positions = [0, 100, 100, 100]  # 持仓日收益恒为 10%，std=0
        self.assertEqual(compute_metrics(equity, positions=positions).sharpe, 0.0)

    def test_other_metrics_unchanged_by_positions(self):
        a = compute_metrics(self.equity)
        b = compute_metrics(self.equity, positions=self.positions)
        self.assertEqual(a.total_return, b.total_return)
        self.assertEqual(a.max_drawdown, b.max_drawdown)
        self.assertEqual(a.annual_return, b.annual_return)


class FeishuCardTest(unittest.TestCase):
    def _send(self, side: Side, extra_info=None, dashboard_url=None):
        notifier = FeishuWebhookNotifier("http://example.com/hook",
                                         dashboard_url=dashboard_url)
        captured = {}

        def fake_post(payload):
            captured.update(payload)
            return True

        notifier._post = fake_post
        bar = make_bar(40.0, ts=datetime(2026, 9, 4, 14, 35))
        ok = notifier.send("002022.SZ", "科华生物", side, 40.0,
                           "科华生物策略", bar, extra_info=extra_info)
        self.assertTrue(ok)
        return captured["card"]

    def test_buy_card_uses_red_and_red_circle(self):
        card = self._send(Side.BUY)
        self.assertEqual(card["header"]["template"], "red")   # A 股口径：买=红
        self.assertIn("🔴", card["header"]["title"]["content"])

    def test_sell_card_uses_green_and_green_circle(self):
        card = self._send(Side.SELL)
        self.assertEqual(card["header"]["template"], "green")  # 卖=绿
        self.assertIn("🟢", card["header"]["title"]["content"])

    def test_risk_hint_block_rendered_when_provided(self):
        card = self._send(Side.BUY,
                          extra_info={"stop_loss": 36.8, "take_profit": 48.0})
        text = str(card["elements"])
        self.assertIn("风控参考位", text)
        self.assertIn("止损：36.80 元", text)
        self.assertIn("止盈：48.00 元", text)
        self.assertIn("不构成投资建议", text)

    def test_no_risk_block_without_hints(self):
        card = self._send(Side.BUY)
        self.assertNotIn("风控参考位", str(card["elements"]))

    def test_dashboard_button_when_url_configured(self):
        card = self._send(Side.BUY, dashboard_url="http://localhost:8000")
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["actions"][0]["url"], "http://localhost:8000")

    def test_no_dashboard_button_by_default(self):
        card = self._send(Side.BUY)
        self.assertFalse([e for e in card["elements"] if e.get("tag") == "action"])


if __name__ == "__main__":
    unittest.main()
