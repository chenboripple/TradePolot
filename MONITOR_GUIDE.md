# TradePilot 实时监控使用指南

## 📋 项目定位

**实时行情监控 + 策略分析 + 信号通知**，人工决策交易。

- ✅ 自动监控股票行情
- ✅ 自动运行交易策略
- ✅ 自动发送交易信号通知
- ❌ 不自动下单交易（由人工决策）

---

## 🚀 快速开始

### 1. 环境准备

```bash
cd /Users/ripple/work\ space/TradePilot

# 激活虚拟环境
source .venv/bin/activate
```

### 2. 测试单次检查

```bash
# 只检查一只标的并输出信号（单次，不循环）
tradepilot monitor 002022.SZ
```

**输出示例：**
```
002022.SZ 科华生物：⚪ 观望 @ 5.97
```

盘前/盘中简报也可以直接用：

```bash
PYTHONPATH=src python3 monitor_brief.py
```

### 3. 启动实时监控

```bash
# 启动监控（按 config.yaml 的 monitor.interval_seconds 循环检查）
tradepilot monitor
```

**输出示例：**
```
2026-03-13 10:30:00 - TradePilot - INFO - 🚀 TradePilot 启动监控...
2026-03-13 10:30:00 - TradePilot - INFO - 📊 监控标的数：6
2026-03-13 10:30:00 - TradePilot - INFO - ⏱️ 检查间隔：300 秒
2026-03-13 10:30:01 - TradePilot - INFO - 检查 002022.SZ，最新价格：5.97
```

### 4. 后台运行（可选）

```bash
# 使用 nohup 后台运行
nohup bash -c "source .venv/bin/activate && tradepilot monitor" > monitor.log 2>&1 &

# 查看日志
tail -f monitor.log

# 停止监控
pkill -f "tradepilot monitor"
```

---

## 📝 配置文件说明

配置文件：`config.yaml`

### 监控标的配置

每只标的通过 `strategy_profile` 指向一套策略参数（见下节）：

```yaml
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    asset_class: stock
    strategy_profile: "科华生物策略"

  - code: "600519.SH"
    name: "贵州茅台"
    asset_class: stock
    strategy_profile: "万华化学策略"   # 可复用已有配置
```

### 策略配置（strategy_profiles）

监控与 Web 看板共用 `combo_vote` 口径：MA 交叉 + RSI + 布林带三策略投票，
达到 `monitor.vote_threshold` 票同向才出信号：

```yaml
strategy_profiles:
  科华生物策略:
    kind: combo_vote
    ma_fast: 10
    ma_slow: 30
    rsi_period: 10
    rsi_oversold: 35    # 超卖线（买入）
    rsi_overbought: 65  # 超买线（卖出）
    bb_period: 20
    bb_std: 2.0
    # vote_threshold: 2   # 可选：覆盖全局阈值
```

### 监控频率与投票

```yaml
monitor:
  interval_seconds: 300        # 5 分钟检查一次
  report_interval_seconds: 3600  # 汇总报告最短间隔；个股买卖信号触发即推送
  use_watchlist: true          # Web 监控台观察池的股票也纳入监控
  vote_threshold: 2            # MA/RSI/BB 需几票同向才出信号（1~3）
  bar_freq: "1min"
  trading_hours:
    start: "09:30"
    end: "15:00"               # 非交易时段自动暂停
```

### 风控参考位（只读提示，不下单）

```yaml
risk:
  stop_loss_pct: 0.08     # 买入信号附参考止损价（-8%）
  take_profit_pct: 0.20   # 买入信号附参考止盈价（+20%）
```

---

## 🔔 通知配置

### 飞书机器人（主通道）

1. 在飞书群「设置 → 群机器人」添加自定义机器人
2. 获取 Webhook URL；建议开启「签名校验」并复制密钥
3. 配置到 `config.yaml`：

```yaml
notifiers:
  feishu:
    enabled: true
    webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_HOOK"
    secret: "YOUR_SECRET"       # 未开启签名校验则留空
    dashboard_url: "http://your-host:8000"  # 可选：信号卡片底部附监控台跳转按钮
```

飞书通知包含两类：
- **个股交易信号**：触发即推送（🔴 买入 / 🟢 卖出，A 股口径：红=买/涨，绿=卖/跌），附风控参考位；同标的同方向 1 小时内去重
- **定期汇总报告**：interactive 卡片，按 `monitor.report_interval_seconds` 限频，附「打开监控台」按钮

联调可用归档脚本验证 webhook：`PYTHONPATH=src python3 experiments/test_feishu.py`

### 企业微信机器人（可选）

```yaml
notifiers:
  wechat:
    enabled: true
    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY"
```

### 钉钉机器人（可选）

```yaml
notifiers:
  dingtalk:
    enabled: true
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
```

### 控制台输出（调试用）

信号与报告始终打印到控制台/日志（`monitor.log`），无需额外配置。

---

## 📊 策略说明

监控采用三策略投票（`combo_vote`），详见 `STRATEGIES.md`：

| 策略 | 看涨条件 | 看跌条件 | 擅长行情 |
|------|---------|---------|---------|
| **MA Cross** | 快线上穿慢线（金叉） | 快线下穿慢线（死叉） | 趋势市 |
| **RSI** | RSI < 超卖线 | RSI > 超买线 | 震荡市 |
| **Bollinger** | 触及下轨 | 触及上轨 | 震荡 + 突破 |

≥ `vote_threshold` 票同向 → 🔴 买入 / 🟢 卖出，否则 ⚪ 观望。

---

## 🛠️ 常见问题

### Q1: 提示"没有接口访问权限"
**A:** Tushare 基础账号（100 积分）权限有限，但足够获取日线数据。不要频繁调用 `stock_basic` 接口（限流 1 次/分钟）。

### Q2: 收不到通知
**A:** 检查：
1. 通知渠道是否启用（`enabled: true`）
2. Webhook URL 是否正确
3. 防火墙是否阻止出站请求
4. 查看日志：`tail -f monitor.log`

### Q3: 信号太多/太少
**A:** 优先调投票阈值，再调策略参数：
- 信号太多：调高 `monitor.vote_threshold`（如 2→3），或增大 `ma_slow`、放宽 RSI/布林带触发条件
- 信号太少：调低 `monitor.vote_threshold`（最低 1，接近"任一指标即触发"），或收紧参数
- 阈值口径与 Web 看板一致，改完两边同时生效

### Q4: 非交易时间也想监控
**A:** 监控在 `monitor.trading_hours` 之外自动暂停（避免无效轮询与 Tushare 限流浪费）。
确需延长时段可修改 `trading_hours.start/end`，但休市期间行情不变，信号不会更新。

---

## 📈 已实现 / 下一步

已实现（本指南旧版列为"优化建议"，现均已落地）：
- ✅ 三策略投票（MA + RSI + 布林带）
- ✅ Web 监控台（观察池、K 线、信号、回测记录）：`tradepilot serve`
- ✅ 统一撮合引擎回测 + walk-forward 样本外验证：`tradepilot backtest` / `tradepilot walkforward`
- ✅ 飞书 interactive 卡片（信号 + 定期汇总 + 监控台跳转）

待做：
1. **多周期监控**：同时监控日线/60 分钟/15 分钟
2. **信号留痕验证**：记录历史推送信号，事后统计命中率
3. **价格预警**：非策略信号，单纯价格突破提醒

---

## 📞 技术支持

遇到问题查看日志：
```bash
tail -f monitor.log
```

日志级别固定为 INFO（`src/ripple_tradePilot/monitor/main.py` 中 `logging.basicConfig`）；
需要更详细输出时临时改为 `logging.DEBUG` 后重启监控。

---

**🎯 记住：信号仅供参考，交易决策请自行判断！**
