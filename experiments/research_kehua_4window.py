#!/usr/bin/env python3
"""
科华生物 (002022.SZ) 四窗口深度策略搜索

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
    
    # 平均交易收益
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
    """
    计算策略评分
    
    评分公式：
    score = total_return - 0.5 * max_drawdown + 4 * sharpe + trade_bonus
    
    交易次数奖励：
    - 1y: 6-30 次交易为最佳
    - 6m: 3-18 次交易为最佳
    - 3m: 2-12 次交易为最佳
    - 1m: 1-6 次交易为最佳
    """
    trades = metrics.total_trades
    
    # 交易次数奖励
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
    """
    检查 1 个月窗口是否明显失效
    
    失效标准：
    - 收益 < -10%
    - 回撤 > 15%
    - 交易次数为 0（完全无信号）
    """
    if metrics_1m is None:
        return True  # 无数据不算失效
    
    if metrics_1m.total_return < -10:
        return False
    if metrics_1m.max_drawdown > 15:
        return False
    if metrics_1m.total_trades == 0:
        return False  # 完全无信号算失效
    
    return True


# ==================== 策略定义 ====================

def create_strategies() -> List[Tuple[str, Any]]:
    """创建策略池"""
    strategies = []
    
    # 1. MA 交叉策略（多参数）
    for fast, slow in [(5, 20), (8, 21), (10, 30), (5, 15), (10, 25), (3, 13), (13, 48)]:
        strategies.append((f"MA_{fast}_{slow}", MovingAverageCross(fast=fast, slow=slow)))
    
    # 2. RSI 策略（多参数）
    for period, oversold, overbought in [
        (6, 20, 60), (8, 25, 65), (10, 30, 70), (14, 30, 70), (6, 25, 65),
        (7, 25, 65), (9, 28, 68), (12, 25, 70)
    ]:
        strategies.append((f"RSI_{period}_{oversold}_{overbought}", 
                          RSI(period=period, oversold=oversold, overbought=overbought)))
    
    # 3. 布林带策略（多参数）
    for period, std in [(20, 2.0), (20, 1.8), (26, 2.0), (14, 1.8), (20, 1.5), (18, 2.2), (22, 1.9)]:
        strategies.append((f"BB_{period}_{std}", BollingerBands(period=period, std_dev=std)))
    
    # 4. Donchian 突破策略（多窗口）
    for window in [10, 20, 30, 15, 25, 8, 12, 18]:
        strategies.append((f"Donchian_{window}", DonchianBreakout(window=window)))
    
    # 5. Dual Thrust 策略（多参数）
    for lookback, k1, k2 in [(4, 0.5, 0.5), (5, 0.6, 0.6), (4, 0.4, 0.6), (5, 0.5, 0.7),
                             (3, 0.5, 0.5), (4, 0.6, 0.4), (5, 0.7, 0.5)]:
        strategies.append((f"DualThrust_{lookback}_{k1}_{k2}", 
                          DualThrust(lookback=lookback, k1=k1, k2=k2)))
    
    # 6. ATR 通道策略（多参数）
    for period, mult in [(14, 2.0), (14, 2.5), (20, 2.0), (10, 2.0), (14, 1.8), (14, 2.2), (18, 2.0)]:
        strategies.append((f"ATR_{period}_{mult}", ATRChannel(period=period, multiplier=mult)))
    
    # 7. MACD 策略（多参数变体）
    for fast, slow, signal in [(12, 26, 9), (8, 21, 5), (10, 24, 8), (12, 26, 9),
                               (6, 19, 5), (9, 22, 7), (11, 26, 8)]:
        strategies.append((f"MACD_{fast}_{slow}_{signal}", 
                          MACD(fast=fast, slow=slow, signal=signal, zero_cross=False)))
    
    # 8. MACD + 零轴过滤
    strategies.append(("MACD_zero", MACD(fast=12, slow=26, signal=9, zero_cross=True)))
    strategies.append(("MACD8_21_5_zero", MACD(fast=8, slow=21, signal=5, zero_cross=True)))
    
    # 9. 均值回归策略（多参数）
    for lookback, entry, exit in [
        (20, 2.0, 0.5), (30, 2.0, 1.0), (20, 2.5, 0.5), (25, 2.0, 0.8),
        (20, 1.8, 0.5), (25, 2.2, 0.6), (30, 2.5, 0.8), (15, 2.0, 0.5)
    ]:
        strategies.append((f"MeanRev_{lookback}_{entry}_{exit}", 
                          MeanReversion(lookback=lookback, entry_std=entry, exit_std=exit)))
    
    # 10. 均值回归 + ATR 动态调整
    for lookback, entry, exit in [(20, 2.0, 0.5), (25, 2.2, 0.6), (30, 2.5, 0.8)]:
        strategies.append((f"MeanRevATR_{lookback}_{entry}_{exit}", 
                          MeanReversion(lookback=lookback, entry_std=entry, exit_std=exit, use_atr=True)))
    
    # 11. 组合策略：MA + RSI + BB (Grid Combo)
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
        (5, 20, 7, 25, 65, 20, 2.0, 2),
        (10, 25, 8, 25, 65, 20, 1.8, 2),
    ]:
        gc = GridCombo(*params)
        strategies.append((gc.name, gc))
    
    # 12. 趋势过滤组合策略
    class TrendFilteredCombo:
        def __init__(self, base_strategy, tf_short, tf_medium, tf_long):
            self.base = base_strategy
            self.tf = TrendFilter(short=tf_short, medium=tf_medium, long=tf_long)
            self.name = f"TF_{base_strategy.name}_{tf_short}_{tf_medium}_{tf_long}"
        
        def on_bar(self, bar):
            tf_sig = self.tf.on_bar(bar)
            base_sig = self.base.on_bar(bar)
            
            if base_sig.side == Side.BUY and not self.tf.allow_buy():
                return Signal(timestamp=bar.timestamp, side=None)
            if base_sig.side == Side.SELL and not self.tf.allow_sell():
                return Signal(timestamp=bar.timestamp, side=None)
            return base_sig
        
        def reset(self):
            self.base.reset()
            self.tf.reset()
    
    # 基于 MA 的趋势过滤
    for fast, slow in [(5, 20), (10, 30)]:
        base = MovingAverageCross(fast=fast, slow=slow)
        for tf_params in [(5, 20, 60), (3, 13, 48), (8, 21, 60)]:
            tfc = TrendFilteredCombo(base, *tf_params)
            strategies.append((tfc.name, tfc))
    
    # 基于 MACD 的趋势过滤
    for macd_params in [(12, 26, 9), (8, 21, 5)]:
        base = MACD(fast=macd_params[0], slow=macd_params[1], signal=macd_params[2], zero_cross=False)
        tfc = TrendFilteredCombo(base, 5, 20, 60)
        strategies.append((tfc.name, tfc))
    
    # 13. Donchian + ATR 混合
    class DonchianATR:
        def __init__(self, donchian_window, atr_period, atr_mult):
            self.donchian = DonchianBreakout(window=donchian_window)
            self.atr = ATRChannel(period=atr_period, multiplier=atr_mult)
            self.name = f"DonchianATR_{donchian_window}_{atr_period}_{atr_mult}"
        
        def on_bar(self, bar):
            d_sig = self.donchian.on_bar(bar)
            a_sig = self.atr.on_bar(bar)
            
            # 两个策略同向时才交易
            if d_sig.side == Side.BUY and a_sig.side == Side.BUY:
                return Signal(timestamp=bar.timestamp, side=Side.BUY, strength=0.8)
            elif d_sig.side == Side.SELL and a_sig.side == Side.SELL:
                return Signal(timestamp=bar.timestamp, side=Side.SELL, strength=0.8)
            # 否则跟随 Donchian
            return d_sig
        
        def reset(self):
            self.donchian.reset()
            self.atr.reset()
    
    for params in [(20, 14, 2.0), (15, 14, 2.0), (20, 14, 2.5), (18, 14, 2.2)]:
        da = DonchianATR(*params)
        strategies.append((da.name, da))
    
    # 14. 双均线 + RSI 过滤
    class MA_RSI_Filter:
        def __init__(self, ma_fast, ma_slow, rsi_period, oversold, overbought):
            self.ma = MovingAverageCross(fast=ma_fast, slow=ma_slow)
            self.rsi = RSI(period=rsi_period, oversold=oversold, overbought=overbought)
            self.name = f"MA_RSI_{ma_fast}_{ma_slow}_{rsi_period}_{oversold}_{overbought}"
        
        def on_bar(self, bar):
            ma_sig = self.ma.on_bar(bar)
            rsi_sig = self.rsi.on_bar(bar)
            
            # MA 金叉 + RSI 未超买 → BUY
            if ma_sig.side == Side.BUY and rsi_sig.side != Side.SELL:
                return Signal(timestamp=bar.timestamp, side=Side.BUY)
            # MA 死叉 + RSI 未超卖 → SELL
            if ma_sig.side == Side.SELL and rsi_sig.side != Side.BUY:
                return Signal(timestamp=bar.timestamp, side=Side.SELL)
            return Signal(timestamp=bar.timestamp, side=None)
        
        def reset(self):
            self.ma.reset()
            self.rsi.reset()
    
    for params in [
        (5, 20, 14, 30, 70),
        (8, 21, 14, 30, 70),
        (10, 30, 14, 30, 70),
        (5, 20, 10, 25, 70),
    ]:
        maf = MA_RSI_Filter(*params)
        strategies.append((maf.name, maf))
    
    return strategies


def run_4window_research(
    symbol: str = "002022.SZ",
    name: str = "科华生物",
) -> Dict[str, Any]:
    """
    运行四窗口策略研究
    
    Args:
        symbol: 股票代码
        name: 股票名称
    
    Returns:
        研究结果字典
    """
    print("=" * 100)
    print(f"TradePilot 四窗口深度策略搜索：{name} ({symbol})")
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
    print("\n🔍 开始四窗口回测...")
    results = []
    
    for strat_name, strategy in strategies:
        period_results = {}
        total_score = 0.0
        valid_count = 0
        
        for horizon_key, _ in HORIZONS:
            bars = horizon_bars[horizon_key]
            
            # 短周期允许更少数据
            min_bars = 5 if horizon_key == '1m' else 10
            
            if len(bars) < min_bars:
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
        
        # 至少需要 3 个周期有效
        if valid_count < 3:
            continue
        
        # 检查 1m 是否明显失效
        metrics_1m = None
        if '1m' in period_results:
            p = period_results['1m']
            metrics_1m = BacktestMetrics(**p)
        
        if not check_1m_validity(metrics_1m):
            # 1m 明显失效，惩罚评分
            total_score -= 20
        
        # 额外偏好：1y 表现加权
        if '1y' in period_results:
            p = period_results['1y']
            # 1y 收益奖励
            total_score += 0.4 * p['total_return']
            # 1y 回撤惩罚
            total_score -= 0.15 * p['max_drawdown']
            # 1y 交易次数适中奖励
            trades = p['total_trades']
            if 6 <= trades <= 30:
                total_score += 5
            elif trades == 0:
                total_score -= 8
        
        # 6m/3m 表现检查
        for h in ['6m', '3m']:
            if h in period_results:
                p = period_results[h]
                # 收益不能太差
                if p['total_return'] < -15:
                    total_score -= 10
                # 回撤不能太大
                if p['max_drawdown'] > 20:
                    total_score -= 8
        
        results.append({
            'name': strat_name,
            'periods': period_results,
            'score': total_score,
            'valid_windows': valid_count,
        })
        
        print(f"  ✓ {strat_name}: score={total_score:.2f} (windows={valid_count})")
    
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
        output_path = ROOT / 'data' / 'backtest' / 'kehua_4window_research.json'
    
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
    output_path = ROOT / 'reports' / 'kehua_4window_research_summary.md'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    best = report.get('best')
    if not best:
        return
    
    md = f"""# 科华生物 (002022.SZ) 四窗口策略搜索总结

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
    
    # 与当前策略对比
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

- `data/backtest/kehua_4window_research.json` - 详细回测结果
- `reports/kehua_4window_research_summary.md` - 本总结文件

---
*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    
    print(f"📄 Markdown 总结已保存：{output_path}")


def main():
    """主函数"""
    symbol = "002022.SZ"
    name = "科华生物"
    
    # 运行研究
    report = run_4window_research(symbol, name)
    
    # 保存报告
    save_report(report)
    
    # 打印摘要
    print_summary(report)
    
    # 生成 Markdown 总结
    generate_markdown_summary(report)


if __name__ == '__main__':
    main()
