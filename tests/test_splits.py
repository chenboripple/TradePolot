"""B3 时序切分 + purge/embargo 测试（零泄漏是硬不变量）。

逐条钉住：
- 零泄漏：任何 train/val 样本的 exit_date 严格早于其测试折首日；
- purge 边界：exit_date == test_start 剔除、< test_start 保留（逐日验证）；
- embargo：测试首日前 N 个交易日内不得有训练样本（horizon=0 时 embargo 才绑定）；
- 折间无重叠 + 时间顺序；val 是 train 日期尾部且同样 purge；
- holdout 隔离 + 与 rolling 组合时保留集永不进 train/val。
"""
from __future__ import annotations

import unittest

import synth
from ripple_tradePilot.ml.splits import holdout_split, rolling_splits


def _calendar(n_dates: int):
    """n_dates 个唯一交易日的 YYYYMMDD 字符串日历（升序）。"""
    return [d.strftime("%Y%m%d") for d in synth.trading_days(n_dates)]


def _pooled(n_dates=100, n_symbols=2, horizon=5, total_dates=130):
    """池化样本：n_symbols × n_dates 行，exit_date = trade_date 之后 horizon+1 个交易日。"""
    cal = _calendar(total_dates)
    trade_dates, exit_dates = [], []
    for _ in range(n_symbols):
        for d in range(n_dates):
            trade_dates.append(cal[d])
            exit_dates.append(cal[d + 1 + horizon])
    return cal, trade_dates, exit_dates


class RollingSplitsLeakageTest(unittest.TestCase):
    def setUp(self):
        self.cal, self.trade_dates, self.exit_dates = _pooled()
        self.cal_index = {d: i for i, d in enumerate(self.cal)}
        self.splits = rolling_splits(
            self.trade_dates, self.exit_dates, n_splits=5, val_ratio=0.2, embargo=2
        )

    def test_returns_n_splits_folds(self):
        self.assertEqual(len(self.splits), 5)
        self.assertEqual([s.split_index for s in self.splits], [0, 1, 2, 3, 4])

    def test_zero_leakage_train_val_exit_before_test_start(self):
        """核心不变量：任何 train/val 样本 exit_date < 测试折首日。"""
        for s in self.splits:
            test_start = s.test_start_date
            for r in list(s.train_idx) + list(s.val_idx):
                self.assertLess(
                    self.exit_dates[r],
                    test_start,
                    f"split {s.split_index} 行 {r} 泄漏：exit {self.exit_dates[r]} >= {test_start}",
                )

    def test_purge_actually_removed_boundary_rows(self):
        # horizon=5 → 靠近测试期的样本 exit_date 越界，purge 必须剔除非零数量
        for s in self.splits:
            self.assertGreater(s.n_purged, 0)

    def test_sets_are_pairwise_disjoint(self):
        for s in self.splits:
            train, val, test = set(s.train_idx), set(s.val_idx), set(s.test_idx)
            self.assertEqual(train & val, set())
            self.assertEqual(train & test, set())
            self.assertEqual(val & test, set())

    def test_train_and_val_strictly_before_test(self):
        for s in self.splits:
            test_start = s.test_start_date
            for r in list(s.train_idx) + list(s.val_idx):
                self.assertLess(self.trade_dates[r], test_start)
            for r in s.test_idx:
                self.assertGreaterEqual(self.trade_dates[r], test_start)
                self.assertLessEqual(self.trade_dates[r], s.test_end_date)

    def test_val_is_chronological_tail_of_train(self):
        for s in self.splits:
            if not s.val_idx or not s.train_idx:
                continue
            max_train = max(self.trade_dates[r] for r in s.train_idx)
            min_val = min(self.trade_dates[r] for r in s.val_idx)
            self.assertLessEqual(max_train, min_val)

    def test_test_folds_non_overlapping_and_ordered(self):
        seen = set()
        last_end = None
        for s in self.splits:
            test = set(s.test_idx)
            self.assertEqual(seen & test, set())
            seen |= test
            if last_end is not None:
                self.assertLess(last_end, s.test_start_date)
            last_end = s.test_end_date

    def test_train_expands_across_folds(self):
        """锚定式（expanding window）：后续折的 train+val 池不小于前折。"""
        sizes = [len(s.train_idx) + len(s.val_idx) for s in self.splits]
        for earlier, later in zip(sizes, sizes[1:]):
            self.assertLessEqual(earlier, later)

    def test_deterministic(self):
        again = rolling_splits(
            self.trade_dates, self.exit_dates, n_splits=5, val_ratio=0.2, embargo=2
        )
        self.assertEqual(
            [(s.train_idx, s.val_idx, s.test_idx) for s in self.splits],
            [(s.train_idx, s.val_idx, s.test_idx) for s in again],
        )


class EmbargoGapTest(unittest.TestCase):
    def test_embargo_empties_band_before_test(self):
        # horizon=0：exit_date == trade_date → purge 不绑定，embargo 才生效
        cal = _calendar(60)
        trade_dates, exit_dates = [], []
        for _ in range(2):  # 池化 2 标的
            for d in range(50):
                trade_dates.append(cal[d])
                exit_dates.append(cal[d])  # 无前瞻重叠
        embargo = 3
        cal_index = {x: i for i, x in enumerate(cal)}
        splits = rolling_splits(
            trade_dates, exit_dates, n_splits=4, val_ratio=0.2, embargo=embargo
        )
        self.assertTrue(splits)
        for s in splits:
            self.assertEqual(s.n_purged, 0)  # exit==trade<test_start，purge 无对象
            self.assertGreater(s.n_embargoed, 0)
            start_pos = cal_index[s.test_start_date]
            for r in list(s.train_idx) + list(s.val_idx):
                # 训练样本必须落在测试首日前 embargo 个交易日之外
                self.assertLessEqual(
                    cal_index[trade_dates[r]],
                    start_pos - embargo - 1,
                    f"split {s.split_index} 行 {r} 落入 embargo 带",
                )


class PurgeBoundaryTest(unittest.TestCase):
    def test_exit_equal_test_start_purged_less_kept(self):
        cal = _calendar(12)
        # 单标的，index == 日期位置；n_splits=1 → 2 块：block0=cal[0:5], block1=cal[5:10]
        # 故 test_start_date == cal[5]
        trade_dates = cal[:10]
        # 逐行设置 exit_date：index3 的 exit == test_start(cal[5]) → 应剔除；
        # index0/1/2/4 的 exit < cal[5] → 保留（embargo=0，仅验 purge 边界）
        exit_dates = [cal[1], cal[2], cal[4], cal[5], cal[4],
                      cal[6], cal[7], cal[8], cal[9], cal[10]]
        splits = rolling_splits(
            trade_dates, exit_dates, n_splits=1, val_ratio=0.2, embargo=0
        )
        self.assertEqual(len(splits), 1)
        s = splits[0]
        self.assertEqual(s.test_start_date, cal[5])
        train_val = set(s.train_idx) | set(s.val_idx)
        # index 3：exit == test_start → 被 purge
        self.assertNotIn(3, train_val)
        # index 0/1/2：exit < test_start → 保留在 train
        self.assertIn(0, train_val)
        self.assertIn(1, train_val)
        self.assertIn(2, train_val)
        # index 4：exit=cal[4] < test_start → 保留（落入 val 尾部）
        self.assertIn(4, train_val)
        # 恰好只 purge 了 index3（embargo=0）
        self.assertEqual(s.n_purged, 1)
        self.assertEqual(s.n_embargoed, 0)
        # test 折 = block1 = cal[5:10] 对应的行 5..9
        self.assertEqual(set(s.test_idx), {5, 6, 7, 8, 9})


class HoldoutSplitTest(unittest.TestCase):
    def test_holdout_isolation(self):
        cal, trade_dates, _ = _pooled(n_dates=100, total_dates=130)
        holdout_start = cal[80]
        hs = holdout_split(trade_dates, holdout_start)
        self.assertEqual(hs.holdout_start_date, holdout_start)
        for r in hs.train_idx:
            self.assertLess(trade_dates[r], holdout_start)
        for r in hs.holdout_idx:
            self.assertGreaterEqual(trade_dates[r], holdout_start)
        # 互斥且并集为全集
        self.assertEqual(set(hs.train_idx) & set(hs.holdout_idx), set())
        self.assertEqual(
            set(hs.train_idx) | set(hs.holdout_idx), set(range(len(trade_dates)))
        )

    def test_rolling_on_train_never_touches_holdout(self):
        cal, trade_dates, exit_dates = _pooled(n_dates=100, total_dates=130)
        holdout_start = cal[80]
        hs = holdout_split(trade_dates, holdout_start)
        holdout_rows = set(hs.holdout_idx)
        # 仅在 train 部分（非 holdout 行）上跑滚动切分
        train_rows = sorted(hs.train_idx)
        sub_trade = [trade_dates[r] for r in train_rows]
        sub_exit = [exit_dates[r] for r in train_rows]
        splits = rolling_splits(sub_trade, sub_exit, n_splits=4, val_ratio=0.2, embargo=2)
        # 子集内的局部下标映射回全局后，绝不含 holdout 行
        for s in splits:
            for local in list(s.train_idx) + list(s.val_idx) + list(s.test_idx):
                self.assertNotIn(train_rows[local], holdout_rows)
            # 且零泄漏在子集内同样成立
            for local in list(s.train_idx) + list(s.val_idx):
                self.assertLess(sub_exit[local], s.test_start_date)


class ValidationTest(unittest.TestCase):
    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            rolling_splits(["20240101", "20240102"], ["20240103"], n_splits=1)

    def test_n_splits_below_one_raises(self):
        cal = _calendar(10)
        with self.assertRaises(ValueError):
            rolling_splits(cal, cal, n_splits=0)

    def test_bad_val_ratio_raises(self):
        cal = _calendar(10)
        for vr in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                rolling_splits(cal, cal, n_splits=2, val_ratio=vr)

    def test_negative_embargo_raises(self):
        cal = _calendar(10)
        with self.assertRaises(ValueError):
            rolling_splits(cal, cal, n_splits=2, embargo=-1)

    def test_insufficient_unique_dates_raises(self):
        cal = _calendar(3)
        with self.assertRaises(ValueError):
            rolling_splits(cal, cal, n_splits=5)  # 需 ≥ 6 个唯一日


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
