"""D4：样本外评估报告（判别 + 校准 + 决策价值四方对比）。

只读 D3 存下的 OOS 预测（``oos.npz``）与 ``ml_models``/``ml_datasets`` 行，**纯 numpy 计算
全部指标，评估链路不需要 sklearn/joblib**（与"未安装环境全模块 import 不炸"一致）。

回答路线图第 4 点的核心问题——"模型过滤是否真把每信号净期望做高了、代价是覆盖率降了
多少"——靠**决策对比表**：在同一标签、同一 OOS 折上并列四种口径，每阈值一行：

====================  ============================================================
rule_baseline         规则引擎全部 BUY 票（不过滤基线，signal 组的 rec_buy==1 子集）
model_thr<t>          模型过滤 p≥t（D 阶段的主角）
buy_hold              同窗买入持有（每个 OOS 决策点都买，覆盖率 1.0）
index                 沪深300 同窗（每个决策点的 horizon 日指数收益，市场基准）
====================  ============================================================

各列：覆盖率 / 胜率 / 平均净收益 / 每信号期望 / 样本数。基线与过滤子集同折同标签对比，
直接看出"过滤把期望收益抬了多少、覆盖率掉多少"。

警告区诚实标注：新鲜度（stale）/ 样本量 / 正例率失衡 / 行业 point-in-time / 是否跑赢基线。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ripple_tradePilot.ml import calibration
from ripple_tradePilot.ml.registry import load_artifact
from ripple_tradePilot.storage import database as db

logger = logging.getLogger(__name__)

__all__ = [
    "DecisionRow",
    "EvalReport",
    "evaluate_model",
    "render_text",
    "dump_json",
    "DEFAULT_THRESHOLDS",
]

DEFAULT_THRESHOLDS: Tuple[float, ...] = (0.5, 0.55, 0.6)


@dataclass(frozen=True)
class DecisionRow:
    """决策对比表单行（一种口径在一个阈值下的表现）。"""

    strategy: str
    threshold: Optional[float]
    coverage: float
    win_rate: float
    avg_net_return: float
    expectancy: float
    n_signals: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "threshold": self.threshold,
            "coverage": self.coverage,
            "win_rate": self.win_rate,
            "avg_net_return": self.avg_net_return,
            "expectancy": self.expectancy,
            "n_signals": self.n_signals,
        }


@dataclass(frozen=True)
class EvalReport:
    """完整 OOS 评估报告。"""

    model_id: str
    kind: str
    target: str
    dataset_id: str
    n_oos: int
    # 判别
    auc: float
    logloss: float
    brier: float
    base_rate: float
    base_rate_brier: float
    beats_base_rate: bool
    # 校准
    slope: float
    intercept: float
    ece: float
    reliability: Tuple[Dict[str, Any], ...]
    # 决策对比
    decision_rows: Tuple[DecisionRow, ...]
    # 状态/警告
    status: str
    stale: bool
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "kind": self.kind,
            "target": self.target,
            "dataset_id": self.dataset_id,
            "n_oos": self.n_oos,
            "discrimination": {
                "auc": self.auc,
                "logloss": self.logloss,
                "brier": self.brier,
                "base_rate": self.base_rate,
                "base_rate_brier": self.base_rate_brier,
                "beats_base_rate": self.beats_base_rate,
            },
            "calibration": {
                "slope": self.slope,
                "intercept": self.intercept,
                "ece": self.ece,
                "reliability": list(self.reliability),
            },
            "decision_table": [r.to_dict() for r in self.decision_rows],
            "status": self.status,
            "stale": self.stale,
            "warnings": list(self.warnings),
        }


def _subset_stats(
    y: np.ndarray, net_ret: np.ndarray, mask: np.ndarray
) -> Tuple[float, float, float, int]:
    """子集的 (coverage, win_rate, avg_net_return, n_signals)。"""
    n_total = int(y.size)
    n_sig = int(mask.sum())
    if n_total == 0 or n_sig == 0:
        return 0.0, 0.0, 0.0, n_sig
    y_sub = y[mask]
    ret_sub = net_ret[mask]
    return (
        n_sig / n_total,
        float(np.mean(y_sub)),
        float(np.mean(ret_sub)),
        n_sig,
    )


def _index_window_returns(
    index_rows: Sequence[Mapping[str, Any]],
    trade_dates: Sequence[str],
    horizon: int,
) -> Tuple[List[float], List[int]]:
    """每个决策点的 horizon 日指数收益（与个股标签同窗口对齐），返回 (returns, wins)。"""
    if not index_rows:
        return [], []
    dates = [str(r.get("trade_date")) for r in index_rows]
    closes = [float(r.get("close")) for r in index_rows]
    pos = {d: i for i, d in enumerate(dates)}
    returns: List[float] = []
    wins: List[int] = []
    for d in trade_dates:
        i = pos.get(str(d))
        if i is None or i + horizon >= len(closes) or closes[i] <= 0:
            continue
        ret = closes[i + horizon] / closes[i] - 1.0
        returns.append(ret)
        wins.append(1 if ret > 0 else 0)
    return returns, wins


def evaluate_model(
    model_id: str,
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    db_path: Optional[Path] = None,
    models_dir: Optional[Path] = None,
    index_code: str = "000300.SH",
) -> EvalReport:
    """评估某模型的 OOS 表现，产出 :class:`EvalReport`（判别 + 校准 + 决策对比 + 警告）。"""
    artifact = load_artifact(model_id, models_dir=models_dir, need_model=False)
    meta = artifact["meta"]
    oos = artifact.get("oos", {})
    if not oos or "p_oos" not in oos:
        raise ValueError(f"模型 {model_id} 缺 OOS 预测，无法评估")

    p = np.asarray(oos["p_oos"], dtype=float)
    y = np.rint(np.asarray(oos["y_oos"], dtype=float)).astype(int)
    net_ret = np.asarray(oos.get("net_ret", np.full_like(p, np.nan)), dtype=float)
    rec_buy = np.asarray(oos.get("rec_buy", np.full_like(p, np.nan)), dtype=float)
    trade_dates = [str(d) for d in oos.get("trade_dates", [])]
    horizon = int(meta.get("horizon", 5) or 5)
    n_oos = int(p.size)

    # --- 判别 ---
    base_rate = float(np.mean(y)) if n_oos else 0.0
    base_rate_brier = base_rate * (1.0 - base_rate)
    brier = calibration.brier_score(y, p)
    logloss = calibration.log_loss(y, p)
    auc = calibration.roc_auc_score(y, p)
    beats_base_rate = bool(brier < base_rate_brier)

    # --- 校准 ---
    slope, intercept = calibration.calibration_slope_intercept(y, p)
    ece = calibration.expected_calibration_error(y, p)
    reliability = tuple(
        {
            "bin": b.bin_index,
            "lower": b.lower,
            "upper": b.upper,
            "count": b.count,
            "mean_predicted": b.mean_predicted,
            "mean_actual": b.mean_actual,
        }
        for b in calibration.reliability_table(y, p, n_bins=10)
    )

    warnings: List[str] = []

    # --- 决策对比表 ---
    rows: List[DecisionRow] = []
    # 基线：规则引擎全部 BUY 票（rec_buy==1）；signal 组缺失（rec_buy 全 NaN）→ 退化为买入持有
    has_rec = bool(np.any(~np.isnan(rec_buy)))
    if has_rec:
        cov, wr, ar, n_sig = _subset_stats(y, net_ret, rec_buy == 1)
        rows.append(DecisionRow("rule_baseline", None, cov, wr, ar, ar, n_sig))
        if n_sig == 0:
            warnings.append(
                "（OOS 折内规则引擎零 BUY 票，rule_baseline 退化为空——"
                "对比以 buy_hold 为准；放宽 vote_threshold 可增基线样本）"
            )
    else:
        rows.append(
            DecisionRow("rule_baseline", None, 1.0, base_rate,
                        float(np.nanmean(net_ret)) if n_oos else 0.0,
                        float(np.nanmean(net_ret)) if n_oos else 0.0, n_oos)
        )
    # 模型过滤：每阈值一行
    for thr in thresholds:
        dv = calibration.decision_value_report(y, np.nan_to_num(net_ret), p, float(thr))
        rows.append(
            DecisionRow(
                f"model_thr{thr:.2f}", float(thr), dv.coverage, dv.win_rate,
                dv.avg_net_return, dv.expectancy, dv.n_signals,
            )
        )
    # 买入持有同窗（每个 OOS 决策点都买）
    bh_ret = float(np.nanmean(net_ret)) if n_oos else 0.0
    rows.append(DecisionRow("buy_hold", None, 1.0, base_rate, bh_ret, bh_ret, n_oos))

    # --- 警告区 ---
    status = str(meta.get("status", "candidate"))
    stale = bool(meta.get("stale", False))
    if stale:
        warnings.append(
            f"⚠️ 数据滞后：数据集 max_trade_date={meta.get('max_trade_date')}，模型标 stale"
        )
    if status == "demo":
        warnings.append("⚠️ 演示模型（demo）：质量门禁未过，force 晋升，非可信在位模型")
    if n_oos < 200:
        warnings.append(f"⚠️ OOS 样本偏少（{n_oos}<200），指标方差大，谨慎解读")
    if not (0.2 <= base_rate <= 0.8):
        warnings.append(f"⚠️ 正例率失衡（base_rate={base_rate:.3f}），AUC/Brier 须结合覆盖率看")
    if not beats_base_rate:
        warnings.append(
            f"⚠️ 未跑赢常数基线：OOS Brier {brier:.4f} ≥ 基线 {base_rate_brier:.4f}，模型无判别增益"
        )
    if np.isnan(auc):
        warnings.append("⚠️ OOS 单一类别，AUC 无定义")

    # 数据集 point-in-time 警告（承接 D2/C3）
    dataset_id = str(meta.get("dataset_id", "") or "")
    if dataset_id:
        ds = db.load_dataset_manifest(dataset_id, db_path)
        if ds and not ds.get("industry_point_in_time", True):
            warnings.append(
                "⚠️ 行业特征为最新单快照（industry_point_in_time=False），增益含轻微前视偏差"
            )

    # 指数同窗基准（市场对照）
    index_rows = db.load_index_bars(index_code, db_path)
    idx_returns, idx_wins = _index_window_returns(index_rows, trade_dates, horizon)
    if idx_returns:
        idx_arr = np.asarray(idx_returns, dtype=float)
        rows.append(
            DecisionRow(
                "index", None, 1.0, float(np.mean(idx_wins)),
                float(np.mean(idx_arr)), float(np.mean(idx_arr)), len(idx_returns),
            )
        )
    else:
        warnings.append(f"（无 {index_code} 指数数据，省略指数同窗基准行）")

    return EvalReport(
        model_id=model_id,
        kind=str(meta.get("kind", "")),
        target=str(meta.get("target", "")),
        dataset_id=dataset_id,
        n_oos=n_oos,
        auc=auc,
        logloss=logloss,
        brier=brier,
        base_rate=base_rate,
        base_rate_brier=base_rate_brier,
        beats_base_rate=beats_base_rate,
        slope=slope,
        intercept=intercept,
        ece=ece,
        reliability=reliability,
        decision_rows=tuple(rows),
        status=status,
        stale=stale,
        warnings=tuple(warnings),
    )


def render_text(report: EvalReport) -> str:
    """把评估报告渲染成 CLI 文本表（判别 + 校准 + 决策对比 + 警告）。"""
    lines: List[str] = []
    lines.append(f"\n📊 模型评估 {report.model_id}（{report.kind}/{report.target}）")
    lines.append(
        f"   数据集 {report.dataset_id} · OOS {report.n_oos} 行 · "
        f"状态 {report.status}{'（stale）' if report.stale else ''}"
    )
    lines.append("   ── 判别 ──")
    auc_txt = "n/a" if np.isnan(report.auc) else f"{report.auc:.4f}"
    lines.append(
        f"   AUC {auc_txt} · LogLoss {report.logloss:.4f} · "
        f"Brier {report.brier:.4f}（基线 {report.base_rate_brier:.4f}）· "
        f"{'✓ 跑赢' if report.beats_base_rate else '✗ 未跑赢'}常数基线"
    )
    lines.append("   ── 校准 ──")
    lines.append(
        f"   slope {report.slope:.3f}（≈1 佳）· intercept {report.intercept:.3f}（≈0 佳）· "
        f"ECE {report.ece:.4f}"
    )
    lines.append("   ── 决策对比（同 OOS 折·同标签）──")
    header = f"   {'口径':<16}{'阈值':>6}{'覆盖率':>9}{'胜率':>9}{'均净收益':>11}{'每信号期望':>12}{'样本':>7}"
    lines.append(header)
    for row in report.decision_rows:
        thr = "—" if row.threshold is None else f"{row.threshold:.2f}"
        # 空子集（OOS 折内规则零 BUY 票时 rule_baseline 就是空的）不给数字：
        # 0.000/0.00% 会被读成"测过了，胜率零、收益零"，而真相是"没有样本"
        if row.n_signals == 0:
            cov = wr = avg = exp = "n/a"
        else:
            cov = f"{row.coverage:.3f}"
            wr = "n/a" if np.isnan(row.win_rate) else f"{row.win_rate:.3f}"
            avg = f"{row.avg_net_return:.4%}"
            exp = f"{row.expectancy:.4%}"
        lines.append(
            f"   {row.strategy:<16}{thr:>6}{cov:>9}{wr:>9}{avg:>11}{exp:>12}{row.n_signals:>7}"
        )
    if report.warnings:
        lines.append("   ── 警告 ──")
        for warning in report.warnings:
            lines.append(f"   {warning}")
    return "\n".join(lines)


def dump_json(report: EvalReport, path: Path | str) -> Path:
    """把评估报告写成 JSON（reports/ 留档）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target
