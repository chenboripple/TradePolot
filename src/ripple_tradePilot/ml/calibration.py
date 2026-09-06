"""校准与决策价值评估（B4 · numpy 纯函数，sklearn 可选加速）。

把"概率到底准不准"和"过了阈值的信号值不值得下手"两件事拆开度量，都是路线图
的硬需求：

- **校准**：模型说"58% 胜率"时，实际是不是 58%？用 Brier / log-loss / 可靠性分桶
  （reliability table）/ ECE / logit 空间的校准斜率截距衡量。完美校准 → slope≈1、
  intercept≈0、ECE≈0。dashboard 此前把"票数占比"当 confidence 显示，正是因为缺这层
  度量——vote_ratio 是规则一致性，不是概率，无法校准。
- **决策价值**：``decision_value_report`` 在给定阈值下输出过滤子集的
  **覆盖率 / 胜率 / 平均净收益 / 每信号期望**，直接实现路线图第 4 点"围绕净期望收益
  做决策，并跟踪覆盖率"。覆盖率是关键护栏——防止"几乎不交易刷高胜率"的假象。

全部为 numpy 纯函数，不依赖 sklearn（IRLS 自实现 2 参数逻辑斯谛拟合）；输入是
一维 array-like，``y`` 为 0/1 标签，``p`` 为预测概率，``net_ret`` 为 B1 净收益标签。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "ReliabilityBin",
    "DecisionValue",
    "brier_score",
    "log_loss",
    "reliability_table",
    "expected_calibration_error",
    "calibration_slope_intercept",
    "coverage_at_threshold",
    "decision_value_report",
]

_EPS = 1e-15


def _as_array(values: Sequence[float]) -> np.ndarray:
    return np.asarray(list(values), dtype=float)


def brier_score(y: Sequence[int], p: Sequence[float]) -> float:
    """Brier 分数 = mean((p - y)^2)，越小越好；完美校准+完美判别 → 0。"""
    y_arr = _as_array(y)
    p_arr = _as_array(p)
    if y_arr.shape != p_arr.shape:
        raise ValueError("y 与 p 长度必须一致")
    if y_arr.size == 0:
        return 0.0
    return float(np.mean((p_arr - y_arr) ** 2))


def log_loss(y: Sequence[int], p: Sequence[float], eps: float = _EPS) -> float:
    """二元交叉熵损失，越小越好；预测概率被夹到 [eps, 1-eps] 防 log(0)。"""
    y_arr = _as_array(y)
    p_arr = np.clip(_as_array(p), eps, 1.0 - eps)
    if y_arr.shape != p_arr.shape:
        raise ValueError("y 与 p 长度必须一致")
    if y_arr.size == 0:
        return 0.0
    return float(-np.mean(y_arr * np.log(p_arr) + (1.0 - y_arr) * np.log(1.0 - p_arr)))


@dataclass(frozen=True)
class ReliabilityBin:
    """可靠性图单桶：桶内样本数、平均预测概率、实际正例率。"""

    bin_index: int
    lower: float
    upper: float
    count: int
    mean_predicted: float
    mean_actual: float

    @property
    def gap(self) -> float:
        """预测与实际的偏差（校准误差贡献），带符号。"""
        return self.mean_predicted - self.mean_actual


def reliability_table(
    y: Sequence[int], p: Sequence[float], n_bins: int = 10
) -> List[ReliabilityBin]:
    """把 [0,1] 等宽分 ``n_bins`` 桶，逐桶统计平均预测 vs 实际正例率。

    空桶（count=0）保留占位（mean 记 0.0），便于画完整可靠性图。分桶用左闭右开
    ``[lower, upper)``，最后一桶右闭以纳入 p==1.0。
    """
    if n_bins < 1:
        raise ValueError("n_bins 必须 ≥ 1")
    y_arr = _as_array(y)
    p_arr = _as_array(p)
    if y_arr.shape != p_arr.shape:
        raise ValueError("y 与 p 长度必须一致")

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: List[ReliabilityBin] = []
    for k in range(n_bins):
        lower, upper = float(edges[k]), float(edges[k + 1])
        if k == n_bins - 1:
            mask = (p_arr >= lower) & (p_arr <= upper)
        else:
            mask = (p_arr >= lower) & (p_arr < upper)
        count = int(mask.sum())
        if count:
            mean_pred = float(p_arr[mask].mean())
            mean_act = float(y_arr[mask].mean())
        else:
            mean_pred = 0.0
            mean_act = 0.0
        bins.append(
            ReliabilityBin(
                bin_index=k,
                lower=lower,
                upper=upper,
                count=count,
                mean_predicted=mean_pred,
                mean_actual=mean_act,
            )
        )
    return bins


def expected_calibration_error(
    y: Sequence[int], p: Sequence[float], n_bins: int = 10
) -> float:
    """ECE = Σ (桶样本占比 × |平均预测 − 实际正例率|)，越小越校准。"""
    y_arr = _as_array(y)
    total = y_arr.size
    if total == 0:
        return 0.0
    ece = 0.0
    for b in reliability_table(y, p, n_bins=n_bins):
        if b.count:
            ece += (b.count / total) * abs(b.mean_predicted - b.mean_actual)
    return float(ece)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def calibration_slope_intercept(
    y: Sequence[int], p: Sequence[float], eps: float = 1e-6, max_iter: int = 50
) -> Tuple[float, float]:
    """logit 空间校准回归：拟合 ``y ~ sigmoid(a + b·logit(p))``，返回 (slope=b, intercept=a)。

    完美校准时 ``logit(p)`` 即真实 logit，故 b≈1、a≈0。b<1 表示模型过度自信
    （预测比实际更极端），b>1 表示不够自信。用 IRLS（迭代重加权最小二乘）自实现
    2 参数逻辑斯谛回归，不依赖 sklearn；Hessian 加极小 ridge 防奇异。
    """
    y_arr = _as_array(y)
    p_arr = np.clip(_as_array(p), eps, 1.0 - eps)
    if y_arr.shape != p_arr.shape:
        raise ValueError("y 与 p 长度必须一致")
    if y_arr.size < 2:
        return (1.0, 0.0)

    logit_p = np.log(p_arr / (1.0 - p_arr))
    X = np.column_stack([np.ones_like(logit_p), logit_p])
    beta = np.array([0.0, 1.0])  # 从"完美校准"初值起步，收敛更快
    ridge = 1e-8
    for _ in range(max_iter):
        # einsum 而非 matmul：规避 macOS Accelerate BLAS 在 numpy 2.x 的虚假 FP 警告
        eta = np.clip(np.einsum("ij,j->i", X, beta), -35.0, 35.0)
        mu = _sigmoid(eta)
        w = np.clip(mu * (1.0 - mu), 1e-9, None)
        z = eta + (y_arr - mu) / w
        H = np.einsum("ni,n,nj->ij", X, w, X) + ridge * np.eye(2)
        g = np.einsum("ni,n,n->i", X, w, z)
        try:
            new_beta = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:  # pragma: no cover - ridge 兜底后几乎不触发
            break
        if np.max(np.abs(new_beta - beta)) < 1e-9:
            beta = new_beta
            break
        beta = new_beta
    return (float(beta[1]), float(beta[0]))


def coverage_at_threshold(p: Sequence[float], threshold: float) -> float:
    """覆盖率 = p ≥ threshold 的样本占比 ∈ [0,1]。

    阈值 ≤ 最小 p → 1.0；阈值 > 最大 p → 0.0。这是"过滤后还剩多少信号"的度量，
    与胜率一起看才能识别"高胜率但几乎不出手"的退化策略。
    """
    p_arr = _as_array(p)
    if p_arr.size == 0:
        return 0.0
    return float(np.mean(p_arr >= threshold))


@dataclass(frozen=True)
class DecisionValue:
    """阈值过滤后子集的决策价值指标（路线图第 4 点：净期望收益 + 覆盖率）。"""

    threshold: float
    n_total: int
    n_signals: int
    coverage: float  # n_signals / n_total
    win_rate: float  # 子集实际正例率 mean(y)
    avg_net_return: float  # 子集平均净收益 == 每信号期望
    avg_win: float  # 子集内 y==1 的平均净收益（无则 0.0）
    avg_loss: float  # 子集内 y==0 的平均净收益（无则 0.0）

    @property
    def expectancy(self) -> float:
        """每信号净期望收益（== avg_net_return，语义别名）。"""
        return self.avg_net_return


def decision_value_report(
    y: Sequence[int],
    net_ret: Sequence[float],
    p: Sequence[float],
    threshold: float,
) -> DecisionValue:
    """在 ``p ≥ threshold`` 的过滤子集上汇总覆盖率/胜率/平均净收益/每信号期望。

    与不过滤的基线对比（D4 评估报告做四方对比表）即可回答"模型过滤是否真的把
    期望收益做高了、代价是覆盖率降了多少"。子集为空时各均值记 0.0、coverage 记 0.0。
    """
    y_arr = _as_array(y)
    ret_arr = _as_array(net_ret)
    p_arr = _as_array(p)
    if not (y_arr.shape == ret_arr.shape == p_arr.shape):
        raise ValueError("y / net_ret / p 长度必须一致")
    n_total = int(y_arr.size)
    if n_total == 0:
        return DecisionValue(
            threshold=float(threshold),
            n_total=0,
            n_signals=0,
            coverage=0.0,
            win_rate=0.0,
            avg_net_return=0.0,
            avg_win=0.0,
            avg_loss=0.0,
        )

    mask = p_arr >= threshold
    n_signals = int(mask.sum())
    if n_signals == 0:
        return DecisionValue(
            threshold=float(threshold),
            n_total=n_total,
            n_signals=0,
            coverage=0.0,
            win_rate=0.0,
            avg_net_return=0.0,
            avg_win=0.0,
            avg_loss=0.0,
        )

    y_sub = y_arr[mask]
    ret_sub = ret_arr[mask]
    wins = ret_sub[y_sub == 1]
    losses = ret_sub[y_sub == 0]
    return DecisionValue(
        threshold=float(threshold),
        n_total=n_total,
        n_signals=n_signals,
        coverage=n_signals / n_total,
        win_rate=float(y_sub.mean()),
        avg_net_return=float(ret_sub.mean()),
        avg_win=float(wins.mean()) if wins.size else 0.0,
        avg_loss=float(losses.mean()) if losses.size else 0.0,
    )
