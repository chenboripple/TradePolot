#!/usr/bin/env python3
"""
简化回测 - 确保产生交易信号
"""

import sys
from pathlib import Path
from datetime import timedelta, datetime

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier
import yaml

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']
FEISHU_WEBHOOK = config['notifiers']['feishu']['webhook']
FEISHU_SECRET = config['notifiers']['feishu']['secret']

print("="*70)
print("🔍 TradePilot 简化回测")
print("="*70)

# 获取数据
loader = TushareDataLoader(TOKEN)
end_date = datetime.now()
start_date = end_date - timedelta(days=90)

bars = list(loader.load_bars('002022.SZ', start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
print(f"\n📊 获取数据：{len(bars)}条")

if not bars:
    print("❌ 无数据")
    sys.exit(1)

# 运行策略
strategy = MovingAverageCross(fast=5, slow=20)

# 预热
for bar in bars[:20]:
    strategy.on_bar(bar)

# 生成信号
signals = []
for bar in bars[20:]:
    signal = strategy.on_bar(bar)
    if signal.side:
        signals.append({
            'date': bar.timestamp.strftime('%Y-%m-%d'),
            'price': bar.close,
            'side': signal.side.value,
        })

print(f"\n📈 交易信号：{len(signals)}个")
for sig in signals[:10]:
    print(f"   {sig['date']} | {sig['side']:4} | {sig['price']:.2f}元")

# 简单回测
capital = 100000
position = 0
trades = []

for sig in signals:
    price = sig['price']
    if sig['side'] == 'BUY' and position == 0:
        # 买入
        shares = int(capital * 0.95 / price / 100) * 100
        if shares > 0:
            cost = shares * price * 1.0003
            capital -= cost
            position = shares
            print(f"\n💰 买入：{shares}股 @ {price:.2f}元，花费¥{cost:,.2f}")
    
    elif sig['side'] == 'SELL' and position > 0:
        # 卖出
        revenue = position * price * 0.9997
        pnl = revenue - (position * price)
        capital += revenue
        trades.append(pnl)
        print(f"\n💰 卖出：{position}股 @ {price:.2f}元，收入¥{revenue:,.2f}, 盈亏¥{pnl:,.2f}")
        position = 0

# 计算最终资产
if position > 0:
    final_value = capital + position * bars[-1].close
else:
    final_value = capital

total_return = (final_value - 100000) / 100000 * 100
win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0

print(f"\n{'='*70}")
print("📊 回测结果")
print(f"{'='*70}")
print(f"初始资金：¥100,000.00")
print(f"最终资金：¥{final_value:,.2f}")
print(f"总收益率：{total_return:.2f}%")
print(f"交易次数：{len(trades)}")
print(f"胜率：{win_rate:.2f}%")

# 发送飞书
print(f"\n📱 发送飞书报告...")
feishu = FeishuWebhookNotifier(FEISHU_WEBHOOK, FEISHU_SECRET)

content = {
    "msg_type": "text",
    "content": {
        "text": f"📊 TradePilot 回测报告\n\n"
                f"标的：科华生物 (002022.SZ)\n"
                f"区间：{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}\n\n"
                f"📈 收益情况:\n"
                f"• 初始资金：¥100,000.00\n"
                f"• 最终资金：¥{final_value:,.2f}\n"
                f"• 总收益率：{total_return:.2f}%\n\n"
                f"💹 交易统计:\n"
                f"• 交易次数：{len(trades)}\n"
                f"• 胜率：{win_rate:.2f}%\n"
                f"• 信号数：{len(signals)}\n\n"
                f"⚠️ 历史回测不代表未来表现"
    }
}

if feishu._send_text(content):
    print("✅ 飞书报告已发送")
else:
    print("❌ 发送失败")

# 保存结果
import json
result_data = {
    'symbol': '002022.SZ',
    'name': '科华生物',
    'start_date': start_date.strftime('%Y-%m-%d'),
    'end_date': end_date.strftime('%Y-%m-%d'),
    'initial_capital': 100000,
    'final_capital': final_value,
    'total_return': total_return,
    'trades_count': len(trades),
    'win_rate': win_rate,
    'signals': signals,
}

result_path = Path(__file__).parent / "data" / "backtest" / "quick_backtest_result.json"
result_path.parent.mkdir(parents=True, exist_ok=True)

with open(result_path, 'w', encoding='utf-8') as f:
    json.dump(result_data, f, indent=2, ensure_ascii=False)

print(f"💾 结果已保存：{result_path}")
print(f"\n{'='*70}")
print("✅ 回测完成")
print(f"{'='*70}")
