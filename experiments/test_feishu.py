#!/usr/bin/env python3
"""
飞书机器人测试脚本

⚠️ 重要提示：
飞书自建应用需要 Webhook URL 才能发送消息。
App ID + App Secret 主要用于获取 tenant_access_token，
但发送消息还需要知道发送给谁（chat_id 或 webhook）。

获取 Webhook 步骤：
1. 打开飞书开放平台：https://open.feishu.cn/
2. 进入你的应用管理页面
3. 点击「机器人」或「Webhook」
4. 复制 Webhook 地址（形如：https://open.feishu.cn/open-apis/bot/v2/hook/xxx）
5. 将 Webhook 地址填入下方 WEBHOOK_URL
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier

# ⚠️ 请在此填入你的飞书 Webhook URL
# 获取方法：飞书开放平台 → 应用管理 → 机器人 → Webhook
WEBHOOK_URL = ""  # ← 填入你的 webhook 地址

# 从配置读取
import yaml
config_path = Path(__file__).parent / "config.yaml"
if config_path.exists():
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        feishu_config = config.get('notifiers', {}).get('feishu', {})
        WEBHOOK_URL = feishu_config.get('webhook', '')
        FEISHU_SECRET = feishu_config.get('secret', '')
else:
    WEBHOOK_URL = ""
    FEISHU_SECRET = ""

print("="*70)
print("📱 飞书机器人测试")
print("="*70)

if not WEBHOOK_URL:
    print("\n❌ 错误：未配置 Webhook URL")
    print("\n📝 获取 Webhook 步骤：")
    print("   1. 打开飞书开放平台：https://open.feishu.cn/")
    print("   2. 进入「应用管理」→ 选择你的应用 (cli_a93f27f7f4789bca)")
    print("   3. 点击「机器人」或「Webhook」")
    print("   4. 复制 Webhook 地址")
    print("   5. 运行：echo 'WEBHOOK_URL=你的 webhook' >> .env")
    print("\n或者在 config.yaml 中添加:")
    print("   notifiers:")
    print("     feishu:")
    print("       webhook: \"https://open.feishu.cn/open-apis/bot/v2/hook/xxx\"")
    sys.exit(1)

print(f"\n✅ Webhook URL: {WEBHOOK_URL[:50]}...")
print(f"🔐 签名校验：{'✅ 已启用' if FEISHU_SECRET else '❌ 未启用'}")

# 创建通知器（带签名）
notifier = FeishuWebhookNotifier(WEBHOOK_URL, FEISHU_SECRET)

# 创建测试消息
test_bar = Bar(
    timestamp=datetime.now(),
    open=5.95,
    high=6.02,
    low=5.90,
    close=5.97,
    volume=5325000
)

print("\n📊 发送测试消息...")
print(f"   标的：科华生物 (002022.SZ)")
print(f"   信号：BUY")
print(f"   价格：5.97 元")

# 发送测试
success = notifier.send(
    symbol="002022.SZ",
    name="科华生物",
    side=Side.BUY,
    price=5.97,
    strategy="MA Cross + RSI + BB",
    bar=test_bar,
    extra_info={
        "MA5": "5.98",
        "MA20": "6.06",
        "RSI": "43.10"
    }
)

print("\n" + "="*70)
if success:
    print("✅ 测试成功！飞书机器人已配置完成")
else:
    print("❌ 测试失败，请检查 Webhook URL 是否正确")
    print("\n常见问题:")
    print("   1. Webhook URL 是否正确复制")
    print("   2. 机器人是否已添加到群聊")
    print("   3. 飞书开放平台的应用状态是否正常")
print("="*70)
