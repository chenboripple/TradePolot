#!/usr/bin/env python3
"""
安凯客车 (000868.SZ) - 新配置 1 年回测
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict
import yaml
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class GridComboStrategy:
    """网格组合策略"""
    def __init__(self, ma_fast=5, ma_slow=20, rsi_period=6, 
                 rsi_oversold=25, rsi_overbought=65, bb_period=20, bb_std=1.8,
                 vote_threshold=2):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)
        self.vote_threshold = vote_threshold
    
    def on_bar(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < 30:
            return "HOLD"
        
        self.ma_strategy.reset()
        self.rsi_strategy.reset()
        self.bb_strategy.reset()
        
        for prev_bar in history[:-1]:
            self.ma_strategy.on_bar(prev_bar)
            self.rsi_strategy.on_bar(prev_bar)
            self.bb_strategy.on_bar(prev_bar)
        
        ma_signal = self.ma_strategy.on_bar(bar)
        rsi_signal = self.rsi_strategy.on_bar(bar)
        bb_signal = self.bb_strategy.on_bar(bar)
        
        buy_count = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.BUY)
        sell_count = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.SELL)
        
        if buy_count >= self.vote_threshold:
            return "BUY"
        elif sell_count >= self.vote_threshold:
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
    
    return {
        'total_return': total_return,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'max_drawdown': max_dd,
        'final_capital': final_value,
        'sharpe': sharpe,
    }


def main():
    loader = TushareDataLoader(TOKEN)
    
    print("="*80)
    print("🔬 安凯客车 (000868.SZ) - 新配置 1 年回测")
    print("="*80)
    
    # 加载 1 年数据
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    bars = list(loader.load_bars('000868.SZ', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print(f"📊 数据：{len(bars)} 个交易日 ({start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')})")
    print(f"   价格区间：{min(b.close for b in bars):.2f} - {max(b.close for b in bars):.2f}")
    print(f"   当前价：{bars[-1].close:.2f}")
    
    # 新配置参数
    new_params = config['strategies']['profiles']['grid_combo_000868']
    print(f"\n⚙️  新配置参数:")
    print(f"   MA: {new_params['ma']['fast']}/{new_params['ma']['slow']}")
    print(f"   RSI: {new_params['rsi']['period']}/{new_params['rsi']['oversold']}/{new_params['rsi']['overbought']}")
    print(f"   BB: {new_params['bb']['period']}/{new_params['bb']['std_dev']}")
    print(f"   投票门槛：2")
    
    # 创建策略
    strategy = GridComboStrategy(
        ma_fast=new_params['ma']['fast'],
        ma_slow=new_params['ma']['slow'],
        rsi_period=new_params['rsi']['period'],
        rsi_oversold=new_params['rsi']['oversold'],
        rsi_overbought=new_params['rsi']['overbought'],
        bb_period=new_params['bb']['period'],
        bb_std=new_params['bb']['std_dev'],
        vote_threshold=2
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
    
    # 6000 股持仓模拟
    print(f"\n{'='*80}")
    print("💰 持仓模拟 (6000 股，成本 4.60 元)")
    print(f"{'='*80}")
    position_value = 6000 * 4.60
    final_value = position_value * (1 + result['total_return'] / 100)
    
    print(f"   最终价值：{final_value:,.0f} 元")
    print(f"   盈亏：{final_value - position_value:,.0f} 元")
    
    # 对比
    print(f"\n{'='*80}")
    print("📋 对比：新配置 vs 原配置 vs 最优配置")
    print(f"{'='*80}")
    print(f"   新配置 (MA5/20, RSI6/25/65, BB20/1.8, 票=2): {result['total_return']:.2f}%")
    print(f"   原配置 (MA10/30, RSI14/30/70, BB26/2.0, 票=2): -17.50%")
    print(f"   最优配置 (MA10/30, RSI14/30/70, BB26/2.0, 票=1): +28.46%")
    
    print(f"\n{'='*80}")
    print("✅ 回测完成")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
