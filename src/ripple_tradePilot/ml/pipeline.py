"""D6：ML 全管道编排（build-dataset → train → eval →〔可选〕promote → markdown 报告）。

一键演示 / 回归入口：``tradepilot ml pipeline --pool config --start ... --end ...``。
与 CLI 的关系是"编排在这里、交互在那里"——本模块不 import click、不 print，进度通过可选
``progress`` 回调外抛，故整条管道可在测试里直接调用而不必走 CliRunner。

三条与 D 阶段其余模块一致的约定：

1. **懒加载**：顶层只 import 本仓模块（registry/evaluate/dataset 自身都是懒加载 sklearn），
   未装 scikit-learn 时 :func:`run_pipeline` 在第一次训练处抛 ``ImportError``，由 CLI 转成
   exit 3——信号链路（dashboard/monitor）不受影响。
2. **默认不晋升**：``promote=False``。一次回归跑悄悄换掉 dashboard/monitor 正在用的在位
   模型是事故，不是便利；要晋升就显式 ``--promote``，且仍受 D3 四道门禁约束。
3. **诚实定位**：报告尾部固定带一段声明——数据恢复并扩池之前，新鲜度门必触发，产物只能是
   ``stale``/``demo`` 演示工件（见 ``docs/ml-signal-quality.md``）。报告里每个数字都标了
   数据集新鲜度与 OOS 样本数，不给"看起来能用"的错觉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ripple_tradePilot.ml.dataset import build_dataset
from ripple_tradePilot.ml.evaluate import (
    DEFAULT_THRESHOLDS,
    EvalReport,
    evaluate_model,
)
from ripple_tradePilot.ml.features import DEFAULT_INDEX_CODE, FEATURE_GROUPS
from ripple_tradePilot.ml.registry import (
    DEFAULT_EMBARGO,
    DEFAULT_N_SPLITS,
    DEFAULT_VAL_RATIO,
    PromotionReport,
    promote,
    train_from_dataset,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MODEL_KINDS",
    "TrainedModel",
    "PromotionOutcome",
    "PipelineReport",
    "run_pipeline",
    "render_markdown",
    "write_report",
    "collect_warnings",
    "fmt_number",
    "fmt_pct",
]

MODEL_KINDS = ("logreg", "hgb")
CLASSIFICATION_TARGETS = ("win5",)

# 报告尾部固定声明（数据现实 → 演示工件），与 STRATEGIES.md / docs 口径一致
_HONESTY_NOTE = (
    "本报告由 `tradepilot ml pipeline` 自动生成，所有指标均为**锚定滚动样本外（OOS）**结果："
    "选参与校准只看 train/val 折，测试折从未参与任何选择。\n\n"
    "但在数据供给恢复（日线更新到近期）并扩充股票池之前，晋升门禁的**新鲜度门必触发**，"
    "管道产物只能是 `stale`（数据滞后）或 `demo`（质量门未过）的演示工件——"
    "`ml.scoring` 默认不加载 demo，看板/通知也会显式打出警告。"
    "**演示工件不可作为交易依据。** 数据恢复后重跑本命令，才会产生第一个可晋升的模型。"
)


@dataclass(frozen=True)
class TrainedModel:
    """管道里训练出的一个模型（指标取自 D3 :class:`ml.models.TrainResult`）。"""

    kind: str
    target: str
    model_id: str
    is_classification: bool
    n_oos: int
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionOutcome:
    """一次晋升门禁的结果（``promote=False`` 时管道不产出本结构）。"""

    model_id: str
    status: str
    promoted: bool
    stale: bool
    forced: bool
    gates: Tuple[Tuple[str, bool, str], ...] = ()
    warnings: Tuple[str, ...] = ()

    @classmethod
    def from_report(cls, report: PromotionReport) -> "PromotionOutcome":
        return cls(
            model_id=report.model_id,
            status=report.status,
            promoted=report.promoted,
            stale=report.stale,
            forced=report.forced,
            gates=tuple((g.name, g.passed, g.detail) for g in report.gates),
            warnings=tuple(report.warnings),
        )


@dataclass(frozen=True)
class PipelineReport:
    """整条管道的产物（数据 + 报告路径），供 CLI 输出与测试断言。"""

    dataset_id: str
    manifest: Dict[str, Any]
    groups: Tuple[str, ...]
    split_protocol: Dict[str, Any]
    trained: Tuple[TrainedModel, ...]
    evaluations: Tuple[EvalReport, ...]
    promotions: Tuple[PromotionOutcome, ...]
    generated_at: str
    report_path: Optional[str] = None

    @property
    def primary(self) -> Optional[EvalReport]:
        """主目标（win5）的第一份评估报告——决策对比表的主角。"""
        for report in self.evaluations:
            if report.target in CLASSIFICATION_TARGETS:
                return report
        return self.evaluations[0] if self.evaluations else None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _targets(target: str, aux_targets: Sequence[str]) -> Tuple[str, ...]:
    """主目标 + 辅助目标，去重保序（主目标恒在首位）。"""
    ordered = [target]
    for name in aux_targets:
        name = str(name).strip()
        if name and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def run_pipeline(
    symbols: Sequence[str],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    horizon: int = 5,
    aux_horizon: int = 10,
    groups: Sequence[str] = FEATURE_GROUPS,
    index_code: str = DEFAULT_INDEX_CODE,
    kinds: Sequence[str] = MODEL_KINDS,
    target: str = "win5",
    aux_targets: Sequence[str] = ("ret5", "mae5"),
    n_splits: int = DEFAULT_N_SPLITS,
    embargo: int = DEFAULT_EMBARGO,
    val_ratio: float = DEFAULT_VAL_RATIO,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    db_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    models_dir: Optional[Path] = None,
    promote_models: bool = False,
    force: bool = False,
    register: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> PipelineReport:
    """跑完整条管道并返回 :class:`PipelineReport`（**不写报告文件**，见 :func:`write_report`）。

    参数与 ``ml build-dataset`` / ``ml train`` / ``ml eval`` / ``ml promote`` 一一对应，
    语义完全一致——本函数只是把它们串起来，不另立口径。

    异常：``ValueError``（空池/未知 kind/target）、``ImportError``（sklearn 未装，由
    第一次训练抛出）。数据集构建与评估阶段的 ``FileNotFoundError`` 直接外抛（都是配置错误，
    不该被吞成"管道跑完了但什么都没有"）。
    """
    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    symbol_list = [str(s).strip() for s in symbols if str(s).strip()]
    if not symbol_list:
        raise ValueError("股票池为空，无法构建数据集")
    unknown_kinds = [k for k in kinds if k not in MODEL_KINDS]
    if unknown_kinds:
        raise ValueError(f"未知模型 kind：{unknown_kinds}；合法值 {list(MODEL_KINDS)}")
    target_list = _targets(target, aux_targets)

    # 1) 数据集（D2）
    say(f"构建数据集：{len(symbol_list)} 标的 · 组 {','.join(groups)} · horizon {horizon}(+{aux_horizon})")
    manifest = build_dataset(
        symbol_list,
        start=start,
        end=end,
        horizon=horizon,
        aux_horizon=aux_horizon,
        groups=tuple(groups),
        index_code=index_code,
        db_path=db_path,
        out_dir=out_dir,
        register=register,
    )
    say(
        f"数据集 {manifest.dataset_id}：{manifest.n_rows} 行（正例 {manifest.positive_rate:.1%}）"
        f" · {manifest.start_date}~{manifest.end_date} · 新鲜度 {manifest.max_trade_date}"
    )

    # 2) 训练（D3）：kind × target 全组合，各自独立 candidate
    trained: List[TrainedModel] = []
    split_protocol = {
        "n_splits": int(n_splits),
        "embargo": int(embargo),
        "val_ratio": float(val_ratio),
    }
    for kind in kinds:
        for name in target_list:
            say(f"训练 {kind}/{name} …")
            model_id, result = train_from_dataset(
                manifest.dataset_id,
                kind,
                name,
                n_splits=n_splits,
                embargo=embargo,
                val_ratio=val_ratio,
                db_path=db_path,
                models_dir=models_dir,
                register=register,
            )
            metrics = dict(result.metrics)
            trained.append(
                TrainedModel(
                    kind=kind,
                    target=name,
                    model_id=model_id,
                    is_classification=result.is_classification,
                    n_oos=int(metrics.get("n_oos", 0) or 0),
                    metrics=metrics,
                )
            )
            if result.is_classification:
                say(
                    f"  {model_id}：OOS {metrics.get('n_oos', 0)} 行 · "
                    f"AUC {fmt_number(metrics.get('oos_auc'))} · Brier {fmt_number(metrics.get('oos_brier'))}"
                    f"（基线 {fmt_number(metrics.get('base_rate_brier'))}）"
                )
            else:
                say(
                    f"  {model_id}：OOS {metrics.get('n_oos', 0)} 行 · "
                    f"MAE {fmt_number(metrics.get('oos_mae'))} · R² {fmt_number(metrics.get('oos_r2'))}"
                )

    # 3) 评估（D4）：只对分类目标出决策对比表（回归目标的 AUC/Brier 无意义）
    evaluations: List[EvalReport] = []
    for item in trained:
        if not item.is_classification:
            continue
        say(f"评估 {item.model_id} …")
        evaluations.append(
            evaluate_model(
                item.model_id,
                thresholds=tuple(thresholds),
                db_path=db_path,
                models_dir=models_dir,
                index_code=index_code,
            )
        )

    # 4) 晋升（D3 门禁）：默认跳过——见模块 docstring 第 2 条
    promotions: List[PromotionOutcome] = []
    if promote_models:
        for item in trained:
            report = promote(
                item.model_id,
                force=force,
                db_path=db_path,
                models_dir=models_dir,
            )
            promotions.append(PromotionOutcome.from_report(report))
            say(f"晋升 {item.model_id} → {report.status}{'（stale）' if report.stale else ''}")

    return PipelineReport(
        dataset_id=manifest.dataset_id,
        manifest=manifest.to_dict(),
        groups=tuple(manifest.groups),
        split_protocol=split_protocol,
        trained=tuple(trained),
        evaluations=tuple(evaluations),
        promotions=tuple(promotions),
        generated_at=_utc_stamp(),
    )


def write_report(report: PipelineReport, report_dir: Path | str = "reports") -> Path:
    """把 :func:`render_markdown` 的结果写到 ``<report_dir>/ml-pipeline-<dataset_id>.md``。"""
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ml-pipeline-{report.dataset_id}.md"
    path.write_text(render_markdown(report), encoding="utf-8")
    logger.info("管道报告已写入 %s", path)
    return path


# ---------------------------------------------------------------------------
# markdown 渲染
# ---------------------------------------------------------------------------
def fmt_number(value: Any, digits: int = 4) -> str:
    """None / NaN → ``n/a``（报告里绝不把缺失写成 0）。"""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:  # NaN
        return "n/a"
    return f"{number:.{digits}f}"


def fmt_pct(value: Any, digits: int = 2) -> str:
    """比例 → 百分号串；None / NaN → ``n/a``。"""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "n/a"
    return f"{number * 100:.{digits}f}%"


def _decision_cells(row: Any) -> List[str]:
    """决策对比表一行的值列。

    ``n_signals == 0``（OOS 折内规则零 BUY 票时 ``rule_baseline`` 就是空的）→ 全部 ``—``：
    渲染成 0.00% 会被读成"测过了，胜率零、收益零"，而真相是没有样本。与 ``ml eval``
    的文本表同口径。
    """
    if int(row.n_signals) == 0:
        return ["—", "—", "—", "—"]
    return [
        fmt_pct(row.coverage, 1),
        fmt_pct(row.win_rate, 1),
        fmt_pct(row.avg_net_return),
        fmt_pct(row.expectancy),
    ]


def _bucket_cells(bucket: Mapping[str, Any]) -> List[str]:
    """可靠性分桶一行的值列；空桶同样不给 0.0%。"""
    if not int(bucket.get("count") or 0):
        return ["—", "—"]
    return [
        fmt_pct(bucket.get("mean_predicted"), 1),
        fmt_pct(bucket.get("mean_actual"), 1),
    ]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    out.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return out


def render_markdown(report: PipelineReport) -> str:
    """管道报告（markdown）：数据集 → 模型清单 → 决策对比 → 校准 → 门禁 → 警告 → 诚实定位。"""
    manifest = report.manifest
    lines: List[str] = ["# ML 信号质量管道报告", ""]
    lines += [
        f"- 生成时间：`{report.generated_at}`",
        f"- 数据集：`{report.dataset_id}`",
        f"- 样本：{manifest.get('n_rows', 0)} 行（正例 {manifest.get('n_positive', 0)} · "
        f"{fmt_pct(manifest.get('positive_rate'), 1)}）",
        f"- 决策日范围：{manifest.get('start_date')} ~ {manifest.get('end_date')}"
        f"（标签新鲜度戳 `{manifest.get('max_trade_date')}`）",
        f"- 标的：{len(manifest.get('symbols') or [])} 只"
        + (f"，拒入 {len(manifest.get('rejected') or [])} 只" if manifest.get("rejected") else ""),
        f"- 特征组：{','.join(report.groups)}（{len(manifest.get('feature_columns') or [])} 列）",
        f"- 切分协议：锚定滚动 {report.split_protocol.get('n_splits')} 折 · "
        f"val_ratio {report.split_protocol.get('val_ratio')} · "
        f"purge + embargo {report.split_protocol.get('embargo')} 交易日",
        f"- 成本口径：{_cost_text(manifest.get('cost_model'))}",
        "",
    ]

    # --- 模型清单 ---
    lines += ["## 模型（全部为 candidate，除非下方门禁另有结论）", ""]
    lines += _table(
        ["kind", "target", "model_id", "n_oos", "AUC", "Brier", "基线 Brier", "ECE", "MAE", "R²"],
        [
            [
                item.kind, item.target, f"`{item.model_id}`", str(item.n_oos),
                fmt_number(item.metrics.get("oos_auc")),
                fmt_number(item.metrics.get("oos_brier")),
                fmt_number(item.metrics.get("base_rate_brier")),
                fmt_number(item.metrics.get("oos_ece")),
                fmt_number(item.metrics.get("oos_mae")),
                fmt_number(item.metrics.get("oos_r2"), 3),
            ]
            for item in report.trained
        ],
    )
    lines.append("")

    # --- 决策对比表（每个分类模型一张）---
    for evaluation in report.evaluations:
        lines += [
            f"## 决策对比 · `{evaluation.model_id}`（{evaluation.kind}/{evaluation.target}）",
            "",
            f"同一 OOS 折、同一标签下并列四种口径；`n_oos={evaluation.n_oos}`，"
            f"正例率 {fmt_pct(evaluation.base_rate, 1)}，"
            f"判别力 {'**跑赢**' if evaluation.beats_base_rate else '**未跑赢**'}常数基线"
            f"（Brier {fmt_number(evaluation.brier)} vs {fmt_number(evaluation.base_rate_brier)}）。",
            "",
        ]
        lines += _table(
            ["策略", "阈值", "覆盖率", "胜率", "平均净收益", "每信号期望", "样本数"],
            [
                [
                    f"`{row.strategy}`",
                    "—" if row.threshold is None else f"{row.threshold:.2f}",
                    *_decision_cells(row),
                    str(row.n_signals),
                ]
                for row in evaluation.decision_rows
            ],
        )
        lines += [
            "",
            f"校准：slope {fmt_number(evaluation.slope, 3)} · intercept "
            f"{fmt_number(evaluation.intercept, 3)} · ECE {fmt_number(evaluation.ece)}"
            f"（slope≈1、intercept≈0 为理想）。",
            "",
        ]

    # --- 校准 10 桶（只给主模型，避免报告过长）---
    primary = report.primary
    if primary is not None and primary.reliability:
        lines += [f"### 可靠性分桶 · `{primary.model_id}`", ""]
        lines += _table(
            ["桶", "区间", "样本", "预测均值", "实际胜率"],
            [
                [
                    str(bucket.get("bin")),
                    f"[{fmt_number(bucket.get('lower'), 2)}, {fmt_number(bucket.get('upper'), 2)})",
                    str(bucket.get("count")),
                    *_bucket_cells(bucket),
                ]
                for bucket in primary.reliability
            ],
        )
        lines.append("")

    # --- 门禁 ---
    if report.promotions:
        lines += ["## 晋升门禁", ""]
        for outcome in report.promotions:
            lines.append(
                f"- `{outcome.model_id}` → **{outcome.status}**"
                f"{'（stale）' if outcome.stale else ''}"
                f"{'（force 放行）' if outcome.forced else ''}"
            )
            for name, passed, detail in outcome.gates:
                lines.append(f"  - {'✓' if passed else '✗'} `{name}`：{detail}")
        lines.append("")
    else:
        lines += [
            "## 晋升门禁",
            "",
            "本次未请求晋升（`--promote` 未给）：所有模型停在 `candidate`，",
            "看板/通知用的在位模型不受影响。",
            "",
        ]

    # --- 警告 ---
    warnings = collect_warnings(report)
    lines += ["## 警告", ""]
    if warnings:
        lines += [f"- {warning}" for warning in warnings]
    else:
        lines.append("- （无）")
    lines.append("")

    lines += ["## 诚实定位", "", _HONESTY_NOTE, ""]
    return "\n".join(lines)


def _cost_text(cost_model: Any) -> str:
    if not isinstance(cost_model, Mapping) or not cost_model:
        return "n/a"
    return "、".join(f"{key}={value}" for key, value in sorted(cost_model.items()))


def collect_warnings(report: PipelineReport) -> List[str]:
    """数据集 + 各评估报告 + 门禁的警告去重汇总（保持出现顺序）。"""
    seen: List[str] = []
    for item in report.manifest.get("warnings") or []:
        if item not in seen:
            seen.append(str(item))
    for rejected in report.manifest.get("rejected") or []:
        line = f"拒入 {rejected.get('symbol')}：{rejected.get('reason')}"
        if line not in seen:
            seen.append(line)
    for evaluation in report.evaluations:
        for warning in evaluation.warnings:
            if warning not in seen:
                seen.append(str(warning))
    for outcome in report.promotions:
        for warning in outcome.warnings:
            if warning not in seen:
                seen.append(str(warning))
    return seen
