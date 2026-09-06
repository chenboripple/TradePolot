#!/usr/bin/env python3
"""
科华生物 5 年策略优化
测试多种策略类型，寻找最优收益
"""

import sys
from pathlib import Path
from datetime import timedelta, datetime
from typing import Dict, List
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.models.types import Bar, Side
import yaml
import json

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class GridStrategy:
    """网格策略 - 震荡市神器"""
    def __init__(self, grid_spacing_pct: float, grid_levels: int, position_per_grid: float):
        self.grid_spacing_pct = grid_spacing_pct  # 网格间距百分比
        self.grid_levels = grid_levels  # 网格层数
        self.position_per_grid = position_per_grid  # 每格仓位比例
        self.base_price = None
        self.grids = []
        self.position = 0
        self.cash_ratio = 1.0
        
    def on_bar(self, bar: Bar) -> str:
        if self.base_price is None:
            self.base_price = bar.close
            # 初始化网格
            for i in range(-self.grid_levels, self.grid_levels + 1):
                grid_price = self.base_price * (1 + i * self.grid_spacing_pct / 100)
                self.grids.append({'price': grid_price, 'level': i, 'filled': False})
            return "HOLD"
        
        signal = "HOLD"
        
        # 检查是否触发网格
        for grid in self.grids:
            if not grid['filled']:
                # 买入网格（价格低于网格价）
                if grid['level'] < 0 and bar.close <= grid['price'] and self.cash_ratio >= self.position_per_grid:
                    self.position += 1
                    self.cash_ratio -= self.position_per_grid
                    grid['filled'] = True
                    signal = "BUY"
                    break
                # 卖出网格（价格高于网格价）
                elif grid['level'] > 0 and bar.close >= grid['price'] and self.position > 0:
                    self.position -= 1
                    self.cash_ratio += self.position_per_grid
                    grid['filled'] = True
                    signal = "SELL"
                    break
        
        # 重置网格当价格回到基准
        if abs(bar.close - self.base_price) / self.base_price < self.grid_spacing_pct / 100 / 2:
            for grid in self.grids:
                grid['filled'] = False
        
        return signal
    
    def reset(self):
        self.base_price = None
        self.grids = []
        self.position = 0
        self.cash_ratio = 1.0


class MeanReversionStrategy:
    """均值回归策略 - 低买高卖"""
    def __init__(self, lookback: int, entry_std: float, exit_std: float):
        self.lookback = lookback
        self.entry_std = entry_std  # 入场标准差倍数
        self.exit_std = exit_std    # 出场标准差倍数
        self.prices = []
        self.position = 0
        
    def on_bar(self, bar: Bar) -> str:
        self.prices.append(bar.close)
        if len(self.prices) > self.lookback:
            self.prices.pop(0)
        
        if len(self.prices) < self.lookback:
            return "HOLD"
        
        mean = np.mean(self.prices)
        std = np.std(self.prices)
        if std == 0:
            return "HOLD"
        
        z_score = (bar.close - mean) / std
        
        signal = "HOLD"
        if z_score < -self.entry_std and self.position == 0:
            self.position = 1
            signal = "BUY"
        elif z_score > self.exit_std and self.position > 0:
            self.position = 0
            signal = "SELL"
        
        return signal
    
    def reset(self):
        self.prices = []
        self.position = 0


class BreakoutStrategy:
    """突破策略 - 追涨杀跌"""
    def __init__(self, lookback: int, threshold_pct: float):
        self.lookback = lookback
        self.threshold_pct = threshold_pct
        self.prices = []
        self.position = 0
        self.entry_price = 0
        
    def on_bar(self, bar: Bar) -> str:
        self.prices.append(bar.close)
        if len(self.prices) > self.lookback:
            self.prices.pop(0)
        
        if len(self.prices) < self.lookback:
            return "HOLD"
        
        highest = max(self.prices[:-1])
        lowest = min(self.prices[:-1])
        
        signal = "HOLD"
        # 向上突破
        if bar.close > highest * (1 + self.threshold_pct / 100) and self.position == 0:
            self.position = 1
            self.entry_price = bar.close
            signal = "BUY"
        # 向下跌破
        elif bar.close < lowest * (1 - self.threshold_pct / 100) and self.position > 0:
            self.position = 0
            signal = "SELL"
        
        return signal
    
    def reset(self):
        self.prices = []
        self.position = 0
        self.entry_price = 0


class DualThrustStrategy:
    """Dual Thrust - 经典日内突破"""
    def __init__(self, lookback: int, k1: float, k2: float):
        self.lookback = lookback
        self.k1 = k1  # 多头系数
        self.k2 = k2  # 空头系数
        self.bars = []
        self.position = 0
        
    def on_bar(self, bar: Bar) -> str:
        self.bars.append(bar)
        if len(self.bars) > self.lookback:
            self.bars.pop(0)
        
        if len(self.bars) < self.lookback:
            return "HOLD"
        
        # 计算 N 日最高价、最低价、收盘价
        hh = max(b.high for b in self.bars[:-1])
        ll = min(b.low for b in self.bars[:-1])
        hc = max(b.close for b in self.bars[:-1])
        lc = min(b.close for b in self.bars[:-1])
        
        range_val = max(hh - lc, hc - ll)
        upper = hc + self.k1 * range_val
        lower = lc - self.k2 * range_val
        
        signal = "HOLD"
        if bar.close > upper and self.position == 0:
            self.position = 1
            signal = "BUY"
        elif bar.close < lower and self.position > 0:
            self.position = 0
            signal = "SELL"
        
        return signal
    
    def reset(self):
        self.bars = []
        self.position = 0


class MACDStrategy:
    """MACD 策略"""
    def __init__(self, fast: int, slow: int, signal: int):
        self.fast = fast
        self.slow = slow
        self.signal_period = signal
        self.prices = []
        self.position = 0
        self.prev_macd = None
        self.prev_signal = None
        
    def ema(self, data: List[float], period: int) -> float:
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema
    
    def on_bar(self, bar: Bar) -> str:
        self.prices.append(bar.close)
        if len(self.prices) < self.slow + self.signal_period:
            return "HOLD"
        
        # 计算 MACD
        ema_fast = self.ema(self.prices[-self.slow:], self.fast)
        ema_slow = self.ema(self.prices[-self.slow:], self.slow)
        macd = ema_fast - ema_slow
        
        # 计算 Signal
        macd_history = []
        for i in range(len(self.prices) - self.slow + 1):
            ef = self.ema(self.prices[i:i+self.slow], self.fast)
            es = self.ema(self.prices[i:i+self.slow], self.slow)
            macd_history.append(ef - es)
        
        signal_line = self.ema(macd_history, self.signal_period)
        
        result = "HOLD"
        if self.prev_macd is not None:
            # 金叉买入
            if self.prev_macd <= self.prev_signal and macd > signal_line and self.position == 0:
                self.position = 1
                result = "BUY"
            # 死叉卖出
            elif self.prev_macd >= self.prev_signal and macd < signal_line and self.position > 0:
                self.position = 0
                result = "SELL"
        
        self.prev_macd = macd
        self.prev_signal = signal_line
        
        return result
    
    def reset(self):
        self.prices = []
        self.position = 0
        self.prev_macd = None
        self.prev_signal = None


def run_backtest(strategy, bars: List[Bar], initial_capital: float = 100000) -> Dict:
    """运行回测"""
    capital = initial_capital
    position = 0
    entry_price = 0
    trades = []
    equity_curve = [initial_capital]
    
    for bar in bars:
        signal = strategy.on_bar(bar)
        current_value = capital + position * bar.close if position > 0 else capital
        equity_curve.append(current_value)
        
        if signal == "BUY" and position == 0 and capital > 0:
            shares = int(capital * 0.95 / bar.close / 100) * 100
            if shares > 0:
                cost = shares * bar.close * 1.0003
                capital -= cost
                position = shares
                entry_price = bar.close
        
        elif signal == "SELL" and position > 0:
            revenue = position * bar.close * 0.9997
            pnl = (bar.close - entry_price) * position
            capital += revenue
            trades.append(pnl)
            position = 0
    
    # 最终价值
    final_value = capital + position * bars[-1].close if position > 0 else capital
    total_return = (final_value - initial_capital) / initial_capital * 100
    
    # 最大回撤
    peak = initial_capital
    max_dd = 0
    for value in equity_curve:
        peak = max(peak, value)
        dd = (peak - value) / peak * 100
        max_dd = max(max_dd, dd)
    
    # 胜率
    win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0
    
    # 夏普比率 (简化)
    if len(equity_curve) > 1:
        returns = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1] for i in range(1, len(equity_curve))]
        avg_return = np.mean(returns)
        std_return = np.std(returns)
        sharpe = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0
    else:
        sharpe = 0
    
    return {
        'total_return': total_return,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'max_drawdown': max_dd,
        'final_capital': final_value,
        'sharpe': sharpe,
    }


def optimize_grid(bars: List[Bar]) -> Dict:
    """优化网格策略参数"""
    print("\n🔲 网格策略参数优化...")
    best_result = None
    best_params = None
    
    for spacing in [2, 3, 5, 8, 10]:
        for levels in [3, 5, 7, 10]:
            for pos_per_grid in [0.1, 0.15, 0.2]:
                strategy = GridStrategy(spacing, levels, pos_per_grid)
                result = run_backtest(strategy, bars)
                
                if best_result is None or result['total_return'] > best_result['total_return']:
                    best_result = result
                    best_params = {'spacing': spacing, 'levels': levels, 'pos_per_grid': pos_per_grid}
    
    print(f"   最优：间距{best_params['spacing']}%, {best_params['levels']}层, 每格{best_params['pos_per_grid']*100:.0f}%")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%")
    return {'strategy': 'grid', 'params': best_params, 'result': best_result}


def optimize_mean_reversion(bars: List[Bar]) -> Dict:
    """优化均值回归策略"""
    print("\n📉 均值回归策略优化...")
    best_result = None
    best_params = None
    
    for lookback in [20, 30, 50, 60]:
        for entry_std in [1.5, 2.0, 2.5]:
            for exit_std in [0.5, 1.0, 1.5]:
                strategy = MeanReversionStrategy(lookback, entry_std, exit_std)
                result = run_backtest(strategy, bars)
                
                if best_result is None or result['total_return'] > best_result['total_return']:
                    best_result = result
                    best_params = {'lookback': lookback, 'entry_std': entry_std, 'exit_std': exit_std}
    
    print(f"   最优：N={best_params['lookback']}, 入场{best_params['entry_std']}σ, 出场{best_params['exit_std']}σ")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%")
    return {'strategy': 'mean_reversion', 'params': best_params, 'result': best_result}


def optimize_breakout(bars: List[Bar]) -> Dict:
    """优化突破策略"""
    print("\n📈 突破策略优化...")
    best_result = None
    best_params = None
    
    for lookback in [10, 20, 30, 55]:
        for threshold in [1, 2, 3, 5]:
            strategy = BreakoutStrategy(lookback, threshold)
            result = run_backtest(strategy, bars)
                
            if best_result is None or result['total_return'] > best_result['total_return']:
                best_result = result
                best_params = {'lookback': lookback, 'threshold': threshold}
    
    print(f"   最优：N={best_params['lookback']}, 阈值{best_params['threshold']}%")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%")
    return {'strategy': 'breakout', 'params': best_params, 'result': best_result}


def optimize_dual_thrust(bars: List[Bar]) -> Dict:
    """优化 Dual Thrust 策略"""
    print("\n⚡ Dual Thrust 策略优化...")
    best_result = None
    best_params = None
    
    for lookback in [4, 5, 10, 20]:
        for k1 in [0.3, 0.5, 0.7]:
            for k2 in [0.3, 0.5, 0.7]:
                strategy = DualThrustStrategy(lookback, k1, k2)
                result = run_backtest(strategy, bars)
                
                if best_result is None or result['total_return'] > best_result['total_return']:
                    best_result = result
                    best_params = {'lookback': lookback, 'k1': k1, 'k2': k2}
    
    print(f"   最优：N={best_params['lookback']}, K1={best_params['k1']}, K2={best_params['k2']}")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%")
    return {'strategy': 'dual_thrust', 'params': best_params, 'result': best_result}


def optimize_macd(bars: List[Bar]) -> Dict:
    """优化 MACD 策略"""
    print("\n📊 MACD 策略优化...")
    best_result = None
    best_params = None
    
    for fast in [6, 8, 12]:
        for slow in [24, 26, 30]:
            for signal in [9, 12]:
                strategy = MACDStrategy(fast, slow, signal)
                result = run_backtest(strategy, bars)
                
                if best_result is None or result['total_return'] > best_result['total_return']:
                    best_result = result
                    best_params = {'fast': fast, 'slow': slow, 'signal': signal}
    
    print(f"   最优：MACD({best_params['fast']},{best_params['slow']},{best_params['signal']})")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%")
    return {'strategy': 'macd', 'params': best_params, 'result': best_result}


def main():
    print("="*80)
    print("🔬 科华生物 (002022.SZ) 5 年策略优化")
    print("="*80)
    
    # 加载 5 年数据
    loader = TushareDataLoader(TOKEN)
    bars = list(loader.load_bars('002022.SZ', '20210101', '20260318'))
    
    print(f"\n📊 数据：{len(bars)} 个交易日")
    print(f"   价格区间：{min(b.close for b in bars):.2f} - {max(b.close for b in bars):.2f}")
    print(f"   当前价：{bars[-1].close:.2f}")
    
    # 优化各策略
    results = []
    results.append(optimize_grid(bars))
    results.append(optimize_mean_reversion(bars))
    results.append(optimize_breakout(bars))
    results.append(optimize_dual_thrust(bars))
    results.append(optimize_macd(bars))
    
    # 排序
    results.sort(key=lambda x: x['result']['total_return'], reverse=True)
    
    # 输出排名
    print("\n" + "="*80)
    print("🏆 策略收益排名 (5 年回测)")
    print("="*80)
    print(f"{'排名':<4} {'策略':<15} {'收益率':>10} {'回撤':>10} {'交易':>6} {'胜率':>8} {'夏普':>8}")
    print("-"*80)
    
    for i, r in enumerate(results, 1):
        res = r['result']
        print(f"{i:<4} {r['strategy']:<15} {res['total_return']:>9.2f}% {res['max_drawdown']:>9.2f}% {res['total_trades']:>6} {res['win_rate']:>7.1f}% {res['sharpe']:>7.2f}")
    
    # 保存结果
    output = {
        'symbol': '002022.SZ',
        'name': '科华生物',
        'period': '2021-01-01 to 2026-03-18',
        'trading_days': len(bars),
        'price_range': [min(b.close for b in bars), max(b.close for b in bars)],
        'current_price': bars[-1].close,
        'results': results,
        'best_strategy': results[0],
    }
    
    output_path = Path(__file__).parent / "data" / "backtest" / "002022_SZ_5y_optimization.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 结果已保存：{output_path}")
    print("="*80)
    
    # 输出最优策略详情
    best = results[0]
    print(f"\n🎯 推荐策略：{best['strategy']}")
    print(f"   参数：{best['params']}")
    print(f"   5 年收益：{best['result']['total_return']:.2f}%")
    print(f"   最大回撤：{best['result']['max_drawdown']:.2f}%")
    print(f"   年化收益：{(1 + best['result']['total_return']/100)**(1/5) - 1:.2%}")
    
    return results


if __name__ == "__main__":
    main()
