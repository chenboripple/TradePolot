#!/usr/bin/env python3
"""
TradePilot 优化参数回测

使用优化后的参数：MA5/15, RSI14/35/65, BB20/2.0
"""

import sys
from pathlib import Path
from datetime import timedelta, datetime
import json

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier
import yaml

# 加载配置
with open(Path(__file__).parent / "config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

TOKEN = config['tushare']['token']
FEISHU_WEBHOOK = config['notifiers']['feishu']['webhook']
FEISHU_SECRET = config['notifiers']['feishu'].get('secret')

# 最优参数
OPTIMAL_PARAMS = {
    'ma': (5, 15),
    'rsi': (14, 35, 65),
    'bb': (20, 2.0),
}


def run_optimized_backtest(ts_code: str, name: str, days: int = 365):
    """使用最优参数回测"""
    print("="*80)
    print(f"🔍 TradePilot 优化参数回测")
    print(f"   标的：{name} ({ts_code})")
    print(f"   参数：MA{OPTIMAL_PARAMS['ma'][0]}/{OPTIMAL_PARAMS['ma'][1]}, "
          f"RSI{OPTIMAL_PARAMS['rsi'][0]}/{OPTIMAL_PARAMS['rsi'][1]}/{OPTIMAL_PARAMS['rsi'][2]}, "
          f"BB{OPTIMAL_PARAMS['bb'][0]}/{OPTIMAL_PARAMS['bb'][1]}")
    print("="*80)
    
    # 获取数据
    loader = TushareDataLoader(TOKEN)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    bars = list(loader.load_bars(ts_code, start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')))
    
    print(f"\n📊 数据条数：{len(bars)}")
    
    # 初始化策略（最优参数）
    ma_strategy = MovingAverageCross(fast=OPTIMAL_PARAMS['ma'][0], slow=OPTIMAL_PARAMS['ma'][1])
    rsi_strategy = RSI(period=OPTIMAL_PARAMS['rsi'][0], 
                       oversold=OPTIMAL_PARAMS['rsi'][1], 
                       overbought=OPTIMAL_PARAMS['rsi'][2])
    bb_strategy = BollingerBands(period=OPTIMAL_PARAMS['bb'][0], std_dev=OPTIMAL_PARAMS['bb'][1])
    
    # 回测
    capital = 100000
    position = 0
    entry_price = 0
    trades = []
    
    for i, bar in enumerate(bars):
        history = bars[:i+1]
        
        if len(history) < 30:
            # 预热
            ma_strategy.on_bar(bar)
            rsi_strategy.on_bar(bar)
            bb_strategy.on_bar(bar)
            continue
        
        # 重置并预热策略
        ma_strategy.reset()
        rsi_strategy.reset()
        bb_strategy.reset()
        
        for prev_bar in history[:-1]:
            ma_strategy.on_bar(prev_bar)
            rsi_strategy.on_bar(prev_bar)
            bb_strategy.on_bar(prev_bar)
        
        # 生成信号
        ma_signal = ma_strategy.on_bar(bar)
        rsi_signal = rsi_strategy.on_bar(bar)
        bb_signal = bb_strategy.on_bar(bar)
        
        # 投票（优化版：任一策略看涨即买入）
        buy_score = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.BUY)
        sell_score = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.SELL)
        
        signal = None
        if buy_score >= 1 and sell_score == 0:
            signal = "BUY"
        elif sell_score >= 1 and buy_score == 0:
            signal = "SELL"
        
        # 执行交易
        if signal == "BUY" and position == 0:
            shares = int(capital * 0.95 / bar.close / 100) * 100
            if shares > 0:
                cost = shares * bar.close * 1.0003
                capital -= cost
                position = shares
                entry_price = bar.close
                print(f"\n💰 买入：{shares}股 @ {bar.close:.2f}元，花费¥{cost:,.2f}")
        
        elif signal == "SELL" and position > 0:
            revenue = position * bar.close * 0.9997
            pnl = (bar.close - entry_price) * position
            capital += revenue
            trades.append(pnl)
            print(f"\n💰 卖出：{position}股 @ {bar.close:.2f}元，收入¥{revenue:,.2f}, 盈亏¥{pnl:,.2f}")
            position = 0
    
    # 计算最终资产
    if position > 0:
        final_value = capital + position * bars[-1].close
    else:
        final_value = capital
    
    total_return = (final_value - 100000) / 100000 * 100
    win_rate = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0
    
    # 打印结果
    print(f"\n{'='*80}")
    print("📊 回测结果")
    print(f"{'='*80}")
    print(f"初始资金：¥100,000.00")
    print(f"最终资金：¥{final_value:,.2f}")
    print(f"总收益率：{total_return:.2f}%")
    print(f"交易次数：{len(trades)}")
    print(f"胜率：{win_rate:.2f}%")
    
    # 保存结果
    result = {
        'symbol': ts_code,
        'name': name,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d'),
        'params': OPTIMAL_PARAMS,
        'initial_capital': 100000,
        'final_capital': final_value,
        'total_return': total_return,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'trades': trades,
    }
    
    output_path = Path(__file__).parent / "data" / "backtest" / f"{ts_code.replace('.', '_')}_optimized_result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 结果已保存：{output_path}")
    
    # 发送飞书
    print(f"\n📱 发送飞书报告...")
    send_feishu_report(result)
    
    return result


def send_feishu_report(result: dict):
    """发送飞书报告（统一走 FeishuWebhookNotifier）。"""
    content = {
        "msg_type": "text",
        "content": {
            "text": f"📊 TradePilot 优化参数回测报告\n\n"
                    f"标的：{result['name']} ({result['symbol']})\n"
                    f"区间：{result['start_date']} 至 {result['end_date']}\n\n"
                    f"⚙️ 优化参数:\n"
                    f"• MA: {result['params']['ma'][0]}/{result['params']['ma'][1]}\n"
                    f"• RSI: {result['params']['rsi'][0]}/{result['params']['rsi'][1]}/{result['params']['rsi'][2]}\n"
                    f"• BB: {result['params']['bb'][0]}/{result['params']['bb'][1]}\n\n"
                    f"📈 收益情况:\n"
                    f"• 初始资金：¥{result['initial_capital']:,.2f}\n"
                    f"• 最终资金：¥{result['final_capital']:,.2f}\n"
                    f"• 总收益率：{result['total_return']:.2f}%\n\n"
                    f"💹 交易统计:\n"
                    f"• 交易次数：{result['total_trades']}\n"
                    f"• 胜率：{result['win_rate']:.1f}%\n\n"
                    f"⚠️ 历史回测不代表未来表现"
        }
    }

    notifier = FeishuWebhookNotifier(FEISHU_WEBHOOK, FEISHU_SECRET)
    if notifier._send_text(content):
        print("✅ 飞书报告已发送")
    else:
        print("⚠️ 飞书报告发送失败")


if __name__ == "__main__":
    # 回测 002022
    result_002022 = run_optimized_backtest("002022.SZ", "科华生物", 365)
    
    print("\n\n")
    
    # 回测 600309
    result_600309 = run_optimized_backtest("600309.SH", "万华化学", 365)
    
    print("\n" + "="*80)
    print("✅ 优化回测完成")
    print("="*80)
