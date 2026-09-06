#!/usr/bin/env python3
"""
泛海微 (603039.SH) 四窗口深度策略搜索

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


def load_bars(symbol: str, days: int) -> List[Bar]:
    """加载指定天数的 K 线数据"""
    loader = TushareDataLoader(token=TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days + 30)  # 多留 30 天缓冲
    
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
    
    avg_trade_return = np.mean(trades) if trades else 0.0
    
    return BacktestMetrics(
        total_return=round(total_return, 2),
        max_drawdown=round(-abs(max_drawdown), 2),
        total_trades=len(trades),
        win_rate=round(win_rate, 2),
        sharpe=round(sharpe, 2),
        annual_return=round(annual_return, 2),
        final_capital=round(final_capital, 2),
        avg_trade_return=round(avg_trade_return, 2)
    )


def create_strategy_variants() -> List[Tuple[str, Any]]:
    """创建策略变体列表"""
    variants = []
    
    # MACD 变体
    for fast in [8, 12, 15]:
        for slow in [20, 26, 30]:
            for signal in [7, 9, 12]:
                for zero_cross in [False, True]:
                    variants.append((
                        f"MACD_f{fast}_s{slow}_sig{signal}{'_zc' if zero_cross else ''}",
                        lambda f=fast, s=slow, sig=signal, zc=zero_cross: MACD(fast=f, slow=s, signal=sig, zero_cross=zc)
                    ))
    
    # Donchian 变体
    for window in [10, 15, 20, 25, 30]:
        for exit_mult in [0.3, 0.5, 0.7]:
            exit_window = int(window * exit_mult)
            if exit_window >= 5:
                variants.append((
                    f"Donchian_w{window}_e{exit_window}",
                    lambda w=window, e=exit_window: DonchianBreakout(window=w, exit_window=e)
                ))
    
    # ATR Channel 变体
    for period in [10, 14, 20]:
        for channel_period in [15, 20, 25]:
            for mult in [1.5, 2.0, 2.5, 3.0]:
                variants.append((
                    f"ATR_p{period}_cp{channel_period}_m{mult}",
                    lambda p=period, cp=channel_period, m=mult: ATRChannel(period=p, channel_period=cp, multiplier=m)
                ))
    
    # 均值回归变体
    for lookback in [15, 20, 25, 30]:
        for entry_std in [1.5, 2.0, 2.5]:
            for exit_std in [0.3, 0.5, 0.8]:
                for use_atr in [False, True]:
                    variants.append((
                        f"MR_lb{lookback}_e{entry_std}_x{exit_std}{'_atr' if use_atr else ''}",
                        lambda lb=lookback, e=entry_std, x=exit_std, a=use_atr: MeanReversion(lookback=lb, entry_std=e, exit_std=x, use_atr=a)
                    ))
    
    # 双 Thrust 变体
    for range in [3, 5, 7]:
        for entry in [0.3, 0.5, 0.7]:
            variants.append((
                                f"DualThrust_r{range}_e{entry}",
                lambda r=range, e=entry: DualThrust(range=r, entry_threshold=e)
            ))
    
    # 趋势过滤组合策略 (MACD + Trend Filter)
    for fast in [10, 12]:
        for slow in [24, 26]:
            for tf_short in [5, 10]:
                for tf_medium in [20, 25]:
                    for tf_long in [50, 60]:
                        variants.append((
                            f"MACD_tf_f{fast}_s{slow}_tf{tf_short}_{tf_medium}_{tf_long}",
                            lambda f=fast, s=slow, tfs=tf_short, tfm=tf_medium, tfl=tf_long: TrendFilteredStrategy(
                                MACD(fast=f, slow=s, signal=9),
                                TrendFilter(short=tfs, medium=tfm, long=tfl)
                            )
                        ))
    
    # 趋势过滤组合策略 (Donchian + Trend Filter)
    for dw in [15, 20, 25]:
        for tf_short in [5, 10]:
            for tf_medium in [20, 25]:
                for tf_long in [50, 60]:
                    variants.append((
                        f"Donchian_tf_w{dw}_tf{tf_short}_{tf_medium}_{tf_long}",
                        lambda w=dw, tfs=tf_short, tfm=tf_medium, tfl=tf_long: TrendFilteredStrategy(
                            DonchianBreakout(window=w, exit_window=max(5, w//2)),
                            TrendFilter(short=tfs, medium=tfm, long=tfl)
                        )
                    ))
    
    # 均线交叉策略
    for short in [5, 10, 15]:
        for long in [20, 30, 50, 60]:
            variants.append((
                f"MA_s{short}_l{long}",
                lambda s=short, l=long: MovingAverageCross(short=s, long=l)
            ))
    
    # RSI 策略
    for period in [10, 14, 20]:
        for oversold in [25, 30, 35]:
            for overbought in [65, 70, 75]:
                variants.append((
                    f"RSI_p{period}_os{oversold}_ob{overbought}",
                    lambda p=period, os=oversold, ob=overbought: RSI(period=p, oversold=os, overbought=ob)
                ))
    
    # 布林带策略
    for period in [15, 20, 25]:
        for std in [1.5, 2.0, 2.5]:
            variants.append((
                f"BB_p{period}_s{std}",
                lambda p=period, s=std: BollingerBands(period=p, std=s)
            ))
    
    return variants


def run_backtest_for_horizon(strategy_func, bars: List[Bar], horizon_name: str) -> Optional[BacktestMetrics]:
    """对单个窗口运行回测"""
    try:
        strategy = strategy_func()
        strategy.reset()
        metrics = backtest_strategy(strategy, bars)
        return metrics
    except Exception as e:
        print(f"  回测失败 ({horizon_name}): {e}")
        return None


def calculate_composite_score(metrics_dict: Dict[str, BacktestMetrics]) -> float:
    """
    计算综合得分
    
    综合考虑：
    - 各窗口收益（加权）
    - 回撤惩罚
    - 交易次数（避免过少）
    - 夏普比率
    """
    if not metrics_dict:
        return 0.0
    
    # 检查是否有窗口明显失效（1 个月收益 < -20%）
    if '1m' in metrics_dict and metrics_dict['1m'].total_return < -20:
        return -1000  # 严重惩罚
    
    # 加权收益
    weighted_return = 0.0
    for horizon, weight in WEIGHTS.items():
        if horizon in metrics_dict:
            weighted_return += metrics_dict[horizon].total_return * weight
    
    # 平均回撤（惩罚）
    avg_drawdown = np.mean([m.max_drawdown for m in metrics_dict.values()])
    
    # 平均夏普
    avg_sharpe = np.mean([m.sharpe for m in metrics_dict.values()])
    
    # 平均交易次数（避免过少）
    avg_trades = np.mean([m.total_trades for m in metrics_dict.values()])
    trade_penalty = max(0, 10 - avg_trades) * 2  # 少于 10 次交易有惩罚
    
    # 综合得分 = 加权收益 - 回撤惩罚 + 夏普奖励 - 交易次数惩罚
    score = weighted_return + avg_drawdown * 0.5 + avg_sharpe * 5 - trade_penalty
    
    return round(score, 2)


def main():
    symbol = '603039.SH'
    print(f"=" * 80)
    print(f"泛海微 (603039.SH) 四窗口深度策略搜索")
    print(f"=" * 80)
    print()
    
    # 加载最长周期数据（1 年 + 缓冲）
    print(f"加载数据...")
    all_bars = load_bars(symbol, 400)
    print(f"  总数据点数：{len(all_bars)}")
    print(f"  日期范围：{all_bars[0].timestamp.date()} 至 {all_bars[-1].timestamp.date()}")
    print()
    
    # 按窗口切分数据
    horizon_bars = {}
    for horizon_name, days in HORIZONS:
        start_idx = max(0, len(all_bars) - days)
        horizon_bars[horizon_name] = all_bars[start_idx:]
        print(f"  {horizon_name}: {len(horizon_bars[horizon_name])} 根 K 线")
    print()
    
    # 创建策略变体
    print(f"创建策略变体...")
    strategy_variants = create_strategy_variants()
    print(f"  总策略数：{len(strategy_variants)}")
    print()
    
    # 运行回测
    print(f"开始回测...")
    results = {}  # {strategy_name: {horizon: metrics}}
    
    for i, (strategy_name, strategy_func) in enumerate(strategy_variants):
        if (i + 1) % 50 == 0:
            print(f"  进度：{i + 1}/{len(strategy_variants)}")
        
        metrics_dict = {}
        valid = True
        
        for horizon_name, bars in horizon_bars.items():
            if len(bars) < 30:  # 数据太少跳过
                continue
            
            metrics = run_backtest_for_horizon(strategy_func, bars, horizon_name)
            if metrics:
                metrics_dict[horizon_name] = metrics
            else:
                valid = False
                break
        
        if valid and len(metrics_dict) == len(HORIZONS):
            results[strategy_name] = metrics_dict
    
    print(f"  完成回测：{len(results)} 个有效策略")
    print()
    
    # 计算综合得分并排序
    print(f"计算综合得分...")
    scored_results = []
    for strategy_name, metrics_dict in results.items():
        score = calculate_composite_score(metrics_dict)
        scored_results.append((strategy_name, score, metrics_dict))
    
    scored_results.sort(key=lambda x: x[1], reverse=True)
    print(f"  最高得分：{scored_results[0][1] if scored_results else 'N/A'}")
    print()
    
    # 输出 Top 10
    print(f"=" * 80)
    print(f"Top 10 策略")
    print(f"=" * 80)
    
    for i, (name, score, metrics_dict) in enumerate(scored_results[:10], 1):
        print(f"\n{i}. {name}")
        print(f"   综合得分：{score}")
        for horizon in ['1y', '6m', '3m', '1m']:
            if horizon in metrics_dict:
                m = metrics_dict[horizon]
                print(f"   {horizon}: 收益 {m.total_return:>7.2f}% | 回撤 {m.max_drawdown:>7.2f}% | 交易 {m.total_trades:>3} 次 | 夏普 {m.sharpe:>6.2f}")
    
    # 保存详细结果
    print(f"\n保存结果...")
    
    # 保存 JSON
    output_dir = ROOT / 'data' / 'backtest'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    json_output = {
        'symbol': symbol,
        'timestamp': datetime.now().isoformat(),
        'horizons': [h[0] for h in HORIZONS],
        'weights': WEIGHTS,
        'total_strategies_tested': len(strategy_variants),
        'valid_strategies': len(results),
        'top_10': [],
        'all_results': {}
    }
    
    for name, score, metrics_dict in scored_results[:10]:
        top_entry = {
            'name': name,
            'score': score,
            'metrics': {}
        }
        for horizon, m in metrics_dict.items():
            top_entry['metrics'][horizon] = asdict(m)
        json_output['top_10'].append(top_entry)
    
    # 保存所有结果（只保存关键信息）
    for name, metrics_dict in list(results.items())[:100]:  # 限制保存数量
        json_output['all_results'][name] = {
            'score': calculate_composite_score(metrics_dict),
            'metrics': {h: asdict(m) for h, m in metrics_dict.items()}
        }
    
    json_path = output_dir / f'{symbol.replace(".", "_")}_4window_research.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_output, f, indent=2, ensure_ascii=False)
    print(f"  JSON: {json_path}")
    
    # 生成 Markdown 报告
    report_path = output_dir / f'{symbol.replace(".", "_")}_4window_research_summary.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"# 泛海微 (603039.SH) 四窗口策略研究报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"## 研究概要\n\n")
        f.write(f"- **测试策略总数**: {len(strategy_variants)}\n")
        f.write(f"- **有效策略数**: {len(results)}\n")
        f.write(f"- **测试窗口**: 1 年、6 个月、3 个月、1 个月\n")
        f.write(f"- **权重配置**: 1 年 50%, 6 个月 25%, 3 个月 20%, 1 个月 5%\n\n")
        
        f.write(f"## Top 10 策略\n\n")
        for i, (name, score, metrics_dict) in enumerate(scored_results[:10], 1):
            f.write(f"### {i}. {name}\n\n")
            f.write(f"**综合得分**: {score}\n\n")
            f.write(f"| 窗口 | 收益率 | 最大回撤 | 交易次数 | 夏普比率 |\n")
            f.write(f"|------|--------|----------|----------|----------|\n")
            for horizon in ['1y', '6m', '3m', '1m']:
                if horizon in metrics_dict:
                    m = metrics_dict[horizon]
                    f.write(f"| {horizon} | {m.total_return:.2f}% | {m.max_drawdown:.2f}% | {m.total_trades} | {m.sharpe:.2f} |\n")
            f.write(f"\n")
        
        # 最佳策略详细分析
        if scored_results:
            best_name, best_score, best_metrics = scored_results[0]
            f.write(f"## 最佳策略详解\n\n")
            f.write(f"**策略名称**: {best_name}\n\n")
            f.write(f"**四窗口表现**:\n\n")
            for horizon in ['1y', '6m', '3m', '1m']:
                if horizon in best_metrics:
                    m = best_metrics[horizon]
                    f.write(f"- **{horizon}**:\n")
                    f.write(f"  - 收益率：{m.total_return:.2f}%\n")
                    f.write(f"  - 最大回撤：{m.max_drawdown:.2f}%\n")
                    f.write(f"  - 交易次数：{m.total_trades}\n")
                    f.write(f"  - 胜率：{m.win_rate:.2f}%\n")
                    f.write(f"  - 夏普比率：{m.sharpe:.2f}\n")
                    f.write(f"  - 年化收益：{m.annual_return:.2f}%\n")
                    f.write(f"  - 最终资金：{m.final_capital:.2f}\n")
                    f.write(f"  - 平均单笔收益：{m.avg_trade_return:.2f}\n\n")
    
    print(f"  Markdown: {report_path}")
    
    # 输出最终摘要
    print(f"\n" + "=" * 80)
    print(f"最终摘要")
    print(f"=" * 80)
    
    if scored_results:
        best_name, best_score, best_metrics = scored_results[0]
        print(f"\n最佳策略：{best_name}")
        print(f"综合得分：{best_score}")
        print(f"\n四窗口表现:")
        for horizon in ['1y', '6m', '3m', '1m']:
            if horizon in best_metrics:
                m = best_metrics[horizon]
                print(f"  {horizon}: 收益 {m.total_return:>7.2f}% | 回撤 {m.max_drawdown:>7.2f}% | 交易 {m.total_trades:>3} 次 | 胜率 {m.win_rate:>5.1f}%")
        
        print(f"\n文件已保存:")
        print(f"  - {json_path}")
        print(f"  - {report_path}")
    else:
        print(f"未找到有效策略")


if __name__ == '__main__':
    main()
