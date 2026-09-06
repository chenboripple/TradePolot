"""D3 ``ml/models.py`` 的离线单元测试（锚定滚动训练 + 校准 + NaN 容忍）。

度量质量用 :func:`synth.ml_frame` 的逻辑斯谛**真值链路**（强信号，有解析真值可对），
不用 seed_market_db（随机游走弱信号，模型学不到东西，仅适合管道/CLI 测试）。覆盖：

- logreg/hgb 分类强信号 → OOS AUC≥0.6、Brier<0.25 且跑赢常数基线；
- hgb 原生吞整列 NaN 不崩、logreg 经中位数插补亦不崩，预测全有限；
- 分类有校准器、回归无校准器；OOS 索引/真值与切分一致；per_split 记录；
- 回归目标（ret5/mae5）摘要 MAE/R²；
- ``_summarize`` 与手算一致（base_rate_brier=p̄(1−p̄)）；
- 入参校验（未知 target/kind、行不匹配、空 splits、一维 X、全折跳过）；
- **sklearn 懒加载契约**：三模块顶层无 sklearn import（AST 断言）、缺失时清晰报错。
"""

from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import synth

from ripple_tradePilot.ml import calibration, evaluate, models, registry
from ripple_tradePilot.ml.models import TrainResult, _summarize, train_model
from ripple_tradePilot.ml.splits import rolling_splits


def _date_axis(n: int, horizon: int = 5):
    """n 个唯一交易日（YYYYMMDD）+ 对应 exit_date（i+1+horizon，尾部夹到末日）。"""
    dates = [d.strftime("%Y%m%d") for d in synth.trading_days(n)]
    exits = [dates[min(i + 1 + horizon, n - 1)] for i in range(n)]
    return dates, exits


def _splits(n: int, *, n_splits: int = 5, horizon: int = 5):
    dates, exits = _date_axis(n, horizon)
    return rolling_splits(dates, exits, n_splits, val_ratio=0.2, embargo=2)


class ClassificationQualityTest(unittest.TestCase):
    """强信号真值链路上的判别/校准质量（plan：logreg n=600×5 → AUC≥0.6、Brier<0.25）。"""

    def test_logreg_strong_signal_beats_base_rate(self):
        X, y, _ret, _p = synth.ml_frame(n_rows=600, n_features=5, seed=0)
        result = train_model("logreg", "win5", X, y, _splits(600))
        m = result.metrics
        self.assertTrue(result.is_classification)
        self.assertGreaterEqual(m["oos_auc"], 0.6, m)
        self.assertLess(m["oos_brier"], 0.25, m)
        # 跑赢常数基线（Brier < p̄(1−p̄)）——promote 的判别力门正是这条
        self.assertLess(m["oos_brier"], m["base_rate_brier"], m)
        self.assertGreater(m["n_oos"], 0)

    def test_hgb_strong_signal(self):
        X, y, _ret, _p = synth.ml_frame(n_rows=1200, n_features=6, seed=3)
        result = train_model("hgb", "win5", X, y, _splits(1200))
        m = result.metrics
        self.assertGreaterEqual(m["oos_auc"], 0.6, m)
        self.assertLess(m["oos_brier"], m["base_rate_brier"], m)

    def test_hgb_tolerates_all_nan_column(self):
        # plan：hgb n=2000×8 → 整组置 NaN 不崩、AUC≥0.55
        X, y, _ret, _p = synth.ml_frame(n_rows=2000, n_features=8, seed=1)
        X[:, 3] = np.nan  # 整个特征组缺失（D1 _has_* 降级的情形）
        result = train_model("hgb", "win5", X, y, _splits(2000))
        m = result.metrics
        self.assertTrue(np.isfinite(result.p_oos).all())
        self.assertGreaterEqual(m["oos_auc"], 0.55, m)

    def test_logreg_tolerates_nan_via_imputer(self):
        # logreg 走 SimpleImputer(median)+指示列：整列 NaN 亦不崩、预测有限
        X, y, _ret, _p = synth.ml_frame(n_rows=800, n_features=5, seed=2)
        X[:, 1] = np.nan
        result = train_model("logreg", "win5", X, y, _splits(800))
        self.assertTrue(np.isfinite(result.p_oos).all())
        self.assertEqual(result.p_oos.shape[0], result.oos_idx.shape[0])

    def test_classification_has_calibrator(self):
        X, y, _ret, _p = synth.ml_frame(n_rows=800, n_features=5, seed=4)
        result = train_model("logreg", "win5", X, y, _splits(800))
        # val 折足够且双类 → 末折应拟合出 Platt 校准器
        self.assertIsNotNone(result.calibrator)
        self.assertIn("C", result.selected_params)  # logreg 选了正则强度

    def test_oos_predictions_align_with_indices(self):
        X, y, _ret, _p = synth.ml_frame(n_rows=600, n_features=5, seed=5)
        splits = _splits(600)
        result = train_model("logreg", "win5", X, y, splits)
        # p_oos/y_oos 与 oos_idx 同长，y_oos 等于原 y 在这些行号上的值
        self.assertEqual(result.p_oos.shape[0], result.oos_idx.shape[0])
        self.assertEqual(result.y_oos.shape[0], result.oos_idx.shape[0])
        np.testing.assert_array_equal(result.y_oos, y[result.oos_idx])
        # oos_idx 升序、唯一，且都落在某折的 test_idx 内
        self.assertTrue(np.all(np.diff(result.oos_idx) > 0))
        test_union = set().union(*(set(s.test_idx) for s in splits))
        self.assertTrue(set(result.oos_idx.tolist()).issubset(test_union))

    def test_per_split_records(self):
        X, y, _ret, _p = synth.ml_frame(n_rows=600, n_features=5, seed=6)
        result = train_model("logreg", "win5", X, y, _splits(600))
        self.assertEqual(result.n_splits_used, len(result.per_split))
        self.assertGreater(result.n_splits_used, 1)
        for rec in result.per_split:
            self.assertGreater(rec["n_test"], 0)
            self.assertIn("params", rec)


class RegressionTest(unittest.TestCase):
    """回归目标（ret5/mae5）：无校准器，摘要给 MAE/R²。"""

    def _linear_cont(self, n=800, seed=7):
        rng = np.random.default_rng(seed)
        X = rng.normal(size=(n, 4))
        coef = np.array([1.5, -1.0, 0.5, 0.0])
        ycont = np.einsum("ij,j->i", X, coef) + rng.normal(scale=0.3, size=n)
        return X, ycont

    def test_logreg_regression_r2_positive(self):
        X, ycont = self._linear_cont()
        result = train_model("logreg", "ret5", X, ycont, _splits(len(ycont)))
        m = result.metrics
        self.assertFalse(result.is_classification)
        self.assertIsNone(result.calibrator)  # 回归不校准
        self.assertTrue(np.isfinite(m["oos_mae"]))
        self.assertGreater(m["oos_r2"], 0.5, m)  # 强线性信号，Ridge 应解释大半方差
        self.assertNotIn("oos_auc", m)

    def test_hgb_regression_mae5(self):
        X, ycont = self._linear_cont(n=1200, seed=8)
        result = train_model("hgb", "mae5", X, ycont, _splits(len(ycont)))
        m = result.metrics
        self.assertIsNone(result.calibrator)
        self.assertTrue(np.isfinite(m["oos_mae"]))
        self.assertGreater(m["oos_mae"], 0.0)


class SummarizeTest(unittest.TestCase):
    """``_summarize`` 与手算/真值一致。"""

    def test_classification_base_rate_brier_handcalc(self):
        y = np.array([1, 1, 0, 0, 0, 1])  # p̄ = 0.5
        p = np.array([0.9, 0.8, 0.2, 0.1, 0.4, 0.6])
        m = _summarize("win5", y, p, threshold=0.5)
        self.assertAlmostEqual(m["base_rate"], 0.5)
        self.assertAlmostEqual(m["base_rate_brier"], 0.25)  # p̄(1−p̄)=0.5×0.5
        self.assertAlmostEqual(m["oos_brier"], calibration.brier_score(y, p))
        self.assertAlmostEqual(m["oos_auc"], calibration.roc_auc_score(y, p))
        # 完美排序 → AUC=1.0
        self.assertAlmostEqual(m["oos_auc"], 1.0)
        self.assertAlmostEqual(m["coverage_at_threshold"], float(np.mean(p >= 0.5)))

    def test_regression_summary(self):
        y = np.array([1.0, 2.0, 3.0, 4.0])
        p = np.array([1.1, 1.9, 3.05, 3.8])
        m = _summarize("ret5", y, p, threshold=0.5)
        self.assertAlmostEqual(m["oos_mae"], float(np.mean(np.abs(p - y))))
        ss_res = float(np.sum((p - y) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        self.assertAlmostEqual(m["oos_r2"], 1.0 - ss_res / ss_tot)
        self.assertEqual(m["n_oos"], 4)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.X, self.y, _r, _p = synth.ml_frame(n_rows=200, n_features=3, seed=9)
        self.splits = _splits(200)

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            train_model("logreg", "bogus", self.X, self.y, self.splits)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            train_model("xgboost", "win5", self.X, self.y, self.splits)

    def test_row_mismatch_raises(self):
        with self.assertRaises(ValueError):
            train_model("logreg", "win5", self.X, self.y[:-5], self.splits)

    def test_1d_x_raises(self):
        with self.assertRaises(ValueError):
            train_model("logreg", "win5", self.X[:, 0], self.y, self.splits)

    def test_empty_splits_raises(self):
        with self.assertRaises(ValueError):
            train_model("logreg", "win5", self.X, self.y, [])

    def test_all_splits_skipped_raises(self):
        # 单一类别（全 0）→ 每折训练集单类被跳过 → 无 OOS → 抛错
        y_const = np.zeros_like(self.y)
        with self.assertRaises(ValueError):
            train_model("logreg", "win5", self.X, y_const, self.splits)


class LazySklearnContractTest(unittest.TestCase):
    """sklearn 懒加载：顶层不 import（AST 断言）+ 缺失时清晰报错。"""

    def test_no_toplevel_sklearn_import(self):
        for mod in (models, registry, evaluate):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            for node in tree.body:  # 仅模块顶层
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                tops = [(n or "").split(".")[0] for n in names]
                self.assertNotIn(
                    "sklearn", tops,
                    f"{mod.__name__} 顶层不应 import sklearn（懒加载契约）",
                )

    def test_require_sklearn_clear_error_when_missing(self):
        real_import = builtins.__import__

        def fake(name, *args, **kwargs):
            if name == "sklearn" or name.startswith("sklearn."):
                raise ImportError("No module named 'sklearn'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake):
            with self.assertRaises(ImportError) as ctx:
                models._require_sklearn()
        self.assertIn("scikit-learn", str(ctx.exception))

    def test_train_model_propagates_importerror(self):
        X, y, _r, _p = synth.ml_frame(n_rows=100, n_features=3, seed=10)
        with patch.object(models, "_require_sklearn", side_effect=ImportError("no sklearn")):
            with self.assertRaises(ImportError):
                train_model("logreg", "win5", X, y, _splits(100))


if __name__ == "__main__":
    unittest.main()
