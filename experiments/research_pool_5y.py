#!/usr/bin/env python3
"""
TradePilot 监控池 5 年策略调研
目标：对监控池全部股票，用 5 年数据测试多类策略与参数，寻找“相对敏感且稳健”的方案。

评分思路：
- 收益高
- 回撤低
- 交易频率适中（太少不敏感，太多不稳健）
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
import json
import math

import yaml
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side

ROOT = Path(__file__).parent
with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)
TOKEN = CONFIG["tushare"]["token"]


class StrategyBase:
    def signal(self, bar: Bar, history: List[Bar]) -> str:
        return "HOLD"


class GridComboStrategy(StrategyBase):
    def __init__(self, ma_fast, ma_slow, rsi_period, rsi_oversold, rsi_overbought, bb_period, bb_std, vote_threshold=2):
        self.ma = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb = BollingerBands(period=bb_period, std_dev=bb_std)
        self.vote_threshold = vote_threshold
        self.meta = {
            "kind": "grid_combo",
            "ma": [ma_fast, ma_slow],
            "rsi": [rsi_period, rsi_oversold, rsi_overbought],
            "bb": [bb_period, bb_std],
            "vote_threshold": vote_threshold,
        }

    def signal(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < max(30, self.meta["ma"][1], self.meta["bb"][0]):
            return "HOLD"
        self.ma.reset(); self.rsi.reset(); self.bb.reset()
        for prev in history[:-1]:
            self.ma.on_bar(prev)
            self.rsi.on_bar(prev)
            self.bb.on_bar(prev)
        signals = [self.ma.on_bar(bar), self.rsi.on_bar(bar), self.bb.on_bar(bar)]
        buy_count = sum(1 for s in signals if s.side == Side.BUY)
        sell_count = sum(1 for s in signals if s.side == Side.SELL)
        if buy_count >= self.vote_threshold:
            return "BUY"
        if sell_count >= self.vote_threshold:
            return "SELL"
        return "HOLD"


class MACDStrategy(StrategyBase):
    def __init__(self, fast=12, slow=26, signal=9):
        self.fast = fast
        self.slow = slow
        self.signal_p = signal
        self.meta = {"kind": "macd", "fast": fast, "slow": slow, "signal": signal}

    @staticmethod
    def ema(data: List[float], period: int) -> float:
        if len(data) < period:
            return data[-1]
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def signal(self, bar: Bar, history: List[Bar]) -> str:
        closes = [b.close for b in history]
        if len(closes) < self.slow + self.signal_p + 5:
            return "HOLD"
        macd_series = []
        for i in range(self.slow, len(closes) + 1):
            window = closes[:i]
            ef = self.ema(window[-self.slow:], self.fast)
            es = self.ema(window[-self.slow:], self.slow)
            macd_series.append(ef - es)
        if len(macd_series) < self.signal_p + 2:
            return "HOLD"
        prev_macd = macd_series[-2]
        curr_macd = macd_series[-1]
        prev_sig = self.ema(macd_series[:-1], self.signal_p)
        curr_sig = self.ema(macd_series, self.signal_p)
        if prev_macd <= prev_sig and curr_macd > curr_sig:
            return "BUY"
        if prev_macd >= prev_sig and curr_macd < curr_sig:
            return "SELL"
        return "HOLD"


class BreakoutRSIStrategy(StrategyBase):
    def __init__(self, window=20, rsi_period=6, buy_rsi_min=55, sell_rsi_max=45):
        self.window = window
        self.rsi = RSI(period=rsi_period, oversold=30, overbought=70)
        self.buy_rsi_min = buy_rsi_min
        self.sell_rsi_max = sell_rsi_max
        self.meta = {
            "kind": "breakout_rsi",
            "window": window,
            "rsi_period": rsi_period,
            "buy_rsi_min": buy_rsi_min,
            "sell_rsi_max": sell_rsi_max,
        }

    def signal(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < self.window + 5:
            return "HOLD"
        self.rsi.reset()
        for prev in history[:-1]:
            self.rsi.on_bar(prev)
        self.rsi.on_bar(bar)
        latest_rsi = getattr(self.rsi, "_last_rsi", None)
        highs = [b.high for b in history]
        lows = [b.low for b in history]
        highest_prev = max(highs[-(self.window + 1):-1])
        lowest_prev = min(lows[-(self.window + 1):-1])
        if latest_rsi is None:
            return "HOLD"
        if bar.close > highest_prev and latest_rsi >= self.buy_rsi_min:
            return "BUY"
        if bar.close < lowest_prev and latest_rsi <= self.sell_rsi_max:
            return "SELL"
        return "HOLD"


class MeanReversionStrategy(StrategyBase):
    def __init__(self, lookback=20, entry_std=2.0, exit_std=0.5):
        self.lookback = lookback
        self.entry_std = entry_std
        self.exit_std = exit_std
        self.meta = {"kind": "mean_reversion", "lookback": lookback, "entry_std": entry_std, "exit_std": exit_std}

    def signal(self, bar: Bar, history: List[Bar]) -> str:
        closes = [b.close for b in history]
        if len(closes) < self.lookback:
            return "HOLD"
        w = closes[-self.lookback:]
        mean = float(np.mean(w))
        std = float(np.std(w))
        if std == 0:
            return "HOLD"
        z = (bar.close - mean) / std
        if z <= -self.entry_std:
            return "BUY"
        if z >= self.exit_std:
            return "SELL"
        return "HOLD"


def run_backtest(strategy: StrategyBase, bars: List[Bar], initial_capital: float = 100000.0) -> Dict[str, Any]:
    capital = initial_capital
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [initial_capital]

    for i, bar in enumerate(bars):
        history = bars[: i + 1]
        sig = strategy.signal(bar, history)
        current_value = capital + position * bar.close if position > 0 else capital
        equity_curve.append(current_value)

        if sig == "BUY" and position == 0:
            shares = int(capital * 0.95 / bar.close / 100) * 100
            if shares > 0:
                cost = shares * bar.close * 1.0003
                capital -= cost
                position = shares
                entry_price = bar.close
        elif sig == "SELL" and position > 0:
            revenue = position * bar.close * 0.9997
            pnl = (bar.close - entry_price) * position
            capital += revenue
            trades.append(pnl)
            position = 0

    final_value = capital + position * bars[-1].close if position > 0 else capital
    total_return = (final_value - initial_capital) / initial_capital * 100

    peak = initial_capital
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak else 0
        max_dd = max(max_dd, dd)

    win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0.0
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        curr = equity_curve[i]
        if prev:
            returns.append((curr - prev) / prev)
    sharpe = 0.0
    if returns:
        avg = float(np.mean(returns))
        std = float(np.std(returns))
        sharpe = (avg / std * math.sqrt(252)) if std > 0 else 0.0

    return {
        "total_return": total_return,
        "max_drawdown": max_dd,
        "total_trades": len(trades),
        "win_rate": win_rate,
        "sharpe": sharpe,
        "final_capital": final_value,
    }


def candidate_strategies(profile_name: str, profile: Dict[str, Any]) -> List[StrategyBase]:
    cands: List[StrategyBase] = []

    # 当前策略 + 近邻参数
    ma_fast_values = sorted(set([max(3, profile.get("ma", {}).get("fast", 10) - 2), profile.get("ma", {}).get("fast", 10), profile.get("ma", {}).get("fast", 10) + 2]))
    ma_slow_values = sorted(set([max(8, profile.get("ma", {}).get("slow", 30) - 5), profile.get("ma", {}).get("slow", 30), profile.get("ma", {}).get("slow", 30) + 5]))
    rsi_period_values = sorted(set([max(4, profile.get("rsi", {}).get("period", 14) - 2), profile.get("rsi", {}).get("period", 14), profile.get("rsi", {}).get("period", 14) + 2]))
    oversold_values = sorted(set([20, 25, 30, 35]))
    overbought_values = sorted(set([60, 65, 70, 75]))
    bb_period_values = sorted(set([20, profile.get("bb", {}).get("period", 20), 26]))
    bb_std_values = sorted(set([1.8, 2.0]))

    for ma_fast in ma_fast_values:
        for ma_slow in ma_slow_values:
            if ma_fast >= ma_slow:
                continue
            for rsi_period in rsi_period_values:
                for oversold in oversold_values:
                    for overbought in overbought_values:
                        if oversold >= overbought:
                            continue
                        for bb_period in bb_period_values:
                            for bb_std in bb_std_values:
                                for vote in [1, 2]:
                                    cands.append(GridComboStrategy(ma_fast, ma_slow, rsi_period, oversold, overbought, bb_period, bb_std, vote))

    # MACD 族
    for fast, slow, signal in [(8, 21, 6), (10, 24, 8), (12, 26, 9), (6, 19, 5)]:
        cands.append(MACDStrategy(fast, slow, signal))

    # 突破类
    for window, rp, br, sr in [(10, 6, 55, 45), (20, 6, 55, 45), (20, 8, 60, 40), (30, 6, 58, 42)]:
        cands.append(BreakoutRSIStrategy(window, rp, br, sr))

    # 均值回归
    for lookback, entry_std, exit_std in [(20, 2.0, 0.5), (20, 2.5, 0.5), (30, 2.0, 1.0), (30, 2.5, 1.0)]:
        cands.append(MeanReversionStrategy(lookback, entry_std, exit_std))

    return cands


def strategy_score(result: Dict[str, Any]) -> float:
    # 兼顾收益、回撤、敏感度（每年 4~20 次较理想）
    trades = result["total_trades"]
    trade_bonus = 0.0
    if 4 <= trades <= 20:
        trade_bonus = 8.0
    elif 2 <= trades <= 30:
        trade_bonus = 4.0
    elif trades == 0:
        trade_bonus = -12.0
    else:
        trade_bonus = -2.0
    return result["total_return"] - 0.6 * result["max_drawdown"] + 6.0 * result["sharpe"] + trade_bonus


def main():
    loader = TushareDataLoader(TOKEN)
    symbols = CONFIG.get("symbols", [])
    profiles = CONFIG.get("strategies", {}).get("profiles", {})
    start = "20210101"
    end = datetime.now().strftime("%Y%m%d")

    report = []
    print("=" * 100)
    print("TradePilot 监控池 5 年策略调研")
    print("=" * 100)

    for sym in symbols:
        code = sym["code"]
        name = sym.get("name", code)
        profile_name = sym.get("strategy_profile")
        profile = profiles.get(profile_name, {})

        bars = list(loader.load_bars(code, start, end))
        if len(bars) < 200:
            print(f"\n{name}({code}) 数据不足，跳过")
            continue

        print(f"\n{name} ({code}) | 当前画像: {profile_name} | 数据: {len(bars)} 根")
        cands = candidate_strategies(profile_name, profile)
        results = []
        for strat in cands:
            r = run_backtest(strat, bars)
            score = strategy_score(r)
            results.append({"meta": strat.meta, "result": r, "score": score})

        results.sort(key=lambda x: x["score"], reverse=True)
        best = results[0]
        current = None
        # 近似当前策略：找 kind=grid_combo 且参数完全匹配 vote=2
        for item in results:
            m = item["meta"]
            if m.get("kind") != "grid_combo":
                continue
            if m.get("vote_threshold") != 2:
                continue
            pm = profile.get("ma", {})
            pr = profile.get("rsi", {})
            pb = profile.get("bb", {})
            if m.get("ma") == [pm.get("fast"), pm.get("slow")] and m.get("rsi") == [pr.get("period"), pr.get("oversold"), pr.get("overbought")] and m.get("bb") == [pb.get("period"), pb.get("std_dev")]:
                current = item
                break

        if current is None:
            current = {"meta": {"kind": "current_unknown"}, "result": {"total_return": None, "max_drawdown": None, "total_trades": None, "sharpe": None}, "score": None}

        print(f"  当前策略: 收益 {current['result']['total_return']:.2f}% | 回撤 {current['result']['max_drawdown']:.2f}% | 交易 {current['result']['total_trades']} | 夏普 {current['result']['sharpe']:.2f}")
        print(f"  推荐策略: {best['meta']} | 收益 {best['result']['total_return']:.2f}% | 回撤 {best['result']['max_drawdown']:.2f}% | 交易 {best['result']['total_trades']} | 夏普 {best['result']['sharpe']:.2f}")

        report.append({
            "code": code,
            "name": name,
            "profile_name": profile_name,
            "bars": len(bars),
            "current": current,
            "best": best,
            "top5": results[:5],
        })

    out = ROOT / "data" / "backtest" / "monitor_pool_5y_research.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 100)
    print(f"结果已保存: {out}")
    print("=" * 100)


if __name__ == "__main__":
    main()
