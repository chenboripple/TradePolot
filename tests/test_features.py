"""D1 ``ml/features.py`` 的离线测试。

覆盖：手算锚点（线性序列 ret_5/ret_1 精确值、恒定成交量 vol_ratio=1、单调上涨 RSI=100、
线性序列恒为 20 日新高）、signal 组结构（one-hot 和恒 1、comp 带符号、state_age 递增）、
market/industry 组的因果与缺失降级、**组开关输出列集合精确匹配**、空输入优雅返回、
以及**前视探针**（扰动尾部 10 根 bar，断言之前所有特征行逐列不变 + 尾行确实被扰动改变，
证明探针非平凡）。全程合成夹具、零网络、零真实 DB。
"""

import unittest

import numpy as np
import pandas as pd

import synth
from ripple_tradePilot.ml import features as F
from ripple_tradePilot.signals.profile import ComponentSpec, ProfileSpec


def _ma_spec(fast: int = 5, slow: int = 10, threshold: int = 1) -> ProfileSpec:
    """单 MA 组件画像（确定性：单调上涨序列恒 fast>slow → BUY）。"""
    return ProfileSpec(
        kind="combo_vote",
        components=(ComponentSpec(kind="ma", name="ma", params={"fast": fast, "slow": slow}),),
        vote_threshold=threshold,
        source="test",
    )


def _rows(count: int = 120, seed: int = 3):
    return synth.daily_rows(synth.daily_bars(count, seed=seed))


# ---------------------------------------------------------------------------
# price_volume 手算锚点
# ---------------------------------------------------------------------------
class PriceVolumeAnchorTest(unittest.TestCase):
    def setUp(self):
        # 线性收盘价 10.00,10.05,10.10,...（step=0.05），恒定成交量
        self.closes = synth.linear_closes(40, start_price=10.0, step=0.05)
        self.rows = synth.daily_rows(synth.bars_from_closes(self.closes))
        self.pv = F.price_volume_features(self.rows)

    def test_ret5_and_ret1_exact(self):
        # close[i]=10+0.05i；ret_5@i=5 = 10.25/10.00-1 = 0.025；ret_1@i=1 = 10.05/10.00-1 = 0.005
        self.assertAlmostEqual(self.pv["ret_5"].iloc[5], 0.025, places=10)
        self.assertAlmostEqual(self.pv["ret_1"].iloc[1], 0.005, places=10)

    def test_warmup_is_nan(self):
        # ret_5 前 5 个位置 NaN（因果，不看未来）
        self.assertTrue(np.isnan(self.pv["ret_5"].iloc[4]))
        self.assertFalse(np.isnan(self.pv["ret_5"].iloc[5]))

    def test_vol_ratio_constant_volume_is_one(self):
        # 恒定成交量 → volume/MA20(volume)=1（warmup 后）
        self.assertAlmostEqual(self.pv["vol_ratio"].iloc[25], 1.0, places=10)
        self.assertTrue(np.isnan(self.pv["vol_ratio"].iloc[18]))  # <20 根 NaN

    def test_rsi_monotonic_up_is_100(self):
        # 单调上涨 → losses=0 → RSI=100
        self.assertAlmostEqual(self.pv["rsi14"].iloc[20], 100.0, places=6)
        self.assertTrue(np.isnan(self.pv["rsi14"].iloc[13]))  # period=14 前 NaN

    def test_breakout_linear_always_new_high(self):
        # 线性上涨 → 收盘恒为近 20 日最高 → breakout_20=1（warmup 后）
        self.assertEqual(self.pv["breakout_20"].iloc[30], 1.0)
        self.assertTrue(np.isnan(self.pv["breakout_20"].iloc[10]))

    def test_high_low_range_positive(self):
        # bars_from_closes 用 spread 展开 high/low → 振幅恒正
        self.assertTrue((self.pv["high_low_range"].dropna() > 0).all())

    def test_column_set_exact(self):
        self.assertEqual(list(self.pv.columns), list(F.PRICE_VOLUME_COLUMNS))


# ---------------------------------------------------------------------------
# signal 组
# ---------------------------------------------------------------------------
class SignalFeatureTest(unittest.TestCase):
    def test_ma_only_uptrend_all_buy_and_state_age_grows(self):
        rows = synth.daily_rows(synth.bars_from_closes(synth.linear_closes(40)))
        sig = F.signal_features(rows, _ma_spec(fast=5, slow=10, threshold=1))
        # 深度上涨段：MA fast>slow → BUY
        self.assertEqual(sig["rec_buy"].iloc[-1], 1.0)
        self.assertEqual(sig["buy_count"].iloc[-1], 1.0)
        self.assertEqual(sig["comp_ma"].iloc[-1], 1.0)  # +1 买
        self.assertEqual(sig["vote_threshold"].iloc[-1], 1.0)
        # state_age：连续 BUY 持续 bar 数 > 1
        self.assertGreater(sig["state_age"].iloc[-1], 1.0)

    def test_recommendation_one_hot_sums_to_one(self):
        rows = _rows(120, seed=5)
        sig = F.signal_features(rows, F.default_signal_spec())
        rec_sum = sig[["rec_buy", "rec_sell", "rec_hold", "rec_conflict"]].sum(axis=1)
        self.assertTrue((rec_sum == 1.0).all())

    def test_component_columns_signed_in_range(self):
        rows = _rows(120, seed=6)
        sig = F.signal_features(rows, F.default_signal_spec())
        for name in ("comp_ma", "comp_rsi", "comp_bollinger"):
            self.assertIn(name, sig.columns)
            self.assertTrue(set(sig[name].unique()) <= {-1.0, 0.0, 1.0})

    def test_buy_sell_count_bounded_by_components(self):
        rows = _rows(120, seed=7)
        sig = F.signal_features(rows, F.default_signal_spec())  # 三件套
        self.assertTrue((sig["buy_count"] + sig["sell_count"] <= 3.0).all())
        self.assertTrue((sig["max_strength"] >= 0.0).all())

    def test_state_age_resets_on_recommendation_change(self):
        rows = _rows(150, seed=8)
        sig = F.signal_features(rows, F.default_signal_spec())
        recs = sig[["rec_buy", "rec_sell", "rec_hold", "rec_conflict"]].to_numpy().argmax(axis=1)
        ages = sig["state_age"].to_numpy()
        for i in range(1, len(recs)):
            if recs[i] != recs[i - 1]:
                self.assertEqual(ages[i], 1.0)  # 状态切换 → 龄重置为 1
            else:
                self.assertEqual(ages[i], ages[i - 1] + 1.0)


# ---------------------------------------------------------------------------
# market 组
# ---------------------------------------------------------------------------
class MarketFeatureTest(unittest.TestCase):
    def test_columns_and_length(self):
        mkt = F.market_features(synth.index_rows(60), synth.market_daily_rows(60))
        self.assertEqual(list(mkt.columns), ["trade_date", *F.MARKET_COLUMNS])
        self.assertEqual(len(mkt), 60)

    def test_idx_ret_warmup_then_value(self):
        mkt = F.market_features(synth.index_rows(60), synth.market_daily_rows(60))
        self.assertTrue(np.isnan(mkt["idx_ret_5"].iloc[4]))
        self.assertFalse(np.isnan(mkt["idx_ret_5"].iloc[10]))

    def test_empty_inputs_returns_empty_frame(self):
        mkt = F.market_features(None, None)
        self.assertTrue(mkt.empty)
        self.assertEqual(list(mkt.columns), ["trade_date", *F.MARKET_COLUMNS])

    def test_index_only_fills_breadth_columns_nan(self):
        mkt = F.market_features(synth.index_rows(40), None)
        self.assertTrue(mkt["up_ratio"].isna().all())
        self.assertTrue(mkt["limit_up_count"].isna().all())
        self.assertFalse(mkt["idx_ret_5"].isna().all())

    def test_breadth_only_fills_index_columns_nan(self):
        mkt = F.market_features(None, synth.market_daily_rows(40))
        self.assertTrue(mkt["idx_ret_5"].isna().all())
        self.assertFalse(mkt["up_ratio"].isna().all())


# ---------------------------------------------------------------------------
# industry 组
# ---------------------------------------------------------------------------
class IndustryFeatureTest(unittest.TestCase):
    def test_rel_strength_is_stock_minus_board(self):
        board = synth.board_rows(60)
        dates = [str(r["trade_date"]) for r in board]
        stock_closes = [10.0 + 0.1 * i for i in range(60)]
        ind = F.industry_features(board, dates, stock_closes)
        # stock_ret_5@i=30 = 13.0/12.5-1 = 0.04；rel = stock_ret_5 - board_ret_5
        expected_stock_ret5 = 13.0 / 12.5 - 1.0
        self.assertAlmostEqual(
            ind["rel_strength_5"].iloc[30],
            expected_stock_ret5 - ind["board_ret_5"].iloc[30],
            places=10,
        )
        self.assertEqual(list(ind.columns), ["trade_date", *F.INDUSTRY_COLUMNS])

    def test_empty_board_returns_empty(self):
        ind = F.industry_features(None, ["20240102"], [10.0])
        self.assertTrue(ind.empty)
        self.assertEqual(list(ind.columns), ["trade_date", *F.INDUSTRY_COLUMNS])

    def test_board_warmup_nan(self):
        board = synth.board_rows(60)
        dates = [str(r["trade_date"]) for r in board]
        ind = F.industry_features(board, dates, [10.0] * 60)
        self.assertTrue(np.isnan(ind["board_ret_20"].iloc[10]))
        self.assertFalse(np.isnan(ind["board_ret_20"].iloc[25]))


# ---------------------------------------------------------------------------
# symbol_feature_frame：列集合 / 标志列 / 组开关 / 空输入
# ---------------------------------------------------------------------------
class SymbolFrameTest(unittest.TestCase):
    def setUp(self):
        self.rows = _rows(150, seed=11)
        self.idx = synth.index_rows(150)
        self.breadth = synth.market_daily_rows(150)
        self.board = synth.board_rows(150)

    def _feature_cols(self, frame):
        skip = {"symbol", "trade_date", "_has_market", "_has_industry"}
        return set(c for c in frame.columns if c not in skip)

    def test_full_frame_columns_and_flags(self):
        frame = F.symbol_feature_frame(
            "600000.SH", self.rows,
            index_rows=self.idx, breadth_rows=self.breadth, board_rows=self.board,
        )
        self.assertEqual(self._feature_cols(frame), set(F.expected_columns(F.FEATURE_GROUPS)))
        self.assertEqual(frame["_has_market"].iloc[-1], 1.0)
        self.assertEqual(frame["_has_industry"].iloc[-1], 1.0)
        self.assertEqual(len(frame), 150)

    def test_groups_switch_column_set_exact(self):
        combos = [
            ("signal",), ("price_volume",), ("market",), ("industry",),
            ("signal", "price_volume"), ("price_volume", "market", "industry"),
        ]
        for groups in combos:
            frame = F.symbol_feature_frame(
                "X", self.rows, groups=groups,
                index_rows=self.idx, breadth_rows=self.breadth, board_rows=self.board,
            )
            self.assertEqual(
                self._feature_cols(frame), set(F.expected_columns(groups)), groups
            )

    def test_no_market_data_flag_zero_and_nan(self):
        frame = F.symbol_feature_frame(
            "X", self.rows, groups=("price_volume", "market")  # 不传 index/breadth
        )
        self.assertEqual(frame["_has_market"].sum(), 0.0)
        self.assertTrue(frame["idx_ret_5"].isna().all())

    def test_no_industry_data_flag_zero(self):
        frame = F.symbol_feature_frame("X", self.rows, groups=("price_volume", "industry"))
        self.assertEqual(frame["_has_industry"].sum(), 0.0)
        self.assertTrue(frame["board_ret_5"].isna().all())

    def test_empty_rows_returns_empty_frame_with_columns(self):
        frame = F.symbol_feature_frame("X", [])
        self.assertTrue(frame.empty)
        self.assertIn("ret_5", frame.columns)
        self.assertIn("symbol", frame.columns)

    def test_unknown_group_raises(self):
        with self.assertRaises(ValueError):
            F.symbol_feature_frame("X", self.rows, groups=("bogus",))

    def test_assemble_sets_multiindex_and_concats(self):
        f1 = F.symbol_feature_frame("A", self.rows, groups=("price_volume",))
        f2 = F.symbol_feature_frame("B", self.rows, groups=("price_volume",))
        asm = F.assemble_dataset_frames([f1, f2])
        self.assertEqual(asm.index.names, ["symbol", "trade_date"])
        self.assertEqual(len(asm), 2 * len(self.rows))

    def test_assemble_empty(self):
        self.assertTrue(F.assemble_dataset_frames([]).empty)


# ---------------------------------------------------------------------------
# 前视探针
# ---------------------------------------------------------------------------
class LookaheadProbeTest(unittest.TestCase):
    def setUp(self):
        self.rows = _rows(150, seed=21)
        self.idx = synth.index_rows(150)
        self.breadth = synth.market_daily_rows(150)
        self.board = synth.board_rows(150)

    def test_probe_passes_for_all_groups(self):
        # 因果管道：扰动尾部不改变之前行 → 不抛错
        F.assert_no_lookahead(
            self.rows, n_tail=10,
            index_rows=self.idx, breadth_rows=self.breadth, board_rows=self.board,
        )

    def test_probe_passes_price_volume_only(self):
        F.assert_no_lookahead(self.rows, n_tail=15, groups=("price_volume",))

    def test_perturbation_changes_tail_but_not_prefix(self):
        # 证明探针非平凡：尾部确实被扰动改变，而前缀逐列不变
        base = F.symbol_feature_frame("X", self.rows, groups=("price_volume",))
        pert = F.symbol_feature_frame(
            "X", F._perturb_tail(self.rows, 10), groups=("price_volume",)
        )
        cutoff = len(self.rows) - 10
        cols = ["ret_5", "rsi14", "close_ma20_ratio", "vol_ratio"]
        pd.testing.assert_frame_equal(
            base.iloc[:cutoff][cols].reset_index(drop=True),
            pert.iloc[:cutoff][cols].reset_index(drop=True),
            check_exact=False, rtol=1e-9, atol=1e-9,
        )
        # 尾行 ret_5 因 close×10 而剧变（扰动确实生效）
        self.assertNotAlmostEqual(base["ret_5"].iloc[-1], pert["ret_5"].iloc[-1], places=3)

    def test_short_series_probe_noop(self):
        # rows <= n_tail → 无可校验前缀，直接返回（不抛错）
        F.assert_no_lookahead(_rows(8, seed=1), n_tail=10, groups=("price_volume",))


if __name__ == "__main__":
    unittest.main()
