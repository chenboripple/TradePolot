#!/usr/bin/env python3
"""
泛海微 (603039.SH) 当前策略 vs 最佳策略对比

当前策略：breakout (Donchian 20 日 + RSI 过滤)
最佳策略：MACD_f12_s20_sig7 (MACD 12/20/7)
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.macd import MACD
from ripple_tradePilot.strategies.donchian import DonchianBreakout
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.models.types import Bar, Side
import yaml
import numpy as np

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']


class CurrentBreakoutStrategy:
    """当前泛海微使用的 breakout 策略"""
    def __init__(self, breakout_window=20, rsi_period=6, buy_rsi_min=55, exit_ma=10, sell_rsi_max=45):
        self.donchian = DonchianBreakout(window=breakout_window)
        self.rsi = RSI(period=rsi_period, oversold=sell_rsi_max, overbought=buy_rsi_min)
        self.exit_ma = exit_ma
        self._ma_closes = []
        self.params = {
            'breakout_window': breakout_window,
            'rsi_period': rsi_period,
            'buy_rsi_min': buy_rsi_min,
            'exit_ma': exit_ma,
            'sell_rsi_max': sell_rsi_max
        }
    
    def on_bar(self, bar: Bar) -> str:
        """生成信号：BUY/SELL/HOLD"""
        # 更新数据
        self._ma_closes.append(bar.close)
        if len(self._ma_closes) > self.exit_ma:
            self._ma_closes.pop(0)
        
        donchian_signal = self.donchian.on_bar(bar)
        rsi_signal = self.rsi.on_bar(bar)
        
        # 获取 RSI 值
        rsi_value = self.rsi.get_current_rsi()
        if rsi_value is None:
            rsi_value = 50
        
        # 买入：突破上轨 + RSI 足够强
        if donchian_signal.side == Side.BUY and rsi_value >= 55:
            return "BUY"
        
        # 卖出：跌破下轨 或 RSI 超买回落
        if donchian_signal.side == Side.SELL or rsi_value <= 45:
            return "SELL"
        
        # 均线退出
        if len(self._ma_closes) >= self.exit_ma:
            ma = sum(self._ma_closes) / len(self._ma_closes)
            if bar.close < ma:
                return "SELL"
        
        return "HOLD"
    
    def reset(self):
        self.donchian.reset()
        self.rsi.reset()
        self._ma_closes = []


class BestMACDStrategy:
    """最佳策略：MACD(12, 20, 7)"""
    def __init__(self):
        self.macd = MACD(fast=12, slow=20, signal=7, zero_cross=False)
        self.params = {
            'fast': 12,
            'slow': 20,
            'signal': 7,
            'zero_cross': False
        }
    
    def on_bar(self, bar: Bar) -> str:
        """生成信号：BUY/SELL/HOLD"""
        signal = self.macd.on_bar(bar)
        
        if signal.side == Side.BUY:
            return "BUY"
        elif signal.side == Side.SELL:
            return "SELL"
        else:
            return "HOLD"
    
    def reset(self):
        self.macd.reset()


def load_bars(symbol: str, days: int) -> List[Bar]:
    """加载指定天数的 K 线数据"""
    loader = TushareDataLoader(token=TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days + 30)
    
    df = loader.get_daily_bars(symbol, start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d'))
    
    if df is None or len(df) == 0:
        return []
    
    bars = []
    for _, row in df.iterrows():
        bars.append(Bar(
            timestamp=datetime.strptime(str(row['trade_date']), '%Y%m%d'),
            open=row['open'],
            high=row['high'],
            low=row['low'],
            close=row['close'],
            volume=row['vol'],
        ))
    
    return bars


def run_backtest(strategy, bars: List[Bar], initial_capital: float = 100000.0) -> Dict:
    """运行回测"""
    capital = initial_capital
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [capital]
    
    commission = 0.0003
    slippage = 0.001
    
    strategy.reset()
    
    for i, bar in enumerate(bars):
        signal = strategy.on_bar(bar)
        
        current_equity = capital + position * bar.close if position > 0 else capital
        equity_curve.append(current_equity)
        
        if signal == "BUY" and position == 0:
            buy_price = bar.close * (1 + slippage)
            shares = int(capital * 0.95 / buy_price / 100) * 100
            if shares > 0:
                cost = shares * buy_price * (1 + commission)
                if cost <= capital:
                    capital -= cost
                    position = shares
                    entry_price = buy_price
        
        elif signal == "SELL" and position > 0:
            sell_price = bar.close * (1 - slippage)
            revenue = position * sell_price * (1 - commission)
            capital += revenue
            
            pnl = (sell_price - entry_price) * position
            trades.append(pnl)
            position = 0
            entry_price = 0.0
    
    if position > 0:
        final_capital = capital + position * bars[-1].close
    else:
        final_capital = capital
    
    total_return = (final_capital - initial_capital) / initial_capital * 100
    
    if len(bars) > 0:
        days = (bars[-1].timestamp - bars[0].timestamp).days
        if days > 0:
            annual_return = ((final_capital / initial_capital) ** (365 / days) - 1) * 100
        else:
            annual_return = 0.0
    else:
        annual_return = 0.0
    
    peak = initial_capital
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100 if peak > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
    
    winning_trades = sum(1 for t in trades if t > 0)
    win_rate = winning_trades / len(trades) * 100 if trades else 0.0
    
    if len(equity_curve) > 1:
        returns = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1] 
                   for i in range(1, len(equity_curve)) if equity_curve[i-1] > 0]
        if returns and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0
    
    avg_trade_return = np.mean(trades) if trades else 0.0
    
    return {
        'total_return': round(total_return, 2),
        'max_drawdown': round(-abs(max_drawdown), 2),
        'total_trades': len(trades),
        'win_rate': round(win_rate, 2),
        'sharpe': round(sharpe, 2),
        'annual_return': round(annual_return, 2),
        'final_capital': round(final_capital, 2),
        'avg_trade_return': round(avg_trade_return, 2)
    }


def main():
    symbol = '603039.SH'
    print(f"=" * 80)
    print(f"泛海微 (603039.SH) 当前策略 vs 最佳策略对比")
    print(f"=" * 80)
    print()
    
    # 加载数据
    print(f"加载数据...")
    all_bars = load_bars(symbol, 400)
    print(f"  总数据点数：{len(all_bars)}")
    print(f"  日期范围：{all_bars[0].timestamp.date()} 至 {all_bars[-1].timestamp.date()}")
    print()
    
    # 定义窗口
    horizons = [
        ('1y', 365),
        ('6m', 183),
        ('3m', 90),
        ('1m', 30),
    ]
    
    # 创建策略
    current_strategy = CurrentBreakoutStrategy()
    best_strategy = BestMACDStrategy()
    
    print(f"策略配置:")
    print(f"  当前策略：breakout (window=20, rsi=6, buy_rsi_min=55, exit_ma=10, sell_rsi_max=45)")
    print(f"  最佳策略：MACD (fast=12, slow=20, signal=7)")
    print()
    
    # 运行回测
    print(f"运行回测...")
    results = {
        'current': {},
        'best': {}
    }
    
    for horizon_name, days in horizons:
        start_idx = max(0, len(all_bars) - days)
        horizon_bars = all_bars[start_idx:]
        
        if len(horizon_bars) < 30:
            continue
        
        current_metrics = run_backtest(CurrentBreakoutStrategy(), horizon_bars)
        best_metrics = run_backtest(BestMACDStrategy(), horizon_bars)
        
        results['current'][horizon_name] = current_metrics
        results['best'][horizon_name] = best_metrics
        
        print(f"  {horizon_name}: 完成")
    
    print()
    
    # 输出对比
    print(f"=" * 80)
    print(f"策略对比结果")
    print(f"=" * 80)
    print()
    
    for horizon in ['1y', '6m', '3m', '1m']:
        if horizon not in results['current']:
            continue
        
        curr = results['current'][horizon]
        best = results['best'][horizon]
        
        print(f"{horizon}:")
        print(f"  当前策略: 收益 {curr['total_return']:>7.2f}% | 回撤 {curr['max_drawdown']:>7.2f}% | 交易 {curr['total_trades']:>3} 次 | 夏普 {curr['sharpe']:>6.2f}")
        print(f"  最佳策略: 收益 {best['total_return']:>7.2f}% | 回撤 {best['max_drawdown']:>7.2f}% | 交易 {best['total_trades']:>3} 次 | 夏普 {best['sharpe']:>6.2f}")
        
        return_diff = best['total_return'] - curr['total_return']
        dd_diff = best['max_drawdown'] - curr['max_drawdown']  # 负值越小越好
        sharpe_diff = best['sharpe'] - curr['sharpe']
        
        print(f"  差异：收益 {return_diff:>+7.2f}% | 回撤 {dd_diff:>+7.2f}% | 夏普 {sharpe_diff:>+6.2f}")
        print()
    
    # 综合评估
    print(f"=" * 80)
    print(f"综合评估")
    print(f"=" * 80)
    print()
    
    # 计算加权收益
    weights = {'1y': 0.50, '6m': 0.25, '3m': 0.20, '1m': 0.05}
    
    curr_weighted = sum(results['current'][h]['total_return'] * w for h, w in weights.items() if h in results['current'])
    best_weighted = sum(results['best'][h]['total_return'] * w for h, w in weights.items() if h in results['best'])
    
    curr_avg_dd = np.mean([results['current'][h]['max_drawdown'] for h in results['current']])
    best_avg_dd = np.mean([results['best'][h]['max_drawdown'] for h in results['best']])
    
    curr_avg_sharpe = np.mean([results['current'][h]['sharpe'] for h in results['current']])
    best_avg_sharpe = np.mean([results['best'][h]['sharpe'] for h in results['best']])
    
    print(f"加权平均收益:")
    print(f"  当前策略：{curr_weighted:.2f}%")
    print(f"  最佳策略：{best_weighted:.2f}%")
    print(f"  提升：{best_weighted - curr_weighted:+.2f}%")
    print()
    
    print(f"平均回撤:")
    print(f"  当前策略：{curr_avg_dd:.2f}%")
    print(f"  最佳策略：{best_avg_dd:.2f}%")
    print(f"  改善：{best_avg_dd - curr_avg_dd:+.2f}% (负值=回撤增加)")
    print()
    
    print(f"平均夏普比率:")
    print(f"  当前策略：{curr_avg_sharpe:.2f}")
    print(f"  最佳策略：{best_avg_sharpe:.2f}")
    print(f"  提升：{best_avg_sharpe - curr_avg_sharpe:+.2f}")
    print()
    
    # 是否推荐切换
    if best_weighted > curr_weighted and best_avg_sharpe > curr_avg_sharpe:
        print(f"✅ 推荐切换至最佳策略 (MACD_12_20_7)")
        print()
        print(f"接入 TradePilot 配置:")
        print(f"  在 config.yaml 中修改 603039.SH 的策略配置:")
        print()
        print(f"  strategies:")
        print(f"    profiles:")
        print(f"      breakout_603039:")
        print(f"        kind: \"macd\"")
        print(f"        macd: {{ fast: 12, slow: 20, signal: 7, zero_cross: false }}")
        print()
    else:
        print(f"⚠️ 当前策略已足够优秀，暂不推荐切换")
    
    # 保存报告
    output_dir = Path(__file__).parent / 'reports'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    report_path = output_dir / 'fanhaiwei_current_vs_best_comparison.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"# 泛海微 (603039.SH) 当前策略 vs 最佳策略对比报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        f.write(f"## 策略配置\n\n")
        f.write(f"### 当前策略\n\n")
        f.write(f"```yaml\n")
        f.write(f"breakout_603039:\n")
        f.write(f"  kind: \"breakout\"\n")
        f.write(f"  breakout_window: 20\n")
        f.write(f"  rsi_period: 6\n")
        f.write(f"  buy_rsi_min: 55\n")
        f.write(f"  exit_ma: 10\n")
        f.write(f"  sell_rsi_max: 45\n")
        f.write(f"```\n\n")
        
        f.write(f"### 最佳策略\n\n")
        f.write(f"```yaml\n")
        f.write(f"macd_603039:\n")
        f.write(f"  kind: \"macd\"\n")
        f.write(f"  macd:\n")
        f.write(f"    fast: 12\n")
        f.write(f"    slow: 20\n")
        f.write(f"    signal: 7\n")
        f.write(f"    zero_cross: false\n")
        f.write(f"```\n\n")
        
        f.write(f"## 四窗口对比\n\n")
        for horizon in ['1y', '6m', '3m', '1m']:
            if horizon not in results['current']:
                continue
            
            curr = results['current'][horizon]
            best = results['best'][horizon]
            
            f.write(f"### {horizon}\n\n")
            f.write(f"| 指标 | 当前策略 | 最佳策略 | 差异 |\n")
            f.write(f"|------|----------|----------|------|\n")
            f.write(f"| 收益率 | {curr['total_return']:.2f}% | {best['total_return']:.2f}% | {best['total_return'] - curr['total_return']:+.2f}% |\n")
            f.write(f"| 最大回撤 | {curr['max_drawdown']:.2f}% | {best['max_drawdown']:.2f}% | {best['max_drawdown'] - curr['max_drawdown']:+.2f}% |\n")
            f.write(f"| 交易次数 | {curr['total_trades']} | {best['total_trades']} | {best['total_trades'] - curr['total_trades']:+d} |\n")
            f.write(f"| 胜率 | {curr['win_rate']:.2f}% | {best['win_rate']:.2f}% | {best['win_rate'] - curr['win_rate']:+.2f}% |\n")
            f.write(f"| 夏普比率 | {curr['sharpe']:.2f} | {best['sharpe']:.2f} | {best['sharpe'] - curr['sharpe']:+.2f} |\n")
            f.write(f"\n")
        
        f.write(f"## 综合评估\n\n")
        f.write(f"| 指标 | 当前策略 | 最佳策略 | 差异 |\n")
        f.write(f"|------|----------|----------|------|\n")
        f.write(f"| 加权收益 | {curr_weighted:.2f}% | {best_weighted:.2f}% | {best_weighted - curr_weighted:+.2f}% |\n")
        f.write(f"| 平均回撤 | {curr_avg_dd:.2f}% | {best_avg_dd:.2f}% | {best_avg_dd - curr_avg_dd:+.2f}% |\n")
        f.write(f"| 平均夏普 | {curr_avg_sharpe:.2f} | {best_avg_sharpe:.2f} | {best_avg_sharpe - curr_avg_sharpe:+.2f} |\n")
        f.write(f"\n")
        
        if best_weighted > curr_weighted and best_avg_sharpe > curr_avg_sharpe:
            f.write(f"## ✅ 推荐切换\n\n")
            f.write(f"最佳策略 (MACD_12_20_7) 在加权收益和夏普比率上均优于当前策略。\n\n")
            f.write(f"### 接入 TradePilot 配置\n\n")
            f.write(f"在 `config.yaml` 中修改 603039.SH 的策略配置:\n\n")
            f.write(f"```yaml\n")
            f.write(f"strategies:\n")
            f.write(f"  profiles:\n")
            f.write(f"    breakout_603039:\n")
            f.write(f"      kind: \"macd\"\n")
            f.write(f"      macd:\n")
            f.write(f"        fast: 12\n")
            f.write(f"        slow: 20\n")
            f.write(f"        signal: 7\n")
            f.write(f"        zero_cross: false\n")
            f.write(f"```\n\n")
        else:
            f.write(f"## ⚠️ 暂不推荐切换\n\n")
            f.write(f"当前策略已足够优秀，或最佳策略优势不明显。\n\n")
    
    print(f"\n报告已保存：{report_path}")


if __name__ == '__main__':
    main()
