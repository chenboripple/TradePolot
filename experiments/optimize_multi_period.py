#!/usr/bin/env python3
"""
安凯客车 - 多周期策略优化
寻找 1 个月、3 个月、1 年都能赚钱的稳健策略
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
                 vote_threshold=1):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)
        self.vote_threshold = vote_threshold
        self.params = {
            'ma': (ma_fast, ma_slow),
            'rsi': (rsi_period, rsi_oversold, rsi_overbought),
            'bb': (bb_period, bb_std),
            'vote_threshold': vote_threshold
        }
    
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
    print("🔬 安凯客车 (000868.SZ) - 多周期策略优化")
    print("="*80)
    
    # 加载不同周期数据
    end_date = datetime.now()
    
    bars_1m = list(loader.load_bars('000868.SZ', (end_date - timedelta(days=30)).strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    bars_3m = list(loader.load_bars('000868.SZ', (end_date - timedelta(days=90)).strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    bars_1y = list(loader.load_bars('000868.SZ', (end_date - timedelta(days=365)).strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print(f"\n📊 数据:")
    print(f"   1 个月：{len(bars_1m)} 个交易日")
    print(f"   3 个月：{len(bars_3m)} 个交易日")
    print(f"   1 年：{len(bars_1y)} 个交易日")
    
    # 参数网格（精简版）
    ma_configs = [(5, 20), (6, 20), (8, 25), (10, 30)]
    rsi_configs = [
        (6, 20, 60), (6, 25, 65), (6, 30, 70),
        (8, 25, 65), (8, 30, 70),
        (10, 30, 70), (14, 30, 70),
    ]
    bb_configs = [(20, 2.0), (20, 1.8), (26, 2.0)]
    vote_thresholds = [1]  # 只测试票=1
    
    print(f"\n🔬 参数网格搜索...")
    print(f"   组合数：{len(ma_configs) * len(rsi_configs) * len(bb_configs) * len(vote_thresholds)}")
    
    best_configs = []
    
    for vote_threshold in vote_thresholds:
        for ma_fast, ma_slow in ma_configs:
            for rsi_period, rsi_oversold, rsi_overbought in rsi_configs:
                for bb_period, bb_std in bb_configs:
                    strategy = GridComboStrategy(
                        ma_fast=ma_fast, ma_slow=ma_slow,
                        rsi_period=rsi_period,
                        rsi_oversold=rsi_oversold,
                        rsi_overbought=rsi_overbought,
                        bb_period=bb_period,
                        bb_std=bb_std,
                        vote_threshold=vote_threshold
                    )
                    
                    # 测试 3 个周期
                    if len(bars_1m) >= 30:
                        result_1m = run_backtest(strategy, bars_1m)
                    else:
                        result_1m = {'total_return': 0, 'max_drawdown': 0}
                    
                    if len(bars_3m) >= 30:
                        result_3m = run_backtest(strategy, bars_3m)
                    else:
                        result_3m = {'total_return': 0, 'max_drawdown': 0}
                    
                    result_1y = run_backtest(strategy, bars_1y)
                    
                    # 综合评分：要求 3 个周期都赚钱，优先 1 年收益
                    if result_1m['total_return'] >= 0 and result_3m['total_return'] >= 0 and result_1y['total_return'] >= 0:
                        score = result_1y['total_return'] * 0.5 + result_3m['total_return'] * 0.3 + result_1m['total_return'] * 0.2
                        best_configs.append({
                            'params': {
                                'ma': (ma_fast, ma_slow),
                                'rsi': (rsi_period, rsi_oversold, rsi_overbought),
                                'bb': (bb_period, bb_std),
                                'vote_threshold': vote_threshold
                            },
                            'result_1m': result_1m,
                            'result_3m': result_3m,
                            'result_1y': result_1y,
                            'score': score
                        })
    
    # 按综合评分排序
    best_configs.sort(key=lambda x: x['score'], reverse=True)
    
    print(f"\n{'='*80}")
    print(f"🏆 多周期稳健策略 Top 10")
    print(f"{'='*80}")
    print(f"{'排名':<4} {'1 月收益':>10} {'3 月收益':>10} {'1 年收益':>10} {'综合评分':>10} {'MA':<10} {'RSI':<15} {'票':>4}")
    print(f"{'-'*80}")
    
    for i, cfg in enumerate(best_configs[:10], 1):
        ma_str = f"M{cfg['params']['ma'][0]}/{cfg['params']['ma'][1]}"
        rsi_str = f"P{cfg['params']['rsi'][0]}/{cfg['params']['rsi'][1]}/{cfg['params']['rsi'][2]}"
        print(f"{i:<4} {cfg['result_1m']['total_return']:>9.2f}% {cfg['result_3m']['total_return']:>9.2f}% {cfg['result_1y']['total_return']:>9.2f}% {cfg['score']:>9.2f} {ma_str:<10} {rsi_str:<15} {cfg['params']['vote_threshold']:>4}")
    
    # 对比当前配置
    print(f"\n{'='*80}")
    print("📋 对比：当前配置 vs 最优配置")
    print(f"{'='*80}")
    
    current_params = config['strategies']['profiles']['grid_combo_000868']
    current_strategy = GridComboStrategy(
        ma_fast=current_params['ma']['fast'],
        ma_slow=current_params['ma']['slow'],
        rsi_period=current_params['rsi']['period'],
        rsi_oversold=current_params['rsi']['oversold'],
        rsi_overbought=current_params['rsi']['overbought'],
        bb_period=current_params['bb']['period'],
        bb_std=current_params['bb']['std_dev'],
        vote_threshold=2
    )
    
    if len(bars_1m) >= 30:
        current_1m = run_backtest(current_strategy, bars_1m)
    else:
        current_1m = {'total_return': 0}
    if len(bars_3m) >= 30:
        current_3m = run_backtest(current_strategy, bars_3m)
    else:
        current_3m = {'total_return': 0}
    current_1y = run_backtest(current_strategy, bars_1y)
    
    print(f"\n当前配置 (MA{current_params['ma']['fast']}/{current_params['ma']['slow']}, RSI{current_params['rsi']['period']}/{current_params['rsi']['oversold']}/{current_params['rsi']['overbought']}, 票=2):")
    print(f"   1 个月：{current_1m['total_return']:.2f}%")
    print(f"   3 个月：{current_3m['total_return']:.2f}%")
    print(f"   1 年：{current_1y['total_return']:.2f}%")
    
    if best_configs:
        best = best_configs[0]
        print(f"\n最优配置 (MA{best['params']['ma'][0]}/{best['params']['ma'][1]}, RSI{best['params']['rsi'][0]}/{best['params']['rsi'][1]}/{best['params']['rsi'][2]}, BB{best['params']['bb'][0]}/{best['params']['bb'][1]}, 票={best['params']['vote_threshold']}):")
        print(f"   1 个月：{best['result_1m']['total_return']:.2f}%")
        print(f"   3 个月：{best['result_3m']['total_return']:.2f}%")
        print(f"   1 年：{best['result_1y']['total_return']:.2f}%")
    
    # 持仓模拟
    print(f"\n{'='*80}")
    print("💰 持仓模拟 (6000 股，成本 4.60 元)")
    print(f"{'='*80}")
    position_value = 6000 * 4.60
    
    if best_configs:
        best = best_configs[0]
        current_final = position_value * (1 + current_1y['total_return'] / 100)
        best_final = position_value * (1 + best['result_1y']['total_return'] / 100)
        
        print(f"   当前配置：{current_final:,.0f} 元 (盈亏：{current_final - position_value:,.0f} 元)")
        print(f"   最优配置：{best_final:,.0f} 元 (盈亏：{best_final - position_value:,.0f} 元)")
        print(f"   差异：{best_final - current_final:,.0f} 元")
    
    print(f"\n{'='*80}")
    print("✅ 优化完成")
    print(f"{'='*80}")
    
    return best_configs


if __name__ == "__main__":
    main()
