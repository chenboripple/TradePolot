"""D4 ``ml/evaluate.py`` 的离线测试（判别 + 校准 + 决策对比表 + 警告 + 渲染）。

评估链路**只读 oos.npz + meta.json + ml_datasets/index_daily 行，纯 numpy 计算**——故本测试
直接手写工件目录（meta.json + oos.npz，不写 model.joblib），既精确控制 p_oos/y_oos，又顺带
证明评估无需 sklearn/joblib。覆盖：完美模型校准≈理论值（slope≈1、ECE≈0、跑赢基线）、
常数模型判定不跑赢、决策对比表四方口径与阈值、过滤胜率≥基线、rec_buy 基线（含全 NaN 回退）、
指数同窗行（有/无）、各类诚实警告、render_text 分节、dump_json 往返、to_dict。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import synth

from ripple_tradePilot.ml.evaluate import (
    DEFAULT_THRESHOLDS,
    EvalReport,
    dump_json,
    evaluate_model,
    render_text,
)
from ripple_tradePilot.storage import database as db


class _EvalBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "backtest.db"
        self.models = root / "models"
        db.init_database(self.db)

    def _write_artifact(
        self, model_id, *, p_oos, y_oos, net_ret=None, rec_buy=None, trade_dates=None,
        status="candidate", stale=False, horizon=5, dataset_id="", kind="logreg",
        target="win5", n_rows=2000, n_positive=900, max_trade_date="20260801",
    ):
        """直接手写工件目录（meta.json + oos.npz）——不写 model.joblib，证明评估免 sklearn。"""
        d = self.models / model_id
        d.mkdir(parents=True, exist_ok=True)
        p = np.asarray(p_oos, dtype=float)
        y = np.asarray(y_oos, dtype=float)
        n = p.size
        net = np.zeros(n) if net_ret is None else np.asarray(net_ret, dtype=float)
        rec = np.full(n, np.nan) if rec_buy is None else np.asarray(rec_buy, dtype=float)
        tds = (
            np.array(["20260105"] * n, dtype="<U8")
            if trade_dates is None
            else np.asarray(list(trade_dates), dtype="<U8")
        )
        np.savez(
            d / "oos.npz",
            oos_idx=np.arange(n, dtype=np.int64), p_oos=p, y_oos=y,
            net_ret=net, rec_buy=rec, trade_dates=tds,
        )
        meta = {
            "model_id": model_id, "kind": kind, "target": target, "horizon": horizon,
            "dataset_id": dataset_id, "status": status, "stale": stale,
            "max_trade_date": max_trade_date, "n_rows": n_rows, "n_positive": n_positive,
            "warnings": [],
        }
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return model_id

    def _perfect(self, n=2000, seed=0):
        """真值链路完美模型：p_oos=p_true、y_oos=y（有解析真值，校准应≈理论值）。"""
        _X, y, ret, p_true = synth.ml_frame(n_rows=n, n_features=5, seed=seed)
        return p_true, y.astype(float), ret


class PerfectModelTest(_EvalBase):
    def test_calibration_near_theoretical(self):
        p, y, ret = self._perfect()
        mid = self._write_artifact("logreg-win5-perfect", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertTrue(rep.beats_base_rate)
        # 完美校准：logit 空间 slope≈1、intercept≈0、ECE 小
        self.assertAlmostEqual(rep.slope, 1.0, delta=0.15)
        self.assertAlmostEqual(rep.intercept, 0.0, delta=0.2)
        self.assertLess(rep.ece, 0.05)
        self.assertGreater(rep.auc, 0.7)
        self.assertLess(rep.brier, rep.base_rate_brier)

    def test_filtered_win_rate_beats_baseline(self):
        p, y, ret = self._perfect()
        mid = self._write_artifact("logreg-win5-filter", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, thresholds=(0.5, 0.6), db_path=self.db, models_dir=self.models)
        rows = {r.strategy: r for r in rep.decision_rows}
        base = rows["buy_hold"].win_rate
        # 高阈值过滤子集的真实胜率应高于整体基率（模型确实在挑更可能赢的）
        self.assertGreaterEqual(rows["model_thr0.60"].win_rate, base)
        # 覆盖率随阈值升高而下降
        self.assertGreater(rows["model_thr0.50"].coverage, rows["model_thr0.60"].coverage)

    def test_no_warnings_for_healthy_model(self):
        p, y, ret = self._perfect()
        mid = self._write_artifact("logreg-win5-clean", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        # 大样本 + 跑赢基线 + 新鲜 + 非 demo → 无 ⚠️ 级警告（指数缺失的提示除外）
        hard = [w for w in rep.warnings if w.startswith("⚠️")]
        self.assertEqual(hard, [], rep.warnings)


class ConstantModelTest(_EvalBase):
    def test_constant_fails_base_rate(self):
        rng = np.random.default_rng(0)
        y = (rng.random(1000) < 0.5).astype(float)
        p = np.full(1000, y.mean())  # 恒为基率
        mid = self._write_artifact("logreg-win5-const", p_oos=p, y_oos=y)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertFalse(rep.beats_base_rate)
        self.assertAlmostEqual(rep.auc, 0.5, delta=1e-9)  # 常数分 → 全 ties → AUC=0.5
        self.assertAlmostEqual(rep.brier, rep.base_rate_brier, places=9)
        self.assertTrue(any("未跑赢常数基线" in w for w in rep.warnings), rep.warnings)


class DecisionTableTest(_EvalBase):
    def test_strategies_and_thresholds_present(self):
        p, y, ret = self._perfect(n=800)
        mid = self._write_artifact("logreg-win5-dt", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, thresholds=(0.5, 0.55, 0.6), db_path=self.db, models_dir=self.models)
        names = [r.strategy for r in rep.decision_rows]
        self.assertIn("rule_baseline", names)
        self.assertIn("model_thr0.50", names)
        self.assertIn("model_thr0.55", names)
        self.assertIn("model_thr0.60", names)
        self.assertIn("buy_hold", names)
        rows = {r.strategy: r for r in rep.decision_rows}
        # buy_hold：覆盖率 1.0、胜率=基率、样本=n_oos
        self.assertAlmostEqual(rows["buy_hold"].coverage, 1.0)
        self.assertAlmostEqual(rows["buy_hold"].win_rate, rep.base_rate, places=9)
        self.assertEqual(rows["buy_hold"].n_signals, rep.n_oos)
        # model_thr 行的 threshold 字段正确
        self.assertAlmostEqual(rows["model_thr0.55"].threshold, 0.55)

    def test_default_thresholds_used(self):
        p, y, ret = self._perfect(n=600)
        mid = self._write_artifact("logreg-win5-defthr", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        thr_rows = [r for r in rep.decision_rows if r.strategy.startswith("model_thr")]
        self.assertEqual(len(thr_rows), len(DEFAULT_THRESHOLDS))

    def test_rule_baseline_uses_rec_buy(self):
        p, y, ret = self._perfect(n=600)
        rec = np.zeros(p.size)
        rec[:120] = 1.0  # 前 120 行规则 BUY
        mid = self._write_artifact("logreg-win5-rec", p_oos=p, y_oos=y, net_ret=ret, rec_buy=rec)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        rows = {r.strategy: r for r in rep.decision_rows}
        self.assertEqual(rows["rule_baseline"].n_signals, 120)
        self.assertAlmostEqual(rows["rule_baseline"].coverage, 120 / p.size, places=9)

    def test_rule_baseline_all_nan_falls_back(self):
        # rec_buy 全 NaN（signal 组缺失）→ rule_baseline 退化为买入持有口径（覆盖率 1.0）
        p, y, ret = self._perfect(n=500)
        mid = self._write_artifact("logreg-win5-norec", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        rows = {r.strategy: r for r in rep.decision_rows}
        self.assertAlmostEqual(rows["rule_baseline"].coverage, 1.0)
        self.assertEqual(rows["rule_baseline"].n_signals, rep.n_oos)

    def test_rule_baseline_zero_buy_warns(self):
        p, y, ret = self._perfect(n=500)
        rec = np.zeros(p.size)  # 有 rec_buy 列但全 0（OOS 内零 BUY 票）
        mid = self._write_artifact("logreg-win5-zerobuy", p_oos=p, y_oos=y, net_ret=ret, rec_buy=rec)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertTrue(any("零 BUY 票" in w for w in rep.warnings), rep.warnings)


class IndexRowTest(_EvalBase):
    def test_index_row_when_seeded(self):
        idx = synth.index_rows(60, index_code="000300.SH")
        db.upsert_index_daily("000300.SH", idx, "synth", self.db)
        dates = [str(r["trade_date"]) for r in idx][:50]  # horizon=5 窗口都在 60 内
        n = len(dates)
        rng = np.random.default_rng(2)
        y = (rng.random(n) < 0.5).astype(float)
        p = np.clip(y * 0.4 + rng.random(n) * 0.3, 0.01, 0.99)
        mid = self._write_artifact(
            "logreg-win5-idx", p_oos=p, y_oos=y, trade_dates=dates, horizon=5
        )
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models, index_code="000300.SH")
        rows = {r.strategy: r for r in rep.decision_rows}
        self.assertIn("index", rows)
        self.assertEqual(rows["index"].n_signals, 50)
        self.assertFalse(any("省略指数" in w for w in rep.warnings))

    def test_index_row_absent_warns(self):
        p, y, ret = self._perfect(n=300)
        mid = self._write_artifact("logreg-win5-noidx", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models, index_code="000300.SH")
        self.assertNotIn("index", [r.strategy for r in rep.decision_rows])
        self.assertTrue(any("省略指数" in w for w in rep.warnings), rep.warnings)


class WarningTest(_EvalBase):
    def test_stale_warning(self):
        p, y, ret = self._perfect(n=400)
        mid = self._write_artifact("logreg-win5-stale", p_oos=p, y_oos=y, net_ret=ret, stale=True)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertTrue(rep.stale)
        self.assertTrue(any("数据滞后" in w for w in rep.warnings), rep.warnings)

    def test_demo_warning(self):
        p, y, ret = self._perfect(n=400)
        mid = self._write_artifact("logreg-win5-demo", p_oos=p, y_oos=y, net_ret=ret, status="demo")
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertEqual(rep.status, "demo")
        self.assertTrue(any("演示模型" in w for w in rep.warnings), rep.warnings)

    def test_small_oos_warning(self):
        p, y, ret = self._perfect(n=120)  # <200
        mid = self._write_artifact("logreg-win5-small", p_oos=p[:120], y_oos=y[:120], net_ret=ret[:120])
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertTrue(any("样本偏少" in w for w in rep.warnings), rep.warnings)

    def test_imbalance_warning(self):
        rng = np.random.default_rng(1)
        y = (rng.random(600) < 0.05).astype(float)  # 正例率 5% 失衡
        p = np.full(600, y.mean())
        mid = self._write_artifact("logreg-win5-imb", p_oos=p, y_oos=y)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertTrue(any("正例率失衡" in w for w in rep.warnings), rep.warnings)

    def test_point_in_time_warning(self):
        # 注册一个 industry_point_in_time=False 的数据集，工件引用它 → 前视警告
        db.register_dataset(
            {"dataset_id": "ds_pit", "industry_point_in_time": False, "n_rows": 100}, self.db
        )
        p, y, ret = self._perfect(n=400)
        mid = self._write_artifact(
            "logreg-win5-pit", p_oos=p, y_oos=y, net_ret=ret, dataset_id="ds_pit"
        )
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertTrue(any("前视" in w for w in rep.warnings), rep.warnings)

    def test_missing_oos_raises(self):
        d = self.models / "logreg-win5-empty"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"model_id": "logreg-win5-empty"}), encoding="utf-8")
        # 无 oos.npz → 评估抛 ValueError
        with self.assertRaises(ValueError):
            evaluate_model("logreg-win5-empty", db_path=self.db, models_dir=self.models)


class RenderTest(_EvalBase):
    def test_render_text_sections(self):
        p, y, ret = self._perfect(n=600)
        mid = self._write_artifact("logreg-win5-render", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        text = render_text(rep)
        for token in ("模型评估", "判别", "校准", "决策对比", "AUC", "Brier", "slope", "ECE", "buy_hold"):
            self.assertIn(token, text)

    def test_dump_json_roundtrip(self):
        p, y, ret = self._perfect(n=600)
        mid = self._write_artifact("logreg-win5-json", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        out = Path(self._tmp.name) / "reports" / "eval.json"
        dump_json(rep, out)
        self.assertTrue(out.exists())
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["model_id"], mid)
        self.assertEqual(payload["n_oos"], rep.n_oos)
        self.assertIn("discrimination", payload)
        self.assertIn("calibration", payload)
        self.assertEqual(len(payload["decision_table"]), len(rep.decision_rows))
        self.assertAlmostEqual(payload["discrimination"]["auc"], rep.auc)

    def test_to_dict_structure(self):
        p, y, ret = self._perfect(n=300)
        mid = self._write_artifact("logreg-win5-todict", p_oos=p, y_oos=y, net_ret=ret)
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertIsInstance(rep, EvalReport)
        d = rep.to_dict()
        self.assertEqual(d["model_id"], mid)
        self.assertEqual(d["kind"], "logreg")
        self.assertEqual(d["target"], "win5")
        self.assertIsInstance(d["warnings"], list)
        self.assertTrue(all(isinstance(r["strategy"], str) for r in d["decision_table"]))

    def test_evaluate_is_sklearn_free(self):
        # 工件目录无 model.joblib，evaluate 仍成功 → 评估链路不依赖 sklearn/joblib
        p, y, ret = self._perfect(n=300)
        mid = self._write_artifact("logreg-win5-noskl", p_oos=p, y_oos=y, net_ret=ret)
        self.assertFalse((self.models / mid / "model.joblib").exists())
        rep = evaluate_model(mid, db_path=self.db, models_dir=self.models)
        self.assertEqual(rep.model_id, mid)


if __name__ == "__main__":
    unittest.main()
