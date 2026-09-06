"""D5 ``ml/scoring.py`` 的离线测试（train/serve 一致性 + 诚实降级 + 进程缓存）。

核心是 plan §D5 的验收硬测试：**同 (symbol, trade_date) 下打分器内部装配的特征行，与
``build_dataset`` 产出的该行列列一致**（逐列 allclose，含 NaN 位）。两条路都调
``dataset.load_symbol_features``，所以这条断言钉住的是"同源没被将来改坏"，而不是"两套
实现碰巧相等"——后者才是真正会悄悄漂移的东西。

夹具现实：``synth.seed_market_db`` 是弱信号随机游走，logreg OOS Brier(0.2536) 劣于基率
Brier(0.2500)，直接 promote 只能得到 demo（见 STRATEGIES.md 的诚实定位）。本文件多数用例
需要 ``status='promoted'`` 的在位工件，故 :meth:`_ScoringBase._pass_gates` 把 DB 行的门禁
输入改成能过的数值——**门禁判定逻辑本身由 tests/test_registry.py 精确覆盖**，此处不重复；
demo/stale 两条状态则走真门禁（force 晋升）以验证警告文案。全程合成夹具、零网络。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import synth

from ripple_tradePilot.ml import registry, scoring
from ripple_tradePilot.ml.calibration import decision_value_report
from ripple_tradePilot.ml.dataset import build_dataset, load_dataset, resolve_spec
from ripple_tradePilot.ml.scoring import ScoreResult, SignalScorer
from ripple_tradePilot.storage import database as db
from ripple_tradePilot.storage.database import load_daily_bars

SYMBOLS = ["002022.SZ", "600309.SH", "601816.SH"]
TODAY = datetime(2026, 9, 6, tzinfo=timezone.utc)
FRESH_DATE = "20260801"  # 距 TODAY 36 天 ≤ 120 → 新鲜度门过
STALE_DATE = "20240101"  # 距 TODAY 979 天 > 120 → 新鲜度门必触发
TRAINED_AT = "20240601T000000Z"

# 与全局默认三件套（MA 5/20、RSI 14、布林 20/2）明显不同的画像：
# signal 组的 comp_* 列取值随 spec 变，故训练快照必须能在 serve 端无损还原
CUSTOM_PROFILE = {
    "kind": "combo_vote",
    "ma_fast": 5,
    "ma_slow": 10,
    "rsi_period": 7,
    "rsi_oversold": 25,
    "rsi_overbought": 75,
    "vote_threshold": 2,
}


class _ScoringBase(unittest.TestCase):
    """类级建一次数据集（seed 1.5s + build 0.2s），每个用例拷一份库 + 独立工件目录。"""

    @classmethod
    def setUpClass(cls):
        cls._class_tmp = tempfile.TemporaryDirectory()
        root = Path(cls._class_tmp.name)
        cls.master_db = root / "master.db"
        cls.out = root / "ml"
        synth.seed_market_db(cls.master_db, symbols=SYMBOLS, days=300)
        cls.manifest = build_dataset(SYMBOLS, db_path=cls.master_db, out_dir=cls.out)
        cls.custom_manifest = build_dataset(
            SYMBOLS,
            db_path=cls.master_db,
            out_dir=cls.out,
            profile_resolver=lambda _s: dict(CUSTOM_PROFILE),
        )
        # WAL 检查点回收进主库文件，setUp 里单文件拷贝才是完整的
        with sqlite3.connect(cls.master_db) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @classmethod
    def tearDownClass(cls):
        cls._class_tmp.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "backtest.db"
        self.models = root / "models"
        shutil.copyfile(self.master_db, self.db)
        self.addCleanup(scoring.reset_scorer_cache)
        scoring.reset_scorer_cache()

    # --- 工件装配 ---------------------------------------------------------
    def _train(self, target="win5", kind="logreg", dataset_id=None):
        model_id, _ = registry.train_from_dataset(
            dataset_id or self.manifest.dataset_id,
            kind,
            target,
            db_path=self.db,
            models_dir=self.models,
            trained_at=TRAINED_AT,
        )
        return model_id

    def _pass_gates(self, model_id, *, max_trade_date=FRESH_DATE):
        """把该行的门禁输入改成"全过"的样子（见模块 docstring：门禁另有专测）。"""
        row = dict(db.load_model(model_id, self.db))
        row.update(
            {
                "n_rows": 2000,
                "n_positive": 900,
                "max_trade_date": max_trade_date,
                "oos_brier": 0.18,
                "base_rate_brier": 0.24,
            }
        )
        db.register_model(row, self.db)

    def _promoted(self, target="win5", kind="logreg", dataset_id=None, stale=False):
        """训练 + 晋升为在位模型（``stale=True`` 时走"仅新鲜度门不过 + force"真路径）。"""
        model_id = self._train(target, kind, dataset_id)
        self._pass_gates(
            model_id, max_trade_date=STALE_DATE if stale else FRESH_DATE
        )
        report = registry.promote(
            model_id,
            force=stale,
            db_path=self.db,
            models_dir=self.models,
            today=TODAY,
        )
        self.assertEqual(report.status, "promoted")
        self.assertIs(report.stale, stale)
        return model_id

    def _demo(self):
        """真门禁 + force：弱信号 OOS Brier 劣于基率 → 质量门不过 → demo。"""
        model_id = self._train()
        report = registry.promote(
            model_id, force=True, db_path=self.db, models_dir=self.models, today=TODAY
        )
        self.assertEqual(report.status, "demo")
        return model_id

    def _scorer(self, **kwargs):
        scorer = SignalScorer.try_load(
            db_path=self.db, models_dir=self.models, **kwargs
        )
        self.assertIsNotNone(scorer)
        return scorer

    def _meta(self, model_id):
        return registry.load_artifact(model_id, models_dir=self.models)["meta"]

    def _feature_frame(self, scorer, meta, symbol, warnings=None):
        """调打分器内部的特征装配（serve 端真实路径），warnings 由调用方收集。"""
        return scorer._feature_frame(
            symbol,
            meta=meta,
            groups=tuple(meta.get("feature_groups") or ()),
            index_code=str(meta.get("index_code") or "000300.SH"),
            spec=None,
            profile_resolver=None,
            db_path=self.db,
            warnings=[] if warnings is None else warnings,
        )


class TrainServeConsistencyTest(_ScoringBase):
    """plan §D5 验收硬测试：serve 端特征行 == train 端数据集行（逐列 allclose）。"""

    def _compare(self, model_id, dataset_id, manifest):
        scorer = self._scorer()
        meta = self._meta(model_id)
        columns = list(meta["feature_columns"])
        _manifest, frame = load_dataset(dataset_id, self.db)
        trained = {
            (str(row["symbol"]), str(row["trade_date"])): row
            for _, row in frame.iterrows()
        }

        warnings: list = []
        checked = 0
        for symbol in SYMBOLS:
            served_frame = self._feature_frame(scorer, meta, symbol, warnings)
            self.assertFalse(served_frame.empty)
            for _, row in served_frame.iterrows():
                key = (symbol, str(row["trade_date"]))
                if key not in trained:
                    continue  # 数据集丢了标签不全的尾部行，特征帧有而数据集无属正常
                vector = scorer._align(row.to_dict(), columns, [])[0]
                expected = np.array(
                    [float(trained[key][c]) for c in columns], dtype="float64"
                )
                # csv.gz 往返是精确的，留 1e-9 只为浮点打印余量；真偏斜是量级差异
                self.assertTrue(
                    np.allclose(vector, expected, rtol=1e-9, atol=1e-12, equal_nan=True),
                    f"{key} train/serve 特征偏斜",
                )
                checked += 1

        self.assertEqual(warnings, [])  # 有训练画像快照 → 不该报"无训练画像快照"
        self.assertEqual(checked, manifest.n_rows)  # 防空跑：每一行都比过
        return checked, columns

    def test_scorer_rows_match_dataset_rows(self):
        model_id = self._promoted()
        checked, columns = self._compare(
            model_id, self.manifest.dataset_id, self.manifest
        )
        self.assertGreater(checked, 500)
        self.assertEqual(len(columns), len(self.manifest.feature_columns))

    def test_custom_profile_snapshot_roundtrips(self):
        """自定义画像训练 → serve 用 manifest 快照还原同一 spec → 特征仍逐列一致。

        signal 组列名是 ``comp_<name>``，取值完全由 spec 决定；快照还原不了的话，
        serve 会拿默认三件套的票去喂一个按 MA5/10+RSI7 训练的模型——静默偏斜。
        """
        model_id = self._promoted(dataset_id=self.custom_manifest.dataset_id)
        scorer = self._scorer()

        spec = scorer._snapshot_spec(
            self.custom_manifest.dataset_id, SYMBOLS[0], self.db
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.vote_threshold, 2)
        params = {c.name: c.params for c in spec.components}
        self.assertEqual(params["ma"], {"fast": 5, "slow": 10})
        self.assertEqual(params["rsi"]["period"], 7)
        self.assertEqual(params["rsi"]["oversold"], 25.0)
        # 与当前默认解析结果不同 → 本用例不是空跑
        self.assertNotEqual(spec, resolve_spec(SYMBOLS[0]))

        self._compare(model_id, self.custom_manifest.dataset_id, self.custom_manifest)

    def test_custom_profile_actually_changes_signal_features(self):
        """两份数据集的 signal 组取值确有差异（否则上一条测试证明不了快照的价值）。"""
        _m1, default_frame = load_dataset(self.manifest.dataset_id, self.db)
        _m2, custom_frame = load_dataset(self.custom_manifest.dataset_id, self.db)
        joined = default_frame.merge(
            custom_frame, on=["symbol", "trade_date"], suffixes=("_d", "_c")
        )
        self.assertGreater(len(joined), 500)
        self.assertTrue(
            (joined["buy_count_d"] != joined["buy_count_c"]).any(),
            "自定义画像未改变 buy_count，夹具失去区分力",
        )

    def test_missing_snapshot_falls_back_with_warning(self):
        """manifest 不在库/不可读 → 退回当前解析，并显式警告口径可能与训练时不同。"""
        model_id = self._promoted()
        scorer = self._scorer()
        meta = dict(self._meta(model_id))
        meta["dataset_id"] = "deadbeef0000"

        warnings: list = []
        frame = self._feature_frame(scorer, meta, SYMBOLS[0], warnings)
        self.assertFalse(frame.empty)
        self.assertEqual(len(warnings), 1)
        self.assertIn("无训练画像快照", warnings[0])

    def test_signal_group_off_suppresses_snapshot_warning(self):
        """无 signal 组时画像无关，不该报那条警告（避免噪声淹没真警告）。"""
        model_id = self._promoted()
        scorer = self._scorer()
        meta = dict(self._meta(model_id))
        meta["dataset_id"] = "deadbeef0000"
        meta["feature_groups"] = ["price_volume", "market"]

        warnings: list = []
        frame = self._feature_frame(scorer, meta, SYMBOLS[0], warnings)
        self.assertFalse(frame.empty)
        self.assertEqual(warnings, [])


class TryLoadTest(_ScoringBase):
    """``try_load`` 的每一种"不可用"都返回 None，绝不抛错拖垮 dashboard/monitor。"""

    def test_none_when_nothing_promoted(self):
        self.assertIsNone(
            SignalScorer.try_load(db_path=self.db, models_dir=self.models)
        )

    def test_candidate_is_not_served(self):
        self._train()  # 只训练，不晋升
        self.assertIsNone(
            SignalScorer.try_load(db_path=self.db, models_dir=self.models)
        )

    def test_demo_excluded_by_default_and_included_on_request(self):
        model_id = self._demo()
        self.assertIsNone(
            SignalScorer.try_load(db_path=self.db, models_dir=self.models)
        )
        scorer = SignalScorer.try_load(
            include_demo=True, db_path=self.db, models_dir=self.models
        )
        self.assertIsNotNone(scorer)
        self.assertEqual(scorer.model_id, model_id)

    def test_retired_model_is_not_served(self):
        self._promoted()
        db.set_model_status(
            db.load_promoted_model("win5", path=self.db)["model_id"],
            "retired",
            path=self.db,
        )
        self.assertIsNone(
            SignalScorer.try_load(db_path=self.db, models_dir=self.models)
        )

    def test_missing_artifact_dir_returns_none(self):
        model_id = self._promoted()
        empty = Path(self._tmp.name) / "empty_models"
        empty.mkdir()
        # 行在库里是在位的，但工件目录不在 → load_promoted 吞掉 FileNotFoundError
        self.assertIsNone(
            SignalScorer.try_load(db_path=self.db, models_dir=empty)
        )
        self.assertTrue((self.models / model_id / "meta.json").exists())

    def test_aux_targets_are_optional(self):
        self._promoted()
        scorer = self._scorer()
        self.assertEqual(scorer.targets, ("win5",))

        self._promoted(target="ret5")
        scorer = self._scorer()
        self.assertEqual(set(scorer.targets), {"win5", "ret5"})

    def test_constructor_requires_primary_target(self):
        with self.assertRaises(ValueError):
            SignalScorer({"ret5": {"meta": {}}})


class ScoreSymbolTest(_ScoringBase):
    """打分主路径：概率、基准日、期望净收益、参考下行。"""

    def setUp(self):
        super().setUp()
        self.model_id = self._promoted()
        self.scorer = self._scorer()

    def test_p_win_is_finite_probability(self):
        result = self.scorer.score_symbol(SYMBOLS[0])
        self.assertIsNotNone(result)
        self.assertEqual(result.model_id, self.model_id)
        self.assertEqual(result.target, "win5")
        self.assertEqual(result.horizon_days, 5)
        self.assertEqual(result.status, "promoted")
        self.assertFalse(result.stale)
        self.assertTrue(np.isfinite(result.p_win))
        self.assertGreaterEqual(result.p_win, 0.0)
        self.assertLessEqual(result.p_win, 1.0)

    def test_as_of_defaults_to_latest_committed_bar(self):
        rows = load_daily_bars(SYMBOLS[0], self.db)
        result = self.scorer.score_symbol(SYMBOLS[0])
        # 预估永远基于库内**已收盘**的最新交易日，不用盘中未确认价
        self.assertEqual(result.as_of, str(rows[-1]["trade_date"]))
        self.assertTrue(any("预估基准日" in w for w in result.warnings))

    def test_explicit_as_of_picks_that_row(self):
        rows = load_daily_bars(SYMBOLS[0], self.db)
        middle = str(rows[len(rows) // 2]["trade_date"])
        result = self.scorer.score_symbol(SYMBOLS[0], as_of=middle)
        self.assertIsNotNone(result)
        self.assertEqual(result.as_of, middle)

    def test_unknown_as_of_returns_none(self):
        self.assertIsNone(self.scorer.score_symbol(SYMBOLS[0], as_of="19900101"))

    def test_symbol_without_bars_returns_none(self):
        self.assertIsNone(self.scorer.score_symbol("999999.SZ"))

    def test_explicit_spec_overrides_snapshot(self):
        """调用方显式给 spec（monitor 已解析好画像时）→ 不再查快照、不报口径警告。"""
        result = self.scorer.score_symbol(SYMBOLS[0], spec=resolve_spec(SYMBOLS[0]))
        self.assertIsNotNone(result)
        self.assertFalse(any("无训练画像快照" in w for w in result.warnings))

    def test_missing_feature_group_degrades_to_nan_with_warning(self):
        """serve 端少了训练时的特征组 → 按 NaN 降级 + ⚠️ 警告，仍然给出预估。"""
        artifact = registry.load_artifact(
            self.model_id, models_dir=self.models, need_model=True
        )
        artifact["meta"]["feature_groups"] = ["signal", "price_volume"]
        scorer = SignalScorer({"win5": artifact}, db_path=self.db)

        result = scorer.score_symbol(SYMBOLS[0])
        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.p_win))
        joined = [w for w in result.warnings if "特征列缺失" in w]
        self.assertEqual(len(joined), 1)
        self.assertIn("⚠️", joined[0])
        self.assertIn("idx_ret_5", joined[0])  # market 组列名被点名

    def test_expected_return_matches_oos_decision_value(self):
        """无 ret5 在位模型时，期望净收益 = 主模型自己 OOS 上 p≥p_win 子集的已实现均值。"""
        result = self.scorer.score_symbol(SYMBOLS[0])
        oos = registry.load_artifact(self.model_id, models_dir=self.models)["oos"]
        report = decision_value_report(
            oos["y_oos"], oos["net_ret"], oos["p_oos"], threshold=result.p_win
        )
        self.assertGreater(report.n_signals, 0)
        self.assertAlmostEqual(result.expected_net_return, report.avg_net_return, places=10)
        self.assertEqual(result.oos_n_signals, report.n_signals)
        self.assertAlmostEqual(result.oos_coverage, report.coverage, places=10)
        self.assertAlmostEqual(result.oos_win_rate, report.win_rate, places=10)
        self.assertIn("OOS 历史", result.return_basis)
        self.assertIn(f"p≥{result.p_win:.3f}", result.return_basis)

    def test_downside_unavailable_is_none_not_zero(self):
        result = self.scorer.score_symbol(SYMBOLS[0])
        self.assertIsNone(result.downside_mae)
        self.assertTrue(any("无 mae5 在位模型" in w for w in result.warnings))

    def test_stale_model_warns(self):
        scorer = SignalScorer.try_load(db_path=self.db, models_dir=self.models)
        self.assertFalse(scorer._artifacts["win5"]["meta"]["stale"])
        stale_id = self._promoted(stale=True, dataset_id=self.custom_manifest.dataset_id)
        stale_scorer = self._scorer()
        self.assertEqual(stale_scorer.model_id, stale_id)
        result = stale_scorer.score_symbol(SYMBOLS[0])
        self.assertTrue(result.stale)
        self.assertTrue(any("数据滞后" in w and "⚠️" in w for w in result.warnings))

    def test_demo_model_warns(self):
        model_id = self._demo()
        scorer = SignalScorer.try_load(
            include_demo=True, db_path=self.db, models_dir=self.models
        )
        self.assertEqual(scorer.model_id, model_id)
        result = scorer.score_symbol(SYMBOLS[0])
        self.assertEqual(result.status, "demo")
        self.assertTrue(any("演示模型" in w for w in result.warnings))

    def test_result_to_dict_is_json_ready(self):
        result = self.scorer.score_symbol(SYMBOLS[0])
        payload = result.to_dict()
        self.assertEqual(payload["model_id"], self.model_id)
        self.assertEqual(payload["p_win"], round(result.p_win, 4))
        self.assertIsNone(payload["downside_mae"])  # None 原样保留，不补 0
        self.assertIsInstance(payload["warnings"], list)
        json.dumps(payload, ensure_ascii=False)  # 可序列化


class AuxTargetTest(_ScoringBase):
    """ret5 / mae5 在位时接管对应字段。"""

    def setUp(self):
        super().setUp()
        self._promoted()
        self._promoted(target="ret5")
        self._promoted(target="mae5")
        self.scorer = self._scorer()

    def test_three_targets_loaded(self):
        self.assertEqual(set(self.scorer.targets), {"win5", "ret5", "mae5"})

    def test_expected_return_from_ret5_model(self):
        result = self.scorer.score_symbol(SYMBOLS[0])
        self.assertIsNotNone(result.expected_net_return)
        self.assertTrue(np.isfinite(result.expected_net_return))
        self.assertIn("ret5 在位模型", result.return_basis)
        self.assertEqual(result.oos_n_signals, 0)  # 走点预测，不是 OOS 子集口径
        self.assertIsNone(result.oos_coverage)

    def test_downside_from_mae5_model_is_negative(self):
        result = self.scorer.score_symbol(SYMBOLS[0])
        self.assertIsNotNone(result.downside_mae)
        # mae5 标签是 |回撤| 的非负量，统一以负数呈现，UI 直读"参考下行 −3.1%"
        self.assertLessEqual(result.downside_mae, 0.0)
        self.assertFalse(any("无 mae5 在位模型" in w for w in result.warnings))


class OosFallbackTest(_ScoringBase):
    """OOS 子集口径的三种边界：无 OOS、子集为空、子集过小。"""

    def _scorer_with_oos(self, oos):
        artifact = registry.load_artifact(
            self._promoted_once(), models_dir=self.models, need_model=True
        )
        if oos is None:
            artifact.pop("oos", None)
        else:
            artifact["oos"] = oos
        return SignalScorer({"win5": artifact}, db_path=self.db)

    def setUp(self):
        super().setUp()
        self._model_id = None

    def _promoted_once(self):
        if self._model_id is None:
            self._model_id = self._promoted()
        return self._model_id

    def test_no_oos_and_no_ret5_returns_none(self):
        scorer = self._scorer_with_oos(None)
        result = scorer.score_symbol(SYMBOLS[0])
        self.assertIsNotNone(result)
        self.assertIsNone(result.expected_net_return)
        self.assertEqual(result.return_basis, "")
        self.assertTrue(any("无 OOS 历史" in w for w in result.warnings))

    def test_empty_subset_explains_instead_of_zero(self):
        # 全 0 概率 → 任何 p_win 都高于 OOS 见过的最高置信度 → 子集为空
        scorer = self._scorer_with_oos(
            {
                "p_oos": np.zeros(50),
                "y_oos": np.ones(50, dtype=int),
                "net_ret": np.full(50, 0.01),
            }
        )
        result = scorer.score_symbol(SYMBOLS[0])
        self.assertIsNone(result.expected_net_return)
        self.assertTrue(any("OOS 历史中无 p ≥" in w for w in result.warnings))

    def test_small_subset_warns_about_noise(self):
        # 高概率子集只有 5 个样本 → 期望收益噪声大，必须警告而不是照报
        scorer = self._scorer_with_oos(
            {
                "p_oos": np.concatenate([np.full(5, 0.999), np.zeros(200)]),
                "y_oos": np.concatenate([np.ones(5, dtype=int), np.zeros(200, dtype=int)]),
                "net_ret": np.concatenate([np.full(5, 0.02), np.full(200, -0.01)]),
            }
        )
        result = scorer.score_symbol(SYMBOLS[0])
        self.assertLess(result.p_win, 0.999)  # 夹具概率落在那 5 个高置信样本之下
        self.assertEqual(result.oos_n_signals, 5)
        self.assertAlmostEqual(result.expected_net_return, 0.02, places=10)
        self.assertTrue(any("仅 5 个样本" in w and "⚠️" in w for w in result.warnings))


class GetScorerCacheTest(_ScoringBase):
    """进程缓存：不反复反序列化 joblib，但在位模型换了必须立刻跟上。"""

    def test_none_when_nothing_promoted(self):
        self.assertIsNone(
            scoring.get_scorer(db_path=self.db, models_dir=self.models)
        )
        # 负结果也缓存（每次只多一次廉价 DB 查询），晋升后自动失效
        self._promoted()
        scorer = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        self.assertIsNotNone(scorer)

    def test_reuses_instance_while_incumbent_unchanged(self):
        self._promoted()
        first = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        second = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        self.assertIs(first, second)

    def test_reloads_when_incumbent_changes(self):
        first_id = self._promoted()
        first = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        self.assertEqual(first.model_id, first_id)

        # 新数据集再训一个并晋升 → 旧在位退役，缓存必须失效
        second_id = self._promoted(dataset_id=self.custom_manifest.dataset_id)
        self.assertNotEqual(second_id, first_id)
        second = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        self.assertIsNot(second, first)
        self.assertEqual(second.model_id, second_id)

    def test_reset_clears_cache(self):
        self._promoted()
        first = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        scoring.reset_scorer_cache()
        second = scoring.get_scorer(db_path=self.db, models_dir=self.models)
        self.assertIsNot(first, second)
        self.assertEqual(first.model_id, second.model_id)

    def test_demo_flag_is_a_separate_cache_key(self):
        self._demo()
        self.assertIsNone(scoring.get_scorer(db_path=self.db, models_dir=self.models))
        demo = scoring.get_scorer(
            include_demo=True, db_path=self.db, models_dir=self.models
        )
        self.assertIsNotNone(demo)
        self.assertEqual(demo.model_id, db.load_promoted_model(
            "win5", include_demo=True, path=self.db
        )["model_id"])


class PureHelperTest(unittest.TestCase):
    """不需要夹具的纯函数/数据类用例。"""

    def test_parse_date_variants(self):
        expected = datetime(2026, 8, 1)
        for raw in ("20260801", "2026-08-01", "20260801T000000Z",
                    "2026-08-01T00:00:00", "  20260801  "):
            self.assertEqual(scoring._parse_date(raw), expected, raw)

    def test_parse_date_rejects_junk(self):
        for raw in (None, "", "n/a", "2026", "20261301", "abcdefgh"):
            self.assertIsNone(scoring._parse_date(raw), raw)

    def test_score_result_to_dict_rounds_and_preserves_none(self):
        result = ScoreResult(
            model_id="logreg-win5-abc",
            target="win5",
            horizon_days=5,
            as_of="20260801",
            p_win=0.412345678,
            expected_net_return=0.0123456789,
            downside_mae=None,
            status="demo",
            stale=True,
            return_basis="口径",
            oos_n_signals=12,
            oos_coverage=0.345678,
            oos_win_rate=0.5678,
            warnings=("⚠️ 演示模型",),
        )
        payload = result.to_dict()
        self.assertEqual(payload["p_win"], 0.4123)
        self.assertEqual(payload["expected_net_return"], 0.012346)
        self.assertIsNone(payload["downside_mae"])
        self.assertEqual(payload["oos_coverage"], 0.3457)
        self.assertEqual(payload["oos_win_rate"], 0.5678)
        self.assertIs(payload["stale"], True)
        self.assertEqual(payload["status"], "demo")
        self.assertEqual(payload["warnings"], ["⚠️ 演示模型"])

    def test_status_warnings_skip_unparseable_date(self):
        warnings = SignalScorer._status_warnings({"status": "promoted"}, "")
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
