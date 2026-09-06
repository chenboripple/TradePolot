"""共享合成夹具（确定性、离线、A→D 各阶段测试复用）。

约定：
- 所有序列由带 seed 的随机数生成器产生，同参数必然得到逐位一致的结果，
  测试可以断言精确数值；
- 价格恒为正（几何随机_walk / 正弦叠加），不会触发脏值过滤；
- 交易日只跳过周末，不模拟法定节假日（对信号/回测语义无影响）；
- 本模块不做任何网络与真实 DB 访问；seed_market_db 只写调用方给的临时路径。
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from ripple_tradePilot.models.types import Bar

# 固定起点（周二），保证跨测试运行时间戳确定
DEFAULT_START = datetime(2024, 1, 2)


def trading_days(count: int, start: datetime = DEFAULT_START) -> List[datetime]:
    """生成 count 个工作日时间戳（跳过周六周日）。"""
    days: List[datetime] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def sine_closes(
    count: int,
    mean: float = 10.0,
    amplitude: float = 1.5,
    period: float = 20.0,
) -> List[float]:
    """正弦震荡收盘价：能周期性触发 RSI 超买/超卖与布林带触轨，供投票测试用。"""
    return [mean + amplitude * math.sin(i * 2 * math.pi / period) for i in range(count)]


def linear_closes(count: int, start_price: float = 10.0, step: float = 0.05) -> List[float]:
    """等差上涨收盘价：MA 恒多头、RSI 恒 100（losses=0），供确定性断言。"""
    return [start_price + step * i for i in range(count)]


def bars_from_closes(
    closes: Sequence[float],
    start: datetime = DEFAULT_START,
    spread: float = 0.005,
    volume: float = 100000.0,
    ex_div: Optional[Tuple[int, float]] = None,
) -> List[Bar]:
    """收盘价序列 → Bar 列表（open=前收，high/low 按 spread 展开）。

    ex_div=(index, factor)：第 index 根起 OHLC 整体乘 factor，
    模拟除权导致的价格水平跳变（复权混接测试用）。
    """
    days = trading_days(len(closes), start)
    bars: List[Bar] = []
    factor = 1.0
    previous_close = closes[0]
    for index, (day, close) in enumerate(zip(days, closes)):
        if ex_div is not None and index == ex_div[0]:
            factor = ex_div[1]
        scaled_close = close * factor
        scaled_open = previous_close * factor
        high = max(scaled_open, scaled_close) * (1 + spread)
        low = min(scaled_open, scaled_close) * (1 - spread)
        bars.append(
            Bar(
                timestamp=day,
                open=round(scaled_open, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(scaled_close, 4),
                volume=volume,
            )
        )
        previous_close = close
    return bars


def daily_bars(
    count: int,
    start_price: float = 10.0,
    drift: float = 0.0,
    vol: float = 0.01,
    seed: int = 0,
    start: datetime = DEFAULT_START,
    volume: float = 100000.0,
    ex_div: Optional[Tuple[int, float]] = None,
) -> List[Bar]:
    """几何随机游走日线：close_i = close_{i-1} * exp(drift + vol * z_i)，z ~ N(0,1)。"""
    rng = random.Random(seed)
    closes: List[float] = []
    level = start_price
    for _ in range(count):
        level = level * math.exp(drift + vol * rng.gauss(0.0, 1.0))
        closes.append(level)
    return bars_from_closes(closes, start=start, volume=volume, ex_div=ex_div)


def daily_rows(
    bars: Sequence[Bar],
    source: str = "synth",
) -> List[Dict[str, object]]:
    """Bar 列表 → upsert_daily_bars 行格式（trade_date=YYYYMMDD，含 pre_close/pct_chg）。"""
    rows: List[Dict[str, object]] = []
    previous_close: Optional[float] = None
    for bar in bars:
        pre_close = previous_close if previous_close is not None else bar.open
        change = bar.close - pre_close
        pct_chg = (change / pre_close * 100.0) if pre_close else 0.0
        rows.append(
            {
                "trade_date": bar.timestamp.strftime("%Y%m%d"),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "pre_close": round(pre_close, 4),
                "change": round(change, 4),
                "pct_chg": round(pct_chg, 4),
                "vol": bar.volume,
                "volume": bar.volume,
                "amount": round(bar.close * bar.volume, 2),
            }
        )
        previous_close = bar.close
    return rows


def scale_rows(
    rows: Sequence[Dict[str, object]],
    factor: float,
    start: int = 0,
    end: Optional[int] = None,
    price_keys: Iterable[str] = ("open", "high", "low", "close", "pre_close"),
) -> List[Dict[str, object]]:
    """把行序列 [start:end] 的价格列整体乘 factor（构造"新旧复权基准"两组数据）。"""
    scaled: List[Dict[str, object]] = []
    stop = len(rows) if end is None else end
    for index, row in enumerate(rows):
        new_row = dict(row)
        if start <= index < stop:
            for key in price_keys:
                if new_row.get(key) is not None:
                    new_row[key] = round(float(new_row[key]) * factor, 4)
        scaled.append(new_row)
    return scaled


def snapshot_rows(
    prices: Dict[str, float],
    quote_time: Optional[datetime] = None,
    change_pct: float = 0.0,
) -> List[Dict[str, object]]:
    """全市场实时快照行（stock_quotes 表格式的最小集合）。"""
    stamp = (quote_time or DEFAULT_START).strftime("%Y-%m-%d %H:%M:%S")
    rows: List[Dict[str, object]] = []
    for symbol, price in sorted(prices.items()):
        pre_close = round(price / (1 + change_pct / 100.0), 4) if change_pct else price
        rows.append(
            {
                "symbol": symbol,
                "name": symbol,
                "price": price,
                "change_pct": change_pct,
                "pre_close": pre_close,
                "volume": 1000000.0,
                "amount": round(price * 1000000.0, 2),
                "quote_time": stamp,
            }
        )
    return rows


def index_rows(
    count: int,
    index_code: str = "000300.SH",
    start_value: float = 3000.0,
    drift: float = 0.0002,
    seed: int = 7,
    start: datetime = DEFAULT_START,
) -> List[Dict[str, object]]:
    """指数日线行（index_daily 表格式，C 阶段落库用）。"""
    rng = random.Random(seed)
    days = trading_days(count, start)
    rows: List[Dict[str, object]] = []
    level = start_value
    previous = start_value
    for day in days:
        level = level * math.exp(drift + 0.008 * rng.gauss(0.0, 1.0))
        rows.append(
            {
                "trade_date": day.strftime("%Y%m%d"),
                "index_code": index_code,
                "open": round(previous, 2),
                "high": round(max(previous, level) * 1.002, 2),
                "low": round(min(previous, level) * 0.998, 2),
                "close": round(level, 2),
                "pct_chg": round((level / previous - 1) * 100, 4),
                "volume": 1e8,
                "amount": 1e10,
            }
        )
        previous = level
    return rows


def board_rows(
    count: int,
    board_code: str = "BK0475",
    board_name: str = "生物制品",
    seed: int = 11,
    start: datetime = DEFAULT_START,
) -> List[Dict[str, object]]:
    """东财行业板块日线行（industry_board_bars 表格式，C 阶段落库用）。"""
    rng = random.Random(seed)
    days = trading_days(count, start)
    rows: List[Dict[str, object]] = []
    level = 1000.0
    previous = 1000.0
    for day in days:
        level = level * math.exp(0.0001 + 0.01 * rng.gauss(0.0, 1.0))
        rows.append(
            {
                "board_code": board_code,
                "board_name": board_name,
                "trade_date": day.strftime("%Y%m%d"),
                "open": round(previous, 2),
                "high": round(max(previous, level) * 1.003, 2),
                "low": round(min(previous, level) * 0.997, 2),
                "close": round(level, 2),
                "pct_chg": round((level / previous - 1) * 100, 4),
                "amount": 5e9,
                "turnover_rate": 1.5,
            }
        )
        previous = level
    return rows


def membership_map(
    symbols: Sequence[str],
    board_code: str = "BK0475",
    as_of: str = "20260417",
) -> List[Dict[str, object]]:
    """个股→板块成分映射行（industry_membership 表格式）。"""
    return [
        {"board_code": board_code, "symbol": symbol, "as_of": as_of, "source": "em"}
        for symbol in symbols
    ]


def ml_frame(
    n_rows: int = 800,
    n_features: int = 5,
    seed: int = 0,
    coef: Optional[Sequence[float]] = None,
    intercept: float = -0.3,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """逻辑斯谛真值链路样本：p_true = sigmoid(X·coef + intercept)，y ~ Bernoulli(p_true)。

    返回 (X, y, ret, p_true)：ret = y*0.02 + (1-y)*(-0.025) + 噪声，
    校准/AUC/决策价值测试都有解析真值可对。
    """
    rng = np.random.default_rng(seed)
    coefficients = (
        np.asarray(coef, dtype=float)
        if coef is not None
        else np.linspace(1.2, -0.6, n_features)
    )
    X = rng.normal(size=(n_rows, n_features))
    # einsum 而非 matmul：规避 macOS Accelerate BLAS 在 numpy 2.x 的虚假 FP 警告
    logits = np.einsum("ij,j->i", X, coefficients) + intercept
    p_true = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.random(n_rows) < p_true).astype(int)
    ret = np.where(y == 1, 0.02, -0.025) + rng.normal(scale=0.01, size=n_rows)
    return X, y, ret, p_true


def market_daily_rows(
    count: int, start: datetime = DEFAULT_START
) -> List[Dict[str, object]]:
    """确定性市场宽度行（market_daily 表格式）：随日变化的涨跌家数/涨停/成交额。

    成交额刻意非常数，保证 D 阶段 ``total_amount_z`` 等 z 分数特征非退化（std>0）。
    """
    days = trading_days(count, start)
    rows: List[Dict[str, object]] = []
    for i, day in enumerate(days):
        advancers = 1500 + (i * 37) % 1500
        decliners = 1200 + (i * 53) % 1200
        unchanged = 100 + (i * 11) % 200
        total = advancers + decliners + unchanged
        rows.append(
            {
                "trade_date": day.strftime("%Y%m%d"),
                "advancers": advancers,
                "decliners": decliners,
                "unchanged": unchanged,
                "limit_up": 20 + (i * 7) % 60,
                "limit_down": 5 + (i * 3) % 25,
                "total_amount": 8e11 + (i * 1.7e9) % 4e11,
                "up_ratio": round(advancers / total, 4) if total else None,
            }
        )
    return rows


def seed_market_db(
    db_path: Path,
    symbols: Sequence[str] = ("002022.SZ", "600309.SH", "601816.SH"),
    days: int = 300,
    *,
    with_market: bool = True,
    board_code: str = "BK0475",
    board_name: str = "生物制品",
) -> Path:
    """一体化临时库：多标的确定性日线 + 指数 + 市场宽度 + 行业板块（D 阶段全特征组）。

    所有时序共用 ``trading_days(days, DEFAULT_START)`` 日历，trade_date 逐日对齐，
    保证 D1 特征管道按 trade_date 的 market/industry 左连接命中。``with_market=False``
    只灌个股日线（纯 price_volume/signal 测试用）。
    """
    from ripple_tradePilot.storage.database import (
        init_database,
        record_market_daily,
        upsert_daily_bars,
        upsert_index_daily,
        upsert_industry_board_bars,
        upsert_industry_boards,
        upsert_industry_membership,
    )

    init_database(db_path)
    for offset, symbol in enumerate(symbols):
        bars = daily_bars(days, seed=100 + offset, drift=0.0003 * (offset - 1))
        upsert_daily_bars(
            symbol,
            daily_rows(bars),
            "synth",
            db_path,
            data_version=f"synth|20260417|seed{100 + offset}",
        )

    if not with_market:
        return db_path

    # 指数（沪深300）+ 市场宽度
    upsert_index_daily("000300.SH", index_rows(days), "synth", db_path)
    for row in market_daily_rows(days):
        record_market_daily(str(row["trade_date"]), row, "synth", db_path)

    # 行业板块：单板块覆盖全部标的（成分快照 as_of 取末日，D 阶段标 point_in_time=False）
    upsert_industry_boards(
        [{"board_code": board_code, "board_name": board_name}], "synth", db_path
    )
    upsert_industry_board_bars(
        board_code, board_rows(days, board_code=board_code, board_name=board_name),
        "synth", db_path,
    )
    last_day = trading_days(days)[-1].strftime("%Y%m%d")
    upsert_industry_membership(
        board_code, list(symbols), last_day, "synth", db_path
    )
    return db_path
