"""D6 ``ml/pipeline.py`` 的离线单元测试（一键管道编排 + markdown 报告）。

两层覆盖，开销分明：

- **纯渲染/校验层**（占多数）：手搓 :class:`PipelineReport` / :class:`EvalReport`，零训练、
  零 sklearn、零 DB——报告里"缺失不写成 0"、空子集不给数字、门禁分节、警告去重这些
  诚实性约定全在这层钉死。
- **真跑层**（:class:`RealPipelineTest`）：合成库上跑三次 ``run_pipeline``（默认 / force 晋升 /
  门禁拒），``setUpClass`` 共享，验证与 D2/D3/D4 的接线口径（数据集只注册一次、评估只出
  分类目标、默认不动在位模型、force 落 demo+stale）。

合成数据是随机游走弱信号 + 停在 2025-02，故门禁必拒、force 必落 demo——这正是护栏意图，
度量质量由 tests/test_models.py 的 ``synth.ml_frame`` 真值链路负责，本文件不重复。
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import synth

from ripple_tradePilot.ml import pipeline
from ripple_tradePilot.ml.dataset import build_dataset
from ripple_tradePilot.ml.evaluate import DecisionRow, EvalReport
from ripple_tradePilot.ml.pipeline import (
    MODEL_KINDS,
    PipelineReport,
    PromotionOutcome,
    TrainedModel,
    collect_warnings,
    fmt_number,
    fmt_pct,
    render_markdown,
    run_pipeline,
    write_report,
)
from ripple_tradePilot.ml.registry import GateResult, PromotionReport
from ripple_tradePilot.storage import database as db

SYMBOLS = ("002022.SZ", "600309.SH", "601816.SH")


# ---------------------------------------------------------------------------
# 手搓报告（纯渲染层夹具）
# ---------------------------------------------------------------------------
def _decision_row(strategy, *, threshold=None, coverage=0.5, win_rate=0.55,
                  avg_net_return=0.012, expectancy=0.012, n_signals=100):
    return DecisionRow(
        strategy=strategy, threshold=threshold, coverage=coverage, win_rate=win_rate,
        avg_net_return=avg_net_return, expectancy=expectancy, n_signals=n_signals,
    )


def _eval_report(**overrides):
    payload = dict(
        model_id="logreg-win5-aaaa1111",
        kind="logreg",
        target="win5",
        dataset_id="bbbb2222cccc",
        n_oos=735,
        auc=0.5582,
        logloss=0.68,
        brier=0.2536,
        base_rate=0.497,
        base_rate_brier=0.25,
        beats_base_rate=False,
        slope=0.415,
        intercept=-0.099,
        ece=0.0675,
        reliability=(
            {"bin": 0, "lower": 0.0, "upper": 0.1, "count": 0,
             "mean_predicted": 0.0, "mean_actual": 0.0},
            {"bin": 5, "lower": 0.5, "upper": 0.6, "count": 283,
             "mean_predicted": 0.555, "mean_actual": 0.481},
        ),
        decision_rows=(
            _decision_row("rule_baseline", n_signals=0, coverage=0.0, win_rate=0.0,
                          avg_net_return=0.0, expectancy=0.0),
            _decision_row("model_thr0.50", threshold=0.5),
            _decision_row("buy_hold", coverage=1.0),
            _decision_row("index", coverage=1.0, win_rate=0.571, avg_net_return=0.0022),
        ),
        status="candidate",
        stale=False,
        warnings=("⚠️ 未跑赢常数基线：OOS Brier 0.2536 ≥ 基线 0.2500，模型无判别增益",),
    )
    payload.update(overrides)
    return EvalReport(**payload)


def _trained(kind="logreg", target="win5", model_id="logreg-win5-aaaa1111",
             is_classification=True, n_oos=735, **metrics):
    return TrainedModel(
        kind=kind, target=target, model_id=model_id, is_classification=is_classification,
        n_oos=n_oos, metrics=metrics,
    )


def _report(**overrides):
    """一份"像真的"的 :class:`PipelineReport`（manifest 用 D2 的字典形态）。"""
    payload = dict(
        dataset_id="bbbb2222cccc",
        manifest={
            "dataset_id": "bbbb2222cccc",
            "n_rows": 882,
            "n_positive": 431,
            "positive_rate": 0.4887,
            "start_date": "20240102",
            "end_date": "20250214",
            "max_trade_date": "20250214",
            "symbols": list(SYMBOLS),
            "rejected": [],
            "feature_columns": [f"f{i}" for i in range(40)],
            "cost_model": {"fee_rate": 0.0003, "slippage": 0.001},
            "warnings": [],
        },
        groups=("signal", "price_volume", "market", "industry"),
        split_protocol={"n_splits": 5, "embargo": 2, "val_ratio": 0.2},
        trained=(
            _trained(oos_auc=0.5582, oos_brier=0.2536, base_rate_brier=0.25, oos_ece=0.0675),
            _trained(target="ret5", model_id="logreg-ret5-dddd3333",
                     is_classification=False, oos_mae=0.0268, oos_r2=-0.881),
        ),
        evaluations=(_eval_report(),),
        promotions=(),
        generated_at="20260906T120000Z",
    )
    payload.update(overrides)
    return PipelineReport(**payload)


def _promotion(**overrides):
    payload = dict(
        model_id="logreg-win5-aaaa1111",
        status="demo",
        promoted=True,
        stale=True,
        forced=True,
        gates=(
            ("min_samples", True, "n_rows=882 需 ≥ 800"),
            ("max_staleness", False, "数据滞后 569 天（>120 即拒）"),
        ),
        warnings=("门禁未过：max_staleness（数据滞后 569 天（>120 即拒））",),
    )
    payload.update(overrides)
    return PromotionOutcome(**payload)


class FormatterTest(unittest.TestCase):
    """缺失一律 ``n/a``——报告里绝不把"没有这个数"渲染成 0。"""

    def test_none_and_nan_render_na(self):
        for value in (None, float("nan")):
            self.assertEqual(fmt_number(value), "n/a")
            self.assertEqual(fmt_pct(value), "n/a")

    def test_numbers(self):
        self.assertEqual(fmt_number(0.55821), "0.5582")
        self.assertEqual(fmt_number(-0.8812, 3), "-0.881")
        self.assertEqual(fmt_pct(0.4887, 1), "48.9%")
        self.assertEqual(fmt_pct(0.0123), "1.23%")
        self.assertEqual(fmt_pct(-0.031), "-3.10%")

    def test_junk_falls_back_to_str(self):
        self.assertEqual(fmt_number("abc"), "abc")
        self.assertEqual(fmt_pct("x"), "x")

    def test_targets_dedup_and_order(self):
        self.assertEqual(pipeline._targets("win5", ("ret5", "mae5")), ("win5", "ret5", "mae5"))
        self.assertEqual(pipeline._targets("win5", ("win5", "ret5", "", " ret5 ")),
                         ("win5", "ret5"))
        self.assertEqual(pipeline._targets("win5", ()), ("win5",))


class RenderMarkdownTest(unittest.TestCase):
    def test_sections_present(self):
        text = render_markdown(_report())
        for token in ("# ML 信号质量管道报告", "bbbb2222cccc", "882 行", "48.9%",
                      "20240102 ~ 20250214", "20250214", "3 只", "signal,price_volume,market,industry",
                      "40 列", "锚定滚动 5 折", "val_ratio 0.2", "embargo 2",
                      "fee_rate=0.0003", "## 模型", "## 决策对比", "### 可靠性分桶",
                      "## 晋升门禁", "## 警告", "## 诚实定位"):
            self.assertIn(token, text)

    def test_model_table_lists_every_trained_model(self):
        text = render_markdown(_report())
        self.assertIn("logreg-win5-aaaa1111", text)
        self.assertIn("logreg-ret5-dddd3333", text)

    def test_regression_row_metrics_are_na_not_zero(self):
        # 回归目标没有 AUC/Brier：写成 0.0000 会被读成"判别力为零"（其实是"不适用"）
        row = [line for line in render_markdown(_report()).splitlines()
               if "logreg-ret5-dddd3333" in line][0]
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[4:8], ["n/a", "n/a", "n/a", "n/a"])  # AUC/Brier/基线/ECE
        self.assertEqual(cells[8], "0.0268")                       # MAE 有值
        self.assertEqual(cells[9], "-0.881")                       # R² 有值

    def test_empty_decision_subset_renders_dash(self):
        # OOS 折内规则零 BUY 票 → rule_baseline 空子集；四个值列必须是 —，不是 0.00%
        text = render_markdown(_report())
        baseline = [line for line in text.splitlines() if "rule_baseline" in line][0]
        cells = [c.strip() for c in baseline.strip("|").split("|")]
        self.assertEqual(cells[0], "`rule_baseline`")
        self.assertEqual(cells[1], "—")          # 无阈值
        self.assertEqual(cells[2:6], ["—", "—", "—", "—"])
        self.assertEqual(cells[6], "0")          # 样本数照实给 0
        filtered = [line for line in text.splitlines() if "model_thr0.50" in line][0]
        self.assertIn("50.0%", filtered)         # 有样本的行照给百分数

    def test_empty_reliability_bucket_renders_dash(self):
        text = render_markdown(_report())
        bucket0 = [line for line in text.splitlines()
                   if line.startswith("| 0 |")][0]
        self.assertIn("| 0 | — | — |", bucket0)
        bucket5 = [line for line in text.splitlines() if line.startswith("| 5 |")][0]
        self.assertIn("55.5%", bucket5)
        self.assertIn("48.1%", bucket5)

    def test_no_evaluations_skips_decision_sections(self):
        text = render_markdown(_report(evaluations=()))
        self.assertIsNone(_report(evaluations=()).primary)
        self.assertNotIn("## 决策对比", text)
        self.assertNotIn("### 可靠性分桶", text)
        self.assertIn("## 模型", text)           # 清单照出（训练成功了，只是没得评估）

    def test_primary_prefers_classification_target(self):
        regression_first = _report(evaluations=(
            _eval_report(model_id="logreg-ret5-dddd3333", kind="logreg", target="ret5"),
            _eval_report(),
        ))
        self.assertEqual(regression_first.primary.model_id, "logreg-win5-aaaa1111")
        only_regression = _report(evaluations=(
            _eval_report(model_id="logreg-ret5-dddd3333", target="ret5"),
        ))
        self.assertEqual(only_regression.primary.model_id, "logreg-ret5-dddd3333")

    def test_no_promotion_says_so_explicitly(self):
        text = render_markdown(_report())
        self.assertIn("本次未请求晋升", text)
        self.assertIn("candidate", text)

    def test_promotion_section_renders_gates(self):
        text = render_markdown(_report(promotions=(_promotion(),)))
        self.assertNotIn("本次未请求晋升", text)
        self.assertIn("**demo**", text)
        self.assertIn("（stale）", text)
        self.assertIn("（force 放行）", text)
        self.assertIn("✓ `min_samples`：n_rows=882 需 ≥ 800", text)
        self.assertIn("✗ `max_staleness`：数据滞后 569 天", text)

    def test_missing_cost_model_renders_na(self):
        manifest = dict(_report().manifest, cost_model=None)
        self.assertIn("成本口径：n/a", render_markdown(_report(manifest=manifest)))

    def test_rejected_symbols_counted_and_warned(self):
        manifest = dict(
            _report().manifest,
            rejected=[{"symbol": "600000.SH", "reason": "复权基准漂移嫌疑"}],
        )
        text = render_markdown(_report(manifest=manifest))
        self.assertIn("拒入 1 只", text)
        self.assertIn("拒入 600000.SH：复权基准漂移嫌疑", text)

    def test_honesty_note_is_always_the_last_section(self):
        for report in (_report(), _report(evaluations=()), _report(promotions=(_promotion(),))):
            text = render_markdown(report)
            self.assertIn("## 诚实定位", text)
            tail = text.split("## 诚实定位", 1)[1]
            self.assertIn("演示工件不可作为交易依据", tail)
            self.assertIn("锚定滚动样本外", tail)
            self.assertNotIn("##", tail)          # 之后不再有任何分节

    def test_no_warnings_renders_placeholder(self):
        report = _report(evaluations=(_eval_report(warnings=()),))
        warnings_block = render_markdown(report).split("## 警告", 1)[1]
        self.assertIn("（无）", warnings_block.split("## 诚实定位")[0])


class CollectWarningsTest(unittest.TestCase):
    def test_dedups_across_sources_and_keeps_order(self):
        shared = "⚠️ 数据滞后"
        manifest = dict(_report().manifest, warnings=[shared, "行业单快照"])
        report = _report(
            manifest=manifest,
            evaluations=(_eval_report(warnings=(shared, "⚠️ 未跑赢常数基线")),),
            promotions=(_promotion(warnings=("门禁未过：max_staleness",)),),
        )
        self.assertEqual(
            collect_warnings(report),
            [shared, "行业单快照", "⚠️ 未跑赢常数基线", "门禁未过：max_staleness"],
        )

    def test_empty_when_nothing_to_say(self):
        report = _report(evaluations=(_eval_report(warnings=()),))
        self.assertEqual(collect_warnings(report), [])

    def test_rejected_symbols_become_warnings(self):
        manifest = dict(_report().manifest,
                        rejected=[{"symbol": "600000.SH", "reason": "日线不足"}])
        report = _report(manifest=manifest, evaluations=(_eval_report(warnings=()),))
        self.assertEqual(collect_warnings(report), ["拒入 600000.SH：日线不足"])


class WriteReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_writes_named_file_and_creates_dirs(self):
        report = _report()
        path = write_report(report, self.root / "nested" / "reports")
        self.assertEqual(path.name, "ml-pipeline-bbbb2222cccc.md")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), render_markdown(report))

    def test_rerun_overwrites_same_path(self):
        first = write_report(_report(), self.root)
        second = write_report(_report(), self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.root.glob("ml-pipeline-*.md"))), 1)

    def test_accepts_str_dir(self):
        path = write_report(_report(), str(self.root))
        self.assertTrue(path.exists())


class PromotionOutcomeTest(unittest.TestCase):
    def test_from_report_flattens_gates(self):
        report = PromotionReport(
            model_id="hgb-win5-eeee4444",
            promoted=True,
            status="promoted",
            stale=True,
            forced=True,
            gates=(GateResult("min_samples", True, "n_rows=900 需 ≥ 800"),
                   GateResult("max_staleness", False, "数据滞后 130 天（>120 即拒）")),
            warnings=("数据滞后（max_trade_date=20250214），已标 stale=True",),
        )
        outcome = PromotionOutcome.from_report(report)
        self.assertEqual(outcome.model_id, "hgb-win5-eeee4444")
        self.assertEqual(outcome.status, "promoted")
        self.assertTrue(outcome.promoted and outcome.stale and outcome.forced)
        self.assertEqual(outcome.gates,
                         (("min_samples", True, "n_rows=900 需 ≥ 800"),
                          ("max_staleness", False, "数据滞后 130 天（>120 即拒）")))
        self.assertEqual(len(outcome.warnings), 1)


class ValidationTest(unittest.TestCase):
    """入参校验必须在任何副作用之前（不留半截数据集/工件）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "untouched.db"

    def test_empty_pool_raises_without_touching_db(self):
        for symbols in ((), [], ["", "  "]):
            with self.subTest(symbols=symbols):
                with self.assertRaises(ValueError) as ctx:
                    run_pipeline(symbols, db_path=self.db)
                self.assertIn("股票池为空", str(ctx.exception))
        self.assertFalse(self.db.exists())

    def test_unknown_kind_raises_without_touching_db(self):
        with self.assertRaises(ValueError) as ctx:
            run_pipeline(SYMBOLS, kinds=("xgb", "logreg"), db_path=self.db)
        self.assertIn("xgb", str(ctx.exception))
        self.assertIn(str(list(MODEL_KINDS)), str(ctx.exception))
        self.assertFalse(self.db.exists())


class RealPipelineTest(unittest.TestCase):
    """真跑三次管道（合成库 3 标的 × 300 天），验与 D2/D3/D4 的接线口径。

    训练是秒级开销 → ``setUpClass`` 跑一次共享，各测试只读不改（晋升改的是模型状态，
    而三次跑的 model_id 各不相同，互不干扰）。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        # 三次跑各用一份 DB 副本：晋升会改 ml_models 行状态，共享一个库会让"默认不晋升"
        # 的断言被后两次 force/gated 跑污染。数据集内容一致 → 三份 dataset_id 相同。
        cls.messages = []
        cls.plain = cls._run(root, "plain", messages=cls.messages, aux_targets=("ret5",))
        cls.forced = cls._run(root, "forced", promote_models=True, force=True)
        cls.gated = cls._run(root, "gated", promote_models=True)
        cls.db = root / "plain.db"

    @classmethod
    def _run(cls, root, tag, messages=None, **kwargs):
        db_path = synth.seed_market_db(root / f"{tag}.db", symbols=SYMBOLS, days=300)
        return run_pipeline(
            SYMBOLS, db_path=db_path, out_dir=root / f"ml-{tag}",
            models_dir=root / f"models-{tag}", kinds=("logreg",),
            n_splits=3, embargo=1,
            progress=(messages.append if messages is not None else None),
            **kwargs,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_dataset_id_stable_across_runs_and_registered(self):
        datasets = db.list_datasets(self.db)
        self.assertEqual(len(datasets), 1)
        self.assertEqual(datasets[0]["dataset_id"], self.plain.dataset_id)
        # 同参数三次跑（不同 DB 副本）→ 同 dataset_id：内容寻址，不含机器/时间因素
        self.assertEqual(self.plain.dataset_id, self.forced.dataset_id)
        self.assertEqual(self.plain.dataset_id, self.gated.dataset_id)
        row = datasets[0]
        self.assertEqual(row["n_rows"], self.plain.manifest["n_rows"])
        self.assertEqual(row["max_trade_date"], self.plain.manifest["max_trade_date"])
        self.assertGreater(row["n_rows"], 0)

    def test_dataset_matches_standalone_build(self):
        # 管道不另立口径：与 ml build-dataset 同参数 → 同 dataset_id
        manifest = build_dataset(
            SYMBOLS, db_path=self.db, out_dir=Path(self._tmp.name) / "ml"
        )
        self.assertEqual(manifest.dataset_id, self.plain.dataset_id)

    def test_trained_order_and_classification_flags(self):
        self.assertEqual(
            [(item.kind, item.target, item.is_classification) for item in self.plain.trained],
            [("logreg", "win5", True), ("logreg", "ret5", False)],
        )
        self.assertTrue(all(item.model_id.startswith(f"{item.kind}-{item.target}-")
                            for item in self.plain.trained))
        self.assertTrue(all(item.n_oos > 0 for item in self.plain.trained))
        self.assertTrue(math.isfinite(self.plain.trained[0].metrics["oos_auc"]))

    def test_evaluations_only_for_classification_targets(self):
        self.assertEqual(len(self.plain.evaluations), 1)
        self.assertEqual(self.plain.evaluations[0].model_id, self.plain.trained[0].model_id)
        self.assertEqual(self.plain.primary.target, "win5")
        self.assertEqual(self.forced.primary.target, "win5")

    def test_split_protocol_and_groups_recorded(self):
        self.assertEqual(self.plain.split_protocol,
                         {"n_splits": 3, "embargo": 1, "val_ratio": 0.2})
        self.assertEqual(self.plain.groups, ("signal", "price_volume", "market", "industry"))
        self.assertEqual(len(self.plain.manifest["feature_columns"]), 40)

    def test_generated_at_is_utc_stamp(self):
        self.assertRegex(self.plain.generated_at, r"^\d{8}T\d{6}Z$")
        self.assertIsNone(self.plain.report_path)  # 写文件是 write_report 的事

    def test_progress_callback_reports_every_stage(self):
        joined = "\n".join(self.messages)
        for token in ("构建数据集", "数据集 ", "训练 logreg/win5", "训练 logreg/ret5",
                      "评估 logreg-win5-"):
            self.assertIn(token, joined)
        self.assertNotIn("晋升", joined)  # 默认不晋升 → 连提都不提

    def test_default_run_leaves_models_candidate(self):
        statuses = {row["model_id"]: row["status"] for row in db.list_models(self.db)}
        for item in self.plain.trained:
            self.assertEqual(statuses[item.model_id], "candidate")
        self.assertEqual(self.plain.promotions, ())

    def test_forced_promotion_lands_demo_and_stale(self):
        outcome = self.forced.promotions[0]
        self.assertEqual(outcome.model_id, self.forced.trained[0].model_id)
        self.assertEqual(outcome.status, "demo")   # 弱信号不跑赢基线 → 质量门不过 → 演示工件
        self.assertTrue(outcome.promoted and outcome.stale and outcome.forced)
        self.assertEqual([name for name, _passed, _detail in outcome.gates],
                         ["min_samples", "min_positives", "max_staleness", "beats_base_rate"])
        self.assertTrue(any("max_staleness" in w for w in outcome.warnings))
        row = db.load_model(outcome.model_id, Path(self._tmp.name) / "forced.db")
        self.assertEqual(row["status"], "demo")
        self.assertTrue(row["stale"])

    def test_gated_promotion_keeps_candidate(self):
        outcome = self.gated.promotions[0]
        self.assertEqual(outcome.status, "candidate")
        self.assertFalse(outcome.promoted or outcome.forced)
        self.assertTrue(any(not passed for _n, passed, _d in outcome.gates))
        row = db.load_model(outcome.model_id, Path(self._tmp.name) / "gated.db")
        self.assertEqual(row["status"], "candidate")
        self.assertFalse(row["stale"])

    def test_real_report_renders_both_models_and_gates(self):
        text = render_markdown(self.plain)
        self.assertIn(self.plain.dataset_id, text)
        for item in self.plain.trained:
            self.assertIn(item.model_id, text)
        forced_text = render_markdown(self.forced)
        self.assertIn("## 晋升门禁", forced_text)
        self.assertIn("demo", forced_text)

    def test_real_report_written_to_disk(self):
        path = write_report(self.plain, Path(self._tmp.name) / "reports")
        self.assertEqual(path.name, f"ml-pipeline-{self.plain.dataset_id}.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 诚实定位", text)
        self.assertRegex(text, r"锚定滚动 3 折")
        self.assertIn("| logreg | win5 |", text)
        self.assertIn("| logreg | ret5 |", text)


if __name__ == "__main__":
    unittest.main()
