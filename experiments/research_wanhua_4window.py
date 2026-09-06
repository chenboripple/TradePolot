#!/usr/bin/env python3
"""
万华化学 (600309.SH) 四窗口深度策略搜索

功能：
- 仅测试 4 个窗口：1 个月、3 个月、6 个月、1 年
- 重点看 1 年表现，同时要求 6 个月和 3 个月不要太差，1 个月至少不明显失效
- 测试多种策略变体：MACD 变体、Donchian、ATR 通道、均值回归、趋势过滤、组合策略等
- 产出最佳策略配置与详细报告
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
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
from ripple_tradePilot.models.types import Bar, Side, Signal

# 加载配置
CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
TOKEN = CONFIG['tushare']['token']

# 回测周期定义 - 仅 4 个窗口
HORIZONS = [
    ('1y', 365),
    ('6m', 183),
    ('3m', 90),
    ('1m', 30),
]

# 权重配置 - 重点加权 1y，其次 6m/3m，1m 权重低但作为失效检查
WEIGHTS = {
    '1y': 0.50,   # 重点看 1 年
    '6m': 0.25,   # 6 个月次重要
    '3m': 0.20,   # 3 个月再次
    '1m': 0.05,   # 1 个月权重低，但用于检查是否明显失效
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
    avg_trade_return: float


def backtest_strategy(strategy, bars: List[Bar], initial_capital: float = 100000.0) -> BacktestMetrics:
    """回测单个策略"""
    capital = initial_capital
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [capital]
    
    commission = 0.0003  # 万三
    slippage = 0.001     # 千一
    
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
    
    avg_trade_return = np.mean(trades) if trades else 0.0
    
    return BacktestMetrics(
        total_return=total_return,
        max_drawdown=max_drawdown,
        total_trades=len(trades),
        win_rate=win_rate,
        sharpe=sharpe,
        annual_return=annual_return,
        final_capital=final_capital,
        avg_trade_return=avg_trade_return
    )


def calculate_score(metrics: BacktestMetrics, horizon: str) -> float:
    """计算策略评分"""
    trades = metrics.total_trades
    
    trade_bonus = 0
    if horizon == '1y':
        if 6 <= trades <= 30:
            trade_bonus = 8
        elif trades == 0:
            trade_bonus = -10
    elif horizon == '6m':
        if 3 <= trades <= 18:
            trade_bonus = 6
        elif trades == 0:
            trade_bonus = -8
    elif horizon == '3m':
        if 2 <= trades <= 12:
            trade_bonus = 4
        elif trades == 0:
            trade_bonus = -6
    elif horizon == '1m':
        if 1 <= trades <= 6:
            trade_bonus = 2
        elif trades == 0:
            trade_bonus = -4
    
    score = (
        metrics.total_return 
        - 0.5 * metrics.max_drawdown 
        + 4 * metrics.sharpe 
        + trade_bonus
    )
    
    return score


def check_1m_validity(metrics_1m: Optional[BacktestMetrics]) -> bool:
    """检查 1 个月窗口是否明显失效"""
    if metrics_1m is None:
        return True
    
    if metrics_1m.total_return < -10:
        return False
    if metrics_1m.max_drawdown > 15:
        return False
    if metrics_1m.total_trades == 0:
        return False
    
    return True


def create_strategies() -> List[Tuple[str, Any]]:
    """创建策略池"""
    strategies = []
    
    # 1. MA 交叉策略（多参数）
    for fast, slow in [(5, 20), (8, 21), (10, 30), (5, 15), (10, 25), (3, 13), (13, 48), (10, 34), (15, 40)]:
        strategies.append((f"MA_{fast}_{slow}", MovingAverageCross(fast=fast, slow=slow)))
    
    # 2. RSI 策略（多参数）
    for period, oversold, overbought in [
        (6, 20, 60), (8, 25, 65), (10, 30, 70), (14, 30, 70), (6, 25, 65),
        (7, 25, 65), (9, 28, 68), (12, 25, 70), (16, 35, 65), (14, 35, 65)
    ]:
        strategies.append((f"RSI_{period}_{oversold}_{overbought}", 
                          RSI(period=period, oversold=oversold, overbought=overbought)))
    
    # 3. 布林带策略（多参数）
    for period, std in [(20, 2.0), (20, 1.8), (26, 2.0), (14, 1.8), (20, 1.5), (18, 2.2), (22, 1.9), (26, 1.8)]:
        strategies.append((f"BB_{period}_{std}", BollingerBands(period=period, std_dev=std)))
    
    # 4. Donchian 突破策略（多窗口）
    for window in [10, 20, 30, 15, 25, 8, 12, 18, 22, 28]:
        strategies.append((f"Donchian_{window}", DonchianBreakout(window=window)))
    
    # 5. Dual Thrust 策略（多参数）
    for lookback, k1, k2 in [(4, 0.5, 0.5), (5, 0.6, 0.6), (4, 0.4, 0.6), (5, 0.5, 0.7),
                             (3, 0.5, 0.5), (4, 0.6, 0.4), (5, 0.7, 0.5), (4, 0.5, 0.6)]:
        strategies.append((f"DualThrust_{lookback}_{k1}_{k2}", 
                          DualThrust(lookback=lookback, k1=k1, k2=k2)))
    
    # 6. ATR 通道策略（多参数）
    for period, mult in [(14, 2.0), (14, 2.5), (20, 2.0), (10, 2.0), (14, 1.8), (14, 2.2), (18, 2.0), (20, 2.5)]:
        strategies.append((f"ATR_{period}_{mult}", ATRChannel(period=period, multiplier=mult)))
    
    # 7. MACD 策略（多参数变体）
    for fast, slow, signal in [(12, 26, 9), (8, 21, 5), (10, 24, 8), (12, 26, 9),
                               (6, 19, 5), (9, 22, 7), (11, 26, 8), (12, 20, 7), (12, 30, 9),
                               (8, 26, 9), (10, 30, 8), (15, 26, 9)]:
        strategies.append((f"MACD_{fast}_{slow}_{signal}", 
                          MACD(fast=fast, slow=slow, signal=signal, zero_cross=False)))
    
    # 8. MACD + 零轴过滤
    strategies.append(("MACD_zero", MACD(fast=12, slow=26, signal=9, zero_cross=True)))
    strategies.append(("MACD8_21_5_zero", MACD(fast=8, slow=21, signal=5, zero_cross=True)))
    strategies.append(("MACD12_20_7_zero", MACD(fast=12, slow=20, signal=7, zero_cross=True)))
    
    # 9. 均值回归策略（多参数）
    for lookback, entry_std, exit_std in [
        (20, 2.0, 0.5), (30, 2.0, 1.0), (20, 2.5, 0.5), (25, 2.0, 0.8),
        (20, 1.8, 0.5), (25, 2.2, 0.6), (30, 2.5, 0.8), (15, 2.0, 0.5)
    ]:
        strategies.append((f"MeanRev_{lookback}_{entry_std}_{exit_std}",
                          MeanReversion(lookback=lookback, entry_std=entry_std, exit_std=exit_std)))
    
    # 10. 趋势过滤组合策略
    for short, medium, long_ma in [(5, 20, 60), (5, 15, 45), (8, 21, 50)]:
        for base_strat_name, base_strat in [
            ("RSI6_20_60", RSI(period=6, oversold=20, overbought=60)),
            ("BB20_2.0", BollingerBands(period=20, std_dev=2.0)),
        ]:
            strategies.append((f"TrendFilt_{short}_{medium}_{long_ma}_{base_strat_name}",
                              TrendFilteredStrategy(base_strat, TrendFilter(short=short, medium=medium, long=long_ma))))
    
    # 11. Grid Combo 策略（多参数组合）
    for ma_fast, ma_slow, rsi_period, rsi_os, rsi_ob, bb_period, bb_std in [
        (5, 20, 6, 20, 60, 20, 2.0),
        (10, 30, 14, 30, 70, 26, 2.0),
        (5, 15, 6, 25, 65, 20, 1.8),
        (10, 34, 16, 35, 65, 26, 1.8),
        (8, 21, 8, 25, 65, 20, 2.0),
        (5, 20, 14, 35, 65, 20, 2.0),
    ]:
        strategies.append((f"Grid_{ma_fast}_{ma_slow}_{rsi_period}_{rsi_os}_{rsi_ob}_{bb_period}_{bb_std}",
                          create_grid_combo(ma_fast, ma_slow, rsi_period, rsi_os, rsi_ob, bb_period, bb_std)))
    
    return strategies


def create_grid_combo(ma_fast, ma_slow, rsi_period, rsi_os, rsi_ob, bb_period, bb_std):
    """创建 Grid Combo 策略（投票组合）"""
    from ripple_tradePilot.strategies.base import Strategy
    
    class GridComboStrategy(Strategy):
        def __init__(self):
            self.ma = MovingAverageCross(fast=ma_fast, slow=ma_slow)
            self.rsi = RSI(period=rsi_period, oversold=rsi_os, overbought=rsi_ob)
            self.bb = BollingerBands(period=bb_period, std_dev=bb_std)
        
        def on_bar(self, bar: Bar) -> Signal:
            ma_sig = self.ma.on_bar(bar)
            rsi_sig = self.rsi.on_bar(bar)
            bb_sig = self.bb.on_bar(bar)
            
            buy_votes = sum([
                ma_sig.side == Side.BUY,
                rsi_sig.side == Side.BUY,
                bb_sig.side == Side.BUY
            ])
            sell_votes = sum([
                ma_sig.side == Side.SELL,
                rsi_sig.side == Side.SELL,
                bb_sig.side == Side.SELL
            ])
            
            if buy_votes >= 2:
                return Signal(timestamp=bar.timestamp, side=Side.BUY, strength=buy_votes/3)
            elif sell_votes >= 2:
                return Signal(timestamp=bar.timestamp, side=Side.SELL, strength=sell_votes/3)
            else:
                return Signal(timestamp=bar.timestamp, side=None)
        
        def reset(self):
            self.ma.reset()
            self.rsi.reset()
            self.bb.reset()
    
    return GridComboStrategy()


def run_research(symbol: str, name: str, output_path: Optional[Path] = None):
    """运行四窗口研究"""
    now = datetime.now()
    
    # 计算各窗口起止日期
    end_date = now.strftime('%Y%m%d')
    
    print(f"\n{'='*80}")
    print(f"🔬 万华化学 (600309.SH) 四窗口深度策略搜索")
    print(f"{'='*80}")
    print(f"研究时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据源：Tushare")
    print(f"测试窗口：1 年、6 个月、3 个月、1 个月")
    print(f"{'='*80}\n")
    
    # 初始化数据加载器
    loader = TushareDataLoader(token=TOKEN, rate_limit_delay=CONFIG['tushare'].get('rate_limit_delay', 1.5))
    
    # 获取 1 年数据（最长窗口）
    print("📥 加载数据...")
    start_date_1y = (now - timedelta(days=365)).strftime('%Y%m%d')
    bars_1y = list(loader.load_bars(symbol, start_date_1y, end_date))
    print(f"   加载 {len(bars_1y)} 条 K 线 (从 {start_date_1y} 到 {end_date})")
    
    # 切分各窗口数据
    bars_dict = {
        '1y': bars_1y,
        '6m': bars_1y[-123:] if len(bars_1y) > 123 else bars_1y,  # ~123 交易日 = 6 个月
        '3m': bars_1y[-62:] if len(bars_1y) > 62 else bars_1y,   # ~62 交易日 = 3 个月
        '1m': bars_1y[-21:] if len(bars_1y) > 21 else bars_1y,   # ~21 交易日 = 1 个月
    }
    
    # 创建策略池
    print("\n📦 创建策略池...")
    strategies = create_strategies()
    print(f"   共 {len(strategies)} 个策略")
    
    # 运行回测
    print("\n🧪 运行回测...")
    results = []
    
    for strat_name, strategy in strategies:
        total_score = 0.0
        period_results = {}
        valid_count = 0
        
        for horizon_key, horizon_days in HORIZONS:
            bars = bars_dict[horizon_key]
            if len(bars) < 10:
                continue
            
            strategy.reset()
            metrics = backtest_strategy(strategy, bars)
            period_results[horizon_key] = asdict(metrics)
            valid_count += 1
            
            score = calculate_score(metrics, horizon_key)
            total_score += WEIGHTS[horizon_key] * score
        
        if valid_count < 3:
            continue
        
        metrics_1m = None
        if '1m' in period_results:
            p = period_results['1m']
            metrics_1m = BacktestMetrics(**p)
        
        if not check_1m_validity(metrics_1m):
            total_score -= 20
        
        if '1y' in period_results:
            p = period_results['1y']
            total_score += 0.4 * p['total_return']
            total_score -= 0.15 * p['max_drawdown']
            trades = p['total_trades']
            if 6 <= trades <= 30:
                total_score += 5
            elif trades == 0:
                total_score -= 8
        
        for h in ['6m', '3m']:
            if h in period_results:
                p = period_results[h]
                if p['total_return'] < -15:
                    total_score -= 10
                if p['max_drawdown'] > 20:
                    total_score -= 8
        
        results.append({
            'name': strat_name,
            'periods': period_results,
            'score': total_score,
            'valid_windows': valid_count,
        })
        
        print(f"  ✓ {strat_name}: score={total_score:.2f} (windows={valid_count})")
    
    results.sort(key=lambda x: x['score'], reverse=True)
    
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
    
    if output_path is None:
        output_path = ROOT / 'data' / 'backtest' / 'wanhua_4window_research.json'
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 报告已保存：{output_path}")
    
    return report


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
    print(f"   有效窗口：{best['valid_windows']}/4")
    
    print("\n📈 各周期表现:")
    for horizon in report['horizons']:
        if horizon in best['periods']:
            p = best['periods'][horizon]
            print(f"   {horizon}: 收益={p['total_return']:.2f}% | 回撤={p['max_drawdown']:.2f}% | "
                  f"交易={p['total_trades']} | 胜率={p['win_rate']:.1f}% | Sharpe={p['sharpe']:.2f}")
    
    print("\n🥈 Top 5 策略:")
    for i, r in enumerate(report['top_10'][:5], 1):
        print(f"   {i}. {r['name']}: score={r['score']:.2f}")
    
    print("\n" + "=" * 100)


def generate_markdown_summary(report: Dict[str, Any], current_strategy_info: Optional[Dict] = None):
    """生成 Markdown 格式总结"""
    output_path = ROOT / 'reports' / 'wanhua_4window_research_summary.md'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    best = report.get('best')
    if not best:
        return
    
    md = f"""# 万华化学 (600309.SH) 四窗口策略搜索总结

**研究日期**: {report['research_date']}

## 🏆 最佳策略

**策略名称**: `{best['name']}`

**综合评分**: {best['score']:.2f}

**有效窗口**: {best['valid_windows']}/4

## 📈 各周期表现

| 周期 | 收益率 | 最大回撤 | 交易次数 | 胜率 | Sharpe | 年化收益 |
|------|--------|----------|----------|------|--------|----------|
"""
    
    for horizon in report['horizons']:
        if horizon in best['periods']:
            p = best['periods'][horizon]
            md += f"| {horizon} | {p['total_return']:.2f}% | {p['max_drawdown']:.2f}% | {p['total_trades']} | {p['win_rate']:.1f}% | {p['sharpe']:.2f} | {p['annual_return']:.2f}% |\n"
    
    md += f"""
## 🥈 Top 5 策略对比

| 排名 | 策略名称 | 综合评分 | 有效窗口 |
|------|----------|----------|----------|
"""
    
    for i, r in enumerate(report['top_10'][:5], 1):
        md += f"| {i} | `{r['name']}` | {r['score']:.2f} | {r['valid_windows']}/4 |\n"
    
    if current_strategy_info:
        md += f"""
## 📊 与当前策略对比

| 指标 | 最佳策略 | 当前策略 | 提升 |
|------|----------|----------|------|
| 1y 收益率 | {best['periods'].get('1y', {}).get('total_return', 0):.2f}% | {current_strategy_info.get('1y_return', 0):.2f}% | {best['periods'].get('1y', {}).get('total_return', 0) - current_strategy_info.get('1y_return', 0):+.2f}% |
| 1y 最大回撤 | {best['periods'].get('1y', {}).get('max_drawdown', 0):.2f}% | {current_strategy_info.get('1y_drawdown', 0):.2f}% | {current_strategy_info.get('1y_drawdown', 0) - best['periods'].get('1y', {}).get('max_drawdown', 0):+.2f}% |
| 1y 交易次数 | {best['periods'].get('1y', {}).get('total_trades', 0)} | {current_strategy_info.get('1y_trades', 0)} | {best['periods'].get('1y', {}).get('total_trades', 0) - current_strategy_info.get('1y_trades', 0):+d} |
"""
    
    md += f"""
## 📊 研究配置

- **测试周期**: {', '.join(report['horizons'])}
- **权重配置**: 1y({int(WEIGHTS['1y']*100)}%), 6m({int(WEIGHTS['6m']*100)}%), 3m({int(WEIGHTS['3m']*100)}%), 1m({int(WEIGHTS['1m']*100)}%)
- **测试策略数**: {report['total_strategies']}

## 💡 建议

根据回测结果，建议：

1. **主策略**: 使用最佳策略配置，重点关注 1y 和 6m 表现
2. **风控**: 设置最大回撤止损，建议不超过 15%
3. **监控**: 定期检查 1m 窗口表现，确保策略未失效
4. **备选**: 考虑组合使用 Top 3 策略，分散风险

## 📁 输出文件

- `data/backtest/wanhua_4window_research.json` - 详细回测结果
- `reports/wanhua_4window_research_summary.md` - 本总结文件

---
*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    
    print(f"📄 Markdown 总结已保存：{output_path}")


def get_current_strategy_performance(symbol: str) -> Optional[Dict]:
    """获取当前策略表现（从配置文件）"""
    config = CONFIG
    
    symbol_info = None
    for s in config.get('symbols', []):
        if s.get('code') == symbol:
            symbol_info = s
            break
    
    if not symbol_info:
        return None
    
    profile_name = symbol_info.get('strategy_profile')
    profile = config.get('strategies', {}).get('profiles', {}).get(profile_name)
    
    if not profile:
        return None
    
    return {
        'profile_name': profile_name,
        'kind': profile.get('kind'),
        'params': profile,
    }


if __name__ == '__main__':
    symbol = '600309.SH'
    name = '万华化学'
    
    report = run_research(symbol, name)
    print_summary(report)
    
    current_info = get_current_strategy_performance(symbol)
    generate_markdown_summary(report, current_info)
    
    print("\n✅ 研究完成！")
