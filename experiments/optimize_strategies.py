#!/usr/bin/env python3
"""
TradePilot 策略参数优化

功能：
- 网格搜索最优参数组合
- 对比不同参数回测效果
- 生成优化报告
"""

import sys
from pathlib import Path
from datetime import timedelta, datetime
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side
import yaml

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class SimpleBacktest:
    """简化回测引擎（用于参数优化）"""
    
    def __init__(self, ma_fast: int, ma_slow: int, rsi_period: int, 
                 rsi_oversold: float, rsi_overbought: float,
                 bb_period: int, bb_std: float):
        self.ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        self.bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)
    
    def generate_signal(self, bar: Bar, history: List[Bar]) -> str:
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
        
        # 投票机制（优化版：加权投票）
        buy_score = 0
        sell_score = 0
        
        if ma_signal.side == Side.BUY: buy_score += 1
        elif ma_signal.side == Side.SELL: sell_score += 1
        
        if rsi_signal.side == Side.BUY: buy_score += 1
        elif rsi_signal.side == Side.SELL: sell_score += 1
        
        if bb_signal.side == Side.BUY: buy_score += 1
        elif bb_signal.side == Side.SELL: sell_score += 1
        
        # 优化：降低门槛，buy_score >= 1 就买入
        if buy_score >= 1 and sell_score == 0:
            return "BUY"
        elif sell_score >= 1 and buy_score == 0:
            return "SELL"
        else:
            return "HOLD"
    
    def run(self, bars: List[Bar]) -> Dict:
        """运行回测"""
        capital = 100000
        position = 0
        entry_price = 0
        trades = []
        equity_curve = []
        
        for i, bar in enumerate(bars):
            history = bars[:i+1]
            signal = self.generate_signal(bar, history)
            
            if signal == "BUY" and position == 0:
                # 买入
                shares = int(capital * 0.95 / bar.close / 100) * 100
                if shares > 0:
                    cost = shares * bar.close * 1.0003
                    capital -= cost
                    position = shares
                    entry_price = bar.close
            
            elif signal == "SELL" and position > 0:
                # 卖出
                revenue = position * bar.close * 0.9997
                pnl = (bar.close - entry_price) * position
                capital += revenue
                trades.append(pnl)
                position = 0
        
        # 计算最终资产
        if position > 0:
            final_value = capital + position * bars[-1].close
        else:
            final_value = capital
        
        total_return = (final_value - 100000) / 100000 * 100
        
        # 计算最大回撤
        peak = 100000
        max_dd = 0
        test_cap = 100000
        for bar in bars:
            if position > 0:
                test_cap = capital + position * bar.close
            peak = max(peak, test_cap)
            dd = (peak - test_cap) / peak * 100
            max_dd = min(max_dd, -dd)
        
        win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0
        
        return {
            'total_return': total_return,
            'total_trades': len(trades),
            'win_rate': win_rate,
            'max_drawdown': -max_dd,
            'final_capital': final_value,
        }


def optimize_parameters(ts_code: str, name: str, days: int = 365):
    """参数优化"""
    print("="*80)
    print(f"🔍 TradePilot 策略参数优化")
    print(f"   标的：{name} ({ts_code})")
    print(f"   回测周期：{days}天")
    print("="*80)
    
    # 获取数据
    loader = TushareDataLoader(TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    bars = list(loader.load_bars(ts_code, start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print(f"\n📊 数据条数：{len(bars)}")
    
    # 参数网格
    ma_params = [(3, 10), (3, 12), (5, 15), (5, 20), (10, 30)]
    rsi_params = [(10, 35, 65), (14, 30, 70), (14, 35, 65)]
    bb_params = [(14, 1.8), (20, 2.0), (20, 1.5)]
    
    results = []
    
    print(f"\n🔬 开始参数网格搜索...")
    print(f"   参数组合数：{len(ma_params) * len(rsi_params) * len(bb_params)}")
    
    for ma_fast, ma_slow in ma_params:
        for rsi_period, rsi_oversold, rsi_overbought in rsi_params:
            for bb_period, bb_std in bb_params:
                backtester = SimpleBacktest(
                    ma_fast=ma_fast, ma_slow=ma_slow,
                    rsi_period=rsi_period, rsi_oversold=rsi_oversold, rsi_overbought=rsi_overbought,
                    bb_period=bb_period, bb_std=bb_std
                )
                
                result = backtester.run(bars)
                
                results.append({
                    'ma': (ma_fast, ma_slow),
                    'rsi': (rsi_period, rsi_oversold, rsi_overbought),
                    'bb': (bb_period, bb_std),
                    **result
                })
    
    # 排序（按收益/回撤比）
    results.sort(key=lambda x: x['total_return'] / (x['max_drawdown'] + 0.01), reverse=True)
    
    # 输出 Top 10
    print(f"\n{'='*80}")
    print("📊 Top 10 参数组合")
    print(f"{'='*80}")
    print(f"{'排名':<4} {'收益率':>8} {'回撤':>8} {'交易':>6} {'胜率':>8} {'MA':<10} {'RSI':<15} {'BB':<10}")
    print(f"{'-'*80}")
    
    for i, r in enumerate(results[:10], 1):
        ma_str = f"M{r['ma'][0]}/{r['ma'][1]}"
        rsi_str = f"P{r['rsi'][0]}/{r['rsi'][1]}/{r['rsi'][2]}"
        bb_str = f"P{r['bb'][0]}/{r['bb'][1]}"
        
        print(f"{i:<4} {r['total_return']:>7.2f}% {r['max_drawdown']:>7.2f}% {r['total_trades']:>6} {r['win_rate']:>7.1f}% {ma_str:<10} {rsi_str:<15} {bb_str:<10}")
    
    # 对比默认参数
    print(f"\n{'='*80}")
    print("📋 默认参数 vs 最优参数")
    print(f"{'='*80}")
    
    default_backtester = SimpleBacktest(ma_fast=5, ma_slow=20, rsi_period=14, 
                                         rsi_oversold=30, rsi_overbought=70, 
                                         bb_period=20, bb_std=2.0)
    default_result = default_backtester.run(bars)
    
    best = results[0]
    
    print(f"\n默认参数 (MA5/20, RSI14/30/70, BB20/2.0):")
    print(f"   收益率：{default_result['total_return']:.2f}%")
    print(f"   最大回撤：{default_result['max_drawdown']:.2f}%")
    print(f"   交易次数：{default_result['total_trades']}")
    print(f"   胜率：{default_result['win_rate']:.1f}%")
    
    print(f"\n最优参数 (MA{best['ma'][0]}/{best['ma'][1]}, RSI{best['rsi'][0]}/{best['rsi'][1]}/{best['rsi'][2]}, BB{best['bb'][0]}/{best['bb'][1]}):")
    print(f"   收益率：{best['total_return']:.2f}%")
    print(f"   最大回撤：{best['max_drawdown']:.2f}%")
    print(f"   交易次数：{best['total_trades']}")
    print(f"   胜率：{best['win_rate']:.1f}%")
    
    improvement = best['total_return'] - default_result['total_return']
    print(f"\n📈 优化提升：收益率 +{improvement:.2f}%")
    
    # 保存结果
    import json
    output_path = Path(__file__).parent / "data" / "backtest" / f"{ts_code.replace('.', '_')}_optimization.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            'symbol': ts_code,
            'name': name,
            'days': days,
            'default_params': default_result,
            'best_params': best,
            'top_10': results[:10],
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 结果已保存：{output_path}")
    print(f"{'='*80}")
    
    return best


if __name__ == "__main__":
    # 优化 002022
    best_002022 = optimize_parameters("002022.SZ", "科华生物", 365)
    
    print("\n\n")
    
    # 优化 600309
    best_600309 = optimize_parameters("600309.SH", "万华化学", 365)
    
    print("\n" + "="*80)
    print("✅ 优化完成")
    print("="*80)
