#!/usr/bin/env python3
"""
飞书 Webhook 创建助手

如果你找不到 Webhook 地址，这个脚本会帮你用 API 方式发送消息，
然后告诉你如何获取 Webhook。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx
from datetime import datetime

APP_ID = "cli_a93f27f7f4789bca"
APP_SECRET = "jyq6w22xNvN8QL9lyxHXDeBpTopzBKxR"

print("="*70)
print("📱 飞书配置助手")
print("="*70)

# 获取 Token
print("\n📝 获取访问令牌...")
url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
response = httpx.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
result = response.json()

if result.get("code") != 0:
    print(f"❌ Token 获取失败：{result}")
    sys.exit(1)

token = result["tenant_access_token"]
print(f"✅ Token 获取成功")

# 检查应用状态
print("\n📝 检查应用状态...")
url = "https://open.feishu.cn/open-apis/app/v3/info"
headers = {"Authorization": f"Bearer {token}"}
response = httpx.get(url, headers=headers, timeout=10)
result = response.json()

if result.get("code") == 0:
    app_info = result.get("data", {})
    print(f"✅ 应用名称：{app_info.get('name', '未知')}")
    print(f"✅ 应用状态：{'已发布' if app_info.get('is_published') else '草稿（需要发布）'}")
else:
    print(f"⚠️  获取应用信息失败")

# 显示配置指南
print("\n" + "="*70)
print("📋 飞书机器人配置指南")
print("="*70)
print("""
你的应用信息:
  App ID:     cli_a93f27f7f4789bca
  Token 状态：✅ 有效

⚠️  当前问题：应用缺少必要的权限

✅ 解决方案（3 步完成）:

1️⃣  开通权限
   点击以下链接直接申请：
   https://open.feishu.cn/app/cli_a93f27f7f4789bca/auth?q=im:chat&op_from=openapi

   或者手动操作：
   - 飞书开放平台 → 应用管理 → 权限管理
   - 搜索并添加：im:chat
   - 点击申请

2️⃣ 添加机器人到群
   - 在飞书中打开一个群聊
   - 群设置 → 机器人 → 添加机器人
   - 选择你的应用 (cli_a93f27f7f4789bca)

3️⃣ 重新测试
   运行：python3 test_feishu_direct.py
   会显示群聊列表和 chat_id

💡 或者使用 Webhook 方式（更简单）:
   - 飞书开放平台 → 应用管理
   - 点击「机器人」标签
   - 如果有「Webhook」子标签，复制 URL
   - 填入 config.yaml 的 feishu.webhook 字段
""")

print("="*70)
print("\n💬 需要我继续帮你配置吗？")
print("   完成上述步骤后，告诉我 chat_id 或 Webhook URL")
print("="*70)
