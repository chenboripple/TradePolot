#!/usr/bin/env python3
"""
科华生物 (002022.SZ) 深度策略研究

功能：
- 多周期回测：5y/3y/1y/6m/3m/1m
- 测试多种策略：MA/RSI/BB/Donchian/DualThrust/ATR/MACD/MeanRev
- 加权评分：重点加权 1y/6m/3m
- 产出最佳策略配置与详细报告
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
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
from ripple_tradePilot.strategies.donchian import DonchianBreakout
from ripple_tradePilot.strategies.dual_thrust import DualThrust
from ripple_tradePilot.strategies.atr_channel import ATRChannel
from ripple_tradePilot.strategies.macd import MACD
from ripple_tradePilot.strategies.mean_reversion import MeanReversion
from ripple_tradePilot.strategies.trend_filter import TrendFilter, TrendFilteredStrategy
from ripple_tradePilot.models.types import Bar, Side

# 加载配置
CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
TOKEN = CONFIG['tushare']['token']

# 回测周期定义
HORIZONS = [
    ('5y', 365 * 5),
    ('3y', 365 * 3),
    ('1y', 365),
    ('6m', 183),
    ('3m', 90),
    ('1m', 30),
]

# 权重配置（重点加权 1y/6m/3m）
WEIGHTS = {
    '5y': 0.10,
    '3y': 0.15,
    '1y': 0.35,
    '6m': 0.20,
    '3m': 0.15,
    '1m': 0.05,
}


@dataclass
class BacktestMetrics:
    total_return: float
    max_drawdown: float
    total_trades: int
    win_rate: float
    sharpe: float
    annual_return: float
    final_capital: float


def backtest_strategy(strategy, bars: List[Bar], initial_capital: float = 100000.0) -> BacktestMetrics:
    """
    回测单个策略
    
    Args:
        strategy: 策略实例
        bars: K 线数据
        initial_capital: 初始资金
    
    Returns:
        BacktestMetrics
    """
    capital = initial_capital
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [capital]
    
    commission = 0.0003  # 万三
    slippage = 0.001     # 千一
    
    for i, bar in enumerate(bars):
        # 生成信号
        signal = strategy.on_bar(bar)
        
        # 记录权益
        current_equity = capital + position * bar.close if position > 0 else capital
        equity_curve.append(current_equity)
        
        if signal.side == Side.BUY and position == 0:
            # 买入
            buy_price = bar.close * (1 + slippage)
            shares = int(capital * 0.95 / buy_price / 100) * 100
            if shares > 0:
                cost = shares * buy_price * (1 + commission)
                if cost <= capital:
                    capital -= cost
                    position = shares
                    entry_price = buy_price
        
        elif signal.side == Side.SELL and position > 0:
            # 卖出
            sell_price = bar.close * (1 - slippage)
            revenue = position * sell_price * (1 - commission)
            capital += revenue
            
            pnl = (sell_price - entry_price) * position
            trades.append(pnl)
            position = 0
            entry_price = 0.0
    
    # 计算最终资金
    if position > 0:
        final_capital = capital + position * bars[-1].close
    else:
        final_capital = capital
    
    total_return = (final_capital - initial_capital) / initial_capital * 100
    
    # 年化收益
    if len(bars) > 0:
        days = (bars[-1].timestamp - bars[0].timestamp).days
        if days > 0:
            annual_return = ((final_capital / initial_capital) ** (365 / days) - 1) * 100
        else:
            annual_return = 0.0
    else:
        annual_return = 0.0
    
    # 最大回撤
    peak = initial_capital
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100 if peak > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
    
    # 胜率
    winning_trades = sum(1 for t in trades if t > 0)
    win_rate = winning_trades / len(trades) * 100 if trades else 0.0
    
    # 夏普比率
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


def calculate_score(metrics: BacktestMetrics, horizon: str) -> float:
    """
    计算策略评分
    
    评分公式：
    score = total_return - 0.5 * max_drawdown + 4 * sharpe + trade_bonus
    
    交易次数奖励：
    - 1y/6m/3m: 3-18 次交易为最佳
    - 5y/3y: 6-60 次交易为最佳
    """
    trades = metrics.total_trades
    
    # 交易次数奖励
    trade_bonus = 0
    if horizon in ('1y', '6m', '3m'):
        if 3 <= trades <= 18:
            trade_bonus = 6
        elif trades == 0:
            trade_bonus = -8
    elif horizon in ('5y', '3y'):
        if 6 <= trades <= 60:
            trade_bonus = 6
        elif trades == 0:
            trade_bonus = -8
    
    score = (
        metrics.total_return 
        - 0.5 * metrics.max_drawdown 
        + 4 * metrics.sharpe 
        + trade_bonus
    )
    
    return score


# ==================== 策略定义 ====================

def create_strategies() -> List[tuple]:
    """创建策略池"""
    strategies = []
    
    # 1. MA 交叉策略（多参数）
    for fast, slow in [(5, 20), (8, 21), (10, 30), (5, 15), (10, 25)]:
        strategies.append((f"MA_{fast}_{slow}", MovingAverageCross(fast=fast, slow=slow)))
    
    # 2. RSI 策略（多参数）
    for period, oversold, overbought in [
        (6, 20, 60), (8, 25, 65), (10, 30, 70), (14, 30, 70), (6, 25, 65)
    ]:
        strategies.append((f"RSI_{period}_{oversold}_{overbought}", 
                          RSI(period=period, oversold=oversold, overbought=overbought)))
    
    # 3. 布林带策略（多参数）
    for period, std in [(20, 2.0), (20, 1.8), (26, 2.0), (14, 1.8), (20, 1.5)]:
        strategies.append((f"BB_{period}_{std}", BollingerBands(period=period, std_dev=std)))
    
    # 4. Donchian 突破策略
    for window in [10, 20, 30, 15, 25]:
        strategies.append((f"Donchian_{window}", DonchianBreakout(window=window)))
    
    # 5. Dual Thrust 策略
    for lookback, k1, k2 in [(4, 0.5, 0.5), (5, 0.6, 0.6), (4, 0.4, 0.6), (5, 0.5, 0.7)]:
        strategies.append((f"DualThrust_{lookback}_{k1}_{k2}", 
                          DualThrust(lookback=lookback, k1=k1, k2=k2)))
    
    # 6. ATR 通道策略
    for period, mult in [(14, 2.0), (14, 2.5), (20, 2.0), (10, 2.0)]:
        strategies.append((f"ATR_{period}_{mult}", ATRChannel(period=period, multiplier=mult)))
    
    # 7. MACD 策略
    for fast, slow, signal in [(12, 26, 9), (8, 21, 5), (10, 24, 8), (12, 26, 9)]:
        strategies.append((f"MACD_{fast}_{slow}_{signal}", 
                          MACD(fast=fast, slow=slow, signal=signal, zero_cross=False)))
    
    # 8. MACD + 零轴过滤
    strategies.append(("MACD_zero", MACD(fast=12, slow=26, signal=9, zero_cross=True)))
    
    # 9. 均值回归策略
    for lookback, entry, exit in [
        (20, 2.0, 0.5), (30, 2.0, 1.0), (20, 2.5, 0.5), (25, 2.0, 0.8)
    ]:
        strategies.append((f"MeanRev_{lookback}_{entry}_{exit}", 
                          MeanReversion(lookback=lookback, entry_std=entry, exit_std=exit)))
    
    # 10. 组合策略：MA + RSI + BB (Grid Combo)
    from ripple_tradePilot.models.types import Signal
    
    class GridCombo:
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
    
    for params in [
        (5, 20, 6, 20, 60, 20, 2.0, 1),
        (5, 20, 6, 20, 60, 20, 2.0, 2),
        (10, 30, 8, 25, 65, 20, 1.8, 1),
        (5, 15, 6, 20, 60, 20, 2.0, 1),
        (8, 21, 6, 25, 65, 26, 2.0, 1),
    ]:
        gc = GridCombo(*params)
        strategies.append((gc.name, gc))
    
    return strategies


def run_multi_horizon_research(
    symbol: str = "002022.SZ",
    name: str = "科华生物",
) -> Dict[str, Any]:
    """
    运行多周期策略研究
    
    Args:
        symbol: 股票代码
        name: 股票名称
    
    Returns:
        研究结果字典
    """
    print("=" * 100)
    print(f"TradePilot 深度策略研究：{name} ({symbol})")
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
        bars = list(loader.load_bars(symbol, start, end))
        horizon_bars[key] = bars
        print(f"  {key}: {len(bars)} 条")
    
    # 创建策略池
    strategies = create_strategies()
    print(f"\n📊 策略池：{len(strategies)} 个策略")
    
    # 回测所有策略
    print("\n🔍 开始多周期回测...")
    results = []
    
    for strat_name, strategy in strategies:
        period_results = {}
        total_score = 0.0
        valid_count = 0
        
        for horizon_key, _ in HORIZONS:
            bars = horizon_bars[horizon_key]
            
            # 短周期允许更少数据
            min_bars = 10 if horizon_key in ('1m', '3m') else 20
            
            if len(bars) < min_bars:
                # 跳过数据不足的周期，但不使策略无效
                continue
            
            # 重置策略
            strategy.reset()
            
            # 运行回测
            metrics = backtest_strategy(strategy, bars)
            period_results[horizon_key] = asdict(metrics)
            valid_count += 1
            
            # 计算评分
            score = calculate_score(metrics, horizon_key)
            total_score += WEIGHTS[horizon_key] * score
        
        # 至少需要 4 个周期有效
        if valid_count < 4:
            continue
        
        # 额外偏好：1y 表现不能太差
        if '1y' in period_results:
            total_score += 0.6 * period_results['1y']['total_return']
            total_score -= 0.2 * period_results['1y']['max_drawdown']
        
        results.append({
            'name': strat_name,
            'periods': period_results,
            'score': total_score,
        })
        
        print(f"  ✓ {strat_name}: score={total_score:.2f}")
    
    # 排序
    results.sort(key=lambda x: x['score'], reverse=True)
    
    # 生成报告
    report = {
        'symbol': symbol,
        'name': name,
        'research_date': now.strftime('%Y-%m-%d %H:%M:%S'),
        'horizons': [h[0] for h in HORIZONS],
        'weights': WEIGHTS,
        'total_strategies': len(results),
        'top_10': results[:10],
        'best': results[0] if results else None,
    }
    
    return report


def save_report(report: Dict[str, Any], output_path: Optional[Path] = None):
    """保存研究报告"""
    if output_path is None:
        output_path = ROOT / 'data' / 'backtest' / 'kehua_deep_research.json'
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 报告已保存：{output_path}")


def print_summary(report: Dict[str, Any]):
    """打印摘要"""
    print("\n" + "=" * 100)
    print("📊 研究摘要")
    print("=" * 100)
    
    best = report.get('best')
    if not best:
        print("❌ 无有效结果")
        return
    
    print(f"\n🏆 最佳策略：{best['name']}")
    print(f"   综合评分：{best['score']:.2f}")
    
    print("\n📈 各周期表现:")
    for horizon in report['horizons']:
        if horizon in best['periods']:
            p = best['periods'][horizon]
            print(f"   {horizon}: 收益={p['total_return']:.2f}% | 回撤={p['max_drawdown']:.2f}% | "
                  f"交易={p['total_trades']} | 胜率={p['win_rate']:.1f}% | Sharpe={p['sharpe']:.2f}")
    
    print("\n🥈 Top 3 策略:")
    for i, r in enumerate(report['top_10'][:3], 1):
        print(f"   {i}. {r['name']}: score={r['score']:.2f}")
    
    print("\n" + "=" * 100)


def main():
    """主函数"""
    symbol = "002022.SZ"
    name = "科华生物"
    
    # 运行研究
    report = run_multi_horizon_research(symbol, name)
    
    # 保存报告
    save_report(report)
    
    # 打印摘要
    print_summary(report)
    
    # 生成 Markdown 总结
    generate_markdown_summary(report)


def generate_markdown_summary(report: Dict[str, Any]):
    """生成 Markdown 格式总结"""
    output_path = ROOT / 'reports' / 'kehua_deep_research_summary.md'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    best = report.get('best')
    if not best:
        return
    
    md = f"""# 科华生物 (002022.SZ) 深度策略研究总结

**研究日期**: {report['research_date']}

## 🏆 最佳策略

**策略名称**: `{best['name']}`

**综合评分**: {best['score']:.2f}

## 📈 各周期表现

| 周期 | 收益率 | 最大回撤 | 交易次数 | 胜率 | Sharpe |
|------|--------|----------|----------|------|--------|
"""
    
    for horizon in report['horizons']:
        if horizon in best['periods']:
            p = best['periods'][horizon]
            md += f"| {horizon} | {p['total_return']:.2f}% | {p['max_drawdown']:.2f}% | {p['total_trades']} | {p['win_rate']:.1f}% | {p['sharpe']:.2f} |\n"
    
    md += f"""
## 🥈 Top 3 策略对比

| 排名 | 策略名称 | 综合评分 |
|------|----------|----------|
"""
    
    for i, r in enumerate(report['top_10'][:3], 1):
        md += f"| {i} | `{r['name']}` | {r['score']:.2f} |\n"
    
    md += f"""
## 📊 研究配置

- **测试周期**: {', '.join(report['horizons'])}
- **权重配置**: 1y(35%), 6m(20%), 3m(15%), 3y(15%), 5y(10%), 1m(5%)
- **测试策略数**: {report['total_strategies']}

## 💡 建议

根据回测结果，建议：

1. **短期 (1-6 个月)**: 使用最佳策略配置，重点关注 1y 和 6m 表现
2. **中期 (1-3 年)**: 考虑组合使用 Top 3 策略，分散风险
3. **风控**: 设置最大回撤止损，建议不超过 15%

## 📁 新增文件

- `src/ripple_tradePilot/strategies/donchian.py` - Donchian 通道突破策略
- `src/ripple_tradePilot/strategies/dual_thrust.py` - Dual Thrust 策略
- `src/ripple_tradePilot/strategies/atr_channel.py` - ATR 通道策略
- `src/ripple_tradePilot/strategies/macd.py` - MACD 策略
- `src/ripple_tradePilot/strategies/mean_reversion.py` - 均值回归策略
- `src/ripple_tradePilot/strategies/trend_filter.py` - 趋势过滤策略
- `research_kehua_deep.py` - 深度研究脚本
- `data/backtest/kehua_deep_research.json` - 详细回测结果
- `reports/kehua_deep_research_summary.md` - 本总结文件

---
*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    
    print(f"📄 Markdown 总结已保存：{output_path}")


if __name__ == '__main__':
    main()
