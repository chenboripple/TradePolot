"""D5：线上打分（dashboard / monitor 消费在位模型的唯一入口）。

三条铁律，与本包其余模块一致：

1. **懒加载**：本模块顶层只 import numpy，sklearn/joblib 由 :mod:`ml.registry` 在函数内
   惰性加载。未安装时 :meth:`SignalScorer.try_load` 返回 ``None``，dashboard 退化为只显示
   ``vote_ratio``（规则票占比）、monitor 通知不带预估块——**信号链路照常运行**。
2. **train/serve 同源**：特征装配调 :func:`ml.dataset.load_symbol_features`，与
   :func:`ml.dataset.build_dataset` 是**同一个函数**；画像用训练时记在 manifest
   ``profile_snapshot`` 里的快照还原（:func:`ml.dataset.resolve_spec`）。同 (symbol,
   trade_date) 两边逐列一致，偏斜在结构上不可能发生（``tests/test_scoring.py`` 硬测试钉住）。
3. **诚实降级**：缺特征组 → NaN 交给模型（hgb 原生容忍 / logreg 插补），并写进 warnings；
   无 ``ret5``/``mae5`` 在位模型 → 对应字段 ``None`` 而非编造；模型 ``stale``/``demo`` →
   warnings 明说。**打分永不抛错拖垮调用方**：任何异常都退化为 ``None`` + 日志。

期望净收益的来源分两种，都在 warnings 里注明口径：

- 有 ``ret5`` 在位模型 → 直接用它预测（同特征装配、独立对齐列）；
- 只有 ``win5`` → 退而用**该模型自己的 OOS 历史**：在 ``oos.npz`` 存下的
  (p_oos, y_oos, net_ret) 上取 ``p ≥ 当前 p_win`` 的子集，报其**已实现**平均净收益
  （:func:`ml.calibration.decision_value_report`，即路线图第 4 点的"净期望收益 + 覆盖率"）。
  这不是模型对未来的点预测，而是"历史上模型给出同等及以上置信度时，平均赚到多少"——
  子集样本数一并返回，过小则警告。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ripple_tradePilot.ml.calibration import decision_value_report
from ripple_tradePilot.ml.dataset import (
    MarketInputs,
    load_symbol_features,
    resolve_spec,
)
from ripple_tradePilot.ml.features import DEFAULT_INDEX_CODE, FEATURE_GROUPS
from ripple_tradePilot.ml.registry import load_promoted
from ripple_tradePilot.storage.database import load_dataset_manifest, load_promoted_model

logger = logging.getLogger(__name__)

__all__ = [
    "PRIMARY_TARGET",
    "RETURN_TARGET",
    "DOWNSIDE_TARGET",
    "ScoreResult",
    "SignalScorer",
    "get_scorer",
    "reset_scorer_cache",
]

# 主模型（胜率）与两个可选辅助模型（净收益 / 下行风险）的目标名，对应 D3 的 target 枚举
PRIMARY_TARGET = "win5"
RETURN_TARGET = "ret5"
DOWNSIDE_TARGET = "mae5"

# OOS 子集样本数低于此值 → 期望收益估计噪声过大，必须警告
_MIN_OOS_SIGNALS = 30


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> Optional[datetime]:
    """解析交易日戳为 naive ``datetime``；不可解析返回 ``None``（调用方据此跳过天数计算）。

    统一吃掉库内出现的几种写法：``20260801`` / ``2026-08-01`` / ``20260801T000000Z`` /
    ``2026-08-01T00:00:00``——去掉分隔符后取前 8 位数字按 ``YYYYMMDD`` 解析。
    """
    text = str(value or "").strip()
    if not text:
        return None
    digits = text.replace("-", "").replace(":", "").replace("T", "").replace("Z", "")[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return None


@dataclass(frozen=True)
class ScoreResult:
    """单标的的一次打分的结果（dashboard ``forecast`` 块 / monitor 通知 / 台账四列的来源）。

    三个数值字段都可能是 ``None``——**没有就是没有**，绝不用 0 或基率填充冒充预测：

    - ``p_win``：主模型（``win5``）给出的 5 日胜出概率（已 Platt 校准）；
    - ``expected_net_return``：期望净收益，口径见 :attr:`return_basis`；
    - ``downside_mae``：持有期最大不利偏移（负数，下行风险参考），需 ``mae5`` 在位模型。
    """

    model_id: str
    target: str
    horizon_days: int
    as_of: str
    p_win: Optional[float]
    expected_net_return: Optional[float] = None
    downside_mae: Optional[float] = None
    status: str = "promoted"
    stale: bool = False
    return_basis: str = ""
    oos_n_signals: int = 0
    oos_coverage: Optional[float] = None
    oos_win_rate: Optional[float] = None
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """API/前端用的 JSON 形态（浮点收敛到 6 位，None 原样保留）。"""

        def num(value: Optional[float], digits: int = 6) -> Optional[float]:
            return None if value is None else round(float(value), digits)

        return {
            "model_id": self.model_id,
            "target": self.target,
            "horizon_days": self.horizon_days,
            "as_of": self.as_of,
            "p_win": num(self.p_win, 4),
            "expected_net_return": num(self.expected_net_return),
            "downside_mae": num(self.downside_mae),
            "status": self.status,
            "stale": self.stale,
            "return_basis": self.return_basis,
            "oos_n_signals": self.oos_n_signals,
            "oos_coverage": num(self.oos_coverage, 4),
            "oos_win_rate": num(self.oos_win_rate, 4),
            "warnings": list(self.warnings),
        }


class SignalScorer:
    """持有已载入的在位模型工件，按标的打分。

    构造开销在 ``joblib.load``（反序列化），故进程内应复用实例——用 :func:`get_scorer`
    拿缓存版，别自己反复 :meth:`try_load`。
    """

    def __init__(
        self,
        artifacts: Mapping[str, Mapping[str, Any]],
        *,
        db_path: Optional[Path] = None,
        models_dir: Optional[Path] = None,
    ):
        if PRIMARY_TARGET not in artifacts:
            raise ValueError(f"打分器必须有主模型（target={PRIMARY_TARGET}）")
        self._artifacts: Dict[str, Mapping[str, Any]] = dict(artifacts)
        self._db_path = db_path
        self._models_dir = models_dir

    # --- 构造 -------------------------------------------------------------
    @classmethod
    def try_load(
        cls,
        *,
        target: str = PRIMARY_TARGET,
        aux_targets: Sequence[str] = (RETURN_TARGET, DOWNSIDE_TARGET),
        include_demo: bool = False,
        db_path: Optional[Path] = None,
        models_dir: Optional[Path] = None,
    ) -> Optional["SignalScorer"]:
        """尝试装配打分器；**任何缺失都返回 ``None``，绝不抛错**。

        返回 ``None`` 的情形：sklearn/joblib 未安装、无 ``target`` 在位模型、工件文件缺失。
        辅助目标（ret5/mae5）各自独立可选，缺了只是对应字段为 ``None``。
        ``include_demo=True`` 时纳入 force 晋升的演示工件（UI 会带"演示模型"警告）。
        """
        artifacts: Dict[str, Mapping[str, Any]] = {}
        for name in (target, *aux_targets):
            try:
                artifact = load_promoted(
                    name,
                    include_demo=include_demo,
                    db_path=db_path,
                    models_dir=models_dir,
                )
            except Exception as exc:  # 宽口径：打分不可用绝不能拖垮 dashboard/monitor
                logger.warning("加载在位模型失败（target=%s）：%s", name, exc)
                artifact = None
            if artifact is not None:
                artifacts[name] = artifact
        if target not in artifacts:
            return None
        return cls(artifacts, db_path=db_path, models_dir=models_dir)

    @property
    def model_id(self) -> str:
        return str(self._artifacts[PRIMARY_TARGET]["meta"].get("model_id", ""))

    @property
    def targets(self) -> Tuple[str, ...]:
        """已装配的目标名（主模型必在，辅助模型按可用性）。"""
        return tuple(self._artifacts)

    # --- 打分 -------------------------------------------------------------
    def score_symbol(
        self,
        symbol: str,
        *,
        as_of: Optional[str] = None,
        db_path: Optional[Path] = None,
        spec: Any = None,
        profile_resolver: Any = None,
    ) -> Optional[ScoreResult]:
        """给 ``symbol`` 打分；无日线数据 / 特征全空 / 预测失败 → ``None``（记日志，不抛错）。

        ``as_of`` 指定基准交易日（``YYYYMMDD``）；缺省取该标的特征帧的**最后一行**——即库内
        最新已收盘交易日。数据滞后时 ``as_of`` 会早于今天，这是诚实的：预估永远基于已入库的
        收盘数据，不用盘中未确认价。
        """
        path = db_path if db_path is not None else self._db_path
        artifact = self._artifacts[PRIMARY_TARGET]
        meta = artifact["meta"]
        warnings: List[str] = []

        groups = tuple(meta.get("feature_groups") or FEATURE_GROUPS)
        index_code = str(meta.get("index_code") or DEFAULT_INDEX_CODE)
        try:
            frame = self._feature_frame(
                symbol,
                meta=meta,
                groups=groups,
                index_code=index_code,
                spec=spec,
                profile_resolver=profile_resolver,
                db_path=path,
                warnings=warnings,
            )
        except Exception as exc:
            logger.warning("%s 特征装配失败，跳过打分：%s", symbol, exc)
            return None
        if frame is None or frame.empty:
            logger.info("%s 无可用特征行，跳过打分", symbol)
            return None

        row = self._pick_row(frame, as_of)
        if row is None:
            logger.info("%s 特征帧无 as_of=%s 对应行，跳过打分", symbol, as_of)
            return None
        row_as_of = str(row.get("trade_date", ""))

        columns = list(meta.get("feature_columns") or [])
        try:
            x = self._align(row, columns, warnings)
            predictor = artifact.get("calibrator") or artifact.get("model")
            p_win = float(np.asarray(predictor.predict_proba(x))[0, 1])
        except Exception as exc:
            logger.warning("%s 主模型预测失败，跳过打分：%s", symbol, exc)
            return None
        if not np.isfinite(p_win):
            logger.warning("%s 主模型预测非有限值，跳过打分", symbol)
            return None

        expected, basis, oos = self._expected_return(
            p_win, x, row, columns, meta, warnings, path
        )
        downside = self._downside(x, columns, warnings)
        warnings.extend(self._status_warnings(meta, row_as_of))

        return ScoreResult(
            model_id=str(meta.get("model_id", "")),
            target=str(meta.get("target", PRIMARY_TARGET)),
            horizon_days=int(meta.get("horizon") or 5),
            as_of=row_as_of,
            p_win=p_win,
            expected_net_return=expected,
            downside_mae=downside,
            status=str(meta.get("status", "promoted")),
            stale=bool(meta.get("stale", False)),
            return_basis=basis,
            oos_n_signals=oos[0],
            oos_coverage=oos[1],
            oos_win_rate=oos[2],
            warnings=tuple(warnings),
        )

    # --- 内部：特征装配（train/serve 同源）--------------------------------
    def _feature_frame(
        self,
        symbol: str,
        *,
        meta: Mapping[str, Any],
        groups: Sequence[str],
        index_code: str,
        spec: Any,
        profile_resolver: Any,
        db_path: Optional[Path],
        warnings: List[str],
    ):
        """走 :func:`load_symbol_features`（与 build_dataset 同一函数）装配单标的特征帧。"""
        active_spec = spec
        if active_spec is None:
            snapshot = self._snapshot_spec(meta.get("dataset_id"), symbol, db_path)
            if snapshot is not None:
                active_spec = snapshot
            else:
                active_spec = resolve_spec(symbol, profile_resolver)
                if "signal" in groups:
                    warnings.append(
                        "（无训练画像快照，signal 组用当前解析结果，可能与训练时口径不同）"
                    )
        market = MarketInputs.load(groups, index_code=index_code, db_path=db_path)
        features = load_symbol_features(
            symbol,
            groups=groups,
            spec=active_spec,
            market=market,
            index_code=index_code,
            db_path=db_path,
        )
        return features.frame

    def _snapshot_spec(
        self, dataset_id: Any, symbol: str, db_path: Optional[Path]
    ) -> Optional[Any]:
        """从 ``ml_datasets.profile_snapshot[symbol]['spec']`` 还原训练时的画像。"""
        if not dataset_id:
            return None
        try:
            manifest = load_dataset_manifest(str(dataset_id), db_path)
        except Exception as exc:
            logger.debug("读取数据集 manifest 失败（dataset_id=%s）：%s", dataset_id, exc)
            return None
        if not manifest:
            return None
        snapshot = (manifest.get("profile_snapshot") or {}).get(symbol) or {}
        spec_snapshot = snapshot.get("spec")
        if not spec_snapshot:
            return None
        try:
            return resolve_spec(symbol, snapshot=spec_snapshot)
        except Exception as exc:
            logger.warning("还原训练画像快照失败（%s）：%s", symbol, exc)
            return None

    @staticmethod
    def _pick_row(frame, as_of: Optional[str]):
        """取 ``as_of`` 对应行；缺省取最后一行（库内最新已收盘交易日）。"""
        if frame.empty:
            return None
        if as_of:
            matched = frame.loc[frame["trade_date"].astype(str) == str(as_of)]
            if matched.empty:
                return None
            return matched.iloc[-1].to_dict()
        return frame.iloc[-1].to_dict()

    @staticmethod
    def _align(row: Mapping[str, Any], columns: Sequence[str], warnings: List[str]):
        """按模型记录的 ``feature_columns`` 顺序取一行 → ``(1, n_features)`` float 数组。

        缺列 → NaN（hgb 原生容忍；logreg 的 Pipeline 里有中位数插补 + 缺失指示列）。
        列集合与训练时不一致是 train/serve 偏斜的信号，必须显式警告而非静默补 NaN。
        """
        missing = [c for c in columns if c not in row]
        if missing:
            warnings.append(
                f"⚠️ 特征列缺失 {len(missing)} 个（{', '.join(missing[:5])}"
                f"{'…' if len(missing) > 5 else ''}）——按 NaN 降级，预估可信度下降"
            )
        values = np.array(
            [[float(row[c]) if c in row and row[c] is not None else np.nan for c in columns]],
            dtype="float64",
        )
        return values

    # --- 内部：期望净收益 / 下行风险 --------------------------------------
    def _expected_return(
        self,
        p_win: float,
        x: np.ndarray,
        row: Mapping[str, Any],
        columns: Sequence[str],
        meta: Mapping[str, Any],
        warnings: List[str],
        db_path: Optional[Path],
    ) -> Tuple[Optional[float], str, Tuple[int, Optional[float], Optional[float]]]:
        """期望净收益 + 口径说明 +（OOS 子集样本数, 覆盖率, 胜率）。

        优先用 ``ret5`` 在位模型直接预测；否则用主模型自己的 OOS 历史做同置信度子集的
        已实现均值（见模块 docstring）。两条路都不通 → ``None``。
        """
        ret_artifact = self._artifacts.get(RETURN_TARGET)
        if ret_artifact is not None:
            try:
                ret_meta = ret_artifact["meta"]
                ret_columns = list(ret_meta.get("feature_columns") or columns)
                x_ret = x if ret_columns == list(columns) else self._align(row, ret_columns, [])
                value = float(np.asarray(ret_artifact["model"].predict(x_ret))[0])
                if np.isfinite(value):
                    return value, f"ret5 在位模型 {ret_meta.get('model_id')} 点预测", (0, None, None)
            except Exception as exc:
                logger.warning("ret5 模型预测失败，退回 OOS 口径：%s", exc)

        oos = self._artifacts[PRIMARY_TARGET].get("oos") or {}
        p_oos, y_oos, net_ret = oos.get("p_oos"), oos.get("y_oos"), oos.get("net_ret")
        if p_oos is None or y_oos is None or net_ret is None:
            warnings.append("（无 OOS 历史且无 ret5 在位模型，期望净收益不可用）")
            return None, "", (0, None, None)
        try:
            value_report = decision_value_report(y_oos, net_ret, p_oos, threshold=p_win)
        except Exception as exc:
            logger.warning("OOS 期望收益汇总失败：%s", exc)
            return None, "", (0, None, None)
        if value_report.n_signals == 0:
            warnings.append(
                f"（OOS 历史中无 p ≥ {p_win:.3f} 的样本，期望净收益不可用——当前置信度高于"
                "模型训练期见过的任何样本）"
            )
            return None, "", (0, None, None)
        if value_report.n_signals < _MIN_OOS_SIGNALS:
            warnings.append(
                f"⚠️ 期望净收益基于 OOS 同置信度子集仅 {value_report.n_signals} 个样本"
                f"（<{_MIN_OOS_SIGNALS}），噪声大，仅供排序参考"
            )
        basis = (
            f"主模型 OOS 历史中 p≥{p_win:.3f} 子集的已实现平均净收益"
            f"（n={value_report.n_signals}，覆盖率 {value_report.coverage:.1%}）"
        )
        return (
            value_report.avg_net_return,
            basis,
            (
                value_report.n_signals,
                value_report.coverage,
                value_report.win_rate,
            ),
        )

    def _downside(
        self, x: np.ndarray, columns: Sequence[str], warnings: List[str]
    ) -> Optional[float]:
        """持有期最大不利偏移（负数）。无 ``mae5`` 在位模型 → ``None`` + 提示，绝不编造。"""
        artifact = self._artifacts.get(DOWNSIDE_TARGET)
        if artifact is None:
            warnings.append("（无 mae5 在位模型，参考下行不可用）")
            return None
        try:
            value = float(np.asarray(artifact["model"].predict(x))[0])
        except Exception as exc:
            logger.warning("mae5 模型预测失败：%s", exc)
            return None
        if not np.isfinite(value):
            return None
        # mae5 标签是 |回撤| 的非负量；统一以负数呈现，UI 直接可读为"参考下行 −3.1%"
        return -abs(value)

    @staticmethod
    def _status_warnings(meta: Mapping[str, Any], as_of: str) -> List[str]:
        """模型状态与基准日相关的诚实提示。"""
        out: List[str] = []
        if str(meta.get("status", "")) == "demo":
            out.append("⚠️ 演示模型（demo）：晋升门禁未过，force 放行，不可作为交易依据")
        if meta.get("stale"):
            out.append(
                f"⚠️ 数据滞后：训练集截至 {meta.get('max_trade_date')}，模型已标 stale"
            )
        trained = _parse_date(meta.get("max_trade_date"))
        if trained is not None:
            age = (_today().replace(tzinfo=None) - trained).days
            if age > 0:
                out.append(f"（训练数据截至 {meta.get('max_trade_date')}，距今 {age} 天）")
        if as_of:
            out.append(f"（预估基准日 {as_of} = 库内最新已收盘交易日，非盘中实时）")
        return out


# ---------------------------------------------------------------------------
# 进程级缓存：dashboard 每请求 / monitor 每轮都调，不能反复反序列化 joblib
# ---------------------------------------------------------------------------
_CACHE: Dict[Tuple[Any, ...], Tuple[Optional[str], Optional[SignalScorer]]] = {}


def get_scorer(
    *,
    target: str = PRIMARY_TARGET,
    include_demo: bool = False,
    db_path: Optional[Path] = None,
    models_dir: Optional[Path] = None,
) -> Optional[SignalScorer]:
    """取缓存的打分器；**每次调用只多一次廉价 DB 查询**（当前在位 model_id）。

    model_id 未变 → 复用已载入工件的实例；变了（新晋升 / 退役 / 从有到无）→ 重新
    :meth:`SignalScorer.try_load`。这样既不反复反序列化模型，也不会让进程一直用着已被
    退役的旧模型。sklearn 缺失或无在位模型 → ``None``（调用方优雅降级）。
    """
    key = (target, include_demo, str(db_path or ""), str(models_dir or ""))
    try:
        row = load_promoted_model(target, include_demo=include_demo, path=db_path)
    except Exception as exc:
        logger.warning("查询在位模型失败：%s", exc)
        row = None
    current_id = str(row["model_id"]) if row else None

    cached_id, cached = _CACHE.get(key, (None, None))
    if cached is not None and cached_id == current_id:
        return cached
    if current_id is None:
        _CACHE[key] = (None, None)
        return None

    scorer = SignalScorer.try_load(
        target=target,
        include_demo=include_demo,
        db_path=db_path,
        models_dir=models_dir,
    )
    _CACHE[key] = (current_id if scorer is not None else None, scorer)
    return scorer


def reset_scorer_cache() -> None:
    """清空打分器缓存（测试用；生产无需调用——:func:`get_scorer` 自会按 model_id 失效）。"""
    _CACHE.clear()
