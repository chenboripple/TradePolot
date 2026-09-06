#!/usr/bin/env python3
"""
三策略组合测试 (MA + RSI + BB)
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

# 配置
TOKEN = "your_tushare_token_here"
SYMBOL = "002022.SZ"
NAME = "科华生物"

print("="*70)
print("🔍 TradePilot 三策略组合测试 (MA + RSI + BB)")
print("="*70)

# 初始化
loader = TushareDataLoader(TOKEN)
ma_strategy = MovingAverageCross(fast=5, slow=20)
rsi_strategy = RSI(period=14, oversold=30, overbought=70)
bb_strategy = BollingerBands(period=20, std_dev=2.0)

# 获取数据
print(f"\n📈 获取 {NAME} ({SYMBOL}) 历史数据...")
bars = list(loader.load_bars(SYMBOL))
print(f"   获取到 {len(bars)} 条数据")

if not bars:
    print("❌ 无数据，退出")
    sys.exit(1)

latest_bar = bars[-1]
print(f"   最新交易日：{latest_bar.timestamp.strftime('%Y-%m-%d')}")
print(f"   收盘价：{latest_bar.close:.2f} 元")

# 预热策略
print("\n🔥 预热策略...")
for bar in bars[:-1]:
    ma_strategy.on_bar(bar)
    rsi_strategy.on_bar(bar)
    bb_strategy.on_bar(bar)

# 生成信号
print("\n📊 生成信号...")
print("-"*70)

# MA Cross
ma_signal = ma_strategy.on_bar(latest_bar)
fast_ma = sum(b.close for b in bars[-5:]) / 5
slow_ma = sum(b.close for b in bars[-20:]) / 20

print(f"\n1️⃣  MA Cross (趋势策略)")
print(f"   MA5:   {fast_ma:.2f}")
print(f"   MA20:  {slow_ma:.2f}")
print(f"   差值： {fast_ma - slow_ma:.2f} ({'金叉' if fast_ma > slow_ma else '死叉'})")
if ma_signal.side:
    print(f"   🚨 信号：{ma_signal.side.value}")
else:
    print(f"   ⏸️  信号：观望")

# RSI
rsi_signal = rsi_strategy.on_bar(latest_bar)
current_rsi = rsi_strategy.get_current_rsi()

print(f"\n2️⃣  RSI (超买超卖)")
print(f"   RSI:   {current_rsi:.2f}" if current_rsi else "   RSI:   数据不足")
print(f"   超卖线：30")
print(f"   超买线：70")
if rsi_signal.side:
    print(f"   🚨 信号：{rsi_signal.side.value} (强度：{rsi_signal.strength:.2f})")
else:
    if current_rsi:
        if current_rsi < 30:
            print(f"   💡 状态：超卖区域（但未触发买入）")
        elif current_rsi > 70:
            print(f"   💡 状态：超买区域（但未触发卖出）")
        else:
            print(f"   💡 状态：中性区域")
    print(f"   ⏸️  信号：观望")

# Bollinger Bands
bb_signal = bb_strategy.on_bar(latest_bar)
bb_bands = bb_strategy.get_current_bands()

print(f"\n3️⃣  Bollinger Bands (波动率)")
if bb_bands:
    print(f"   上轨： {bb_bands['upper']:.2f}")
    print(f"   中轨： {bb_bands['middle']:.2f}")
    print(f"   下轨： {bb_bands['lower']:.2f}")
    print(f"   带宽： {bb_bands['width']:.2f} ({'宽' if bb_bands['width'] > slow_ma * 0.1 else '窄'})")
    print(f"   位置： {(latest_bar.close - bb_bands['lower']) / (bb_bands['width'] + 0.001) * 100:.1f}% (0%=下轨，100%=上轨)")
    if bb_signal.side:
        print(f"   🚨 信号：{bb_signal.side.value} (强度：{bb_signal.strength:.2f})")
    else:
        print(f"   ⏸️  信号：观望")
else:
    print(f"   数据不足，无法计算")
    print(f"   ⏸️  信号：观望")

# 综合信号
print("\n" + "="*70)
print("📋 综合信号分析")
print("="*70)

signals = {
    'MA Cross': ma_signal.side,
    'RSI': rsi_signal.side,
    'Bollinger': bb_signal.side,
}

buy_count = sum(1 for s in signals.values() if s == Side.BUY)
sell_count = sum(1 for s in signals.values() if s == Side.SELL)

print(f"\n买入信号：{buy_count}/3")
print(f"卖出信号：{sell_count}/3")
print()

if buy_count >= 2:
    print("🟢 综合建议：BUY (多数策略看涨)")
elif sell_count >= 2:
    print("🔴 综合建议：SELL (多数策略看跌)")
elif buy_count == 1 or sell_count == 1:
    print("🟡 综合建议：HOLD (信号不一致，观望)")
else:
    print("⚪ 综合建议：HOLD (无明确信号)")

print("\n" + "="*70)
print("✅ 测试完成")
print("="*70)

print("\n💡 策略特点:")
print("   - MA Cross: 捕捉趋势行情")
print("   - RSI: 捕捉超买超卖反转")
print("   - Bollinger: 捕捉波动率极值")
print()
print("📝 建议:")
print("   - 2 个以上策略同向 → 高置信度信号")
print("   - 信号冲突 → 观望或降低仓位")
print("   - 震荡市 → RSI/BB 表现更好")
print("   - 趋势市 → MA Cross 表现更好")
