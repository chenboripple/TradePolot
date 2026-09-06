"""D1：特征管道（纯函数，train/serve 单一来源）。

**铁律**（D5 train/serve 一致性的根基）：

1. 每个特征在 T 日只使用 **≤T 收盘可得**的信息——日线/指数/宽度/板块全部因果对齐，
   绝不引入 T+1 及以后的数据（标签才用 T+1 开盘，见 :mod:`ml.labels`）；
2. 特征只依赖 **DB 可重建字段**（``load_daily_bars``/``load_index_bars``/``load_market_daily``/
   ``load_industry_board_bars`` 的列），不依赖任何运行期不可复现的中间态；
3. 同一函数 :func:`symbol_feature_frame` 既被 :mod:`ml.dataset`（批量训练）调用、也被
   :mod:`ml.scoring`（单标的在线打分）调用——**杜绝两套特征实现漂移**。

四组特征可按 ``groups`` 开关（路线图"每次只加一组验证增益"的机制保障）：

- ``signal``：规则引擎输出即特征（buy/sell_count、vote_threshold、recommendation one-hot、
  各组件带符号 side、state_age、max_strength）——直接调 :mod:`signals`，与回测/看板同源；
- ``price_volume``：ret_1/5/20、close/MA−1、rsi14、布林位置/宽度及其 60 日分位、量比、
  成交额 z、20 日突破、ATR% 及其 60 日均值比、日内振幅；
- ``market``：沪深300 的 ret_5/20、close/MA20−1、20 日波动；市场宽度 up_ratio 及其 MA5、
  涨停家数、成交额 z；
- ``industry``：所属东财板块 ret_5/20、close/MA20−1，以及个股相对板块的 5/20 日强弱差。

缺失组一律 **NaN + 组可用标志列**（``_has_market``/``_has_industry``），绝不插值造假；
下游模型（D3 HistGradientBoosting）原生容忍 NaN，logreg 路径再做中位数插补 + 缺失指示。

本模块不触网、不触库：所有输入都是调用方已从 DB 读好的行序列（``Mapping``），输出
``pandas.DataFrame``。指标口径复用 :mod:`indicators`（rsi/bollinger/atr/rolling_max），
保证与 dashboard/monitor/回测位级一致；MA 比值/量比等用 pandas 滚动窗口（与
``indicators.rolling_mean`` 同义，向量化更快）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from ripple_tradePilot.indicators import (
    atr_series,
    bollinger,
    rolling_max,
    rsi_series,
)
from ripple_tradePilot.signals.facade import coerce_bars, evaluate_symbol
from ripple_tradePilot.signals.profile import ProfileSpec, parse_profile

__all__ = [
    "FEATURE_GROUPS",
    "PRICE_VOLUME_COLUMNS",
    "SIGNAL_BASE_COLUMNS",
    "MARKET_COLUMNS",
    "INDUSTRY_COLUMNS",
    "FLAG_COLUMNS",
    "DEFAULT_INDEX_CODE",
    "symbol_feature_frame",
    "price_volume_features",
    "signal_features",
    "market_features",
    "industry_features",
    "assemble_dataset_frames",
    "expected_columns",
    "assert_no_lookahead",
    "default_signal_spec",
]

# 四组特征名（--groups 开关的合法取值；顺序即拼接顺序）
FEATURE_GROUPS = ("signal", "price_volume", "market", "industry")

# 市场环境特征默认基准指数（沪深300）
DEFAULT_INDEX_CODE = "000300.SH"

# 各组输出列（精确集合，供"组开关列集合匹配"测试与 manifest 记录）。
PRICE_VOLUME_COLUMNS = (
    "ret_1", "ret_5", "ret_20",
    "close_ma5_ratio", "close_ma20_ratio",
    "rsi14", "bb_pos", "bb_width", "bb_width_pct60",
    "vol_ratio", "amount_z20", "breakout_20",
    "atr_pct", "atr_pct_ratio60", "high_low_range",
)
# signal 组的固定列；各组件另有 comp_<name> 列（随 profile 变化，见 expected_columns）
SIGNAL_BASE_COLUMNS = (
    "buy_count", "sell_count", "vote_threshold",
    "rec_buy", "rec_sell", "rec_hold", "rec_conflict",
    "state_age", "max_strength",
)
MARKET_COLUMNS = (
    "idx_ret_5", "idx_ret_20", "idx_close_ma20_ratio", "idx_vol20",
    "up_ratio", "up_ratio_ma5", "limit_up_count", "total_amount_z",
)
INDUSTRY_COLUMNS = (
    "board_ret_5", "board_ret_20", "board_close_ma20_ratio",
    "rel_strength_5", "rel_strength_20",
)
# 组可用标志列（缺失组 → 0；用于下游显式区分"无数据"与"数据为 0"）
FLAG_COLUMNS = ("_has_market", "_has_industry")

# 滚动窗口的最小有效样本数（与窗口等长，warmup 期内一律 NaN，绝不部分填充）
_MIN_PERIODS = "full"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _float_or_nan(value: Any) -> float:
    try:
        if value is None:
            return np.nan
        result = float(value)
        return result if np.isfinite(result) else np.nan
    except (TypeError, ValueError):
        return np.nan


def _nan_series(values: Iterable[Optional[float]]) -> pd.Series:
    """把含 None 的指标序列转为 float Series（None→NaN），供 pandas 滚动运算。"""
    return pd.Series(
        [np.nan if v is None else float(v) for v in values], dtype="float64"
    )


def _arrays(rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Any]]:
    """从 DB 行序列抽取 OHLCV/amount/pct_chg 数组（load_daily_bars 的 volume 键为 ``vol``）。"""
    return {
        "trade_date": [str(r.get("trade_date")) for r in rows],
        "open": [_float_or_nan(r.get("open")) for r in rows],
        "high": [_float_or_nan(r.get("high")) for r in rows],
        "low": [_float_or_nan(r.get("low")) for r in rows],
        "close": [_float_or_nan(r.get("close")) for r in rows],
        "volume": [
            _float_or_nan(r.get("vol", r.get("volume"))) for r in rows
        ],
        "amount": [_float_or_nan(r.get("amount")) for r in rows],
    }


def _pct_change(closes: Sequence[float], periods: int) -> np.ndarray:
    """close/close[-periods] − 1；前 periods 个位置 NaN（因果，不看未来）。"""
    arr = np.asarray(closes, dtype="float64")
    out = np.full(len(arr), np.nan)
    if len(arr) <= periods:
        return out
    prev = arr[:-periods]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[periods:] = np.where(prev > 0, arr[periods:] / prev - 1.0, np.nan)
    return out


def _rolling_pct_rank(values: pd.Series, window: int) -> pd.Series:
    """trailing ``window`` 内 ≤ 当前值的比例 [0,1]（波动收缩/扩张的分位刻画）。

    严格 ``min_periods=window``：窗口内任一 NaN 即结果 NaN（warmup 期不部分填充）。
    """
    def _rank(win: np.ndarray) -> float:
        cur = win[-1]
        if np.isnan(cur):
            return np.nan
        return float((win <= cur).mean())

    return values.rolling(window, min_periods=window).apply(
        lambda w: _rank(np.asarray(w, dtype="float64")), raw=True
    )


def _zscore(values: pd.Series, window: int) -> pd.Series:
    """(x − MA_window) / std_window（总体口径 ddof=0，与布林带一致）；std=0 → NaN。"""
    mean = values.rolling(window, min_periods=window).mean()
    std = values.rolling(window, min_periods=window).std(ddof=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (values - mean) / std.replace(0.0, np.nan)
    return out


def _ratio_to_ma(values: pd.Series, window: int) -> pd.Series:
    """x / MA_window − 1（均线偏离）；MA=0 或缺值 → NaN。"""
    mean = values.rolling(window, min_periods=window).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        return values / mean.replace(0.0, np.nan) - 1.0


def default_signal_spec(vote_threshold: Optional[int] = None) -> ProfileSpec:
    """signal 组缺省画像：全局默认三件套（MA+RSI+布林，与 monitor/回测同一常量）。

    延迟导入避免 features ↔ backtest_profile 的循环依赖（后者也引用 signals）。
    """
    from ripple_tradePilot.signals.backtest_profile import default_profile

    return parse_profile(
        default_profile(vote_threshold), source="default"
    )


# ---------------------------------------------------------------------------
# price_volume 组
# ---------------------------------------------------------------------------
def price_volume_features(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """价量技术特征（仅用 ≤T 的 OHLCV）。返回以 0..n-1 为索引、列=PRICE_VOLUME_COLUMNS。

    与调用方 ``rows`` 逐行对齐（同序），供 :func:`symbol_feature_frame` 直接拼列。
    """
    arrays = _arrays(rows)
    closes = arrays["close"]
    highs = arrays["high"]
    lows = arrays["low"]
    volumes = arrays["volume"]
    amounts = arrays["amount"]
    n = len(closes)

    close_s = pd.Series(closes, dtype="float64")
    vol_s = pd.Series(volumes, dtype="float64")
    amt_s = pd.Series(amounts, dtype="float64")

    # 布林带 / RSI / ATR 复用 indicators 纯函数（与看板/监控位级一致），None→NaN
    bb_mid, bb_up, bb_low = bollinger(closes, window=20, num_std=2.0)
    bb_mid_s, bb_up_s, bb_low_s = (
        _nan_series(bb_mid), _nan_series(bb_up), _nan_series(bb_low)
    )
    rsi14 = _nan_series(rsi_series(closes, period=14))
    atr14 = _nan_series(atr_series(highs, lows, closes, period=14))

    # 布林位置 (close−下轨)/(上轨−下轨)，带宽 (上轨−下轨)/中轨
    band_span = (bb_up_s - bb_low_s).replace(0.0, np.nan)
    bb_pos = (close_s - bb_low_s) / band_span
    bb_width = (bb_up_s - bb_low_s) / bb_mid_s.replace(0.0, np.nan)

    # ATR 占价比 + 其 60 日均值比（当前波动 vs 近 60 日常态）
    atr_pct = atr14 / close_s.replace(0.0, np.nan)
    atr_pct_ma60 = atr_pct.rolling(60, min_periods=60).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_pct_ratio60 = atr_pct / atr_pct_ma60.replace(0.0, np.nan)

    # 20 日突破：收盘 ≥ 近 20 日最高收盘（含当日）→ 1，否则 0；warmup 期 NaN
    roll_max_close = _nan_series(rolling_max(closes, 20))
    breakout_20 = pd.Series(
        np.where(
            roll_max_close.isna(),
            np.nan,
            (close_s >= roll_max_close - 1e-12).astype("float64"),
        ),
        dtype="float64",
    )

    # 日内振幅 (high−low)/close
    with np.errstate(divide="ignore", invalid="ignore"):
        high_low_range = (pd.Series(highs) - pd.Series(lows)) / close_s.replace(0.0, np.nan)

    data = {
        "ret_1": _pct_change(closes, 1),
        "ret_5": _pct_change(closes, 5),
        "ret_20": _pct_change(closes, 20),
        "close_ma5_ratio": _ratio_to_ma(close_s, 5).to_numpy(),
        "close_ma20_ratio": _ratio_to_ma(close_s, 20).to_numpy(),
        "rsi14": rsi14.to_numpy(),
        "bb_pos": bb_pos.to_numpy(),
        "bb_width": bb_width.to_numpy(),
        "bb_width_pct60": _rolling_pct_rank(bb_width, 60).to_numpy(),
        "vol_ratio": (vol_s / vol_s.rolling(20, min_periods=20).mean().replace(0.0, np.nan)).to_numpy(),
        "amount_z20": _zscore(amt_s, 20).to_numpy(),
        "breakout_20": breakout_20.to_numpy(),
        "atr_pct": atr_pct.to_numpy(),
        "atr_pct_ratio60": atr_pct_ratio60.to_numpy(),
        "high_low_range": high_low_range.to_numpy(),
    }
    frame = pd.DataFrame(data, index=range(n), dtype="float64")
    return frame[list(PRICE_VOLUME_COLUMNS)]


# ---------------------------------------------------------------------------
# signal 组
# ---------------------------------------------------------------------------
def signal_features(
    rows: Sequence[Mapping[str, Any]],
    spec: ProfileSpec,
) -> pd.DataFrame:
    """规则引擎输出即特征：逐 bar 投票决策 → 票数/one-hot/组件带符号 side/状态龄/强度。

    直接调 :func:`signals.evaluate_symbol`（与回测/看板/监控同一实现），decisions[i]
    与 ``rows[i]`` 对齐。组件列 ``comp_<name>`` ∈ {+1 买, −1 卖, 0 不投票}，按 spec
    组件顺序排列（列集合随 profile 变化，见 :func:`expected_columns`）。
    """
    bars = coerce_bars(rows)
    evaluation = evaluate_symbol(bars, spec)
    decisions = evaluation.decisions
    n = len(decisions)

    component_names = [component.name for component in spec.components]
    rec_one_hot = {"rec_buy": [], "rec_sell": [], "rec_hold": [], "rec_conflict": []}
    buy_counts: List[float] = []
    sell_counts: List[float] = []
    thresholds: List[float] = []
    state_ages: List[float] = []
    max_strengths: List[float] = []
    comp_columns: Dict[str, List[float]] = {f"comp_{name}": [] for name in component_names}

    previous_rec: Optional[str] = None
    run_length = 0
    for decision in decisions:
        rec = decision.recommendation
        buy_counts.append(float(decision.buy_count))
        sell_counts.append(float(decision.sell_count))
        thresholds.append(float(decision.vote_threshold))
        for key in rec_one_hot:
            rec_one_hot[key].append(1.0 if rec == key[4:].upper() else 0.0)

        # state_age：当前 recommendation 连续持续的 bar 数（含本根）
        if rec == previous_rec:
            run_length += 1
        else:
            run_length = 1
            previous_rec = rec
        state_ages.append(float(run_length))

        # max_strength：投票组件中的最大 strength（无投票 → 0）
        voting = [c.strength for c in decision.components if c.side is not None]
        max_strengths.append(float(max(voting)) if voting else 0.0)

        # 组件带符号 side（按 name 取，缺失组件 → 0）
        side_by_name = {c.name: c.side for c in decision.components}
        for name in component_names:
            side = side_by_name.get(name)
            if side is None:
                comp_columns[f"comp_{name}"].append(0.0)
            else:
                # Side.BUY.value == "BUY"
                comp_columns[f"comp_{name}"].append(
                    1.0 if side.value == "BUY" else -1.0
                )

    data: Dict[str, Any] = {
        "buy_count": buy_counts,
        "sell_count": sell_counts,
        "vote_threshold": thresholds,
        "state_age": state_ages,
        "max_strength": max_strengths,
        **rec_one_hot,
        **comp_columns,
    }
    frame = pd.DataFrame(data, index=range(n), dtype="float64")
    ordered = list(SIGNAL_BASE_COLUMNS) + [f"comp_{name}" for name in component_names]
    return frame[ordered]


# ---------------------------------------------------------------------------
# market 组
# ---------------------------------------------------------------------------
def market_features(
    index_rows: Optional[Sequence[Mapping[str, Any]]],
    breadth_rows: Optional[Sequence[Mapping[str, Any]]],
) -> pd.DataFrame:
    """市场环境特征（沪深300 指数 + 全市场宽度），按 ``trade_date`` 索引、跨标的共享。

    指数与宽度各自因果计算后按 ``trade_date`` 外连接：某日只有指数没有宽度（增量积累制
    下宽度历史可能短于指数）→ 宽度列 NaN，反之亦然。空输入 → 空帧（下游 _has_market=0）。
    """
    frames: List[pd.DataFrame] = []

    if index_rows:
        idx = pd.DataFrame(
            {
                "trade_date": [str(r.get("trade_date")) for r in index_rows],
                "close": [_float_or_nan(r.get("close")) for r in index_rows],
                "pct_chg": [_float_or_nan(r.get("pct_chg")) for r in index_rows],
            }
        ).sort_values("trade_date").reset_index(drop=True)
        close_s = idx["close"]
        # 指数日收益：优先用 pct_chg（交易所口径），缺失时回退 close 环比
        ret = idx["pct_chg"] / 100.0
        ret = ret.where(ret.notna(), close_s.pct_change())
        idx_frame = pd.DataFrame(
            {
                "trade_date": idx["trade_date"],
                "idx_ret_5": _pct_change(close_s.tolist(), 5),
                "idx_ret_20": _pct_change(close_s.tolist(), 20),
                "idx_close_ma20_ratio": _ratio_to_ma(close_s, 20).to_numpy(),
                "idx_vol20": ret.rolling(20, min_periods=20).std(ddof=0).to_numpy(),
            }
        )
        frames.append(idx_frame)

    if breadth_rows:
        brd = pd.DataFrame(
            {
                "trade_date": [str(r.get("trade_date")) for r in breadth_rows],
                "up_ratio": [_float_or_nan(r.get("up_ratio")) for r in breadth_rows],
                "limit_up": [_float_or_nan(r.get("limit_up")) for r in breadth_rows],
                "total_amount": [_float_or_nan(r.get("total_amount")) for r in breadth_rows],
            }
        ).sort_values("trade_date").reset_index(drop=True)
        brd_frame = pd.DataFrame(
            {
                "trade_date": brd["trade_date"],
                "up_ratio": brd["up_ratio"].to_numpy(),
                "up_ratio_ma5": brd["up_ratio"].rolling(5, min_periods=5).mean().to_numpy(),
                "limit_up_count": brd["limit_up"].to_numpy(),
                "total_amount_z": _zscore(brd["total_amount"], 20).to_numpy(),
            }
        )
        frames.append(brd_frame)

    if not frames:
        return pd.DataFrame(columns=["trade_date", *MARKET_COLUMNS])

    merged = frames[0]
    for frame in frames[1:]:
        merged = merged.merge(frame, on="trade_date", how="outer")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    # 补齐可能缺失的列（只有指数或只有宽度时），按 MARKET_COLUMNS 定序
    for column in MARKET_COLUMNS:
        if column not in merged.columns:
            merged[column] = np.nan
    return merged[["trade_date", *MARKET_COLUMNS]]


# ---------------------------------------------------------------------------
# industry 组
# ---------------------------------------------------------------------------
def industry_features(
    board_rows: Optional[Sequence[Mapping[str, Any]]],
    stock_dates: Sequence[str],
    stock_closes: Sequence[float],
) -> pd.DataFrame:
    """行业相对强弱特征：所属东财板块的动量 + 个股相对板块的 5/20 日强弱差。

    ``stock_dates``/``stock_closes`` 为个股因果序列（与 ``rows`` 同序），个股收益在此
    重算后与板块按 ``trade_date`` 对齐相减。空板块行 → 空帧（下游 _has_industry=0）。

    ⚠️ 板块归属是"最新单快照"（C3 局限）：调用方须保证 ``board_rows`` 是按
    ``as_of ≤ trade_date`` 解析出的板块，manifest 记 ``industry_point_in_time=False``。
    """
    if not board_rows or not stock_dates:
        return pd.DataFrame(columns=["trade_date", *INDUSTRY_COLUMNS])

    board = pd.DataFrame(
        {
            "trade_date": [str(r.get("trade_date")) for r in board_rows],
            "close": [_float_or_nan(r.get("close")) for r in board_rows],
        }
    ).sort_values("trade_date").reset_index(drop=True)
    board_close = board["close"]

    board_frame = pd.DataFrame(
        {
            "trade_date": board["trade_date"],
            "board_ret_5": _pct_change(board_close.tolist(), 5),
            "board_ret_20": _pct_change(board_close.tolist(), 20),
            "board_close_ma20_ratio": _ratio_to_ma(board_close, 20).to_numpy(),
        }
    )

    # 个股因果收益（与 price_volume 同口径），按 trade_date 对齐到板块
    stock = pd.DataFrame(
        {
            "trade_date": list(stock_dates),
            "stock_ret_5": _pct_change(stock_closes, 5),
            "stock_ret_20": _pct_change(stock_closes, 20),
        }
    )

    merged = stock.merge(board_frame, on="trade_date", how="left")
    merged["rel_strength_5"] = merged["stock_ret_5"] - merged["board_ret_5"]
    merged["rel_strength_20"] = merged["stock_ret_20"] - merged["board_ret_20"]
    return merged[["trade_date", *INDUSTRY_COLUMNS]]


# ---------------------------------------------------------------------------
# 单标的特征帧（train/serve 唯一入口）
# ---------------------------------------------------------------------------
def symbol_feature_frame(
    symbol: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    groups: Sequence[str] = FEATURE_GROUPS,
    spec: Optional[ProfileSpec] = None,
    index_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    breadth_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    board_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> pd.DataFrame:
    """构建单标的的完整特征帧（行=各交易日，列=symbol/trade_date/各组特征/标志列）。

    这是 :mod:`ml.dataset`（批量）与 :mod:`ml.scoring`（在线单标的）共用的**唯一**特征
    入口——同 symbol 同 trade_date 在两处产出逐列一致（D5 硬测试钉住）。

    参数
    ----
    rows：``load_daily_bars(symbol)`` 的行序列（升序，含 OHLCV/amount）。
    groups：启用的特征组子集（默认四组全开）。
    spec：signal 组画像；缺省用 :func:`default_signal_spec`（全局默认三件套）。
    index_rows / breadth_rows：market 组输入（``load_index_bars``/``load_market_daily``）。
    board_rows：industry 组输入（个股所属板块的 ``load_industry_board_bars``）。

    缺失组对应列为 NaN、标志列（``_has_market``/``_has_industry``）为 0；空 ``rows``
    返回带正确列名的空帧（绝不抛错，便于优雅降级）。
    """
    unknown = [g for g in groups if g not in FEATURE_GROUPS]
    if unknown:
        raise ValueError(f"未知特征组：{unknown}；合法值 {FEATURE_GROUPS}")

    columns = ["symbol", "trade_date"]
    if not rows:
        empty = pd.DataFrame(columns=columns + list(expected_columns(groups, spec)))
        for flag in FLAG_COLUMNS:
            if (flag == "_has_market" and "market" in groups) or (
                flag == "_has_industry" and "industry" in groups
            ):
                empty[flag] = pd.Series(dtype="float64")
        return empty

    arrays = _arrays(rows)
    n = len(arrays["trade_date"])
    data: Dict[str, Any] = {
        "symbol": [symbol] * n,
        "trade_date": arrays["trade_date"],
    }

    # 列顺序与 expected_columns 一致：signal → price_volume → market → industry
    if "signal" in groups:
        active_spec = spec if spec is not None else default_signal_spec()
        sig = signal_features(rows, active_spec)
        for column in sig.columns:
            data[column] = sig[column].to_numpy()

    if "price_volume" in groups:
        pv = price_volume_features(rows)
        for column in pv.columns:
            data[column] = pv[column].to_numpy()

    frame = pd.DataFrame(data)

    # market 组：跨标的共享时序，按 trade_date 左连接
    if "market" in groups:
        mkt = market_features(index_rows, breadth_rows)
        available = set(mkt["trade_date"]) if not mkt.empty else set()
        frame = frame.merge(mkt, on="trade_date", how="left")
        frame["_has_market"] = frame["trade_date"].isin(available).astype("float64")

    # industry 组：个股所属板块，按 trade_date 左连接（相对强弱需个股 close 序列）
    if "industry" in groups:
        ind = industry_features(board_rows, arrays["trade_date"], arrays["close"])
        available = set(ind["trade_date"]) if not ind.empty else set()
        # 只取 industry 列（trade_date 用于连接），避免与已有列冲突
        frame = frame.merge(ind, on="trade_date", how="left")
        frame["_has_industry"] = frame["trade_date"].isin(available).astype("float64")

    return frame


def expected_columns(
    groups: Sequence[str], spec: Optional[ProfileSpec] = None
) -> List[str]:
    """给定启用组与画像，返回特征列的精确集合（不含 symbol/trade_date/标志列）。

    供"组开关输出列集合精确匹配"测试与 manifest 的 ``feature_columns`` 记录复用。
    """
    columns: List[str] = []
    if "signal" in groups:
        active_spec = spec if spec is not None else default_signal_spec()
        names = [component.name for component in active_spec.components]
        columns += list(SIGNAL_BASE_COLUMNS) + [f"comp_{name}" for name in names]
    if "price_volume" in groups:
        columns += list(PRICE_VOLUME_COLUMNS)
    if "market" in groups:
        columns += list(MARKET_COLUMNS)
    if "industry" in groups:
        columns += list(INDUSTRY_COLUMNS)
    return columns


def assemble_dataset_frames(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """把各标的特征帧纵向拼接为最终数据集帧，行索引 ``(symbol, trade_date)``。

    仅做 concat + 排序 + 设索引（各组左连接与标志列已在 :func:`symbol_feature_frame`
    内完成）。空输入 → 空帧。标签列由 :mod:`ml.dataset` 另行附加，本函数不碰标签。
    """
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return combined.set_index(["symbol", "trade_date"])


# ---------------------------------------------------------------------------
# 开发期前视探针
# ---------------------------------------------------------------------------
def _perturb_tail(
    rows: Sequence[Mapping[str, Any]], n_tail: int
) -> List[Dict[str, Any]]:
    """把最后 ``n_tail`` 行的价格/量额乘以**交替极端因子**（×10 / ×0.1），trade_date 不变。

    用于前视探针：若任一特征在 T 日偷看了 T+1.. 的数据，扰动尾部会改变 T 日之前的行。
    刻意用**逐行不同**的因子而非均匀缩放——ret/rsi/bb_pos 等比率类特征对均匀缩放
    天然不变（×10 在分子分母相消），只有差分扰动才能把"偷看尾部"的因果违规暴露出来。
    """
    perturbed: List[Dict[str, Any]] = []
    cutoff = len(rows) - n_tail
    for index, row in enumerate(rows):
        new_row = dict(row)
        if index >= cutoff:
            even = (index - cutoff) % 2 == 0
            price_factor = 10.0 if even else 0.1
            value_factor = 100.0 if even else 0.01
            for key in ("open", "high", "low", "close", "pre_close"):
                if new_row.get(key) is not None:
                    new_row[key] = _float_or_nan(new_row[key]) * price_factor
            for key in ("vol", "volume", "amount"):
                if new_row.get(key) is not None:
                    new_row[key] = _float_or_nan(new_row[key]) * value_factor
        perturbed.append(new_row)
    return perturbed


def assert_no_lookahead(
    rows: Sequence[Mapping[str, Any]],
    n_tail: int = 10,
    **feature_kwargs: Any,
) -> None:
    """开发期断言：扰动最后 ``n_tail`` 根 bar，断言其之前的所有特征行逐列不变。

    所有特征按构造都是因果的（rolling/evaluate 只看 ≤T）；本探针用极端扰动经验性地
    证明这一点——若某特征在 T 日引入了 T+1.. 信息，扰动尾部会污染 T 日之前的行，
    断言失败。``feature_kwargs`` 透传给 :func:`symbol_feature_frame`（groups/spec/
    index_rows/breadth_rows/board_rows）。
    """
    if n_tail <= 0 or len(rows) <= n_tail:
        return  # 无可校验的前缀
    base = symbol_feature_frame("PROBE", rows, **feature_kwargs)
    after = symbol_feature_frame("PROBE", _perturb_tail(rows, n_tail), **feature_kwargs)

    cutoff = len(rows) - n_tail
    skip = {"symbol", "trade_date", *FLAG_COLUMNS}
    feature_columns = [c for c in base.columns if c not in skip]
    if not feature_columns:
        return
    left = base.iloc[:cutoff][feature_columns].to_numpy(dtype="float64")
    right = after.iloc[:cutoff][feature_columns].to_numpy(dtype="float64")
    if not np.allclose(left, right, rtol=1e-9, atol=1e-9, equal_nan=True):
        # 定位首个不一致的 (行, 列) 便于排查
        bad = ~np.isclose(left, right, rtol=1e-9, atol=1e-9, equal_nan=True)
        row_idx, col_idx = np.where(bad)
        location = (
            f"行 {row_idx[0]}（trade_date={base.iloc[:cutoff].iloc[row_idx[0]]['trade_date']}）"
            f"列 {feature_columns[col_idx[0]]}"
            if len(row_idx) else "未知"
        )
        raise AssertionError(
            f"检测到前视泄漏：扰动尾部 {n_tail} 根 bar 改变了之前行的特征（{location}）"
        )
