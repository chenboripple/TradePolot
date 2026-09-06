"""D3 ``ml/registry.py`` 的离线测试（工件落盘/回读 + 晋升门禁 + 端到端编排）。

晋升门禁的组合逻辑用**直接注册的合成 ml_models 行**精确驱动（无需训练、可逐一控制
n_rows/n_positive/max_trade_date/oos_brier），``today`` 固定为 2026-09-06 使新鲜度门确定；
工件 round-trip / train_from_dataset / load_promoted 用真训练（强信号 ml_frame 走通 promote
全过路径，seed_market_db 走通端到端落库）。覆盖 plan 的"陈旧数据集默认拒晋升、force 后
stale=true、质量门不过 → demo"。全程合成夹具、零网络。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import synth

from ripple_tradePilot.ml import registry
from ripple_tradePilot.ml.dataset import build_dataset
from ripple_tradePilot.ml.models import train_model
from ripple_tradePilot.ml.splits import rolling_splits
from ripple_tradePilot.storage import database as db

SYMBOLS = ["002022.SZ", "600309.SH", "601816.SH"]
TODAY = datetime(2026, 9, 6, tzinfo=timezone.utc)
FRESH_DATE = "20260801"  # 距 TODAY 36 天 ≤ 120 → 新鲜度门过
STALE_DATE = "20240101"  # 距 TODAY 979 天 > 120 → 新鲜度门必触发（当前数据现实）


class _RegistryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "backtest.db"
        self.models = root / "models"
        db.init_database(self.db)

    # --- 合成 db 行（精确驱动门禁）---
    def _register_row(
        self, model_id, *, n_rows=2000, n_positive=900, max_trade_date=FRESH_DATE,
        oos_brier=0.18, base_rate_brier=0.24, target="win5", horizon=5,
        status="candidate", stale=False, kind="logreg",
    ):
        db.register_model(
            {
                "model_id": model_id, "kind": kind, "target": target, "horizon": horizon,
                "dataset_id": "ds_x", "n_rows": n_rows, "n_positive": n_positive,
                "max_trade_date": max_trade_date, "oos_brier": oos_brier,
                "base_rate_brier": base_rate_brier, "status": status, "stale": stale,
            },
            self.db,
        )
        return model_id

    # --- 真训练强信号工件（走通 promote 全过路径）---
    def _persist_strong(
        self, *, max_trade_date=FRESH_DATE, n_rows=2000, n_positive=900,
        trained_at="20240601T000000Z", kind="logreg", target="win5", seed=1,
    ):
        n = 1500
        X, y, ret, _p = synth.ml_frame(n_rows=n, n_features=5, seed=seed)
        dates = [d.strftime("%Y%m%d") for d in synth.trading_days(n)]
        exits = [dates[min(i + 6, n - 1)] for i in range(n)]
        splits = rolling_splits(dates, exits, 5, val_ratio=0.2, embargo=2)
        result = train_model(kind, target, X, y, splits)
        model_id = registry.make_model_id(
            "ds_strong", kind, target, n_splits=5, embargo=2, val_ratio=0.2,
            trained_at=trained_at,
        )
        oos_idx = result.oos_idx
        meta = {
            "model_id": model_id, "kind": kind, "target": target, "horizon": 5,
            "dataset_id": "ds_strong", "feature_columns": [f"f{i}" for i in range(5)],
            "feature_groups": ["price_volume"], "n_features": 5, "threshold": 0.5,
            "n_train": result.per_split[-1]["n_train"], "n_rows": n_rows,
            "n_positive": n_positive, "max_trade_date": max_trade_date,
            "index_code": "000300.SH", "industry_point_in_time": False,
            "split_protocol": {"n_splits": 5, "embargo": 2, "val_ratio": 0.2},
            "sklearn_version": registry.sklearn_version(), "trained_at": trained_at,
            "status": "candidate", "stale": False, "warnings": [],
        }
        oos_extras = {
            "net_ret": ret[oos_idx],
            "rec_buy": np.ones(int(oos_idx.size)),
            "trade_dates": [dates[i] for i in oos_idx],
        }
        registry.persist_model(
            result, meta=meta, oos_extras=oos_extras,
            models_dir=self.models, db_path=self.db,
        )
        return model_id, result


class MakeModelIdTest(unittest.TestCase):
    def test_deterministic_and_distinct(self):
        kw = dict(n_splits=5, embargo=2, val_ratio=0.2, trained_at="20240101T000000Z")
        a = registry.make_model_id("ds1", "logreg", "win5", **kw)
        b = registry.make_model_id("ds1", "logreg", "win5", **kw)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("logreg-win5-"))
        self.assertEqual(len(a.split("-")[-1]), 10)  # hash 前 10 位
        # 任一要素变化 → 不同 id（每次 run 唯一 candidate）
        self.assertNotEqual(a, registry.make_model_id("ds1", "hgb", "win5", **kw))
        self.assertNotEqual(a, registry.make_model_id("ds1", "logreg", "ret5", **kw))
        self.assertNotEqual(a, registry.make_model_id("ds2", "logreg", "win5", **kw))
        self.assertNotEqual(
            a,
            registry.make_model_id(
                "ds1", "logreg", "win5", n_splits=5, embargo=2, val_ratio=0.2,
                trained_at="20240102T000000Z",
            ),
        )

    def test_auto_stamp_when_trained_at_none(self):
        mid = registry.make_model_id("ds1", "logreg", "win5", n_splits=5, embargo=2, val_ratio=0.2)
        self.assertTrue(mid.startswith("logreg-win5-"))


class ArtifactRoundTripTest(_RegistryBase):
    def test_save_then_load_meta_and_oos(self):
        model_id, result = self._persist_strong()
        art = registry.load_artifact(model_id, models_dir=self.models, need_model=False)
        meta = art["meta"]
        self.assertEqual(meta["model_id"], model_id)
        self.assertEqual(meta["dataset_id"], "ds_strong")
        self.assertEqual(meta["split_protocol"]["n_splits"], 5)
        self.assertEqual(meta["status"], "candidate")
        oos = art["oos"]
        np.testing.assert_allclose(oos["p_oos"], result.p_oos)
        np.testing.assert_allclose(oos["y_oos"], result.y_oos)
        np.testing.assert_array_equal(oos["oos_idx"], result.oos_idx)
        self.assertEqual(oos["trade_dates"].shape[0], result.oos_idx.size)

    def test_load_with_model(self):
        model_id, result = self._persist_strong()
        art = registry.load_artifact(model_id, models_dir=self.models, need_model=True)
        self.assertIsNotNone(art["model"])
        self.assertIsNotNone(art["calibrator"])  # 分类有校准器
        # 载入的模型可预测，且与训练时末折一致（行数匹配）
        X, _y, _r, _p = synth.ml_frame(n_rows=20, n_features=5, seed=99)
        pred = art["calibrator"].predict_proba(X)[:, 1]
        self.assertEqual(pred.shape[0], 20)
        self.assertTrue(np.isfinite(pred).all())

    def test_artifact_files_present(self):
        model_id, _ = self._persist_strong()
        d = self.models / model_id
        for name in ("model.joblib", "calibrator.joblib", "meta.json", "oos.npz"):
            self.assertTrue((d / name).exists(), name)

    def test_load_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            registry.load_artifact("logreg-win5-doesnotexist", models_dir=self.models)


class PromoteGateTest(_RegistryBase):
    """门禁组合逻辑（合成行精确驱动，today 固定）。"""

    def test_all_gates_pass_promotes(self):
        mid = self._register_row("logreg-win5-aaa", max_trade_date=FRESH_DATE)
        rep = registry.promote(mid, db_path=self.db, models_dir=self.models, today=TODAY)
        self.assertTrue(rep.promoted)
        self.assertEqual(rep.status, "promoted")
        self.assertFalse(rep.stale)
        self.assertTrue(all(g.passed for g in rep.gates))
        self.assertEqual(db.load_model(mid, self.db)["status"], "promoted")

    def test_only_staleness_fails_no_force_rejects(self):
        mid = self._register_row("logreg-win5-bbb", max_trade_date=STALE_DATE)
        rep = registry.promote(mid, db_path=self.db, models_dir=self.models, today=TODAY)
        self.assertFalse(rep.promoted)
        self.assertEqual(rep.status, "candidate")  # 维持 candidate
        gate = {g.name: g for g in rep.gates}
        self.assertFalse(gate["max_staleness"].passed)
        self.assertTrue(gate["beats_base_rate"].passed)
        self.assertTrue(any("默认拒绝晋升" in w for w in rep.warnings))
        self.assertEqual(db.load_model(mid, self.db)["status"], "candidate")

    def test_only_staleness_fails_force_promotes_stale(self):
        mid = self._register_row("logreg-win5-ccc", max_trade_date=STALE_DATE)
        rep = registry.promote(
            mid, force=True, db_path=self.db, models_dir=self.models, today=TODAY
        )
        self.assertTrue(rep.promoted)
        self.assertTrue(rep.forced)
        self.assertEqual(rep.status, "promoted")  # 仅新鲜度不过 → 在位
        self.assertTrue(rep.stale)  # 但标 stale
        row = db.load_model(mid, self.db)
        self.assertEqual(row["status"], "promoted")
        self.assertTrue(row["stale"])

    def test_min_samples_fail_force_demo(self):
        mid = self._register_row("logreg-win5-ddd", n_rows=100, max_trade_date=FRESH_DATE)
        rep = registry.promote(
            mid, force=True, db_path=self.db, models_dir=self.models, today=TODAY
        )
        self.assertEqual(rep.status, "demo")  # 质量门不过 → 演示工件
        gate = {g.name: g for g in rep.gates}
        self.assertFalse(gate["min_samples"].passed)
        self.assertEqual(db.load_model(mid, self.db)["status"], "demo")

    def test_min_positives_fail_force_demo(self):
        mid = self._register_row("logreg-win5-eee", n_positive=10, max_trade_date=FRESH_DATE)
        rep = registry.promote(
            mid, force=True, db_path=self.db, models_dir=self.models, today=TODAY
        )
        self.assertEqual(rep.status, "demo")
        gate = {g.name: g for g in rep.gates}
        self.assertFalse(gate["min_positives"].passed)

    def test_beats_base_rate_fail_force_demo(self):
        # 弱模型：OOS Brier 不优于常数基线
        mid = self._register_row(
            "logreg-win5-fff", oos_brier=0.30, base_rate_brier=0.24, max_trade_date=FRESH_DATE
        )
        rep = registry.promote(
            mid, force=True, db_path=self.db, models_dir=self.models, today=TODAY
        )
        self.assertEqual(rep.status, "demo")
        gate = {g.name: g for g in rep.gates}
        self.assertFalse(gate["beats_base_rate"].passed)

    def test_regression_target_skips_brier_gate(self):
        # 回归目标（ret5）无 beats_base_rate 门，只有样本/正例/新鲜度三闸
        mid = self._register_row(
            "logreg-ret5-ggg", target="ret5", max_trade_date=FRESH_DATE,
            oos_brier=None, base_rate_brier=None,
        )
        rep = registry.promote(mid, db_path=self.db, models_dir=self.models, today=TODAY)
        names = {g.name for g in rep.gates}
        self.assertNotIn("beats_base_rate", names)
        self.assertTrue(rep.promoted)
        self.assertEqual(rep.status, "promoted")

    def test_unparseable_date_fails_staleness(self):
        mid = self._register_row("logreg-win5-hhh", max_trade_date=None)
        rep = registry.promote(mid, db_path=self.db, models_dir=self.models, today=TODAY)
        gate = {g.name: g for g in rep.gates}
        self.assertFalse(gate["max_staleness"].passed)
        self.assertFalse(rep.promoted)

    def test_promote_retires_old_incumbent(self):
        old = self._register_row("logreg-win5-old", status="promoted", max_trade_date=FRESH_DATE)
        new = self._register_row("logreg-win5-new", max_trade_date=FRESH_DATE)
        registry.promote(new, db_path=self.db, models_dir=self.models, today=TODAY)
        self.assertEqual(db.load_model(new, self.db)["status"], "promoted")
        self.assertEqual(db.load_model(old, self.db)["status"], "retired")  # 同 target 旧在位退役

    def test_promote_missing_model_raises(self):
        with self.assertRaises(FileNotFoundError):
            registry.promote("logreg-win5-nope", db_path=self.db, models_dir=self.models, today=TODAY)

    def test_custom_gate_thresholds(self):
        # 放宽 min_samples 到 50 → 原本 100 行也过样本门
        mid = self._register_row("logreg-win5-iii", n_rows=100, max_trade_date=FRESH_DATE)
        rep = registry.promote(
            mid, db_path=self.db, models_dir=self.models, today=TODAY, min_samples=50
        )
        self.assertTrue(rep.promoted)
        self.assertEqual(rep.status, "promoted")


class PromoteRealArtifactTest(_RegistryBase):
    """真训练强信号工件 + promote 全过 → meta.json 状态同步 + load_promoted。"""

    def test_promote_syncs_meta_json(self):
        mid, _ = self._persist_strong(max_trade_date=FRESH_DATE)
        rep = registry.promote(mid, db_path=self.db, models_dir=self.models, today=TODAY)
        self.assertEqual(rep.status, "promoted")
        art = registry.load_artifact(mid, models_dir=self.models)
        self.assertEqual(art["meta"]["status"], "promoted")
        self.assertFalse(art["meta"]["stale"])

    def test_load_promoted_returns_artifact(self):
        mid, _ = self._persist_strong(max_trade_date=FRESH_DATE)
        registry.promote(mid, db_path=self.db, models_dir=self.models, today=TODAY)
        loaded = registry.load_promoted("win5", db_path=self.db, models_dir=self.models)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["meta"]["model_id"], mid)
        self.assertIsNotNone(loaded["model"])

    def test_load_promoted_excludes_demo_by_default(self):
        # 质量门不过 → force → demo；load_promoted 默认排除 demo
        mid, _ = self._persist_strong(max_trade_date=FRESH_DATE, n_rows=100)
        registry.promote(
            mid, force=True, db_path=self.db, models_dir=self.models, today=TODAY
        )
        self.assertEqual(db.load_model(mid, self.db)["status"], "demo")
        self.assertIsNone(registry.load_promoted("win5", db_path=self.db, models_dir=self.models))
        # include_demo=True → 纳入
        loaded = registry.load_promoted(
            "win5", include_demo=True, db_path=self.db, models_dir=self.models
        )
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["meta"]["model_id"], mid)

    def test_load_promoted_none_when_empty(self):
        self.assertIsNone(registry.load_promoted("win5", db_path=self.db, models_dir=self.models))


class TrainFromDatasetTest(_RegistryBase):
    """端到端：seed_market_db → build_dataset → train_from_dataset → candidate 落库 + 工件。"""

    def setUp(self):
        super().setUp()
        self.out = Path(self._tmp.name) / "ml"
        synth.seed_market_db(self.db, symbols=SYMBOLS, days=300)
        self.manifest = build_dataset(SYMBOLS, db_path=self.db, out_dir=self.out)

    def test_train_from_dataset_registers_candidate(self):
        model_id, result = registry.train_from_dataset(
            self.manifest.dataset_id, "logreg", "win5",
            db_path=self.db, models_dir=self.models, trained_at="20240601T000000Z",
        )
        self.assertTrue(model_id.startswith("logreg-win5-"))
        row = db.load_model(model_id, self.db)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["dataset_id"], self.manifest.dataset_id)
        self.assertEqual(row["n_rows"], self.manifest.n_rows)
        self.assertEqual(row["n_positive"], self.manifest.n_positive)
        self.assertEqual(row["max_trade_date"], self.manifest.max_trade_date)
        self.assertGreater(row["n_oos"], 0)
        # 工件落盘
        self.assertTrue((self.models / model_id / "meta.json").exists())
        self.assertTrue((self.models / model_id / "oos.npz").exists())

    def test_meta_has_provenance(self):
        model_id, _ = registry.train_from_dataset(
            self.manifest.dataset_id, "logreg", "win5",
            db_path=self.db, models_dir=self.models, trained_at="20240601T000000Z",
        )
        meta = registry.load_artifact(model_id, models_dir=self.models)["meta"]
        self.assertEqual(meta["split_protocol"], {"n_splits": 5, "embargo": 2, "val_ratio": 0.2})
        self.assertEqual(set(meta["feature_groups"]), set(self.manifest.groups))
        self.assertEqual(meta["n_features"], len(self.manifest.feature_columns))
        self.assertNotEqual(meta["sklearn_version"], "")

    def test_hgb_target_variants(self):
        # hgb 分类 + 回归目标都跑得通
        mid_hgb, _ = registry.train_from_dataset(
            self.manifest.dataset_id, "hgb", "win5",
            db_path=self.db, models_dir=self.models, trained_at="20240601T000000Z",
        )
        self.assertTrue(mid_hgb.startswith("hgb-win5-"))
        mid_ret, res_ret = registry.train_from_dataset(
            self.manifest.dataset_id, "logreg", "ret5",
            db_path=self.db, models_dir=self.models, trained_at="20240601T000000Z",
        )
        self.assertTrue(mid_ret.startswith("logreg-ret5-"))
        self.assertFalse(res_ret.is_classification)

    def test_no_register_skips_db(self):
        model_id, _ = registry.train_from_dataset(
            self.manifest.dataset_id, "logreg", "win5",
            db_path=self.db, models_dir=self.models, register=False,
            trained_at="20240601T000000Z",
        )
        self.assertIsNone(db.load_model(model_id, self.db))
        self.assertTrue((self.models / model_id / "meta.json").exists())

    def test_unknown_dataset_raises(self):
        with self.assertRaises(FileNotFoundError):
            registry.train_from_dataset(
                "deadbeef0000", "logreg", "win5", db_path=self.db, models_dir=self.models
            )

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            registry.train_from_dataset(
                self.manifest.dataset_id, "logreg", "bogus",
                db_path=self.db, models_dir=self.models,
            )


if __name__ == "__main__":
    unittest.main()
