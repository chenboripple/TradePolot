#!/usr/bin/env python3
"""
科华生物 - 最近 1 年策略对比
MACD vs 当前策略
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
import yaml
import numpy as np

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class MACDStrategy:
    """MACD 策略"""
    def __init__(self, fast=12, slow=24, signal=9):
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


class GridComboStrategy:
    """当前科华生物使用的 grid_combo 策略"""
    def __init__(self, ma_fast=10, ma_slow=30, rsi_period=8, 
                 rsi_oversold=25, rsi_overbought=75, bb_period=20, bb_std=2.0):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)
    
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


def run_backtest(strategy, bars: List[Bar], initial_capital: float = 100000, use_history: bool = False) -> Dict:
    """运行回测"""
    capital = initial_capital
    position = 0
    entry_price = 0
    trades = []
    equity_curve = [initial_capital]
    
    for i, bar in enumerate(bars):
        if use_history:
            history = bars[:i+1]
            signal = strategy.on_bar(bar, history)
        else:
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
    print("="*80)
    print("🔬 科华生物 (002022.SZ) - 最近 1 年策略对比")
    print("="*80)
    
    # 加载最近 1 年数据
    loader = TushareDataLoader(TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    bars = list(loader.load_bars('002022.SZ', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print(f"\n📊 数据：{len(bars)} 个交易日")
    print(f"   时间范围：{start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')}")
    print(f"   价格区间：{min(b.close for b in bars):.2f} - {max(b.close for b in bars):.2f}")
    print(f"   当前价：{bars[-1].close:.2f}")
    
    # 当前策略
    current_params = config['strategies']['profiles']['grid_combo_002022']
    print(f"\n⚙️  测试策略:")
    print(f"   1) 当前策略：MA{current_params['ma']['fast']}/{current_params['ma']['slow']} + RSI{current_params['rsi']['period']}/{current_params['rsi']['oversold']}/{current_params['rsi']['overbought']} + BB{current_params['bb']['period']}/{current_params['bb']['std_dev']}")
    print(f"   2) MACD: 12/24/9")
    
    # 回测当前策略
    print(f"\n🏃 回测当前策略...")
    current_strategy = GridComboStrategy(
        ma_fast=current_params['ma']['fast'],
        ma_slow=current_params['ma']['slow'],
        rsi_period=current_params['rsi']['period'],
        rsi_oversold=current_params['rsi']['oversold'],
        rsi_overbought=current_params['rsi']['overbought'],
        bb_period=current_params['bb']['period'],
        bb_std=current_params['bb']['std_dev']
    )
    current_result = run_backtest(current_strategy, bars, use_history=True)
    
    # 回测 MACD
    print(f"🏃 回测 MACD 策略...")
    macd_strategy = MACDStrategy(fast=12, slow=24, signal=9)
    macd_result = run_backtest(macd_strategy, bars)
    
    # 输出结果
    print(f"\n{'='*80}")
    print("📊 回测结果对比")
    print(f"{'='*80}")
    print(f"{'指标':<15} {'当前策略':>15} {'MACD':>15} {'差距':>15}")
    print(f"{'-'*80}")
    print(f"{'总收益率':<15} {current_result['total_return']:>14.2f}% {macd_result['total_return']:>14.2f}% {macd_result['total_return'] - current_result['total_return']:>+14.2f}%")
    print(f"{'最大回撤':<15} {current_result['max_drawdown']:>14.2f}% {macd_result['max_drawdown']:>14.2f}% {macd_result['max_drawdown'] - current_result['max_drawdown']:>+14.2f}%")
    print(f"{'交易次数':<15} {current_result['total_trades']:>15} {macd_result['total_trades']:>15} {macd_result['total_trades'] - current_result['total_trades']:>+15}")
    print(f"{'胜率':<15} {current_result['win_rate']:>14.1f}% {macd_result['win_rate']:>14.1f}% {macd_result['win_rate'] - current_result['win_rate']:>+14.1f}%")
    print(f"{'夏普比率':<15} {current_result['sharpe']:>15.2f} {macd_result['sharpe']:>15.2f} {macd_result['sharpe'] - current_result['sharpe']:>+15.2f}")
    
    # 持仓模拟
    print(f"\n{'='*80}")
    print("💰 持仓模拟 (53 万持仓)")
    print(f"{'='*80}")
    
    position_value = 530000
    current_final = position_value * (1 + current_result['total_return'] / 100)
    macd_final = position_value * (1 + macd_result['total_return'] / 100)
    
    print(f"   当前策略：{current_final:,.0f} 元 (盈亏：{current_final - position_value:,.0f} 元)")
    print(f"   MACD 策略：{macd_final:,.0f} 元 (盈亏：{macd_final - position_value:,.0f} 元)")
    print(f"   差异：{macd_final - current_final:,.0f} 元")
    
    print(f"\n{'='*80}")


if __name__ == "__main__":
    main()
