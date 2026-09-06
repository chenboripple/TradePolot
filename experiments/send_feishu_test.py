#!/usr/bin/env python3
"""
飞书测试脚本（不需要签名）

使用前请在飞书开放平台关闭签名校验
"""

import httpx

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/859cba37-0ce9-4381-90d4-dc15140af209"

content = {
    "msg_type": "text",
    "content": {
        "text": "✅ TradePilot 测试消息\n\n如果你收到这条消息，说明飞书机器人配置成功！\n\n时间：2026-03-15 02:41"
    }
}

print("📱 发送飞书测试消息...")
print(f"Webhook: {WEBHOOK}\n")

response = httpx.post(WEBHOOK, json=content, timeout=10)
result = response.json()

print(f"飞书响应：{result}\n")

if result.get("code") == 0 or result.get("StatusCode") == 0:
    print("✅ 发送成功！")
else:
    print("❌ 发送失败")
    print(f"\n错误信息：{result.get('msg', 'Unknown')}")
    print("\n💡 请在飞书开放平台关闭签名校验:")
    print("   https://open.feishu.cn/app/cli_a93f27f7f4789bca")
    print("   应用管理 → 机器人 → 安全设置 → 关闭签名校验")
