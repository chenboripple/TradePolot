"""D3：模型训练（锚定滚动 OOS + 概率校准）。

把 D2 数据集喂进**滚动 walk-forward** 训练：每折只用该折 ``train_idx`` 拟合、``val_idx``
选超参/拟合校准、``test_idx`` 出**样本外**预测；拼接全部 test 折 → 一条诚实的 OOS 预测
序列（模型选择绝不看 test）。最终 serving 工件取**末折**模型（最近一段 walk-forward，
D5 加载它对未来打分）。

铁律：

- **sklearn 懒加载**——模块顶层只依赖 numpy；``train_model`` 内部才 import sklearn。
  未安装时 ``import ripple_tradePilot.ml.models`` 不炸，只有真去训练才报清晰错误。
- **NaN 容忍分两条路**：hgb（``HistGradientBoosting*``）原生吞 NaN，缺特征组无需插补；
  logreg 走 ``SimpleImputer(median, add_indicator=True) + StandardScaler`` 管道（中位数
  插补 + 缺失指示列），与 D1"缺组填 NaN + _has_* 标志"自洽。
- **分类才校准**：``CalibratedClassifierCV(method="sigmoid", cv="prefit")`` 在 val 折拟合
  Platt scaling；回归目标（ret5/mae5）不校准。

目标 → 标签列 / 任务类型：

===========  =====================  ==========
target       label column           task
===========  =====================  ==========
``win5``     ``label_win``          分类（主，预测净收益为正的概率）
``ret5``     ``label_net_return``   回归（预期净收益）
``mae5``     ``label_mae``          回归（下行风险 MAE）
===========  =====================  ==========

本模块只算不写：工件落盘/落库在 :mod:`registry`，评估报告在 :mod:`evaluate`。
"""

from __future__ import annotations

import contextlib
import logging
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from ripple_tradePilot.ml import calibration
from ripple_tradePilot.ml.splits import RollingSplit

logger = logging.getLogger(__name__)

__all__ = [
    "TrainResult",
    "train_model",
    "TARGET_LABEL_COLUMN",
    "CLASSIFICATION_TARGETS",
    "LOGREG_C_GRID",
    "DEFAULT_THRESHOLD",
]

# target → (标签列, 是否分类)
TARGET_LABEL_COLUMN: Dict[str, str] = {
    "win5": "label_win",
    "ret5": "label_net_return",
    "mae5": "label_mae",
}
CLASSIFICATION_TARGETS = frozenset({"win5"})
# logreg 正则强度网格（在 val 折按 Brier 选）
LOGREG_C_GRID: Tuple[float, ...] = (0.01, 0.1, 1.0)
# 决策表/覆盖率默认阈值
DEFAULT_THRESHOLD = 0.5

_HGB_KWARGS: Dict[str, Any] = {
    "max_iter": 200,
    "learning_rate": 0.06,
    "max_leaf_nodes": 15,
    "l2_regularization": 1.0,
    "n_iter_no_change": 10,
    "random_state": 0,
}


def _require_sklearn() -> Any:
    """懒加载 sklearn；未安装时给出明确、不致命的错误（信号链路不依赖 ML）。"""
    try:
        import sklearn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ImportError(
            "模型训练需要 scikit-learn：pip install 'scikit-learn>=1.4,<1.7'。"
            "未安装时 dashboard/monitor 的信号链路照常运行（ML 为可选增强）。"
        ) from exc
    return sklearn


@contextlib.contextmanager
def _quiet_sklearn() -> Iterator[None]:
    """抑制训练期三类**已知无害**告警，避免淹没 CLI 输出（数值结果不受影响）。

    - macOS Accelerate BLAS 在 numpy 2.x 下 ``matmul`` 的虚假 FP ``RuntimeWarning``
      （divide/overflow/invalid）——与 calibration.py 用 einsum 规避的是同一类伪告警，
      特征已标准化 + L2 正则，结果有限；
    - ``SimpleImputer`` 对全 NaN 列的 ``UserWarning``——缺特征组按设计整列填 NaN
      （D1 的 _has_* 降级），中位数插补 + 缺失指示列已正确处理；
    - ``CalibratedClassifierCV(cv="prefit")`` 在 sklearn 1.6 的弃用提示——我们 pin
      ``<1.7``，行为不变（1.8 移除前再迁 ``FrozenEstimator``）。

    **不**抑制 ``ConvergenceWarning``（message 不匹配下面的过滤器），真实不收敛仍可见。
    """
    with warnings.catch_warnings(), np.errstate(
        divide="ignore", over="ignore", invalid="ignore"
    ):
        warnings.simplefilter("ignore", RuntimeWarning)
        warnings.filterwarnings(
            "ignore", message=r".*Skipping features without any observed values.*"
        )
        warnings.filterwarnings("ignore", message=r".*cv='prefit'.*")
        yield


@dataclass
class TrainResult:
    """一次滚动训练的产物：OOS 预测 + 末折 serving 工件 + 摘要指标。"""

    kind: str
    target: str
    is_classification: bool
    oos_idx: np.ndarray  # 指向原 X/y 的行号（升序）
    p_oos: np.ndarray  # OOS 预测（分类=校准后概率，回归=预测值）
    y_oos: np.ndarray  # OOS 真值
    model: Any  # 末折 base 模型（serving 用）
    calibrator: Optional[Any]  # 末折校准器（回归为 None）
    selected_params: Dict[str, Any]  # 末折选中的超参
    metrics: Dict[str, float]  # OOS 摘要指标（register_model 落库）
    n_splits_used: int
    per_split: List[Dict[str, Any]] = field(default_factory=list)


def _fit_classifier(
    kind: str,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: Optional[np.ndarray],
    y_val: Optional[np.ndarray],
    sklearn: Any,
) -> Tuple[Any, Optional[Any], Dict[str, Any]]:
    """拟合分类 base（logreg 选 C / hgb 早停）+ 在 val 折 Platt 校准。

    返回 ``(base, calibrator, params)``；val 缺失或单类时 calibrator 为 None（直接用
    base.predict_proba）。
    """
    params: Dict[str, Any] = {"kind": kind}
    if kind == "logreg":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        best_c, best_brier = 0.1, float("inf")
        if x_val is not None and len(np.unique(y_train)) >= 2:
            for c in LOGREG_C_GRID:
                pipe = Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scaler", StandardScaler()),
                        ("clf", LogisticRegression(C=c, max_iter=1000)),
                    ]
                )
                pipe.fit(x_train, y_train)
                p_val = pipe.predict_proba(x_val)[:, 1]
                brier = calibration.brier_score(y_val, p_val)
                if brier < best_brier:
                    best_brier, best_c = brier, c
        params["C"] = best_c
        params["val_brier"] = best_brier if np.isfinite(best_brier) else None
        base = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(C=best_c, max_iter=1000)),
            ]
        )
        base.fit(x_train, y_train)
    elif kind == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier

        # 早停从 train 内部切 validation_fraction；样本太少则关早停防警告
        enable_es = x_train.shape[0] >= 50
        kwargs = dict(_HGB_KWARGS)
        kwargs["early_stopping"] = enable_es
        if enable_es:
            kwargs["validation_fraction"] = 0.15
        base = HistGradientBoostingClassifier(**kwargs)
        base.fit(x_train, y_train)
        params["early_stopping"] = enable_es
        params["n_iter_"] = int(getattr(base, "n_iter_", kwargs["max_iter"]))
    else:
        raise ValueError(f"未知模型 kind：{kind}（合法：logreg/hgb）")

    calibrator = _fit_calibrator(base, x_val, y_val, sklearn)
    return base, calibrator, params


def _fit_calibrator(
    base: Any, x_val: Optional[np.ndarray], y_val: Optional[np.ndarray], sklearn: Any
) -> Optional[Any]:
    """在 val 折用 ``cv="prefit"`` 拟合 sigmoid 校准；val 不足/单类则返回 None。"""
    if x_val is None or y_val is None or x_val.shape[0] < 8:
        return None
    if len(np.unique(y_val)) < 2:
        return None
    from sklearn.calibration import CalibratedClassifierCV

    try:
        calibrator = CalibratedClassifierCV(base, method="sigmoid", cv="prefit")
        calibrator.fit(x_val, y_val)
        return calibrator
    except Exception as exc:  # pragma: no cover - 退化数据兜底
        logger.warning("校准拟合失败，回退未校准概率：%s", exc)
        return None


def _fit_regressor(
    kind: str, x_train: np.ndarray, y_train: np.ndarray, sklearn: Any
) -> Tuple[Any, Dict[str, Any]]:
    """拟合回归 base（ret5/mae5）；hgb 原生吞 NaN，logreg 走插补+Ridge 管道。"""
    params: Dict[str, Any] = {"kind": kind}
    if kind == "hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor

        enable_es = x_train.shape[0] >= 50
        kwargs = dict(_HGB_KWARGS)
        kwargs["early_stopping"] = enable_es
        if enable_es:
            kwargs["validation_fraction"] = 0.15
        base = HistGradientBoostingRegressor(**kwargs)
        base.fit(x_train, y_train)
        params["early_stopping"] = enable_es
        params["n_iter_"] = int(getattr(base, "n_iter_", kwargs["max_iter"]))
    elif kind == "logreg":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        base = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scaler", StandardScaler()),
                ("reg", Ridge(alpha=1.0)),
            ]
        )
        base.fit(x_train, y_train)
        params["alpha"] = 1.0
    else:
        raise ValueError(f"未知模型 kind：{kind}（合法：logreg/hgb）")
    return base, params


def _summarize(
    target: str, y_oos: np.ndarray, p_oos: np.ndarray, threshold: float
) -> Dict[str, float]:
    """OOS 摘要指标（numpy 纯算，评估链路复用，无需 sklearn）。"""
    metrics: Dict[str, float] = {"n_oos": int(y_oos.size)}
    if target in CLASSIFICATION_TARGETS:
        y_int = np.rint(y_oos).astype(int)
        base_rate = float(np.mean(y_int)) if y_int.size else 0.0
        metrics["base_rate"] = base_rate
        # 常数基线 Brier = p̄(1-p̄)（预测恒为基率时的 Brier）
        metrics["base_rate_brier"] = base_rate * (1.0 - base_rate)
        metrics["oos_brier"] = calibration.brier_score(y_int, p_oos)
        metrics["oos_logloss"] = calibration.log_loss(y_int, p_oos)
        metrics["oos_auc"] = calibration.roc_auc_score(y_int, p_oos)
        metrics["oos_ece"] = calibration.expected_calibration_error(y_int, p_oos)
        metrics["coverage_at_threshold"] = calibration.coverage_at_threshold(
            p_oos, threshold
        )
        dv = calibration.decision_value_report(y_int, np.zeros_like(p_oos), p_oos, threshold)
        metrics["win_rate_at_threshold"] = dv.win_rate
    else:
        resid = p_oos - y_oos
        metrics["oos_mae"] = float(np.mean(np.abs(resid))) if resid.size else float("nan")
        ss_res = float(np.sum(resid**2)) if resid.size else float("nan")
        ss_tot = float(np.sum((y_oos - np.mean(y_oos)) ** 2)) if y_oos.size else float("nan")
        metrics["oos_r2"] = (1.0 - ss_res / ss_tot) if ss_tot else float("nan")
    return metrics


def train_model(
    kind: str,
    target: str,
    x: Sequence[Sequence[float]],
    y: Sequence[float],
    splits: Sequence[RollingSplit],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> TrainResult:
    """锚定滚动训练 → 拼接全 OOS 预测（模型选择只看 train/val）。

    参数
    ----
    kind：``"logreg"``（线性 + 插补）或 ``"hgb"``（梯度提升，原生吞 NaN）。
    target：``"win5"``（分类）/``"ret5"``/``"mae5"``（回归）。
    x/y：特征矩阵与标签（行序与 ``splits`` 的索引一致，即 D2 帧的行序）。
    splits：:func:`ripple_tradePilot.ml.splits.rolling_splits` 的折序列。
    threshold：覆盖率/决策表默认阈值（仅影响摘要指标，不改训练）。

    返回 :class:`TrainResult`：``p_oos`` 为各 test 折拼接的样本外预测（未覆盖的训练
    种子段为 NaN 并被剔除），``model``/``calibrator`` 为末折 serving 工件。
    """
    sklearn = _require_sklearn()
    if target not in TARGET_LABEL_COLUMN:
        raise ValueError(f"未知 target：{target}；合法 {sorted(TARGET_LABEL_COLUMN)}")
    if kind not in ("logreg", "hgb"):
        raise ValueError(f"未知模型 kind：{kind}（合法：logreg/hgb）")
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    if x_arr.ndim != 2:
        raise ValueError("x 必须是二维特征矩阵")
    if x_arr.shape[0] != y_arr.shape[0]:
        raise ValueError("x 与 y 行数必须一致")
    if not splits:
        raise ValueError("splits 为空，无法训练")

    is_classification = target in CLASSIFICATION_TARGETS
    n = y_arr.shape[0]
    p_oos = np.full(n, np.nan)
    per_split: List[Dict[str, Any]] = []
    last_model: Any = None
    last_calibrator: Optional[Any] = None
    last_params: Dict[str, Any] = {}

    for split in splits:
        train_idx = np.asarray(split.train_idx, dtype=int)
        val_idx = np.asarray(split.val_idx, dtype=int)
        test_idx = np.asarray(split.test_idx, dtype=int)
        if train_idx.size == 0 or test_idx.size == 0:
            logger.debug("折 %d 训练/测试为空，跳过", split.split_index)
            continue
        x_tr, y_tr = x_arr[train_idx], y_arr[train_idx]
        x_val = x_arr[val_idx] if val_idx.size else None
        y_val = y_arr[val_idx] if val_idx.size else None
        x_te = x_arr[test_idx]

        if is_classification and len(np.unique(y_tr)) < 2:
            logger.warning("折 %d 训练集单一类别，跳过", split.split_index)
            continue

        with _quiet_sklearn():
            if is_classification:
                base, calibrator, params = _fit_classifier(kind, x_tr, y_tr, x_val, y_val, sklearn)
                predictor = calibrator if calibrator is not None else base
                p_te = predictor.predict_proba(x_te)[:, 1]
            else:
                base, params = _fit_regressor(kind, x_tr, y_tr, sklearn)
                calibrator = None
                p_te = base.predict(x_te)

        p_oos[test_idx] = p_te
        per_split.append(
            {
                "split_index": split.split_index,
                "n_train": int(train_idx.size),
                "n_val": int(val_idx.size),
                "n_test": int(test_idx.size),
                "n_purged": split.n_purged,
                "n_embargoed": split.n_embargoed,
                "params": params,
            }
        )
        last_model, last_calibrator, last_params = base, calibrator, params

    oos_mask = ~np.isnan(p_oos)
    oos_idx = np.flatnonzero(oos_mask)
    if oos_idx.size == 0:
        raise ValueError("无任何 OOS 预测（所有折均被跳过）——检查数据量/类别平衡/splits")
    if last_model is None:  # pragma: no cover - oos_idx 非空时必有末折
        raise ValueError("训练未产出可用模型")

    p_sel = p_oos[oos_idx]
    y_sel = y_arr[oos_idx]
    metrics = _summarize(target, y_sel, p_sel, threshold)

    logger.info(
        "🧠 训练完成 kind=%s target=%s：OOS %d 行 · %d 折 · %s",
        kind,
        target,
        oos_idx.size,
        len(per_split),
        " ".join(f"{k}={v:.4f}" for k, v in metrics.items() if isinstance(v, float)),
    )
    return TrainResult(
        kind=kind,
        target=target,
        is_classification=is_classification,
        oos_idx=oos_idx,
        p_oos=p_sel,
        y_oos=y_sel,
        model=last_model,
        calibrator=last_calibrator,
        selected_params=last_params,
        metrics=metrics,
        n_splits_used=len(per_split),
        per_split=per_split,
    )
