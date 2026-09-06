#!/usr/bin/env python3
"""
飞书机器人测试 - 直接使用 App ID + App Secret

步骤：
1. 确保应用已开通「机器人」权限
2. 将机器人添加到目标群聊
3. 获取群聊 ID
4. 运行测试
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx
from ripple_tradePilot.models.types import Bar, Side

# 配置
APP_ID = "cli_a93f27f7f4789bca"
APP_SECRET = "jyq6w22xNvN8QL9lyxHXDeBpTopzBKxR"

print("="*70)
print("📱 飞书机器人配置助手")
print("="*70)

# 步骤 1: 获取 tenant_access_token
print("\n📝 步骤 1: 获取访问令牌...")
url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
payload = {
    "app_id": APP_ID,
    "app_secret": APP_SECRET
}

try:
    response = httpx.post(url, json=payload, timeout=10)
    result = response.json()
    
    if result.get("code") != 0:
        print(f"❌ 获取 Token 失败：{result}")
        print("\n可能原因:")
        print("   1. App ID 或 App Secret 错误")
        print("   2. 应用未发布")
        print("   3. 应用权限不足")
        sys.exit(1)
    
    token = result["tenant_access_token"]
    expire = result["expire"]
    print(f"✅ Token 获取成功")
    print(f"   有效期：{expire}秒")
    
except Exception as e:
    print(f"❌ 请求失败：{e}")
    sys.exit(1)

# 步骤 2: 获取机器人信息
print("\n📝 步骤 2: 获取机器人信息...")
url = "https://open.feishu.cn/open-apis/bot/v3/info"
headers = {
    "Authorization": f"Bearer {token}"
}

try:
    response = httpx.get(url, headers=headers, timeout=10)
    result = response.json()
    
    if result.get("code") == 0:
        bot_info = result["data"]
        print(f"✅ 机器人信息:")
        print(f"   名称：{bot_info.get('display_name', '未知')}")
        print(f"   App ID: {bot_info.get('app_id', '未知')}")
    else:
        print(f"⚠️  获取机器人信息失败：{result}")
        print("   可能未开通机器人功能")
        
except Exception as e:
    print(f"❌ 请求失败：{e}")

# 步骤 3: 获取群聊列表（需要机器人已加入群）
print("\n📝 步骤 3: 获取机器人所在的群聊...")
url = "https://open.feishu.cn/open-apis/im/v1/chats"
headers = {
    "Authorization": f"Bearer {token}"
}
params = {
    "page_size": 20
}

try:
    response = httpx.get(url, headers=headers, params=params, timeout=10)
    result = response.json()
    
    if result.get("code") == 0:
        chats = result.get("data", {}).get("items", [])
        if chats:
            print(f"✅ 找到 {len(chats)} 个群聊:")
            for i, chat in enumerate(chats[:5], 1):  # 显示前 5 个
                print(f"   {i}. {chat.get('name', '未知群')} (chat_id: {chat.get('chat_id', '未知')})")
            
            if len(chats) > 5:
                print(f"   ... 还有 {len(chats) - 5} 个群")
            
            print("\n💡 请复制一个 chat_id，然后告诉我，我帮你发送测试消息")
            print("   例如：oc_XXXXXXXXXXXXXXXX")
        else:
            print("⚠️  机器人还未加入任何群聊")
            print("\n📝 下一步:")
            print("   1. 在飞书中创建一个群聊")
            print("   2. 添加机器人到群:")
            print("      - 群设置 → 机器人 → 添加机器人")
            print("      - 选择你的应用 (cli_a93f27f7f4789bca)")
            print("   3. 重新运行此脚本")
    else:
        print(f"⚠️  获取群聊失败：{result}")
        print("   可能需要开通「群组」权限")
        
except Exception as e:
    print(f"❌ 请求失败：{e}")

# 步骤 4: 说明
print("\n" + "="*70)
print("📋 配置说明")
print("="*70)
print("""
方式 1: 使用 chat_id（当前方式）
  - 优点：功能完整，可发送富文本
  - 缺点：需要获取 chat_id

方式 2: 使用 Webhook（更简单）
  - 获取方法：
    1. 飞书开放平台 → 应用管理
    2. 点击「机器人」标签
    3. 点击「Webhook」子标签
    4. 复制 Webhook 地址
  - 优点：配置简单，无需 chat_id
  - 缺点：功能受限

方式 3: 使用用户 ID（私聊）
  - 需要获取用户的 open_id
  - 适合个人通知
""")

print("\n💡 下一步:")
print("   1. 如果上面显示了群聊列表 → 告诉我 chat_id，我发送测试")
print("   2. 如果显示'未加入任何群' → 先添加机器人到群")
print("   3. 如果想用 Webhook → 去开放平台找 Webhook 地址")
print("="*70)
