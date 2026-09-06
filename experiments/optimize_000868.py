#!/usr/bin/env python3
"""
安凯客车 (000868.SZ) - 策略参数优化
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
    def __init__(self, ma_fast=10, ma_slow=30, rsi_period=14, 
                 rsi_oversold=30, rsi_overbought=70, bb_period=26, bb_std=2.0,
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
    
    calmar = total_return / (max_dd + 0.01)
    
    return {
        'total_return': total_return,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'max_drawdown': max_dd,
        'final_capital': final_value,
        'sharpe': sharpe,
        'calmar': calmar,
    }


def optimize_params(bars: List[Bar], period_label: str) -> Dict:
    """优化策略参数"""
    print(f"\n🔬 {period_label} 参数优化...")
    
    ma_configs = [
        (3, 10), (3, 15), (3, 20),
        (5, 15), (5, 20), (5, 25),
        (6, 15), (6, 20), (6, 25),
        (8, 20), (8, 25), (8, 30),
        (10, 25), (10, 30), (10, 35),
        (12, 30), (12, 35),
    ]
    
    rsi_configs = [
        (4, 15, 55), (4, 20, 60), (4, 25, 65),
        (5, 15, 55), (5, 20, 60), (5, 25, 65),
        (6, 15, 55), (6, 20, 60), (6, 25, 65), (6, 30, 70),
        (7, 20, 60), (7, 25, 65), (7, 30, 70),
        (8, 20, 60), (8, 25, 65), (8, 30, 70),
        (9, 25, 65), (9, 30, 70),
        (10, 25, 65), (10, 30, 70), (10, 35, 75),
        (14, 30, 70), (14, 35, 65),
    ]
    
    bb_configs = [(20, 2.0), (20, 1.8), (26, 2.0), (26, 1.8)]
    vote_thresholds = [1]
    
    print(f"   组合数：{len(ma_configs) * len(rsi_configs) * len(bb_configs)}")
    
    best_result = None
    best_config = None
    all_results = []
    
    for ma_fast, ma_slow in ma_configs:
        for rsi_period, rsi_oversold, rsi_overbought in rsi_configs:
            for bb_period, bb_std in bb_configs:
                for vote_threshold in vote_thresholds:
                    strategy = GridComboStrategy(
                        ma_fast=ma_fast, ma_slow=ma_slow,
                        rsi_period=rsi_period,
                        rsi_oversold=rsi_oversold,
                        rsi_overbought=rsi_overbought,
                        bb_period=bb_period,
                        bb_std=bb_std,
                        vote_threshold=vote_threshold
                    )
                    
                    result = run_backtest(strategy, bars)
                    
                    cfg = {
                        'ma': (ma_fast, ma_slow),
                        'rsi': (rsi_period, rsi_oversold, rsi_overbought),
                        'bb': (bb_period, bb_std),
                        'vote_threshold': vote_threshold
                    }
                    
                    all_results.append({**cfg, **result})
                    
                    score = result['total_return'] / (result['max_drawdown'] + 0.01)
                    if best_result is None or score > (best_result['total_return'] / (best_result['max_drawdown'] + 0.01)):
                        best_result = result
                        best_config = cfg
    
    all_results.sort(key=lambda x: x['total_return'] / (x['max_drawdown'] + 0.01), reverse=True)
    
    print(f"   最优：MA{best_config['ma'][0]}/{best_config['ma'][1]}, RSI{best_config['rsi'][0]}/{best_config['rsi'][1]}/{best_config['rsi'][2]}, BB{best_config['bb'][0]}/{best_config['bb'][1]}")
    print(f"   收益：{best_result['total_return']:.2f}%, 回撤：{best_result['max_drawdown']:.2f}%, 交易：{best_result['total_trades']}")
    
    return {'best_config': best_config, 'best_result': best_result, 'top_10': all_results[:10]}


def main():
    loader = TushareDataLoader(TOKEN)
    
    print("="*80)
    print("🔬 安凯客车 (000868.SZ) - 策略参数优化")
    print("="*80)
    
    # 当前配置
    current_params = config['strategies']['profiles']['grid_combo_000868']
    print(f"\n⚙️  当前配置:")
    print(f"   MA: {current_params['ma']['fast']}/{current_params['ma']['slow']}")
    print(f"   RSI: {current_params['rsi']['period']}/{current_params['rsi']['oversold']}/{current_params['rsi']['overbought']}")
    print(f"   BB: {current_params['bb']['period']}/{current_params['bb']['std_dev']}")
    
    # 加载 1 年数据
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    bars_1y = list(loader.load_bars('000868.SZ', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print(f"\n📊 1 年数据：{len(bars_1y)} 个交易日 ({start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')})")
    print(f"   价格区间：{min(b.close for b in bars_1y):.2f} - {max(b.close for b in bars_1y):.2f}")
    print(f"   当前价：{bars_1y[-1].close:.2f}")
    
    # 1 年优化
    result_1y = optimize_params(bars_1y, "1 年")
    
    # 当前配置回测
    print(f"\n🏃 回测当前配置...")
    current_strategy = GridComboStrategy(
        ma_fast=current_params['ma']['fast'],
        ma_slow=current_params['ma']['slow'],
        rsi_period=current_params['rsi']['period'],
        rsi_oversold=current_params['rsi']['oversold'],
        rsi_overbought=current_params['rsi']['overbought'],
        bb_period=current_params['bb']['period'],
        bb_std=current_params['bb']['std_dev'],
        vote_threshold=2  # 当前配置投票门槛是 2
    )
    current_result = run_backtest(current_strategy, bars_1y)
    
    # 输出对比
    print(f"\n{'='*80}")
    print("📋 当前配置 vs 最优配置")
    print(f"{'='*80}")
    
    print(f"\n当前配置 (MA{current_params['ma']['fast']}/{current_params['ma']['slow']}, RSI{current_params['rsi']['period']}/{current_params['rsi']['oversold']}/{current_params['rsi']['overbought']}, BB{current_params['bb']['period']}/{current_params['bb']['std_dev']}, 票=2):")
    print(f"   收益率：{current_result['total_return']:.2f}%")
    print(f"   最大回撤：{current_result['max_drawdown']:.2f}%")
    print(f"   交易次数：{current_result['total_trades']}")
    print(f"   胜率：{current_result['win_rate']:.1f}%")
    
    print(f"\n最优配置 (MA{result_1y['best_config']['ma'][0]}/{result_1y['best_config']['ma'][1]}, RSI{result_1y['best_config']['rsi'][0]}/{result_1y['best_config']['rsi'][1]}/{result_1y['best_config']['rsi'][2]}, BB{result_1y['best_config']['bb'][0]}/{result_1y['best_config']['bb'][1]}, 票=1):")
    print(f"   收益率：{result_1y['best_result']['total_return']:.2f}%")
    print(f"   最大回撤：{result_1y['best_result']['max_drawdown']:.2f}%")
    print(f"   交易次数：{result_1y['best_result']['total_trades']}")
    print(f"   胜率：{result_1y['best_result']['win_rate']:.1f}%")
    
    improvement = result_1y['best_result']['total_return'] - current_result['total_return']
    print(f"\n📈 优化提升：收益率 +{improvement:.2f}%")
    
    # 安凯持仓模拟 (6000 股，成本 4.60)
    print(f"\n{'='*80}")
    print("💰 持仓模拟 (6000 股，成本 4.60 元)")
    print(f"{'='*80}")
    position_value = 6000 * 4.60  # 2.76 万
    current_final = position_value * (1 + current_result['total_return'] / 100)
    best_final = position_value * (1 + result_1y['best_result']['total_return'] / 100)
    
    print(f"   当前配置：{current_final:,.0f} 元 (盈亏：{current_final - position_value:,.0f} 元)")
    print(f"   最优配置：{best_final:,.0f} 元 (盈亏：{best_final - position_value:,.0f} 元)")
    print(f"   差异：{best_final - current_final:,.0f} 元")
    
    # Top 10
    print(f"\n{'='*80}")
    print("🏆 Top 5 参数组合")
    print(f"{'='*80}")
    print(f"{'排名':<4} {'收益':>8} {'回撤':>8} {'交易':>6} {'胜率':>8} {'MA':<10} {'RSI':<15}")
    print(f"{'-'*80}")
    
    for i, cfg in enumerate(result_1y['top_10'][:5], 1):
        ma_str = f"M{cfg['ma'][0]}/{cfg['ma'][1]}"
        rsi_str = f"P{cfg['rsi'][0]}/{cfg['rsi'][1]}/{cfg['rsi'][2]}"
        print(f"{i:<4} {cfg['total_return']:>7.2f}% {cfg['max_drawdown']:>7.2f}% {cfg['total_trades']:>6} {cfg['win_rate']:>7.1f}% {ma_str:<10} {rsi_str:<15}")
    
    # 保存结果
    import json
    output = {
        'symbol': '000868.SZ',
        'name': '安凯客车',
        'period': f'{start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")}',
        'current_config': {
            'ma': [current_params['ma']['fast'], current_params['ma']['slow']],
            'rsi': [current_params['rsi']['period'], current_params['rsi']['oversold'], current_params['rsi']['overbought']],
            'bb': [current_params['bb']['period'], current_params['bb']['std_dev']],
        },
        'best_config': {
            'ma': list(result_1y['best_config']['ma']),
            'rsi': list(result_1y['best_config']['rsi']),
            'bb': list(result_1y['best_config']['bb']),
            'vote_threshold': result_1y['best_config']['vote_threshold'],
        },
        'best_result': result_1y['best_result'],
        'current_result': current_result,
    }
    
    output_path = Path(__file__).parent / "data" / "backtest" / "000868_SZ_optimization.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 结果已保存：{output_path}")
    print(f"{'='*80}")
    print("✅ 优化完成")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
