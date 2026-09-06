"""D3/D4 CLI 集成测试（``tradepilot ml datasets/train/list/promote/eval``）。

在 seed_market_db 合成库上跑通**全链路**（plan 阶段 D 验收第 1 条）：build-dataset →
train(logreg/hgb/回归) → list → eval → promote（门禁拒 + force 双路径），零网络。
合成数据是随机游走弱信号 + 停在 2025-02（陈旧），故 promote 默认拒、force 落 demo——
这正是"数据恢复前演示模型不冒充可用模型"护栏的预期表现。dataset_id/model_id 一律从
DB 读回（不解析 stdout），断言只看稳定子串。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import synth
from click.testing import CliRunner

from ripple_tradePilot.cli import cli
from ripple_tradePilot.storage import database as db

SYMBOLS = ["002022.SZ", "600309.SH", "601816.SH"]
CONFIG = 'symbols:\n  - code: "002022.SZ"\n    name: a\ntushare:\n  token: T\n'


class CliMlBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = root / "config.yaml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.db = root / "backtest.db"
        self.out = root / "ml"
        self.models = root / "models"
        synth.seed_market_db(self.db, symbols=SYMBOLS, days=300)
        env = patch.dict(
            os.environ,
            {"TRADEPILOT_CONFIG": str(self.config), "TRADEPILOT_BACKTEST_DB": str(self.db)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.runner = CliRunner()

    def _build_dataset(self, *extra):
        r = self.runner.invoke(
            cli, ["ml", "build-dataset", "--pool", "catalog", "--out-dir", str(self.out), *extra]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        return db.list_datasets(self.db)[0]["dataset_id"]

    def _train(self, dataset_id, kind="logreg", target="win5"):
        r = self.runner.invoke(
            cli,
            ["ml", "train", "--dataset", dataset_id, "--model", kind, "--target", target,
             "--models-dir", str(self.models)],
        )
        self.assertEqual(r.exit_code, 0, r.output)
        # 从 stdout 解析刚训练的 model_id（list_models[0] 在同 trained_at 下排序有歧义）
        m = re.search(r"model_id[：:]\s*(\S+)", r.output)
        self.assertIsNotNone(m, r.output)
        return m.group(1)


class DatasetsCommandTest(CliMlBase):
    def test_datasets_lists_after_build(self):
        ds = self._build_dataset()
        r = self.runner.invoke(cli, ["ml", "datasets"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn(ds, r.output)

    def test_datasets_empty_message(self):
        r = self.runner.invoke(cli, ["ml", "datasets"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("无数据集", r.output)


class TrainCommandTest(CliMlBase):
    def test_train_logreg(self):
        ds = self._build_dataset()
        r = self.runner.invoke(
            cli, ["ml", "train", "--dataset", ds, "--model", "logreg", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        for token in ("model_id", "AUC", "Brier", "candidate"):
            self.assertIn(token, r.output)
        row = db.list_models(self.db)[0]
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["dataset_id"], ds)
        self.assertTrue(row["model_id"].startswith("logreg-win5-"))

    def test_train_hgb(self):
        ds = self._build_dataset()
        mid = self._train(ds, kind="hgb")
        self.assertTrue(mid.startswith("hgb-win5-"))

    def test_train_regression_target(self):
        ds = self._build_dataset()
        r = self.runner.invoke(
            cli, ["ml", "train", "--dataset", ds, "--target", "ret5", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("MAE", r.output)  # 回归摘要
        self.assertIn("R²", r.output)
        self.assertTrue(db.list_models(self.db)[0]["model_id"].startswith("logreg-ret5-"))

    def test_train_splits_and_embargo_options(self):
        ds = self._build_dataset()
        r = self.runner.invoke(
            cli, ["ml", "train", "--dataset", ds, "--splits", "3", "--embargo", "1",
                  "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("splits=3", r.output)

    def test_train_unknown_dataset_exit_2(self):
        r = self.runner.invoke(
            cli, ["ml", "train", "--dataset", "deadbeef0000", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 2)
        self.assertIn("数据集未注册", r.output)

    def test_train_bad_model_choice_exit_2(self):
        ds = self._build_dataset()
        r = self.runner.invoke(
            cli, ["ml", "train", "--dataset", ds, "--model", "xgboost", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 2)  # click Choice 拒绝

    def test_train_no_register_skips_db(self):
        ds = self._build_dataset()
        r = self.runner.invoke(
            cli, ["ml", "train", "--dataset", ds, "--no-register", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(db.list_models(self.db), [])  # 未落库


class ListCommandTest(CliMlBase):
    def test_list_shows_model(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        r = self.runner.invoke(cli, ["ml", "list"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn(mid, r.output)
        self.assertIn("candidate", r.output)

    def test_list_empty_message(self):
        r = self.runner.invoke(cli, ["ml", "list"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("无模型", r.output)

    def test_list_status_filter(self):
        ds = self._build_dataset()
        self._train(ds)
        r = self.runner.invoke(cli, ["ml", "list", "--status", "promoted"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("无模型", r.output)  # 尚无 promoted
        r2 = self.runner.invoke(cli, ["ml", "list", "--status", "candidate"])
        self.assertIn("logreg-win5-", r2.output)


class EvalCommandTest(CliMlBase):
    def test_eval_renders_report(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        r = self.runner.invoke(cli, ["ml", "eval", "--model", mid, "--models-dir", str(self.models)])
        self.assertEqual(r.exit_code, 0, r.output)
        for token in ("判别", "校准", "决策对比", "buy_hold", "AUC", "ECE"):
            self.assertIn(token, r.output)

    def test_eval_custom_threshold(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        r = self.runner.invoke(
            cli, ["ml", "eval", "--model", mid, "--threshold", "0.7", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("model_thr0.70", r.output)

    def test_eval_writes_json(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        jpath = Path(self._tmp.name) / "reports" / "eval.json"
        r = self.runner.invoke(
            cli, ["ml", "eval", "--model", mid, "--json", str(jpath), "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertTrue(jpath.exists())
        payload = json.loads(jpath.read_text(encoding="utf-8"))
        self.assertEqual(payload["model_id"], mid)
        self.assertIn("decision_table", payload)

    def test_eval_unknown_model_exit_2(self):
        r = self.runner.invoke(
            cli, ["ml", "eval", "--model", "logreg-win5-nope", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 2)
        self.assertIn("模型工件不存在", r.output)


class PromoteCommandTest(CliMlBase):
    def test_promote_rejects_by_default(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        r = self.runner.invoke(cli, ["ml", "promote", mid, "--models-dir", str(self.models)])
        self.assertEqual(r.exit_code, 1)  # 门禁拒 → exit 1
        self.assertIn("⛔", r.output)
        self.assertIn("max_staleness", r.output)
        self.assertEqual(db.load_model(mid, self.db)["status"], "candidate")

    def test_promote_force_yields_demo(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        r = self.runner.invoke(cli, ["ml", "promote", mid, "--force", "--models-dir", str(self.models)])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("🎖️", r.output)
        # 弱信号（不跑赢基线）+ 陈旧 → force 落 demo + stale
        row = db.load_model(mid, self.db)
        self.assertEqual(row["status"], "demo")
        self.assertTrue(row["stale"])

    def test_promote_force_then_list_status_demo(self):
        ds = self._build_dataset()
        mid = self._train(ds)
        self.runner.invoke(cli, ["ml", "promote", mid, "--force", "--models-dir", str(self.models)])
        r = self.runner.invoke(cli, ["ml", "list", "--status", "demo"])
        self.assertIn(mid, r.output)

    def test_promote_unknown_model_exit_2(self):
        r = self.runner.invoke(cli, ["ml", "promote", "logreg-win5-nope", "--models-dir", str(self.models)])
        self.assertEqual(r.exit_code, 2)
        self.assertIn("模型不存在", r.output)

    def test_promote_relaxed_staleness_still_quality_gated(self):
        # 放宽新鲜度门到极大 → 仅剩质量门；弱信号不跑赢基线 → 默认仍拒
        ds = self._build_dataset()
        mid = self._train(ds)
        r = self.runner.invoke(
            cli, ["ml", "promote", mid, "--max-staleness", "100000", "--models-dir", str(self.models)]
        )
        self.assertEqual(r.exit_code, 1)
        self.assertIn("beats_base_rate", r.output)


class FullPipelineTest(CliMlBase):
    def test_end_to_end_chain(self):
        # build → train(logreg) → train(hgb) → list → eval → promote(force) 全链路绿
        ds = self._build_dataset()
        mid_lr = self._train(ds, kind="logreg")
        mid_hgb = self._train(ds, kind="hgb")
        self.assertNotEqual(mid_lr, mid_hgb)
        r_list = self.runner.invoke(cli, ["ml", "list"])
        self.assertIn(mid_lr, r_list.output)
        self.assertIn(mid_hgb, r_list.output)
        r_eval = self.runner.invoke(cli, ["ml", "eval", "--model", mid_lr, "--models-dir", str(self.models)])
        self.assertEqual(r_eval.exit_code, 0, r_eval.output)
        r_promo = self.runner.invoke(cli, ["ml", "promote", mid_lr, "--force", "--models-dir", str(self.models)])
        self.assertEqual(r_promo.exit_code, 0, r_promo.output)
        # eval 反映晋升后状态（demo + stale 警告）
        r_eval2 = self.runner.invoke(cli, ["ml", "eval", "--model", mid_lr, "--models-dir", str(self.models)])
        self.assertIn("演示模型", r_eval2.output)


if __name__ == "__main__":
    unittest.main()
