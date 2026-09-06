#!/usr/bin/env python3
"""
科华生物 - 超参数优化
扩大搜索范围，测试更多参数组合
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


class FlexibleComboStrategy:
    """灵活组合策略"""
    def __init__(self, ma_fast=5, ma_slow=20, rsi_period=6, 
                 rsi_oversold=20, rsi_overbought=60, bb_period=20, bb_std=2.0,
                 vote_threshold=1, use_bb=False):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)
        self.vote_threshold = vote_threshold
        self.use_bb = use_bb
        self.params = {
            'ma': (ma_fast, ma_slow),
            'rsi': (rsi_period, rsi_oversold, rsi_overbought),
            'bb': (bb_period, bb_std),
            'vote_threshold': vote_threshold,
            'use_bb': use_bb
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
        
        # 收集信号
        signals = [ma_signal, rsi_signal]
        if self.use_bb:
            signals.append(bb_signal)
        
        buy_count = sum(1 for s in signals if s.side == Side.BUY)
        sell_count = sum(1 for s in signals if s.side == Side.SELL)
        
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


def main():
    # 加载最近 1 年数据
    loader = TushareDataLoader(TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    bars = list(loader.load_bars('002022.SZ', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print("="*80)
    print("🚀 科华生物 (002022.SZ) - 超参数优化")
    print("="*80)
    print(f"📊 数据：{len(bars)} 个交易日")
    
    # 扩大参数搜索范围
    ma_configs = [
        (2, 8), (2, 10), (2, 12),
        (3, 8), (3, 10), (3, 12), (3, 15),
        (4, 10), (4, 12), (4, 15), (4, 20),
        (5, 15), (5, 20), (5, 25),
        (6, 15), (6, 20), (6, 25),
        (8, 20), (8, 25), (8, 30),
        (10, 25), (10, 30), (10, 35),
    ]
    
    rsi_configs = [
        (3, 15, 55), (3, 20, 60), (3, 25, 65),
        (4, 15, 55), (4, 20, 60), (4, 25, 65),
        (5, 15, 55), (5, 20, 60), (5, 25, 65),
        (6, 15, 55), (6, 20, 60), (6, 25, 65), (6, 30, 70),
        (7, 20, 60), (7, 25, 65), (7, 30, 70),
        (8, 20, 60), (8, 25, 65), (8, 30, 70),
        (9, 25, 65), (9, 30, 70),
        (10, 25, 65), (10, 30, 70), (10, 35, 75),
    ]
    
    bb_configs = [(20, 2.0), (20, 1.8), (20, 1.5), (26, 2.0), (26, 1.8)]
    vote_thresholds = [1]  # 只测试 1 票
    use_bb_options = [False, True]
    
    print(f"\n📊 参数网格搜索...")
    print(f"   组合数：{len(ma_configs) * len(rsi_configs) * len(bb_configs) * len(vote_thresholds) * len(use_bb_options)}")
    
    best_result = None
    best_config = None
    all_results = []
    count = 0
    
    for ma_fast, ma_slow in ma_configs:
        for rsi_period, rsi_oversold, rsi_overbought in rsi_configs:
            for bb_period, bb_std in bb_configs:
                for vote_threshold in vote_thresholds:
                    for use_bb in use_bb_options:
                        count += 1
                        
                        strategy = FlexibleComboStrategy(
                            ma_fast=ma_fast, ma_slow=ma_slow,
                            rsi_period=rsi_period,
                            rsi_oversold=rsi_oversold,
                            rsi_overbought=rsi_overbought,
                            bb_period=bb_period,
                            bb_std=bb_std,
                            vote_threshold=vote_threshold,
                            use_bb=use_bb
                        )
                        
                        result = run_backtest(strategy, bars)
                        
                        cfg = {
                            'ma': (ma_fast, ma_slow),
                            'rsi': (rsi_period, rsi_oversold, rsi_overbought),
                            'bb': (bb_period, bb_std),
                            'vote_threshold': vote_threshold,
                            'use_bb': use_bb
                        }
                        
                        all_results.append({**cfg, **result})
                        
                        # 按收益/回撤比排序
                        score = result['total_return'] / (result['max_drawdown'] + 0.01)
                        if best_result is None or score > (best_result['total_return'] / (best_result['max_drawdown'] + 0.01)):
                            best_result = result
                            best_config = cfg
        
        if count % 500 == 0:
            print(f"   已测试 {count} 组...")
    
    # 排序
    all_results.sort(key=lambda x: x['total_return'] / (x['max_drawdown'] + 0.01), reverse=True)
    
    # 输出 Top 10
    print(f"\n{'='*80}")
    print("🏆 Top 10 参数组合 (按收益/回撤比)")
    print("="*80)
    print(f"{'排名':<4} {'收益':>8} {'回撤':>8} {'交易':>6} {'胜率':>8} {'MA':<10} {'RSI':<15} {'BB':<10} {'BB 用':>5}")
    print(f"{'-'*80}")
    
    for i, cfg in enumerate(all_results[:10], 1):
        ma_str = f"M{cfg['ma'][0]}/{cfg['ma'][1]}"
        rsi_str = f"P{cfg['rsi'][0]}/{cfg['rsi'][1]}/{cfg['rsi'][2]}"
        bb_str = f"P{cfg['bb'][0]}/{cfg['bb'][1]}"
        use_bb_str = "是" if cfg['use_bb'] else "否"
        
        print(f"{i:<4} {cfg['total_return']:>7.2f}% {cfg['max_drawdown']:>7.2f}% {cfg['total_trades']:>6} {cfg['win_rate']:>7.1f}% {ma_str:<10} {rsi_str:<15} {bb_str:<10} {use_bb_str:>5}")
    
    # 对比当前配置和之前最优
    print(f"\n{'='*80}")
    print("📋 对比：当前 vs 之前最优 vs 新最优")
    print(f"{'='*80}")
    
    # 当前配置回测
    current_strategy = FlexibleComboStrategy(
        ma_fast=10, ma_slow=30,
        rsi_period=8,
        rsi_oversold=25,
        rsi_overbought=75,
        bb_period=20,
        bb_std=2.0,
        vote_threshold=2,
        use_bb=True
    )
    current_result = run_backtest(current_strategy, bars)
    
    print(f"\n当前配置 (MA10/30, RSI8/25/75, BB20/2.0, 票=2):")
    print(f"   收益率：{current_result['total_return']:.2f}%")
    print(f"   最大回撤：{current_result['max_drawdown']:.2f}%")
    print(f"   交易次数：{current_result['total_trades']}")
    
    prev_best = {
        'total_return': 35.81,
        'max_drawdown': 6.42,
        'total_trades': 14,
        'config': 'MA5/20, RSI6/20/60, BB20/2.0, 票=1'
    }
    
    print(f"\n之前最优 ({prev_best['config']}):")
    print(f"   收益率：{prev_best['total_return']:.2f}%")
    print(f"   最大回撤：{prev_best['max_drawdown']:.2f}%")
    print(f"   交易次数：{prev_best['total_trades']}")
    
    bb_info = f", BB{best_config['bb'][0]}/{best_config['bb'][1]}" if best_config['use_bb'] else ""
    print(f"\n新最优 (MA{best_config['ma'][0]}/{best_config['ma'][1]}, RSI{best_config['rsi'][0]}/{best_config['rsi'][1]}/{best_config['rsi'][2]}{bb_info}, 票={best_config['vote_threshold']}):")
    print(f"   收益率：{best_result['total_return']:.2f}%")
    print(f"   最大回撤：{best_result['max_drawdown']:.2f}%")
    print(f"   交易次数：{best_result['total_trades']}")
    
    improvement_vs_current = best_result['total_return'] - current_result['total_return']
    improvement_vs_prev = best_result['total_return'] - prev_best['total_return']
    
    print(f"\n📈 相对当前提升：+{improvement_vs_current:.2f}%")
    print(f"📈 相对之前最优提升：+{improvement_vs_prev:.2f}%")
    
    # 53 万持仓模拟
    print(f"\n{'='*80}")
    print("💰 持仓模拟 (53 万)")
    print(f"{'='*80}")
    position_value = 530000
    current_final = position_value * (1 + current_result['total_return'] / 100)
    prev_final = position_value * (1 + prev_best['total_return'] / 100)
    best_final = position_value * (1 + best_result['total_return'] / 100)
    
    print(f"   当前配置：{current_final:,.0f} 元 (盈亏：{current_final - position_value:,.0f} 元)")
    print(f"   之前最优：{prev_final:,.0f} 元 (盈亏：{prev_final - position_value:,.0f} 元)")
    print(f"   新最优：{best_final:,.0f} 元 (盈亏：{best_final - position_value:,.0f} 元)")
    
    if improvement_vs_prev > 0:
        print(f"\n✅ 发现更优配置！相对之前最优多赚：{best_final - prev_final:,.0f} 元")
    else:
        print(f"\n⚠️ 之前最优配置仍是最佳选择")
    
    # 保存结果
    import json
    output = {
        'symbol': '002022.SZ',
        'period': f'{start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")}',
        'best_config': {
            'ma': list(best_config['ma']),
            'rsi': list(best_config['rsi']),
            'bb': list(best_config['bb']),
            'use_bb': best_config['use_bb'],
            'vote_threshold': best_config['vote_threshold'],
        },
        'best_result': best_result,
        'top_10': all_results[:10],
    }
    
    output_path = Path(__file__).parent / "data" / "backtest" / "002022_SZ_ultra_optimization.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 结果已保存：{output_path}")
    print(f"{'='*80}")
    print("✅ 优化完成")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
