#!/usr/bin/env python3
"""
回测科华生物当前 grid_combo 策略 (5 年数据)
当前配置：MA(10,30) + RSI(8,25,75) + BB(20,2.0)
"""

import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side
import yaml
import numpy as np

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class GridComboStrategy:
    """当前科华生物使用的 grid_combo 策略"""
    def __init__(self, ma_fast=10, ma_slow=30, rsi_period=8, 
                 rsi_oversold=25, rsi_overbought=75, bb_period=20, bb_std=2.0):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)
        
        # 参数记录
        self.params = {
            'ma_fast': ma_fast, 'ma_slow': ma_slow,
            'rsi_period': rsi_period, 'rsi_oversold': rsi_oversold, 'rsi_overbought': rsi_overbought,
            'bb_period': bb_period, 'bb_std': bb_std
        }
    
    def on_bar(self, bar: Bar, history: List[Bar]) -> str:
        """生成信号：BUY/SELL/HOLD"""
        if len(history) < 30:
            return "HOLD"
        
        # 重置策略
        self.ma_strategy.reset()
        self.rsi_strategy.reset()
        self.bb_strategy.reset()
        
        # 预热
        for prev_bar in history[:-1]:
            self.ma_strategy.on_bar(prev_bar)
            self.rsi_strategy.on_bar(prev_bar)
            self.bb_strategy.on_bar(prev_bar)
        
        # 生成信号
        ma_signal = self.ma_strategy.on_bar(bar)
        rsi_signal = self.rsi_strategy.on_bar(bar)
        bb_signal = self.bb_strategy.on_bar(bar)
        
        # 投票机制（>=2 票才行动）
        buy_count = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.BUY)
        sell_count = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.SELL)
        
        if buy_count >= 2:
            return "BUY"
        elif sell_count >= 2:
            return "SELL"
        else:
            return "HOLD"
    
    def reset(self):
        self.ma_strategy.reset()
        self.rsi_strategy.reset()
        self.bb_strategy.reset()


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
    
    # 夏普比率
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


def main():
    print("="*80)
    print("🔬 科华生物 (002022.SZ) - 当前策略 5 年回测")
    print("="*80)
    
    # 加载 5 年数据
    loader = TushareDataLoader(TOKEN)
    bars = list(loader.load_bars('002022.SZ', '20210101', '20260318'))
    
    print(f"\n📊 数据：{len(bars)} 个交易日")
    print(f"   价格区间：{min(b.close for b in bars):.2f} - {max(b.close for b in bars):.2f}")
    print(f"   当前价：{bars[-1].close:.2f}")
    
    # 当前策略参数
    current_params = config['strategies']['profiles']['grid_combo_002022']
    print(f"\n⚙️  当前策略参数:")
    print(f"   MA: {current_params['ma']['fast']}/{current_params['ma']['slow']}")
    print(f"   RSI: {current_params['rsi']['period']}/{current_params['rsi']['oversold']}/{current_params['rsi']['overbought']}")
    print(f"   BB: {current_params['bb']['period']}/{current_params['bb']['std_dev']}")
    
    # 创建策略
    strategy = GridComboStrategy(
        ma_fast=current_params['ma']['fast'],
        ma_slow=current_params['ma']['slow'],
        rsi_period=current_params['rsi']['period'],
        rsi_oversold=current_params['rsi']['oversold'],
        rsi_overbought=current_params['rsi']['overbought'],
        bb_period=current_params['bb']['period'],
        bb_std=current_params['bb']['std_dev']
    )
    
    print(f"\n🏃 运行回测...")
    result = run_backtest(strategy, bars)
    
    # 输出结果
    print(f"\n{'='*80}")
    print("📊 回测结果")
    print(f"{'='*80}")
    print(f"   总收益率：{result['total_return']:.2f}%")
    print(f"   最大回撤：{result['max_drawdown']:.2f}%")
    print(f"   交易次数：{result['total_trades']}")
    print(f"   胜率：{result['win_rate']:.1f}%")
    print(f"   夏普比率：{result['sharpe']:.2f}")
    print(f"   年化收益：{(1 + result['total_return']/100)**(1/5) - 1:.2%}")
    
    # 对比 MACD 最优策略
    print(f"\n{'='*80}")
    print("📋 对比：当前策略 vs MACD 最优策略")
    print(f"{'='*80}")
    
    macd_result = {
        'total_return': 29.94,
        'max_drawdown': 34.76,
        'total_trades': 39,
        'win_rate': 46.2,
        'sharpe': 0.33
    }
    
    print(f"\n当前策略 (MA10/30 + RSI8/25/75 + BB20/2.0):")
    print(f"   收益率：{result['total_return']:.2f}%")
    print(f"   最大回撤：{result['max_drawdown']:.2f}%")
    print(f"   交易次数：{result['total_trades']}")
    print(f"   胜率：{result['win_rate']:.1f}%")
    print(f"   夏普：{result['sharpe']:.2f}")
    
    print(f"\nMACD 最优策略 (MACD12/24/9):")
    print(f"   收益率：{macd_result['total_return']:.2f}%")
    print(f"   最大回撤：{macd_result['max_drawdown']:.2f}%")
    print(f"   交易次数：{macd_result['total_trades']}")
    print(f"   胜率：{macd_result['win_rate']:.1f}%")
    print(f"   夏普：{macd_result['sharpe']:.2f}")
    
    improvement = macd_result['total_return'] - result['total_return']
    print(f"\n📈 切换到 MACD 可提升：+{improvement:.2f}% (5 年累计)")
    
    print(f"\n{'='*80}")


if __name__ == "__main__":
    main()
