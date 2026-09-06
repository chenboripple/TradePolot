# 飞书机器人配置指南

## ✅ 当前状态

- **App ID**: `cli_a93f27f7f4789bca` ✅
- **App Secret**: 正确 ✅
- **Token 获取**: 成功 ✅
- **权限状态**: ❌ 需要开通

---

## 🔧 配置步骤（3 分钟完成）

### 步骤 1: 打开飞书开放平台

访问：https://open.feishu.cn/app/cli_a93f27f7f4789bca/auth

或者直接：
1. 打开 https://open.feishu.cn/
2. 登录企业账号
3. 点击「应用管理」
4. 找到应用 `cli_a93f27f7f4789bca`

---

### 步骤 2: 开通机器人权限

**方式 A: 自动开通（推荐）**

点击链接直接申请权限：
```
https://open.feishu.cn/app/cli_a93f27f7f4789bca/auth?q=im:chat:readonly,im:chat,im:chat.group_info:readonly,im:chat:read&op_from=openapi&token_type=tenant
```

**方式 B: 手动开通**

1. 在应用管理页面，点击左侧「权限管理」
2. 搜索并添加以下权限：
   - `im:chat` - 发送消息
   - `im:chat:readonly` - 读取群聊信息
   - `im:chat:read` - 读取消息
   - `im:chat.group_info:readonly` - 读取群组信息
3. 点击「申请」
4. 等待审核（通常即时通过）

---

### 步骤 3: 添加机器人到群聊

1. 打开飞书，创建一个群聊（或选择现有群）
2. 点击群设置（右上角 ⚙️）
3. 点击「机器人」
4. 点击「添加机器人」
5. 选择你的应用 `cli_a93f27f7f4789bca`
6. 点击「完成」

---

### 步骤 4: 获取群聊 ID

**方法 1: 自动获取（推荐）**

权限开通后，运行：
```bash
cd /Users/ripple/work\ space/TradePilot
source .venv/bin/activate
PYTHONPATH=src python3 experiments/test_feishu_direct.py
```

会显示机器人所在的群聊列表和 chat_id。

**方法 2: 手动获取**

1. 在飞书群聊中，点击群设置
2. 复制群 ID（通常在 URL 或群信息中）
3. 格式类似：`oc_XXXXXXXXXXXXXXXX`

---

### 步骤 5: 配置到 TradePilot

编辑 `config.yaml`：

```yaml
notifiers:
  feishu:
    enabled: true
    app_id: "cli_a93f27f7f4789bca"
    app_secret: "jyq6w22xNvN8QL9lyxHXDeBpTopzBKxR"
    chat_id: "oc_XXXXXXXXXXXXXXXX"  # ← 填入你的群聊 ID
```

---

## 🧪 测试

运行测试脚本：

```bash
cd /Users/ripple/work\ space/TradePilot
source .venv/bin/activate
PYTHONPATH=src python3 experiments/test_feishu.py
```

---

## ⚠️ 常见问题

### Q1: 权限申请后还是报错？
**A:** 等待 1-2 分钟，权限生效需要时间。或者重新获取 Token。

### Q2: 找不到「机器人」选项？
**A:** 
1. 确保应用已发布（不是草稿状态）
2. 点击「添加功能」→ 搜索「机器人」
3. 添加后配置机器人名称和头像

### Q3: 想用 Webhook 方式？
**A:** 
1. 应用管理 → 机器人 → Webhook 标签
2. 复制 Webhook URL
3. 填入 config.yaml 的 `webhook` 字段

### Q4: 想发送给个人而不是群？
**A:** 
1. 需要获取用户的 `open_id`
2. 运行测试脚本时会自动显示
3. 修改代码中的 `receive_id_type` 为 `open_id`

---

## 📞 需要帮助？

完成上述步骤后，告诉我：
1. 权限是否已开通
2. 群聊 ID（如果获取到了）

我会帮你完成最后的配置和测试！

---

**快速链接：**
- [飞书开放平台](https://open.feishu.cn/)
- [权限管理](https://open.feishu.cn/app/cli_a93f27f7f4789bca/auth)
- [机器人文档](https://open.feishu.cn/document/ukTMukTMukTM/uYjNwYjL2YDM14SMzATN)
