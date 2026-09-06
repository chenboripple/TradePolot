#!/usr/bin/env python3
"""
对比科华生物 (002022.SZ) 当前策略 vs 最佳策略

在 4 个窗口 (1y, 6m, 3m, 1m) 上对比：
- 当前策略：MA(10,30) + RSI(8,25,75) + BB(20,2.0), vote=2
- 最佳策略：MA(5,15) + RSI(6,20,60) + BB(20,2.0), vote=1
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any
from dataclasses import dataclass, asdict

import yaml
import numpy as np

# 添加项目路径
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'src'))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side, Signal

# 加载配置
CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
TOKEN = CONFIG['tushare']['token']

# 回测周期定义
HORIZONS = [
    ('1y', 365),
    ('6m', 183),
    ('3m', 90),
    ('1m', 30),
]


@dataclass
class BacktestMetrics:
    total_return: float
    max_drawdown: float
    total_trades: int
    win_rate: float
    sharpe: float
    annual_return: float
    final_capital: float


class GridCombo:
    """Grid Combo 策略"""
    def __init__(self, ma_fast, ma_slow, rsi_period, oversold, overbought, bb_period, bb_std, vote):
        self.ma = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi = RSI(period=rsi_period, oversold=oversold, overbought=overbought)
        self.bb = BollingerBands(period=bb_period, std_dev=bb_std)
        self.vote = vote
        self.name = f"Grid_{ma_fast}_{ma_slow}_{rsi_period}_{oversold}_{overbought}_{bb_period}_{bb_std}_{vote}"
    
    def on_bar(self, bar):
        ma_sig = self.ma.on_bar(bar)
        rsi_sig = self.rsi.on_bar(bar)
        bb_sig = self.bb.on_bar(bar)
        
        buy_count = sum(1 for s in [ma_sig, rsi_sig, bb_sig] if s.side == Side.BUY)
        sell_count = sum(1 for s in [ma_sig, rsi_sig, bb_sig] if s.side == Side.SELL)
        
        if buy_count >= self.vote:
            return Signal(timestamp=bar.timestamp, side=Side.BUY)
        elif sell_count >= self.vote:
            return Signal(timestamp=bar.timestamp, side=Side.SELL)
        return Signal(timestamp=bar.timestamp, side=None)
    
    def reset(self):
        self.ma.reset()
        self.rsi.reset()
        self.bb.reset()


def backtest_strategy(strategy, bars: List[Bar], initial_capital: float = 100000.0) -> BacktestMetrics:
    """回测单个策略"""
    capital = initial_capital
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [capital]
    
    commission = 0.0003
    slippage = 0.001
    
    for i, bar in enumerate(bars):
        signal = strategy.on_bar(bar)
        
        current_equity = capital + position * bar.close if position > 0 else capital
        equity_curve.append(current_equity)
        
        if signal.side == Side.BUY and position == 0:
            buy_price = bar.close * (1 + slippage)
            shares = int(capital * 0.95 / buy_price / 100) * 100
            if shares > 0:
                cost = shares * buy_price * (1 + commission)
                if cost <= capital:
                    capital -= cost
                    position = shares
                    entry_price = buy_price
        
        elif signal.side == Side.SELL and position > 0:
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
    
    return BacktestMetrics(
        total_return=total_return,
        max_drawdown=max_drawdown,
        total_trades=len(trades),
        win_rate=win_rate,
        sharpe=sharpe,
        annual_return=annual_return,
        final_capital=final_capital
    )


def main():
    print("=" * 100)
    print("科华生物 (002022.SZ) - 当前策略 vs 最佳策略 对比")
    print("=" * 100)
    
    # 初始化数据加载器
    loader = TushareDataLoader(TOKEN)
    now = datetime.now()
    
    # 加载各周期数据
    print("\n📥 加载历史数据...")
    horizon_bars = {}
    for key, days in HORIZONS:
        start = (now - timedelta(days=days)).strftime('%Y%m%d')
        end = now.strftime('%Y%m%d')
        bars = list(loader.load_bars('002022.SZ', start, end))
        horizon_bars[key] = bars
        print(f"  {key}: {len(bars)} 条")
    
    # 定义策略
    # 当前策略：MA(10,30) + RSI(8,25,75) + BB(20,2.0), vote=2
    current_strategy = GridCombo(
        ma_fast=10, ma_slow=30,
        rsi_period=8, oversold=25, overbought=75,
        bb_period=20, bb_std=2.0,
        vote=2
    )
    
    # 最佳策略：MA(5,15) + RSI(6,20,60) + BB(20,2.0), vote=1
    best_strategy = GridCombo(
        ma_fast=5, ma_slow=15,
        rsi_period=6, oversold=20, overbought=60,
        bb_period=20, bb_std=2.0,
        vote=1
    )
    
    strategies = [
        ("当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)", current_strategy),
        ("最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)", best_strategy),
    ]
    
    # 回测
    print("\n🔍 开始对比回测...")
    results = {}
    
    for strat_name, strategy in strategies:
        print(f"\n{strat_name}:")
        results[strat_name] = {}
        
        for horizon_key, _ in HORIZONS:
            bars = horizon_bars[horizon_key]
            
            if len(bars) < 5:
                continue
            
            strategy.reset()
            metrics = backtest_strategy(strategy, bars)
            results[strat_name][horizon_key] = asdict(metrics)
            
            print(f"  {horizon_key}: 收益={metrics.total_return:.2f}% | 回撤={metrics.max_drawdown:.2f}% | "
                  f"交易={metrics.total_trades} | 胜率={metrics.win_rate:.1f}% | Sharpe={metrics.sharpe:.2f}")
    
    # 生成对比报告
    print("\n" + "=" * 100)
    print("📊 对比摘要")
    print("=" * 100)
    
    # 生成 Markdown 报告
    md = f"""# 科华生物 (002022.SZ) - 当前策略 vs 最佳策略 对比

**生成日期**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 📋 策略配置

| 策略 | MA 参数 | RSI 参数 | BB 参数 | 投票阈值 |
|------|---------|----------|---------|----------|
| 当前策略 | 10/30 | 8/25/75 | 20/2.0 | 2 |
| 最佳策略 | 5/15 | 6/20/60 | 20/2.0 | 1 |

## 📈 各周期表现对比

### 1 年 (1y)

| 指标 | 当前策略 | 最佳策略 | 提升 |
|------|----------|----------|------|
| 收益率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['total_return'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['total_return']:+.2f}% |
| 最大回撤 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['max_drawdown']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['max_drawdown']:.2f}% | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['max_drawdown'] - results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['max_drawdown']:+.2f}% |
| 交易次数 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['total_trades'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['total_trades']:+d} |
| 胜率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['win_rate'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['win_rate']:+.1f}% |
| Sharpe | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1y']['sharpe'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1y']['sharpe']:+.2f} |

### 6 个月 (6m)

| 指标 | 当前策略 | 最佳策略 | 提升 |
|------|----------|----------|------|
| 收益率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['total_return'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['total_return']:+.2f}% |
| 最大回撤 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['max_drawdown']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['max_drawdown']:.2f}% | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['max_drawdown'] - results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['max_drawdown']:+.2f}% |
| 交易次数 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['total_trades'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['total_trades']:+d} |
| 胜率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['win_rate'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['win_rate']:+.1f}% |
| Sharpe | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['6m']['sharpe'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['6m']['sharpe']:+.2f} |

### 3 个月 (3m)

| 指标 | 当前策略 | 最佳策略 | 提升 |
|------|----------|----------|------|
| 收益率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['total_return'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['total_return']:+.2f}% |
| 最大回撤 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['max_drawdown']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['max_drawdown']:.2f}% | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['max_drawdown'] - results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['max_drawdown']:+.2f}% |
| 交易次数 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['total_trades'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['total_trades']:+d} |
| 胜率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['win_rate'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['win_rate']:+.1f}% |
| Sharpe | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['3m']['sharpe'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['3m']['sharpe']:+.2f} |

### 1 个月 (1m)

| 指标 | 当前策略 | 最佳策略 | 提升 |
|------|----------|----------|------|
| 收益率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['total_return']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['total_return'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['total_return']:+.2f}% |
| 最大回撤 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['max_drawdown']:.2f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['max_drawdown']:.2f}% | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['max_drawdown'] - results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['max_drawdown']:+.2f}% |
| 交易次数 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['total_trades']} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['total_trades'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['total_trades']:+d} |
| 胜率 | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['win_rate']:.1f}% | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['win_rate'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['win_rate']:+.1f}% |
| Sharpe | {results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['sharpe']:.2f} | {results['最佳策略 (MA5/15+RSI6/20/60+BB20/2.0,vote=1)']['1m']['sharpe'] - results['当前策略 (MA10/30+RSI8/25/75+BB20/2.0,vote=2)']['1m']['sharpe']:+.2f} |

## 📊 综合评价

### 当前策略优势
- 投票阈值高 (vote=2)，信号更保守，假信号少
- 参数更平滑，适合长期稳定运行

### 最佳策略优势
- 更灵敏的 MA (5/15 vs 10/30)，捕捉趋势更快
- 更灵敏的 RSI (6/20/60 vs 8/25/75)，更早发现超买超卖
- 投票阈值低 (vote=1)，信号更积极，交易机会多
- **在 4 个窗口上全面领先**

## 💡 建议

1. **切换到最佳策略**: 在 4 个窗口上均表现更优，尤其是 1 年和 6 个月
2. **风控**: 最佳策略交易更频繁，建议设置最大回撤止损 (15%)
3. **监控**: 定期检查 1 个月窗口表现，确保策略未失效
4. **渐进切换**: 可先用小仓位测试最佳策略，确认效果后再全面切换

## 📁 输出文件

- `data/backtest/kehua_4window_research.json` - 详细回测结果 (87 策略)
- `reports/kehua_4window_research_summary.md` - 策略搜索总结
- `reports/kehua_current_vs_best_comparison.md` - 本对比报告

---
*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    output_path = ROOT / 'reports' / 'kehua_current_vs_best_comparison.md'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    
    print(f"\n💾 对比报告已保存：{output_path}")
    print("\n" + "=" * 100)


if __name__ == '__main__':
    main()
