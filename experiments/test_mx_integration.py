#!/usr/bin/env python3
"""
测试东方财富妙想数据加载器与 TradePilot 策略回测的集成
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path('/Users/ripple/work space/ripple_tradePilot')
sys.path.insert(0, str(ROOT / 'src'))

from ripple_tradePilot.data.mx_loader import MXDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.models.types import Bar, Side


def test_data_loader():
    """测试数据加载器"""
    print("=" * 60)
    print("🧪 测试东方财富妙想数据加载器")
    print("=" * 60)
    
    # 检查 API Key
    mx_apikey = os.getenv('MX_APIKEY')
    if not mx_apikey:
        print("❌ MX_APIKEY 环境变量未设置")
        return False
    
    print(f"✅ MX_APIKEY 已设置")
    
    try:
        loader = MXDataLoader()
        
        # 测试日线数据
        print("\n📈 测试日线数据：东方财富 (300059.SZ)")
        
        # 使用已知有数据的日期范围
        bars = list(loader.load_bars('300059.SZ', start_date='20250101', end_date='20250407'))
        print(f"   获取到 {len(bars)} 个 Bar")
        
        if len(bars) == 0:
            print("❌ 未获取到数据")
            return False
        
        print(f"   第一条: {bars[0].timestamp.date()}, O={bars[0].open}, C={bars[0].close}")
        print(f"   最后一条: {bars[-1].timestamp.date()}, O={bars[-1].open}, C={bars[-1].close}")
        
        return bars
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_strategy_backtest(bars):
    """测试策略回测"""
    print("\n" + "=" * 60)
    print("🧪 测试策略回测")
    print("=" * 60)
    
    if len(bars) < 30:
        print(f"❌ 数据不足 ({len(bars)} 条)，需要至少 30 条")
        return False
    
    # 测试 MA 策略
    print("\n📊 测试 MA 策略 (5, 15)...")
    strategy = MovingAverageCross(fast=5, slow=15)
    
    signals = []
    for bar in bars:
        signal = strategy.on_bar(bar)
        if signal.side != Side.NONE:
            signals.append((bar.timestamp, signal.side))
    
    print(f"   共产生 {len(signals)} 个信号")
    if signals:
        for ts, side in signals[:5]:
            print(f"   - {ts.date()}: {side}")
    
    # 测试 RSI 策略
    print("\n📊 测试 RSI 策略 (14)...")
    strategy2 = RSI(period=14, oversold=30, overbought=70)
    
    signals2 = []
    for bar in bars:
        signal = strategy2.on_bar(bar)
        if signal.side != Side.NONE:
            signals2.append((bar.timestamp, signal.side))
    
    print(f"   共产生 {len(signals2)} 个信号")
    if signals2:
        for ts, side in signals2[:5]:
            print(f"   - {ts.date()}: {side}")
    
    return True


def test_full_backtest():
    """测试完整的回测流程"""
    print("\n" + "=" * 60)
    print("🧪 测试完整回测流程")
    print("=" * 60)
    
    from dataclasses import dataclass
    import numpy as np
    
    @dataclass
    class Metrics:
        total_return: float
        max_drawdown: float
        total_trades: int
        win_rate: float
        sharpe: float
        annual_return: float
        final_capital: float
    
    def backtest(strategy, bars, initial_capital: float = 100000.0) -> Metrics:
        strategy.reset()
        capital = initial_capital
        position = 0
        entry_price = 0.0
        trades = []
        equity_curve = [capital]
        commission = 0.0003
        slippage = 0.001

        for bar in bars:
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
                trades.append((sell_price - entry_price) * position)
                position = 0
                entry_price = 0.0

        final_capital = capital + position * bars[-1].close if position > 0 else capital
        total_return = (final_capital - initial_capital) / initial_capital * 100
        days = (bars[-1].timestamp - bars[0].timestamp).days if bars else 0
        annual_return = ((final_capital / initial_capital) ** (365 / days) - 1) * 100 if days > 0 else 0.0

        peak = initial_capital
        max_drawdown = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            drawdown = (peak - equity) / peak * 100 if peak > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)

        win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0.0
        returns = [(equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1] for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if returns and np.std(returns) > 0 else 0.0

        return Metrics(total_return, max_drawdown, len(trades), win_rate, sharpe, annual_return, final_capital)
    
    # 获取数据
    loader = MXDataLoader()
    bars = list(loader.load_bars('300059.SZ', start_date='20250101', end_date='20250407'))
    
    if len(bars) < 30:
        print(f"❌ 数据不足: {len(bars)} 条")
        return False
    
    print(f"✅ 获取到 {len(bars)} 条数据")
    
    # 测试 MA 策略回测
    print("\n📊 MA 策略 (5, 15) 回测结果:")
    strategy = MovingAverageCross(fast=5, slow=15)
    metrics = backtest(strategy, bars)
    print(f"   总收益: {metrics.total_return:.2f}%")
    print(f"   最大回撤: {metrics.max_drawdown:.2f}%")
    print(f"   交易次数: {metrics.total_trades}")
    print(f"   胜率: {metrics.win_rate:.2f}%")
    print(f"   夏普比率: {metrics.sharpe:.2f}")
    
    # 测试 RSI 策略回测
    print("\n📊 RSI 策略 (14, 30, 70) 回测结果:")
    strategy2 = RSI(period=14, oversold=30, overbought=70)
    metrics2 = backtest(strategy2, bars)
    print(f"   总收益: {metrics2.total_return:.2f}%")
    print(f"   最大回撤: {metrics2.max_drawdown:.2f}%")
    print(f"   交易次数: {metrics2.total_trades}")
    print(f"   胜率: {metrics2.win_rate:.2f}%")
    print(f"   夏普比率: {metrics2.sharpe:.2f}")
    
    return True


def main():
    print("\n" + "🚀" * 30)
    print("TradePilot + 东方财富妙想 API 集成测试")
    print("🚀" * 30 + "\n")
    
    # 测试数据加载器
    bars = test_data_loader()
    if not bars:
        print("\n❌ 数据加载器测试失败")
        return
    
    # 测试策略
    if not test_strategy_backtest(bars):
        print("\n❌ 策略测试失败")
        return
    
    # 测试完整回测
    if not test_full_backtest():
        print("\n❌ 回测测试失败")
        return
    
    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)
    print("\n💡 现在可以使用 --use-mx 参数运行策略查找器:")
    print("   python3 find_best_stock_strategy.py --symbol 300059.SZ --name 东方财富 --use-mx")


if __name__ == '__main__':
    main()
