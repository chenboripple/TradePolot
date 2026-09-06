"""A3 价格预警（monitor/price_alert.py）测试：默认关、规则触发、分板块涨跌停、独立通道。

价格预警是监控切日线后保留的"分钟级即时提醒"降级形态：规则化、非投票、与信号链路解耦，
默认关。本测试覆盖配置解析、四类规则（日内涨跌幅 / 逼近涨跌停 / 穿越参考价）、分板块
涨跌停比例（price_limit_for_symbol）、消息前缀与名称映射、脏行跳过。
"""
from __future__ import annotations

import unittest

from ripple_tradePilot.backtest.rules import price_limit_for_symbol
from ripple_tradePilot.monitor.price_alert import (
    ALERT_CROSS_REF,
    ALERT_NEAR_LIMIT_DOWN,
    ALERT_NEAR_LIMIT_UP,
    ALERT_PCT_MOVE,
    PriceAlertConfig,
    evaluate_price_alerts,
    parse_price_alert_config,
)

_PREFIX = "⚡价格预警（非交易信号）"
_ENABLED = PriceAlertConfig(enabled=True)


def _row(symbol="600000.SH", price=10.0, pre_close=10.0, change_pct=0.0, **extra):
    row = {"symbol": symbol, "price": price, "pre_close": pre_close, "change_pct": change_pct}
    row.update(extra)
    return row


def _kinds(alerts):
    return {a.kind for a in alerts}


class ParseConfigTest(unittest.TestCase):
    def test_none_falls_back_to_disabled_defaults(self):
        cfg = parse_price_alert_config(None)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.pct_threshold, 0.05)
        self.assertEqual(cfg.near_limit_pct, 0.02)
        self.assertEqual(cfg.reference_prices, {})

    def test_empty_monitor_section_defaults(self):
        cfg = parse_price_alert_config({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.pct_threshold, 0.05)

    def test_price_alert_key_present_but_none(self):
        cfg = parse_price_alert_config({"price_alert": None})
        self.assertFalse(cfg.enabled)

    def test_full_section_parsed(self):
        cfg = parse_price_alert_config({
            "price_alert": {
                "enabled": True,
                "pct_threshold": 0.03,
                "near_limit_pct": 0.01,
                "reference_prices": {"600000.sh": 11},
            }
        })
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.pct_threshold, 0.03)
        self.assertEqual(cfg.near_limit_pct, 0.01)
        # 键统一大写
        self.assertEqual(cfg.reference_prices, {"600000.SH": 11.0})

    def test_reference_prices_skips_non_numeric(self):
        cfg = parse_price_alert_config({
            "price_alert": {"reference_prices": {"600000.SH": "abc", "000001.SZ": 12.5}}
        })
        self.assertEqual(cfg.reference_prices, {"000001.SZ": 12.5})


class DisabledTest(unittest.TestCase):
    def test_disabled_returns_empty_even_for_huge_move(self):
        cfg = PriceAlertConfig(enabled=False)
        alerts = evaluate_price_alerts([_row(price=20.0, pre_close=10.0)], config=cfg)
        self.assertEqual(alerts, [])


class PctMoveTest(unittest.TestCase):
    def test_up_move_fires_with_direction(self):
        alerts = evaluate_price_alerts([_row(price=10.6, pre_close=10.0)], config=_ENABLED)
        self.assertIn(ALERT_PCT_MOVE, _kinds(alerts))
        move = next(a for a in alerts if a.kind == ALERT_PCT_MOVE)
        self.assertIn("急拉", move.message)
        self.assertAlmostEqual(move.pct_chg, 0.06, places=6)

    def test_down_move_fires_with_direction(self):
        alerts = evaluate_price_alerts([_row(price=9.4, pre_close=10.0)], config=_ENABLED)
        move = next(a for a in alerts if a.kind == ALERT_PCT_MOVE)
        self.assertIn("急跌", move.message)
        self.assertAlmostEqual(move.pct_chg, -0.06, places=6)

    def test_below_threshold_no_pct_move(self):
        alerts = evaluate_price_alerts([_row(price=10.3, pre_close=10.0)], config=_ENABLED)
        self.assertNotIn(ALERT_PCT_MOVE, _kinds(alerts))

    def test_change_pct_fallback_when_no_pre_close(self):
        # pre_close 缺失/为 0 → 回退快照 change_pct/100
        alerts = evaluate_price_alerts(
            [_row(price=10.0, pre_close=0.0, change_pct=6.0)], config=_ENABLED
        )
        self.assertIn(ALERT_PCT_MOVE, _kinds(alerts))
        move = next(a for a in alerts if a.kind == ALERT_PCT_MOVE)
        self.assertAlmostEqual(move.pct_chg, 0.06, places=6)


class NearLimitTest(unittest.TestCase):
    def test_main_board_near_limit_up(self):
        # 600000.SH limit=0.10 → limit_up=11.0，band 2% → 触发线 10.78
        limit = price_limit_for_symbol("600000.SH")
        self.assertEqual(limit, 0.10)
        trigger = 10.0 * (1 + limit) * (1 - 0.02)
        alerts = evaluate_price_alerts([_row(price=trigger + 0.07, pre_close=10.0)], config=_ENABLED)
        self.assertIn(ALERT_NEAR_LIMIT_UP, _kinds(alerts))

    def test_main_board_below_trigger_no_near_limit_up(self):
        alerts = evaluate_price_alerts([_row(price=10.5, pre_close=10.0)], config=_ENABLED)
        self.assertNotIn(ALERT_NEAR_LIMIT_UP, _kinds(alerts))

    def test_gem_board_uses_20pct_limit(self):
        # 300001.SZ limit=0.20 → limit_up=12.0，触发线 11.76
        limit = price_limit_for_symbol("300001.SZ")
        self.assertEqual(limit, 0.20)
        alerts = evaluate_price_alerts(
            [_row(symbol="300001.SZ", price=11.8, pre_close=10.0)], config=_ENABLED
        )
        self.assertIn(ALERT_NEAR_LIMIT_UP, _kinds(alerts))

    def test_gem_board_discriminates_against_10pct_assumption(self):
        # price=11.5：20% 触发线 11.76 → 不触发；若误用 10%（触发线 10.78）则会误触发。
        alerts = evaluate_price_alerts(
            [_row(symbol="300001.SZ", price=11.5, pre_close=10.0)], config=_ENABLED
        )
        self.assertNotIn(ALERT_NEAR_LIMIT_UP, _kinds(alerts))
        # 但日内 +15% 仍触发 pct_move（证明不是整行被跳过）
        self.assertIn(ALERT_PCT_MOVE, _kinds(alerts))

    def test_star_board_uses_20pct_limit(self):
        self.assertEqual(price_limit_for_symbol("688001.SH"), 0.20)

    def test_bse_uses_30pct_limit(self):
        self.assertEqual(price_limit_for_symbol("920001.BJ"), 0.30)

    def test_near_limit_down(self):
        # 600000.SH limit_down=9.0，band 2% → 触发线 9.18
        alerts = evaluate_price_alerts([_row(price=9.1, pre_close=10.0)], config=_ENABLED)
        self.assertIn(ALERT_NEAR_LIMIT_DOWN, _kinds(alerts))
        self.assertNotIn(ALERT_NEAR_LIMIT_UP, _kinds(alerts))


class CrossRefTest(unittest.TestCase):
    def test_cross_ref_fires_above_reference(self):
        cfg = PriceAlertConfig(enabled=True, reference_prices={"600000.SH": 11.0})
        # pre_close=11.0 → 无 pct_move（+1.8%），无涨跌停；只触发 cross_ref
        alerts = evaluate_price_alerts([_row(price=11.2, pre_close=11.0)], config=cfg)
        self.assertEqual(_kinds(alerts), {ALERT_CROSS_REF})

    def test_no_cross_ref_below_reference(self):
        cfg = PriceAlertConfig(enabled=True, reference_prices={"600000.SH": 11.0})
        alerts = evaluate_price_alerts([_row(price=10.5, pre_close=11.0)], config=cfg)
        self.assertNotIn(ALERT_CROSS_REF, _kinds(alerts))
        self.assertEqual(alerts, [])


class MultiKindTest(unittest.TestCase):
    def test_one_row_can_fire_multiple_kinds(self):
        cfg = PriceAlertConfig(enabled=True, reference_prices={"300001.SZ": 11.5})
        # 300001.SZ price=11.9：pct_move(+19%) + near_limit_up(>=11.76) + cross_ref(>=11.5)
        alerts = evaluate_price_alerts(
            [_row(symbol="300001.SZ", price=11.9, pre_close=10.0)], config=cfg
        )
        self.assertEqual(
            _kinds(alerts), {ALERT_PCT_MOVE, ALERT_NEAR_LIMIT_UP, ALERT_CROSS_REF}
        )


class MessageTest(unittest.TestCase):
    def test_every_message_carries_non_signal_prefix(self):
        cfg = PriceAlertConfig(enabled=True, reference_prices={"600000.SH": 10.7})
        alerts = evaluate_price_alerts([_row(price=10.85, pre_close=10.0)], config=cfg)
        self.assertTrue(alerts)
        for alert in alerts:
            self.assertIn(_PREFIX, alert.message)

    def test_names_mapping_used_in_message(self):
        alerts = evaluate_price_alerts(
            [_row(price=10.6, pre_close=10.0)],
            config=_ENABLED,
            names={"600000.SH": "浦发银行"},
        )
        move = next(a for a in alerts if a.kind == ALERT_PCT_MOVE)
        self.assertIn("浦发银行", move.message)

    def test_symbol_used_when_no_name(self):
        alerts = evaluate_price_alerts([_row(price=10.6, pre_close=10.0)], config=_ENABLED)
        move = next(a for a in alerts if a.kind == ALERT_PCT_MOVE)
        self.assertIn("600000.SH", move.message)


class MalformedRowTest(unittest.TestCase):
    def test_bad_rows_skipped_without_exception(self):
        rows = [
            {"symbol": "600000.SH", "pre_close": 10.0},          # 缺 price
            {"symbol": "600000.SH", "price": 0.0, "pre_close": 10.0},   # price=0
            {"symbol": "600000.SH", "price": -5.0, "pre_close": 10.0},  # 负价
            {"symbol": "", "price": 10.6, "pre_close": 10.0},     # 空 symbol
        ]
        alerts = evaluate_price_alerts(rows, config=_ENABLED)
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
