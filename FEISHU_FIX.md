# 飞书通知修复指南

## ❌ 当前问题

飞书机器人返回错误：
```
code: 19021
msg: sign match fail or timestamp is not within one hour from current time
```

**原因：** 飞书强制要求签名校验，但时间戳不同步。

---

## ✅ 解决方案（推荐）

### 在飞书开放平台关闭签名校验

**步骤：**

1. **访问飞书开放平台**
   ```
   https://open.feishu.cn/app/cli_a93f27f7f4789bca
   ```

2. **进入安全设置**
   - 应用管理 → 选择你的机器人
   - 点击「机器人」标签
   - 找到「安全设置」

3. **关闭签名校验**
   - 找到「签名校验」选项
   - 关闭开关
   - 保存配置

4. **测试发送**
   ```bash
   cd /Users/ripple/work\ space/TradePilot
   python3 experiments/send_feishu_test.py
   ```

---

## 🔧 替代方案（如果不想关闭签名）

### 方案 1: 使用企业微信/钉钉

企业微信和钉钉的 Webhook 不需要复杂的签名校验。

**企业微信配置：**
1. 群聊 → 机器人 → 添加
2. 复制 Webhook URL
3. 更新 `config.yaml`

**钉钉配置：**
1. 群聊 → 机器人 → 添加自定义机器人
2. 复制 Webhook URL
3. 更新 `config.yaml`

### 方案 2: 使用邮件通知

配置 SMTP 发送邮件报告。

### 方案 3: 本地日志 + 定时查看

将报告保存到本地，定时查看。

---

## 📊 回测报告摘要

虽然飞书发送失败，但回测结果已保存到本地：

### 最优参数
```
MA:  5/15
RSI: 14/35/65
BB:  20/2.0
```

### 回测结果（365 天）

| 股票 | 收益率 | 交易次数 | 胜率 | 最终资金 |
|------|--------|---------|------|---------|
| 002022 科华生物 | **17.02%** | 4 笔 | 50.0% | ¥117,018 |
| 600309 万华化学 | **45.09%** | 8 笔 | 75.0% | ¥145,088 |

### 优化提升
- 002022: 0.00% → 17.02% (**+17.02%**)
- 600309: 17.70% → 45.09% (**+27.39%**)

---

## 📁 数据文件位置

```
/Users/ripple/work space/TradePilot/data/backtest/
├── 002022_SZ_optimized_result.json    # 002022 回测结果
├── 600309_SH_optimized_result.json    # 600309 回测结果
├── 002022_SZ_optimization.json        # 002022 参数优化
├── 600309_SH_optimization.json        # 600309 参数优化
└── backtest_results.db                # SQLite 数据库
```

---

## 🚀 快速测试飞书

关闭签名校验后，运行以下命令测试：

```bash
cd /Users/ripple/work\ space/TradePilot
python3 -c "
import httpx, os
WEBHOOK = os.environ['FEISHU_WEBHOOK_URL']   # 机密走环境变量，别写进文档/提交进仓库
content = {'msg_type': 'text', 'content': {'text': '测试消息'}}
r = httpx.post(WEBHOOK, json=content)
print('✅ 成功' if r.json().get('code') == 0 else '❌ 失败')
"
```

---

**最后更新：** 2026-03-15 02:35
