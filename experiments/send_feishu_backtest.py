#!/usr/bin/env python3
"""发送万华化学回测报告到飞书"""

import httpx
import json
from pathlib import Path

# 读取回测结果
result_path = Path("/Users/ripple/work space/ripple_tradePilot/data/backtest/600309.SH_20260315_021939_result.json")
if not result_path.exists():
    # 找最新的文件
    import glob
    files = glob.glob("/Users/ripple/work space/ripple_tradePilot/data/backtest/600309.SH_*_result.json")
    if files:
        result_path = Path(sorted(files)[-1])

with open(result_path, 'r', encoding='utf-8') as f:
    result = json.load(f)

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/859cba37-0ce9-4381-90d4-dc15140af209"

content = {
    "msg_type": "text",
    "content": {
        "text": f"""📊 TradePilot 回测报告

标的：{result['name']} ({result['symbol']})
区间：{result['start_date']} 至 {result['end_date']}

📈 收益情况:
• 初始资金：¥{result['initial_capital']:,.2f}
• 最终资金：¥{result['final_capital']:,.2f}
• 总收益率：{result['total_return']:.2f}%
• 年化收益：{result.get('annual_return', 0):.2f}%

📉 风险指标:
• 最大回撤：{result.get('max_drawdown', 0):.2f}%
• 夏普比率：{result.get('sharpe_ratio', 0):.2f}

💹 交易统计:
• 交易次数：{result['total_trades']}
• 胜率：{result.get('win_rate', 0):.2f}%

⚠️ 历史回测不代表未来表现，投资需谨慎"""
    }
}

print("发送飞书报告...")
response = httpx.post(WEBHOOK, json=content, timeout=10)
result_response = response.json()

print(f"响应：{result_response}")
if result_response.get("code") == 0 or result_response.get("StatusCode") == 0:
    print("✅ 飞书报告已发送")
else:
    print("❌ 发送失败，飞书要求签名校验")
    print("\n请在飞书开放平台关闭签名校验，或使用带签名的 webhook")
