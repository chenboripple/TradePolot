"""D2：数据集构建（特征 + 标签 → csv.gz + manifest 落库）。

把 D1 的特征管道与 B1 的前瞻标签拼成可训练数据集，并产出**可溯源的 manifest**：

    build_dataset(symbols, start, end, ...)
      逐 symbol：load_daily_bars → A9 复权巡检（不过则拒入并记录）→
                 解析画像 → symbol_feature_frame（D1，train/serve 同一函数）→
                 label_series（5 主 + 10 辅 + MAE）→ 按 [start,end] + 标签完整过滤
      → 纵向拼接 → 内容 hash 定 dataset_id → 写 data/ml/<id>.csv.gz
      → manifest（json 文件 + ml_datasets 表）

manifest 记录：行数/正例率、日期范围、symbols、被拒标的、特征组、特征列、成本模型、
画像解析快照、每 symbol data_version、新鲜度戳（max_trade_date）、以及**诚实的前视
警告**（行业成分为最新单快照 → ``industry_point_in_time=False``）。

标签列命名（D3 训练目标按此映射）：

- ``label_win``：主 horizon 净收益 > 0（分类目标 "win5"）；
- ``label_net_return``：主 horizon 净收益（回归目标 "ret5"）；
- ``label_mae``：主 horizon 持有期最大回撤（回归目标 "mae5"，下行风险）；
- ``label_net_return_aux``：辅助 horizon（默认 10 日）净收益；
- ``exit_date``：标签平仓日（YYYYMMDD）——B3 时序切分的 purge 按此列剔除跨界样本。

本模块只读 DB（``load_*``）与写文件，不触网。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from ripple_tradePilot.backtest.costs import CostModel
from ripple_tradePilot.data.adjustment import audit_series
from ripple_tradePilot.ml.features import (
    DEFAULT_INDEX_CODE,
    FEATURE_GROUPS,
    assemble_dataset_frames,
    default_signal_spec,
    expected_columns,
    symbol_feature_frame,
)
from ripple_tradePilot.ml.labels import label_series
from ripple_tradePilot.signals.facade import coerce_bars
from ripple_tradePilot.signals.profile import ProfileSpec, parse_profile
from ripple_tradePilot.storage.database import (
    industry_board_for_symbol,
    load_daily_bars,
    load_index_bars,
    load_industry_board_bars,
    load_market_daily,
    register_dataset,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DatasetManifest",
    "LABEL_COLUMNS",
    "DEFAULT_ML_DIR",
    "MarketInputs",
    "SymbolFeatures",
    "build_dataset",
    "load_dataset_frame",
    "load_dataset",
    "load_symbol_features",
    "resolve_spec",
]

# 标签/元数据列（非特征）；D3 训练时据此区分 X 与 y。
LABEL_COLUMNS = (
    "label_win",
    "label_net_return",
    "label_mae",
    "label_net_return_aux",
    "exit_date",
)

DEFAULT_ML_DIR = Path("data/ml")

ProfileResolver = Callable[[str], Union[ProfileSpec, Mapping[str, Any], None]]


@dataclass(frozen=True)
class DatasetManifest:
    """数据集清单（可溯源 + 诚实警告）。``to_dict`` 供 json 序列化与 ml_datasets 落库。"""

    dataset_id: str
    n_rows: int
    n_positive: int
    positive_rate: float
    start_date: str
    end_date: str
    max_trade_date: str
    horizon: int
    aux_horizon: int
    index_code: str
    industry_point_in_time: bool
    csv_path: str
    symbols: Tuple[str, ...]
    rejected: Tuple[Dict[str, str], ...]
    groups: Tuple[str, ...]
    feature_columns: Tuple[str, ...]
    cost_model: Dict[str, float]
    profile_snapshot: Dict[str, Any]
    data_versions: Dict[str, str]
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "n_rows": self.n_rows,
            "n_positive": self.n_positive,
            "positive_rate": self.positive_rate,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "max_trade_date": self.max_trade_date,
            "horizon": self.horizon,
            "aux_horizon": self.aux_horizon,
            "index_code": self.index_code,
            "industry_point_in_time": self.industry_point_in_time,
            "csv_path": self.csv_path,
            "symbols": list(self.symbols),
            "rejected": list(self.rejected),
            "groups": list(self.groups),
            "feature_columns": list(self.feature_columns),
            "cost_model": dict(self.cost_model),
            "profile_snapshot": dict(self.profile_snapshot),
            "data_versions": dict(self.data_versions),
            "warnings": list(self.warnings),
        }


def load_dataset_frame(csv_path: Union[str, Path]) -> pd.DataFrame:
    """回读 ``build_dataset`` 产出的 csv.gz（含特征 + 标签 + exit_date）。"""
    return pd.read_csv(str(csv_path), compression="gzip")


def load_dataset(
    dataset_id: str, db_path: Optional[Path] = None
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """按 ``dataset_id`` 从 ``ml_datasets`` 取 manifest 并回读帧。

    返回 ``(manifest_dict, frame)``；数据集未注册或 csv 缺失则抛 ``FileNotFoundError``。
    D3 训练据此还原 X/y/日期轴，D4/D5 据此对齐特征列。
    """
    from ripple_tradePilot.storage.database import load_dataset_manifest

    manifest = load_dataset_manifest(dataset_id, db_path)
    if manifest is None:
        raise FileNotFoundError(f"数据集未注册：{dataset_id}")
    csv_path = manifest.get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        raise FileNotFoundError(f"数据集 csv 缺失：{csv_path}（dataset_id={dataset_id}）")
    return manifest, load_dataset_frame(csv_path)


def resolve_spec(
    symbol: str,
    profile_resolver: Optional[ProfileResolver] = None,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> ProfileSpec:
    """把 symbol 解析为 signal 组用的 :class:`ProfileSpec`（train/serve 共用）。

    优先级：``snapshot``（**serve** 路径——训练时记在 manifest.profile_snapshot[symbol]["spec"]
    里的画像快照，保证线上打分与训练用同一套组件）→ ``profile_resolver(symbol)``（**train**
    路径）→ :func:`default_signal_spec`（全局默认三件套）。

    snapshot 是 :func:`_spec_snapshot` 的产物（``kind``/``vote_threshold``/``components``
    列表），正好是 :func:`parse_profile` 认得的 components 结构，故可无损还原。
    """
    if snapshot:
        return parse_profile(dict(snapshot), source=str(snapshot.get("source") or "dataset"))
    if profile_resolver is None:
        return default_signal_spec()
    resolved = profile_resolver(symbol)
    if resolved is None:
        return default_signal_spec()
    if isinstance(resolved, ProfileSpec):
        return resolved
    return parse_profile(resolved, source="resolver")


def _spec_snapshot(spec: ProfileSpec) -> Dict[str, Any]:
    return {
        "kind": spec.kind,
        "vote_threshold": spec.vote_threshold,
        "source": spec.source,
        "components": [
            {"name": c.name, "kind": c.kind, "params": dict(c.params)}
            for c in spec.components
        ],
    }


@dataclass(frozen=True)
class MarketInputs:
    """市场级特征输入（跨标的共享）。批量构建只读一次，单标的打分按需现读。

    train(:func:`build_dataset`) 与 serve(:mod:`ml.scoring`) 共用本类——market 组的输入
    来源只有一处定义，杜绝两边读到不同口径的指数/宽度。
    """

    index_rows: Optional[Sequence[Mapping[str, Any]]] = None
    breadth_rows: Optional[Sequence[Mapping[str, Any]]] = None

    @classmethod
    def load(
        cls,
        groups: Sequence[str],
        *,
        index_code: str = DEFAULT_INDEX_CODE,
        db_path: Optional[Path] = None,
    ) -> "MarketInputs":
        """``market`` 组启用时读指数日线 + 全市场宽度；否则返回空输入（特征列自动 NaN）。"""
        if "market" not in groups:
            return cls(None, None)
        return cls(
            index_rows=load_index_bars(index_code, db_path),
            breadth_rows=load_market_daily(db_path),
        )


@dataclass(frozen=True)
class SymbolFeatures:
    """:func:`load_symbol_features` 的产物：原始日线行 + 特征帧 + 板块归属。"""

    rows: Sequence[Mapping[str, Any]]
    frame: pd.DataFrame
    board_code: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not self.rows


def load_symbol_features(
    symbol: str,
    *,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    groups: Sequence[str] = FEATURE_GROUPS,
    spec: Optional[ProfileSpec] = None,
    market: Optional[MarketInputs] = None,
    index_code: str = DEFAULT_INDEX_CODE,
    db_path: Optional[Path] = None,
) -> SymbolFeatures:
    """读库 → 单标的特征帧。**train(:func:`build_dataset`) 与 serve(:mod:`ml.scoring`)
    的唯一装配点**：两边调同一个函数、同一顺序读同一批表，故同 (symbol, trade_date) 的
    特征逐列一致（D5 硬测试钉住），train/serve 偏斜在结构上不可能发生。

    - ``rows`` 传 ``None`` 时自读 ``load_daily_bars(symbol)``；批量场景可先读出来做 A9
      复权巡检再传入，避免重复读库。
    - ``market`` 传 ``None`` 时按 ``groups`` 现读一次（单标的打分场景）；批量场景应
      :meth:`MarketInputs.load` 预读后复用，避免每标的重复读指数/宽度。
    - ``spec`` 传 ``None`` 时用全局默认三件套；serve 路径应传 manifest 里的画像快照
      （见 :func:`resolve_spec`）以对齐训练口径。
    - 无日线数据 → ``SymbolFeatures(rows=[], frame=空帧)``，不抛错（调用方优雅降级）。
    """
    if rows is None:
        rows = load_daily_bars(symbol, db_path)
    active_market = (
        market if market is not None else MarketInputs.load(groups, index_code=index_code, db_path=db_path)
    )
    board_code: Optional[str] = None
    board_rows: Optional[Sequence[Mapping[str, Any]]] = None
    if "industry" in groups:
        board_code = industry_board_for_symbol(symbol, db_path)
        if board_code:
            board_rows = load_industry_board_bars(board_code, db_path)

    frame = symbol_feature_frame(
        symbol,
        rows,
        groups=groups,
        spec=spec,
        index_rows=active_market.index_rows,
        breadth_rows=active_market.breadth_rows,
        board_rows=board_rows,
    )
    return SymbolFeatures(rows=rows, frame=frame, board_code=board_code)


def _data_version(rows: Sequence[Mapping[str, Any]]) -> str:
    """取该标的日线序列的 data_version（A9 溯源戳，序列级元数据，取末行）。"""
    for row in reversed(rows):
        version = row.get("data_version")
        if version:
            return str(version)
    return ""


def _content_hash(frame: pd.DataFrame, params: Mapping[str, Any]) -> str:
    """数据集内容 hash 前 12 位：帧 CSV（确定性浮点表示）+ 关键构建参数。"""
    payload = frame.to_csv(index=False)
    key = json.dumps(
        {"params": params, "payload_sha": hashlib.sha256(payload.encode()).hexdigest()},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _attach_labels(
    frame: pd.DataFrame,
    rows: Sequence[Mapping[str, Any]],
    *,
    horizon: int,
    aux_horizon: int,
    costs: CostModel,
    start: Optional[str],
    end: Optional[str],
) -> pd.DataFrame:
    """给 :func:`load_symbol_features` 产出的特征帧附加标签列，并按 [start,end] 与标签
    完整性过滤。

    特征与标签在此**彻底分离**：特征只由 :func:`load_symbol_features` 装配（serve 路径共用），
    标签是训练集专属（线上打分没有未来），故本函数只在 :func:`build_dataset` 内调用。
    """
    frame = frame.copy()
    bars = coerce_bars(rows)
    labels_main = label_series(bars, horizon=horizon, costs=costs)
    labels_aux = label_series(bars, horizon=aux_horizon, costs=costs)

    trade_dates = [str(r.get("trade_date")) for r in rows]
    n = len(rows)
    win = np.full(n, np.nan)
    net = np.full(n, np.nan)
    mae = np.full(n, np.nan)
    net_aux = np.full(n, np.nan)
    exit_dates: List[Optional[str]] = [None] * n
    for i in range(n):
        label = labels_main[i]
        if label is None:
            continue
        win[i] = 1.0 if label.win else 0.0
        net[i] = label.net_return
        mae[i] = label.mae
        if labels_aux[i] is not None:
            net_aux[i] = labels_aux[i].net_return
        exit_index = i + 1 + horizon
        if exit_index < n:
            exit_dates[i] = trade_dates[exit_index]

    frame["label_win"] = win
    frame["label_net_return"] = net
    frame["label_mae"] = mae
    frame["label_net_return_aux"] = net_aux
    frame["exit_date"] = exit_dates

    # 过滤：标签完整（win 非 NaN）+ trade_date 落在 [start, end]
    mask = frame["label_win"].notna()
    if start:
        mask &= frame["trade_date"] >= start
    if end:
        mask &= frame["trade_date"] <= end
    filtered = frame.loc[mask].copy()
    if not filtered.empty:
        filtered["label_win"] = filtered["label_win"].astype("int64")
    return filtered


def build_dataset(
    symbols: Sequence[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    *,
    horizon: int = 5,
    aux_horizon: int = 10,
    groups: Sequence[str] = FEATURE_GROUPS,
    profile_resolver: Optional[ProfileResolver] = None,
    db_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    index_code: str = DEFAULT_INDEX_CODE,
    costs: Optional[CostModel] = None,
    audit_tol: float = 0.01,
    register: bool = True,
) -> DatasetManifest:
    """构建数据集并落盘 + 落库，返回 :class:`DatasetManifest`。

    参数
    ----
    symbols：标的池（已规范化 symbol）。
    start/end：决策日范围（YYYYMMDD 闭区间）；None 表示不限。注意标签会用到 ``end``
        之后的 bar（前瞻窗口），这是标签的合法未来，不是特征前视。
    horizon/aux_horizon：主/辅前瞻标签天数（B1 口径：T+1 开盘进、T+1+h 开盘出）。
    groups：启用特征组（D1）。
    profile_resolver：symbol→画像（ProfileSpec/原始 dict/None）；缺省用全局默认三件套。
    db_path：SQLite 路径（缺省走环境变量/默认库）。
    out_dir：csv.gz 与 manifest.json 输出目录（缺省 ``data/ml``）。
    index_code：market 组基准指数。
    costs：标签成本模型（缺省镜像引擎默认）。
    audit_tol：A9 复权巡检容差；不过的标的**拒入**并记入 manifest.rejected。
    register：是否写 ml_datasets 表（测试可关，仅产文件）。
    """
    unknown = [g for g in groups if g not in FEATURE_GROUPS]
    if unknown:
        raise ValueError(f"未知特征组：{unknown}；合法值 {FEATURE_GROUPS}")
    if horizon < 1:
        raise ValueError("horizon 必须 ≥ 1")

    cost_model = costs or CostModel()
    ml_dir = Path(out_dir) if out_dir is not None else DEFAULT_ML_DIR
    ml_dir.mkdir(parents=True, exist_ok=True)

    # 市场级输入跨标的共享，只读一次（纯 DB、零网络）；与 serve 路径共用 MarketInputs.load
    market = MarketInputs.load(groups, index_code=index_code, db_path=db_path)

    frames: List[pd.DataFrame] = []
    included: List[str] = []
    rejected: List[Dict[str, str]] = []
    data_versions: Dict[str, str] = {}
    profile_snapshot: Dict[str, Any] = {}

    for symbol in symbols:
        rows = load_daily_bars(symbol, db_path)
        if not rows:
            rejected.append({"symbol": symbol, "reason": "no_data"})
            continue

        # A9 复权混接巡检：不过则拒入（绝不把混接序列喂进训练集）
        audit = audit_series(rows, tol=audit_tol)
        if not audit.clean:
            worst = audit.worst
            detail = (
                f"rebase_suspect@{worst.trade_date}(diff={worst.diff:.4f})"
                if worst
                else "rebase_suspect"
            )
            rejected.append({"symbol": symbol, "reason": detail})
            logger.warning("标的 %s 复权巡检不过，拒入数据集：%s", symbol, detail)
            continue

        spec = resolve_spec(symbol, profile_resolver)
        # 特征装配走 train/serve 唯一入口（serve 侧 ml.scoring 调同一函数、同一顺序）
        features = load_symbol_features(
            symbol,
            rows=rows,
            groups=groups,
            spec=spec,
            market=market,
            index_code=index_code,
            db_path=db_path,
        )
        frame = _attach_labels(
            features.frame,
            rows,
            horizon=horizon,
            aux_horizon=aux_horizon,
            costs=cost_model,
            start=start,
            end=end,
        )
        if frame.empty:
            rejected.append({"symbol": symbol, "reason": "no_labelable_rows"})
            continue

        included.append(symbol)
        data_versions[symbol] = _data_version(rows)
        # 画像 + 板块归属解析快照（provenance：训练用的 spec 与 industry 板块都可回溯，
        # 且 serve 侧据此还原同一 spec —— 见 resolve_spec 的 snapshot 分支）
        profile_snapshot[symbol] = {
            "spec": _spec_snapshot(spec) if "signal" in groups else None,
            "board_code": features.board_code,
        }
        frames.append(frame)

    assembled = assemble_dataset_frames(frames)
    if assembled.empty:
        # 无可训练行：仍产出一个空数据集 manifest（诚实记录，不抛错），便于 CLI 报告
        frame_out = pd.DataFrame(
            columns=["symbol", "trade_date", *expected_columns(groups), *LABEL_COLUMNS]
        )
    else:
        frame_out = assembled.reset_index()

    # 特征列 = 帧列去掉 symbol/trade_date/标志列/标签列（与 expected_columns 一致）
    feature_columns = tuple(expected_columns(groups))
    n_rows = int(len(frame_out))
    n_positive = int(frame_out["label_win"].sum()) if n_rows else 0
    positive_rate = float(n_positive / n_rows) if n_rows else 0.0

    trade_dates = frame_out["trade_date"].tolist() if n_rows else []
    start_date = str(min(trade_dates)) if trade_dates else (start or "")
    end_date = str(max(trade_dates)) if trade_dates else (end or "")
    max_trade_date = end_date

    # 内容 hash 定 dataset_id（同库同参数重跑 → 同 id，register 幂等）
    params = {
        "symbols": sorted(included),
        "groups": list(groups),
        "horizon": horizon,
        "aux_horizon": aux_horizon,
        "index_code": index_code,
        "start": start,
        "end": end,
        "cost_model": {
            "fee_rate": cost_model.fee_rate,
            "stamp_duty": cost_model.stamp_duty,
            "slippage": cost_model.slippage,
            "min_fee": cost_model.min_fee,
        },
    }
    dataset_id = _content_hash(frame_out, params)

    csv_path = ml_dir / f"{dataset_id}.csv.gz"
    frame_out.to_csv(csv_path, index=False, compression="gzip")

    # 诚实警告区
    warnings: List[str] = []
    if "industry" in groups:
        warnings.append(
            "行业成分为最新单快照（as_of≤trade_date 近似），industry_point_in_time=False："
            "存在轻微前视/幸存者偏差，行业特征增益须谨慎解读"
        )
    if rejected:
        warnings.append(
            f"{len(rejected)} 只标的被拒入："
            + "、".join(f"{r['symbol']}({r['reason']})" for r in rejected)
        )
    if "market" in groups and n_rows:
        has_market = float(frame_out.get("_has_market", pd.Series([0.0])).fillna(0).max())
        if has_market <= 0:
            warnings.append("缺指数/宽度数据：market 组全部 NaN 降级（_has_market=0）")
    if "industry" in groups and n_rows:
        has_industry = float(
            frame_out.get("_has_industry", pd.Series([0.0])).fillna(0).max()
        )
        if has_industry <= 0:
            warnings.append("缺板块数据：industry 组全部 NaN 降级（_has_industry=0）")

    manifest = DatasetManifest(
        dataset_id=dataset_id,
        n_rows=n_rows,
        n_positive=n_positive,
        positive_rate=positive_rate,
        start_date=start_date,
        end_date=end_date,
        max_trade_date=max_trade_date,
        horizon=horizon,
        aux_horizon=aux_horizon,
        index_code=index_code,
        industry_point_in_time=False,
        csv_path=str(csv_path),
        symbols=tuple(included),
        rejected=tuple(rejected),
        groups=tuple(groups),
        feature_columns=feature_columns,
        cost_model=params["cost_model"],
        profile_snapshot=profile_snapshot,
        data_versions=data_versions,
        warnings=tuple(warnings),
    )

    # manifest json 文件（与 csv 同目录，便于人工溯源）+ ml_datasets 表
    manifest_path = ml_dir / f"{dataset_id}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if register:
        register_dataset(manifest.to_dict(), db_path)

    logger.info(
        "📦 数据集 %s 构建完成：%d 行（正例 %.1f%%）· %d 标的 · 组 %s · %s",
        dataset_id,
        n_rows,
        positive_rate * 100,
        len(included),
        ",".join(groups),
        csv_path,
    )
    return manifest
