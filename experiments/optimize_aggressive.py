#!/usr/bin/env python3
"""
科华生物 - 激进策略优化
测试更敏感的参数组合，追求更高收益
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side
import numpy as np
import yaml

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class AggressiveGridStrategy:
    """激进网格策略 - 单指标主导 + 快速反应"""
    def __init__(self, rsi_period=4, rsi_oversold=15, rsi_overbought=55, 
                 use_ma_filter=False, ma_fast=3, ma_slow=10):
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.use_ma_filter = use_ma_filter
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow) if use_ma_filter else None
        self.rsi_params = (rsi_period, rsi_oversold, rsi_overbought)
    
    def on_bar(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < 30:
            return "HOLD"
        
        self.rsi_strategy.reset()
        for prev_bar in history[:-1]:
            self.rsi_strategy.on_bar(prev_bar)
        
        rsi_signal = self.rsi_strategy.on_bar(bar)
        
        # RSI 超卖直接买入
        if rsi_signal.side == Side.BUY:
            # 可选：MA 过滤（只在 MA 金叉时买入）
            if self.use_ma_filter and self.ma_strategy:
                self.ma_strategy.reset()
                for prev_bar in history[:-1]:
                    self.ma_strategy.on_bar(prev_bar)
                ma_signal = self.ma_strategy.on_bar(bar)
                if ma_signal.side == Side.BUY:
                    return "BUY"
                return "HOLD"
            return "BUY"
        
        # RSI 超买直接卖出
        if rsi_signal.side == Side.SELL:
            return "SELL"
        
        return "HOLD"
    
    def reset(self):
        self.rsi_strategy.reset()
        if self.ma_strategy:
            self.ma_strategy.reset()


class PureRSIStrategy:
    """纯 RSI 策略 - 无投票，无过滤"""
    def __init__(self, rsi_period=3, rsi_oversold=10, rsi_overbought=50):
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.params = (rsi_period, rsi_oversold, rsi_overbought)
    
    def on_bar(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < 20:
            return "HOLD"
        
        self.rsi_strategy.reset()
        for prev_bar in history[:-1]:
            self.rsi_strategy.on_bar(prev_bar)
        
        signal = self.rsi_strategy.on_bar(bar)
        
        if signal.side == Side.BUY:
            return "BUY"
        elif signal.side == Side.SELL:
            return "SELL"
        return "HOLD"
    
    def reset(self):
        self.rsi_strategy.reset()


class FastMAStrategy:
    """超快均线策略"""
    def __init__(self, ma_fast=2, ma_slow=8, rsi_confirm=True, rsi_period=6):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_confirm = rsi_confirm
        self.rsi_strategy = RSI(period=rsi_period, oversold=30, overbought=70) if rsi_confirm else None
        self.params = (ma_fast, ma_slow, rsi_confirm)
    
    def on_bar(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < 20:
            return "HOLD"
        
        self.ma_strategy.reset()
        for prev_bar in history[:-1]:
            self.ma_strategy.on_bar(prev_bar)
        
        ma_signal = self.ma_strategy.on_bar(bar)
        
        if ma_signal.side == Side.BUY:
            if self.rsi_confirm:
                self.rsi_strategy.reset()
                for prev_bar in history[:-1]:
                    self.rsi_strategy.on_bar(prev_bar)
                rsi_signal = self.rsi_strategy.on_bar(bar)
                if rsi_signal.side != Side.SELL:
                    return "BUY"
                return "HOLD"
            return "BUY"
        
        if ma_signal.side == Side.SELL:
            return "SELL"
        
        return "HOLD"
    
    def reset(self):
        self.ma_strategy.reset()
        if self.rsi_strategy:
            self.rsi_strategy.reset()


class BreakoutRSIStrategy:
    """突破 + RSI 组合"""
    def __init__(self, breakout_window=3, rsi_period=5, rsi_oversold=20, rsi_overbought=60):
        self.window = breakout_window
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.params = (breakout_window, rsi_period, rsi_oversold, rsi_overbought)
    
    def on_bar(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < 30:
            return "HOLD"
        
        # RSI 信号
        self.rsi_strategy.reset()
        for prev_bar in history[:-1]:
            self.rsi_strategy.on_bar(prev_bar)
        rsi_signal = self.rsi_strategy.on_bar(bar)
        
        # 价格突破
        recent_high = max(h.close for h in history[-self.window:-1])
        recent_low = min(h.close for h in history[-self.window:-1])
        
        # 突破 + RSI 确认
        if bar.close > recent_high and rsi_signal.side == Side.BUY:
            return "BUY"
        if bar.close < recent_low and rsi_signal.side == Side.SELL:
            return "SELL"
        
        return "HOLD"
    
    def reset(self):
        self.rsi_strategy.reset()


def run_backtest(strategy, bars: List[Bar], initial_capital: float = 100000) -> Dict:
    """运行回测"""
    capital = initial_capital
    position = 0
    entry_price = 0
    trades = []
    equity_curve = [initial_capital]
    
    for i, bar in enumerate(bars):
        history = bars[:i+1]
        signal = strategy.on_bar(bar, history)
        
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
    
    final_value = capital + position * bars[-1].close if position > 0 else capital
    total_return = (final_value - initial_capital) / initial_capital * 100
    
    peak = initial_capital
    max_dd = 0
    for value in equity_curve:
        peak = max(peak, value)
        dd = (peak - value) / peak * 100
        max_dd = max(max_dd, dd)
    
    win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0
    
    if len(equity_curve) > 1:
        returns = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1] for i in range(1, len(equity_curve))]
        avg_return = np.mean(returns)
        std_return = np.std(returns)
        sharpe = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0
    else:
        sharpe = 0
    
    # 计算卡玛比率 (收益/回撤)
    calmar = total_return / (max_dd + 0.01) if max_dd >= 0 else 0
    
    return {
        'total_return': total_return,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'max_drawdown': max_dd,
        'final_capital': final_value,
        'sharpe': sharpe,
        'calmar': calmar,
    }


def optimize_pure_rsi(bars: List[Bar]) -> Dict:
    """优化纯 RSI 策略"""
    print("\n🔴 纯 RSI 策略优化...")
    best_result = None
    best_params = None
    
    for period in [2, 3, 4, 5, 6]:
        for oversold in [10, 15, 20, 25]:
            for overbought in [45, 50, 55, 60, 65]:
                if oversold >= overbought:
                    continue
                
                strategy = PureRSIStrategy(rsi_period=period, rsi_oversold=oversold, rsi_overbought=overbought)
                result = run_backtest(strategy, bars)
                
                if best_result is None or result['total_return'] > best_result['total_return']:
                    best_result = result
                    best_params = {'period': period, 'oversold': oversold, 'overbought': overbought}
    
    print(f"   最优：RSI({best_params['period']}, {best_params['oversold']}, {best_params['overbought']})")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%, 交易：{best_result['total_trades']}")
    return {'type': 'pure_rsi', 'params': best_params, 'result': best_result}


def optimize_aggressive_grid(bars: List[Bar]) -> Dict:
    """优化激进网格策略"""
    print("\n🟠 激进网格策略优化...")
    best_result = None
    best_params = None
    
    for period in [3, 4, 5, 6]:
        for oversold in [15, 20, 25]:
            for overbought in [50, 55, 60, 65]:
                if oversold >= overbought:
                    continue
                
                for use_ma in [False, True]:
                    for ma_f, ma_s in [(3, 8), (3, 10), (5, 15)]:
                        strategy = AggressiveGridStrategy(
                            rsi_period=period, rsi_oversold=oversold, rsi_overbought=overbought,
                            use_ma_filter=use_ma, ma_fast=ma_f, ma_slow=ma_s
                        )
                        result = run_backtest(strategy, bars)
                        
                        if best_result is None or result['total_return'] > best_result['total_return']:
                            best_result = result
                            best_params = {
                                'period': period, 'oversold': oversold, 'overbought': overbought,
                                'use_ma': use_ma, 'ma_fast': ma_f, 'ma_slow': ma_s
                            }
    
    ma_info = f" + MA{best_params['ma_fast']}/{best_params['ma_slow']}" if best_params['use_ma'] else ""
    print(f"   最优：RSI({best_params['period']}, {best_params['oversold']}, {best_params['overbought']}){ma_info}")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%, 交易：{best_result['total_trades']}")
    return {'type': 'aggressive_grid', 'params': best_params, 'result': best_result}


def optimize_fast_ma(bars: List[Bar]) -> Dict:
    """优化超快均线策略"""
    print("\n🔵 超快均线策略优化...")
    best_result = None
    best_params = None
    
    for ma_f in [2, 3, 4, 5]:
        for ma_s in [6, 8, 10, 12]:
            if ma_f >= ma_s:
                continue
            
            for rsi_conf in [False, True]:
                strategy = FastMAStrategy(ma_fast=ma_f, ma_slow=ma_s, rsi_confirm=rsi_conf)
                result = run_backtest(strategy, bars)
                
                if best_result is None or result['total_return'] > best_result['total_return']:
                    best_result = result
                    best_params = {'ma_fast': ma_f, 'ma_slow': ma_s, 'rsi_confirm': rsi_conf}
    
    rsi_info = " + RSI 确认" if best_params['rsi_confirm'] else ""
    print(f"   最优：MA{best_params['ma_fast']}/{best_params['ma_slow']}{rsi_info}")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%, 交易：{best_result['total_trades']}")
    return {'type': 'fast_ma', 'params': best_params, 'result': best_result}


def optimize_breakout_rsi(bars: List[Bar]) -> Dict:
    """优化突破+RSI 策略"""
    print("\n🟢 突破+RSI 策略优化...")
    best_result = None
    best_params = None
    
    for window in [2, 3, 5]:
        for period in [4, 5, 6, 7]:
            for oversold in [20, 25, 30]:
                for overbought in [55, 60, 65]:
                    if oversold >= overbought:
                        continue
                    
                    strategy = BreakoutRSIStrategy(
                        breakout_window=window, rsi_period=period,
                        rsi_oversold=oversold, rsi_overbought=overbought
                    )
                    result = run_backtest(strategy, bars)
                    
                    if best_result is None or result['total_return'] > best_result['total_return']:
                        best_result = result
                        best_params = {
                            'window': window, 'period': period,
                            'oversold': oversold, 'overbought': overbought
                        }
    
    print(f"   最优：突破{best_params['window']}日 + RSI({best_params['period']}, {best_params['oversold']}, {best_params['overbought']})")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%, 交易：{best_result['total_trades']}")
    return {'type': 'breakout_rsi', 'params': best_params, 'result': best_result}


def main():
    # 加载最近 1 年数据
    loader = TushareDataLoader(TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    bars = list(loader.load_bars('002022.SZ', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print("="*80)
    print("🚀 科华生物 (002022.SZ) - 激进策略优化")
    print("="*80)
    print(f"📊 数据：{len(bars)} 个交易日 ({start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')})")
    print(f"   价格区间：{min(b.close for b in bars):.2f} - {max(b.close for b in bars):.2f}")
    
    # 优化各策略
    results = []
    results.append(optimize_pure_rsi(bars))
    results.append(optimize_aggressive_grid(bars))
    results.append(optimize_fast_ma(bars))
    results.append(optimize_breakout_rsi(bars))
    
    # 按收益排序
    results.sort(key=lambda x: x['result']['total_return'], reverse=True)
    
    # 输出排名
    print("\n" + "="*80)
    print("🏆 激进策略收益排名")
    print("="*80)
    print(f"{'排名':<4} {'策略类型':<15} {'收益率':>10} {'回撤':>10} {'交易':>6} {'胜率':>8} {'夏普':>8} {'卡玛':>8}")
    print("-"*80)
    
    for i, r in enumerate(results, 1):
        res = r['result']
        print(f"{i:<4} {r['type']:<15} {res['total_return']:>9.2f}% {res['max_drawdown']:>9.2f}% {res['total_trades']:>6} {res['win_rate']:>7.1f}% {res['sharpe']:>7.2f} {res['calmar']:>7.2f}")
    
    # 对比之前最优配置
    print(f"\n{'='*80}")
    print("📋 对比：之前最优 vs 激进最优")
    print(f"{'='*80}")
    
    prev_best = {
        'total_return': 35.81,
        'max_drawdown': 6.42,
        'total_trades': 14,
        'win_rate': 64.3,
        'config': 'MA5/20 + RSI6/20/60 + BB20/2.0'
    }
    
    aggressive_best = results[0]
    
    print(f"\n之前最优 ({prev_best['config']}):")
    print(f"   收益率：{prev_best['total_return']:.2f}%")
    print(f"   最大回撤：{prev_best['max_drawdown']:.2f}%")
    print(f"   交易次数：{prev_best['total_trades']}")
    
    print(f"\n激进最优 ({results[0]['type']}, 参数：{results[0]['params']}):")
    print(f"   收益率：{aggressive_best['result']['total_return']:.2f}%")
    print(f"   最大回撤：{aggressive_best['result']['max_drawdown']:.2f}%")
    print(f"   交易次数：{aggressive_best['result']['total_trades']}")
    
    improvement = aggressive_best['result']['total_return'] - prev_best['total_return']
    print(f"\n📈 收益提升：+{improvement:.2f}%")
    
    # 53 万持仓模拟
    print(f"\n{'='*80}")
    print("💰 持仓模拟 (53 万)")
    print(f"{'='*80}")
    position_value = 530000
    prev_final = position_value * (1 + prev_best['total_return'] / 100)
    aggressive_final = position_value * (1 + aggressive_best['result']['total_return'] / 100)
    
    print(f"   之前最优：{prev_final:,.0f} 元 (盈亏：{prev_final - position_value:,.0f} 元)")
    print(f"   激进最优：{aggressive_final:,.0f} 元 (盈亏：{aggressive_final - position_value:,.0f} 元)")
    print(f"   差异：{aggressive_final - prev_final:,.0f} 元")
    
    print(f"\n{'='*80}")
    print("✅ 优化完成")
    print(f"{'='*80}")


if __name__ == "__main__":
    import yaml
    main()
