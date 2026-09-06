"""D3：模型工件注册 + 晋升门禁。

把 :class:`~ripple_tradePilot.ml.models.TrainResult` 落成可复现的工件目录，写 ``ml_models``
表（status='candidate'），并提供**晋升门禁** ``promote`` ——这是"数据恢复前 ML 工件只是
演示"的核心护栏：样本量/正例数/新鲜度/OOS 判别力任一不达标，默认**拒绝晋升**，``force``
才放行且打 ``stale``/``demo`` 标记，UI 据此显示"数据滞后/演示模型"警告。

工件目录布局（``data/ml/models/<model_id>/``）::

    model.joblib       末折 base 模型（logreg 管道 / hgb）
    calibrator.joblib  末折 Platt 校准器（回归目标无此文件）
    meta.json          全 provenance + 指标 + 晋升状态（人可读、可溯源）
    oos.npz            OOS 预测序列（D4 评估只读它，无需重训/无需 sklearn）

joblib 懒加载：模块顶层只依赖 numpy/标准库，未装 sklearn/joblib 时 import 不炸，
只有真去 save/load 工件才报清晰错误。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ripple_tradePilot.ml.dataset import load_dataset
from ripple_tradePilot.ml.models import (
    TARGET_LABEL_COLUMN,
    TrainResult,
    train_model,
)
from ripple_tradePilot.ml.splits import rolling_splits
from ripple_tradePilot.storage import database as db

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODELS_DIR",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_MIN_POSITIVES",
    "DEFAULT_MAX_STALENESS_DAYS",
    "DEFAULT_N_SPLITS",
    "DEFAULT_EMBARGO",
    "DEFAULT_VAL_RATIO",
    "GateResult",
    "PromotionReport",
    "make_model_id",
    "save_artifact",
    "load_artifact",
    "persist_model",
    "train_from_dataset",
    "promote",
    "load_promoted",
    "sklearn_version",
]

DEFAULT_N_SPLITS = 5
DEFAULT_EMBARGO = 2
DEFAULT_VAL_RATIO = 0.2

DEFAULT_MODELS_DIR = Path("data/ml/models")
# 晋升门禁默认阈值（数据恢复前的现实下，新鲜度门必触发）
DEFAULT_MIN_SAMPLES = 800
DEFAULT_MIN_POSITIVES = 80
DEFAULT_MAX_STALENESS_DAYS = 120


def sklearn_version() -> str:
    """当前 sklearn 版本（懒加载）；未安装返回 ``"unavailable"``。"""
    try:
        import sklearn

        return str(sklearn.__version__)
    except ImportError:  # pragma: no cover - 取决于环境
        return "unavailable"


def _require_joblib() -> Any:
    try:
        import joblib
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError(
            "工件持久化需要 joblib（随 scikit-learn 一并安装）。"
        ) from exc
    return joblib


def make_model_id(
    dataset_id: str,
    kind: str,
    target: str,
    *,
    n_splits: int,
    embargo: int,
    val_ratio: float,
    trained_at: Optional[str] = None,
) -> str:
    """模型 ID = ``<kind>-<target>-<内容hash前10>``。

    hash 纳入 dataset_id/训练协议/trained_at，故每次训练 run 产出唯一 candidate（不静默
    覆盖历史），但同一 run 的重复调用稳定。
    """
    stamp = trained_at or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = json.dumps(
        {
            "dataset_id": dataset_id,
            "kind": kind,
            "target": target,
            "n_splits": n_splits,
            "embargo": embargo,
            "val_ratio": val_ratio,
            "trained_at": stamp,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return f"{kind}-{target}-{digest}"


def save_artifact(
    model_id: str,
    result: TrainResult,
    *,
    meta: Mapping[str, Any],
    oos_extras: Mapping[str, Sequence[Any]],
    models_dir: Optional[Path] = None,
) -> Path:
    """落盘工件目录（model/calibrator/meta/oos），返回目录路径。"""
    joblib = _require_joblib()
    root = Path(models_dir) if models_dir is not None else DEFAULT_MODELS_DIR
    artifact_dir = root / model_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    joblib.dump(result.model, artifact_dir / "model.joblib")
    if result.calibrator is not None:
        joblib.dump(result.calibrator, artifact_dir / "calibrator.joblib")

    # OOS 预测序列（D4 评估只读它）；缺列以全 NaN 占位，保持 npz 结构稳定
    n_oos = int(result.oos_idx.size)
    net_ret = np.asarray(oos_extras.get("net_ret", [np.nan] * n_oos), dtype=float)
    rec_buy = np.asarray(oos_extras.get("rec_buy", [np.nan] * n_oos), dtype=float)
    trade_dates = np.asarray(
        list(oos_extras.get("trade_dates", [""] * n_oos)), dtype="<U8"
    )
    np.savez(
        artifact_dir / "oos.npz",
        oos_idx=result.oos_idx.astype(np.int64),
        p_oos=result.p_oos.astype(float),
        y_oos=result.y_oos.astype(float),
        net_ret=net_ret.astype(float),
        rec_buy=rec_buy.astype(float),
        trade_dates=trade_dates,
    )

    full_meta: Dict[str, Any] = dict(meta)
    full_meta["model_id"] = model_id
    full_meta["artifact_path"] = str(artifact_dir)
    (artifact_dir / "meta.json").write_text(
        json.dumps(full_meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return artifact_dir


def load_artifact(
    model_id: str,
    *,
    models_dir: Optional[Path] = None,
    need_model: bool = False,
) -> Dict[str, Any]:
    """读取工件：``meta`` + ``oos``（dict of arrays）；``need_model`` 时载入模型/校准器。

    评估（D4）只需 ``oos`` + ``meta``（纯 numpy，无需 sklearn）；打分（D5）需
    ``need_model=True`` 载入 joblib 工件。
    """
    root = Path(models_dir) if models_dir is not None else DEFAULT_MODELS_DIR
    artifact_dir = root / model_id
    meta_path = artifact_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"模型工件不存在：{artifact_dir}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    out: Dict[str, Any] = {"meta": meta, "artifact_dir": artifact_dir}
    oos_path = artifact_dir / "oos.npz"
    if oos_path.exists():
        with np.load(oos_path, allow_pickle=False) as npz:
            out["oos"] = {key: npz[key] for key in npz.files}
    else:  # pragma: no cover - 正常工件总带 oos.npz
        out["oos"] = {}

    if need_model:
        joblib = _require_joblib()
        out["model"] = joblib.load(artifact_dir / "model.joblib")
        calibrator_path = artifact_dir / "calibrator.joblib"
        out["calibrator"] = (
            joblib.load(calibrator_path) if calibrator_path.exists() else None
        )
    return out


def persist_model(
    result: TrainResult,
    *,
    meta: Mapping[str, Any],
    oos_extras: Mapping[str, Sequence[Any]],
    models_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
    register: bool = True,
) -> str:
    """落盘工件 + 写 ``ml_models``（status='candidate'），返回 model_id。"""
    model_id = str(meta["model_id"])
    artifact_dir = save_artifact(
        model_id, result, meta=meta, oos_extras=oos_extras, models_dir=models_dir
    )
    if register:
        db.register_model(_model_row(model_id, result, meta, artifact_dir), db_path)
    logger.info("💾 模型工件已保存：%s（%s）", model_id, artifact_dir)
    return model_id


def train_from_dataset(
    dataset_id: str,
    kind: str = "logreg",
    target: str = "win5",
    *,
    n_splits: int = DEFAULT_N_SPLITS,
    embargo: int = DEFAULT_EMBARGO,
    val_ratio: float = DEFAULT_VAL_RATIO,
    threshold: float = 0.5,
    db_path: Optional[Path] = None,
    models_dir: Optional[Path] = None,
    register: bool = True,
    trained_at: Optional[str] = None,
) -> Tuple[str, TrainResult]:
    """端到端：数据集 → 滚动切分 → 训练 → 落工件 + 落库（status='candidate'）。

    这是 CLI ``ml train`` 与 D6 ``ml pipeline`` 共用的编排入口。返回 ``(model_id, result)``。

    步骤：``load_dataset`` 还原帧 → 取 ``feature_columns`` 为 X、``target`` 对应标签列为 y
    → :func:`rolling_splits`（按 trade_date/exit_date 防泄漏）→ :func:`train_model` 滚动
    OOS → 组装 meta（含数据集 provenance）+ OOS 旁路列（净收益/规则 BUY 票/日期，供 D4
    决策对比表）→ :func:`persist_model`。
    """
    if target not in TARGET_LABEL_COLUMN:
        raise ValueError(f"未知 target：{target}；合法 {sorted(TARGET_LABEL_COLUMN)}")
    manifest, frame = load_dataset(dataset_id, db_path)
    feature_columns = list(manifest.get("feature_columns") or [])
    if not feature_columns:
        raise ValueError(f"数据集 {dataset_id} 无特征列，无法训练")
    label_column = TARGET_LABEL_COLUMN[target]
    if label_column not in frame.columns:
        raise ValueError(f"数据集缺标签列 {label_column}（target={target}）")
    if frame.empty:
        raise ValueError(f"数据集 {dataset_id} 为空，无法训练")

    x = frame[feature_columns].to_numpy(dtype=float)
    y = frame[label_column].to_numpy(dtype=float)
    trade_dates = frame["trade_date"].astype(str).tolist()
    exit_dates = frame["exit_date"].astype(str).tolist()

    splits = rolling_splits(
        trade_dates, exit_dates, n_splits, val_ratio=val_ratio, embargo=embargo
    )
    result = train_model(kind, target, x, y, splits, threshold=threshold)

    # OOS 旁路列（与 result.oos_idx 对齐）：D4 决策对比表要净收益/规则票/日期
    oos_idx = result.oos_idx
    net_ret_full = (
        frame["label_net_return"].to_numpy(dtype=float)
        if "label_net_return" in frame.columns
        else np.full(len(frame), np.nan)
    )
    rec_buy_full = (
        frame["rec_buy"].to_numpy(dtype=float)
        if "rec_buy" in frame.columns
        else np.full(len(frame), np.nan)
    )
    oos_extras = {
        "net_ret": net_ret_full[oos_idx],
        "rec_buy": rec_buy_full[oos_idx],
        "trade_dates": [trade_dates[i] for i in oos_idx],
    }

    stamp = trained_at or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_id = make_model_id(
        dataset_id, kind, target,
        n_splits=n_splits, embargo=embargo, val_ratio=val_ratio, trained_at=stamp,
    )
    horizon = int(manifest.get("horizon", 5) or 5)
    last_train = result.per_split[-1]["n_train"] if result.per_split else 0
    meta: Dict[str, Any] = {
        "model_id": model_id,
        "kind": kind,
        "target": target,
        "horizon": horizon,
        "dataset_id": dataset_id,
        "feature_columns": feature_columns,
        "feature_groups": list(manifest.get("groups") or []),
        "n_features": len(feature_columns),
        "threshold": float(threshold),
        "n_train": int(last_train),
        "n_rows": int(manifest.get("n_rows", 0) or 0),
        "n_positive": int(manifest.get("n_positive", 0) or 0),
        "max_trade_date": manifest.get("max_trade_date"),
        "index_code": manifest.get("index_code"),
        "industry_point_in_time": bool(manifest.get("industry_point_in_time", False)),
        "split_protocol": {
            "n_splits": n_splits,
            "embargo": embargo,
            "val_ratio": val_ratio,
        },
        "sklearn_version": sklearn_version(),
        "trained_at": stamp,
        "status": "candidate",
        "stale": False,
        "warnings": [],
    }
    persist_model(
        result, meta=meta, oos_extras=oos_extras,
        models_dir=models_dir, db_path=db_path, register=register,
    )
    return model_id, result


def _model_row(
    model_id: str,
    result: TrainResult,
    meta: Mapping[str, Any],
    artifact_dir: Path,
) -> Dict[str, Any]:
    """从 meta + result.metrics 组装 ml_models 行（标量摊平，JSON 字段成组）。"""
    metrics = dict(result.metrics)
    return {
        "model_id": model_id,
        "kind": result.kind,
        "target": result.target,
        "horizon": int(meta.get("horizon", 5) or 5),
        "dataset_id": meta.get("dataset_id"),
        "n_features": int(meta.get("n_features", 0) or 0),
        "threshold": float(meta.get("threshold", 0.5) or 0.5),
        "oos_brier": metrics.get("oos_brier"),
        "oos_auc": metrics.get("oos_auc"),
        "oos_logloss": metrics.get("oos_logloss"),
        "oos_ece": metrics.get("oos_ece"),
        "oos_mae": metrics.get("oos_mae"),
        "oos_r2": metrics.get("oos_r2"),
        "base_rate": metrics.get("base_rate"),
        "base_rate_brier": metrics.get("base_rate_brier"),
        "coverage_at_threshold": metrics.get("coverage_at_threshold"),
        "win_rate_at_threshold": metrics.get("win_rate_at_threshold"),
        "n_oos": int(metrics.get("n_oos", 0) or 0),
        "n_train": int(meta.get("n_train", 0) or 0),
        "n_rows": int(meta.get("n_rows", 0) or 0),
        "n_positive": int(meta.get("n_positive", 0) or 0),
        "max_trade_date": meta.get("max_trade_date"),
        "status": str(meta.get("status", "candidate")),
        "stale": bool(meta.get("stale", False)),
        "artifact_path": str(artifact_dir),
        "sklearn_version": meta.get("sklearn_version"),
        "feature_groups": list(meta.get("feature_groups", []) or []),
        "selected_params": dict(result.selected_params),
        "metrics": metrics,
        "warnings": list(meta.get("warnings", []) or []),
        "trained_at": meta.get("trained_at"),
    }


@dataclass(frozen=True)
class GateResult:
    """单道晋升门禁的判定。"""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PromotionReport:
    """晋升结果（门禁逐条 + 最终状态 + 警告）。"""

    model_id: str
    promoted: bool  # 是否真的改了状态（promoted/demo）
    status: str  # 最终 status
    stale: bool
    forced: bool
    gates: Tuple[GateResult, ...]
    warnings: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "promoted": self.promoted,
            "status": self.status,
            "stale": self.stale,
            "forced": self.forced,
            "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail} for g in self.gates],
            "warnings": list(self.warnings),
        }


def _staleness_days(max_trade_date: Optional[str], today: datetime) -> Optional[int]:
    """数据集最新交易日距 ``today`` 的天数；无法解析返回 None。"""
    if not max_trade_date:
        return None
    try:
        anchor = datetime.strptime(str(max_trade_date)[:8], "%Y%m%d").date()
    except ValueError:
        return None
    return (today.date() - anchor).days


def promote(
    model_id: str,
    *,
    force: bool = False,
    db_path: Optional[Path] = None,
    models_dir: Optional[Path] = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_positives: int = DEFAULT_MIN_POSITIVES,
    max_staleness_days: int = DEFAULT_MAX_STALENESS_DAYS,
    today: Optional[datetime] = None,
) -> PromotionReport:
    """晋升门禁：样本量/正例数/新鲜度/OOS 判别力四道闸。

    - **全过** → ``status='promoted'``、``stale=False``，并退役同 (target,horizon) 的旧在位模型；
    - **有闸不过 + force=False** → 维持 ``candidate``，报告拒绝原因（默认拒晋升）；
    - **有闸不过 + force=True** → 放行：仅新鲜度不过 → ``promoted`` 且 ``stale=True``
      （UI 显示"数据滞后"）；质量闸（样本/正例/判别力）不过 → ``demo``（演示工件，
      ``load_promoted`` 默认不取，需 ``include_demo``）。

    新鲜度门在"数据停在 2026-04-17"的当前现实下**必触发**——这正是护栏意图：
    数据恢复并扩池前，任何模型都进不了"可信在位"状态。
    """
    today = today or datetime.now(timezone.utc)
    model = db.load_model(model_id, db_path)
    if model is None:
        raise FileNotFoundError(f"模型不存在：{model_id}")

    n_rows = int(model.get("n_rows", 0) or 0)
    n_positive = int(model.get("n_positive", 0) or 0)
    max_trade_date = model.get("max_trade_date")
    is_classification = model.get("target") in ("win5",)
    oos_brier = model.get("oos_brier")
    base_rate_brier = model.get("base_rate_brier")

    gates: List[GateResult] = []
    gates.append(
        GateResult(
            "min_samples",
            n_rows >= min_samples,
            f"n_rows={n_rows} 需 ≥ {min_samples}",
        )
    )
    gates.append(
        GateResult(
            "min_positives",
            n_positive >= min_positives,
            f"n_positive={n_positive} 需 ≥ {min_positives}",
        )
    )
    age_days = _staleness_days(max_trade_date, today)
    if age_days is None:
        gates.append(GateResult("max_staleness", False, f"无法解析 max_trade_date={max_trade_date!r}"))
    else:
        gates.append(
            GateResult(
                "max_staleness",
                age_days <= max_staleness_days,
                f"数据滞后 {age_days} 天（>{max_staleness_days} 即拒）",
            )
        )
    if is_classification:
        if oos_brier is None or base_rate_brier is None:
            gates.append(GateResult("beats_base_rate", False, "缺 OOS/基线 Brier，无法判定"))
        else:
            gates.append(
                GateResult(
                    "beats_base_rate",
                    float(oos_brier) < float(base_rate_brier),
                    f"OOS Brier {oos_brier:.4f} 需 < 基线 {base_rate_brier:.4f}",
                )
            )

    failed = [g for g in gates if not g.passed]
    staleness_failed = any(g.name == "max_staleness" and not g.passed for g in gates)
    quality_failed = any(
        g.name in ("min_samples", "min_positives", "beats_base_rate") and not g.passed
        for g in gates
    )

    warnings: List[str] = [f"门禁未过：{g.name}（{g.detail}）" for g in failed]

    if not failed:
        status, stale = "promoted", False
        promoted = True
    elif force:
        stale = staleness_failed
        # 质量闸不过 → 演示工件；否则（仅新鲜度不过）→ 在位但标 stale
        status = "demo" if quality_failed else "promoted"
        promoted = True
        if stale:
            warnings.append(f"数据滞后（max_trade_date={max_trade_date}），已标 stale=True")
        if status == "demo":
            warnings.append("质量门禁未过，force 晋升为 demo（非可信在位模型）")
    else:
        status, stale, promoted = "candidate", bool(model.get("stale")), False
        warnings.append("默认拒绝晋升（加 --force 可放行并打 stale/demo 标记）")

    if promoted:
        db.retire_promoted_models(
            str(model.get("target")), int(model.get("horizon", 5) or 5),
            exclude_model_id=model_id, path=db_path,
        )
        db.set_model_status(model_id, status, stale=stale, warnings=warnings, path=db_path)
        _update_meta_status(model_id, status, stale, warnings, models_dir)
        logger.info("🎖️ 模型 %s 晋升为 %s（stale=%s）", model_id, status, stale)
    else:
        logger.warning("模型 %s 晋升被拒：%s", model_id, "; ".join(g.detail for g in failed))

    return PromotionReport(
        model_id=model_id,
        promoted=promoted,
        status=status,
        stale=stale,
        forced=force,
        gates=tuple(gates),
        warnings=tuple(warnings),
    )


def _update_meta_status(
    model_id: str,
    status: str,
    stale: bool,
    warnings: Sequence[str],
    models_dir: Optional[Path],
) -> None:
    """同步 meta.json 的 status/stale/warnings（与 DB 一致，便于离线溯源）。"""
    root = Path(models_dir) if models_dir is not None else DEFAULT_MODELS_DIR
    meta_path = root / model_id / "meta.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):  # pragma: no cover - 损坏工件兜底
        return
    meta["status"] = status
    meta["stale"] = stale
    meta["warnings"] = list(warnings)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def load_promoted(
    target: str = "win5",
    *,
    include_demo: bool = False,
    db_path: Optional[Path] = None,
    models_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """取在位模型工件（meta + model + calibrator）供 D5 打分；无则 ``None``。

    默认只取 ``status='promoted'``（排除 demo）；``include_demo=True`` 时也纳入演示工件
    （UI 须显示"演示模型"警告）。sklearn/joblib 缺失时返回 ``None``（懒加载，不抛错）。
    """
    row = db.load_promoted_model(target, include_demo=include_demo, path=db_path)
    if row is None:
        return None
    try:
        return load_artifact(
            str(row["model_id"]), models_dir=models_dir, need_model=True
        )
    except (FileNotFoundError, ImportError) as exc:
        logger.warning("在位模型 %s 工件不可用：%s", row["model_id"], exc)
        return None
