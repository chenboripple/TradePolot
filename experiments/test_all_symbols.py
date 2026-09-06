#!/usr/bin/env python3
"""
批量测试所有监控股票
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Side
import yaml

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']
SYMBOLS = config['symbols']

print("="*80)
print("📊 TradePilot 批量股票分析")
print("="*80)

# 初始化
loader = TushareDataLoader(TOKEN)
ma_strategy = MovingAverageCross(fast=5, slow=20)
rsi_strategy = RSI(period=14, oversold=30, overbought=70)
bb_strategy = BollingerBands(period=20, std_dev=2.0)

print(f"\n📋 监控股票列表 ({len(SYMBOLS)}只):")
for i, sym in enumerate(SYMBOLS, 1):
    print(f"   {i}. {sym['name']} ({sym['code']})")

print("\n" + "="*80)

# 分析每只股票
results = []

for symbol_config in SYMBOLS:
    code = symbol_config['code']
    name = symbol_config['name']
    
    print(f"\n📈 {name} ({code})")
    print("-"*80)
    
    try:
        # 获取数据
        bars = list(loader.load_bars(code))
        if not bars:
            print(f"   ❌ 无数据")
            continue
        
        latest_bar = bars[-1]
        
        # 预热策略
        for bar in bars[:-1]:
            ma_strategy.on_bar(bar)
            rsi_strategy.on_bar(bar)
            bb_strategy.on_bar(bar)
        
        # 生成信号
        ma_signal = ma_strategy.on_bar(latest_bar)
        rsi_signal = rsi_strategy.on_bar(latest_bar)
        bb_signal = bb_strategy.on_bar(latest_bar)
        
        # 计算指标
        fast_ma = sum(b.close for b in bars[-5:]) / 5
        slow_ma = sum(b.close for b in bars[-20:]) / 20
        current_rsi = rsi_strategy.get_current_rsi()
        bb_bands = bb_strategy.get_current_bands()
        
        # 显示数据
        print(f"   最新价：{latest_bar.close:.2f} 元 ({latest_bar.timestamp.strftime('%Y-%m-%d')})")
        print(f"   MA5: {fast_ma:.2f} | MA20: {slow_ma:.2f} | 差值：{fast_ma - slow_ma:.2f}")
        print(f"   RSI: {current_rsi:.2f}" if current_rsi else "   RSI: 数据不足")
        
        if bb_bands:
            bb_position = (latest_bar.close - bb_bands['lower']) / (bb_bands['width'] + 0.001) * 100
            print(f"   BB: 上轨{bb_bands['upper']:.2f} | 中轨{bb_bands['middle']:.2f} | 下轨{bb_bands['lower']:.2f}")
            print(f"   位置：{bb_position:.1f}% (0%=下轨，100%=上轨)")
        
        # 信号统计
        signals = {
            'MA Cross': ma_signal.side,
            'RSI': rsi_signal.side,
            'Bollinger': bb_signal.side,
        }
        
        buy_count = sum(1 for s in signals.values() if s == Side.BUY)
        sell_count = sum(1 for s in signals.values() if s == Side.SELL)
        
        # 综合建议
        if buy_count >= 2:
            recommendation = "🔴 买入"
        elif sell_count >= 2:
            recommendation = "🟢 卖出"
        elif buy_count == 1 or sell_count == 1:
            recommendation = "🟡 观望 (信号冲突)"
        else:
            recommendation = "⚪ 观望"
        
        print(f"\n   信号：BUY={buy_count}/3 | SELL={sell_count}/3")
        print(f"   综合建议：{recommendation}")
        
        results.append({
            'code': code,
            'name': name,
            'price': latest_bar.close,
            'recommendation': recommendation,
            'buy_count': buy_count,
            'sell_count': sell_count,
        })
        
        # 重置策略
        ma_strategy.reset()
        rsi_strategy.reset()
        bb_strategy.reset()
        
    except Exception as e:
        print(f"   ❌ 错误：{e}")
        continue

# 汇总
print("\n" + "="*80)
print("📋 汇总报告")
print("="*80)

buy_stocks = [r for r in results if '买入' in r['recommendation']]
sell_stocks = [r for r in results if '卖出' in r['recommendation']]
hold_stocks = [r for r in results if '观望' in r['recommendation']]

if buy_stocks:
    print(f"\n🔴 买入信号 ({len(buy_stocks)}只):")
    for r in buy_stocks:
        print(f"   • {r['name']} ({r['code']}) - {r['price']:.2f}元")

if sell_stocks:
    print(f"\n🟢 卖出信号 ({len(sell_stocks)}只):")
    for r in sell_stocks:
        print(f"   • {r['name']} ({r['code']}) - {r['price']:.2f}元")

if hold_stocks:
    print(f"\n⚪ 观望 ({len(hold_stocks)}只):")
    for r in hold_stocks:
        print(f"   • {r['name']} ({r['code']}) - {r['price']:.2f}元")

print("\n" + "="*80)
print("✅ 分析完成")
print("="*80)
