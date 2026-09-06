# TradePilot 回测功能实现总结


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

## ✅ 已完成功能

### 1. 回测模块

**文件：** `src/ripple_tradePilot/backtest/runner.py`

**功能：**
- ✅ 调用 Tushare 获取历史数据
- ✅ 三策略组合回测（MA + RSI + BB）
- ✅ 计算收益指标（总收益、年化、回撤、夏普）
- ✅ 计算交易统计（交易次数、胜率）
- ✅ 保存回测结果（JSON + CSV + SQLite）
- ✅ 发送飞书通知

---

### 2. 回测脚本

**文件：** `examples/run_backtest.py`

**用法：**
```bash
# 默认回测 002022 过去 90 天
python3 examples/run_backtest.py

# 自定义股票和天数
python3 examples/run_backtest.py 002022.SZ 科华生物 90
python3 examples/run_backtest.py 600519.SH 贵州茅台 180
```

---

### 3. 数据存储

**目录：** `data/backtest/`

**存储内容：**
- `*.json` - 回测结果（完整数据）
- `*_trades.csv` - 交易记录
- `backtest_results.db` - SQLite 数据库（汇总数据）

---

## 📊 回测结果（002022 科华生物，90 天）

### 回测区间
- **开始：** 2025-12-15
- **结束：** 2026-03-15
- **数据条数：** 57 条

### 回测结果
| 指标 | 数值 |
|------|------|
| 初始资金 | ¥100,000.00 |
| 最终资金 | ¥100,000.00 |
| 总收益率 | 0.00% |
| 年化收益 | 0.00% |
| 最大回撤 | 0.00% |
| 夏普比率 | 0.00 |
| 交易次数 | 0 |
| 胜率 | 0.00% |

### 信号分析
- **总信号数：** 1 个
- **买入信号：** 0 个
- **卖出信号：** 1 个（2026-02-04，6.24 元）

**结果解读：**
过去 3 个月科华生物股价在 5.6-6.2 元区间震荡，三策略组合未产生明确的买入信号（需要≥2 个策略同时看涨）。这说明：
1. 市场处于震荡期，无明确趋势
2. 策略参数相对保守，避免频繁交易
3. 空仓观望是合理的选择

---

## 📱 飞书通知

### 通知内容
```
📊 TradePilot 回测报告

标的：科华生物 (002022.SZ)
区间：2025-12-15 至 2026-03-15

📈 收益情况:
• 初始资金：¥100,000.00
• 最终资金：¥100,000.00
• 总收益率：0.00%

💹 交易统计:
• 交易次数：0
• 胜率：0.00%
• 信号数：1

⚠️ 历史回测不代表未来表现
```

### 发送状态
✅ 已成功发送到飞书群聊

---

## 🔧 策略参数

### 默认参数（保守型）
```yaml
ma_cross:
  fast: 5
  slow: 20

rsi:
  period: 14
  oversold: 30
  overbought: 70

bollinger:
  period: 20
  std_dev: 2.0
```

### 回测参数（敏感型）
```yaml
ma_cross:
  fast: 3
  slow: 10

rsi:
  period: 10
  oversold: 35
  overbought: 65

bollinger:
  period: 14
  std_dev: 1.8
```

---

## 📁 文件结构

```
ripple_tradePilot/
├── src/ripple_tradePilot/
│   ├── backtest/
│   │   ├── engine.py          # 原有回测引擎
│   │   ├── report.py          # 原有绩效报告
│   │   └── runner.py          # ⭐新增：完整回测系统
│   ├── data/
│   │   └── tushare_loader.py  # Tushare 数据接口
│   ├── strategies/
│   │   ├── moving_average.py  # MA 策略
│   │   ├── rsi.py             # RSI 策略
│   │   └── bollinger.py       # 布林带策略
│   └── notifiers/
│       └── feishu.py          # 飞书通知
├── data/
│   └── backtest/              # ⭐新增：回测数据存储
│       ├── *.json             # 回测结果
│       ├── *_trades.csv       # 交易记录
│       └── backtest_results.db # SQLite 数据库
├── examples/run_backtest.py            # ⭐新增：回测脚本
├── experiments/quick_backtest.py          # ⭐新增：简化回测
└── config.yaml                # 配置文件
```

---

## 🚀 使用示例

### 示例 1: 回测单只股票
```bash
cd /Users/ripple/work\ space/TradePilot
source .venv/bin/activate
PYTHONPATH=src python3 examples/run_backtest.py 002022.SZ 科华生物 90
```

### 示例 2: 批量回测多只股票
```bash
# 创建批量回测脚本
cat > batch_backtest.sh << 'EOF'
#!/bin/bash
python3 examples/run_backtest.py 002022.SZ 科华生物 90
python3 examples/run_backtest.py 600309.SH 万华化学 90
python3 examples/run_backtest.py 603039.SH 泛海微 90
python3 examples/run_backtest.py 000999.SZ 华润三九 90
python3 examples/run_backtest.py 000868.SZ 安凯客车 90
python3 examples/run_backtest.py 601816.SH 京沪高铁 90
EOF

chmod +x batch_backtest.sh
./batch_backtest.sh
```

### 示例 3: 查询历史回测结果
```bash
cd /Users/ripple/work\ space/TradePilot
source .venv/bin/activate
PYTHONPATH=src python3 -c "
import sqlite3
conn = sqlite3.connect('data/backtest/backtest_results.db')
df = pd.read_sql_query('SELECT * FROM backtest_results ORDER BY created_at DESC', conn)
print(df[['symbol', 'name', 'total_return', 'total_trades', 'win_rate']])
conn.close()
"
```

---

## 💡 优化建议

### 1. 策略优化
- 添加参数网格搜索功能
- 支持策略参数优化（贝叶斯优化/遗传算法）
- 添加更多策略（MACD、KDJ 等）

### 2. 回测增强
- 支持多标的组合回测
- 添加基准对比（沪深 300 等）
- 支持不同仓位管理策略

### 3. 性能分析
- 添加交易成本分析
- 支持不同手续费/滑点设置
- 添加月度/年度收益分析

### 4. 可视化
- 资金曲线图
- 收益分布图
- 回撤时间序列

---

## ⚠️ 注意事项

1. **历史回测不代表未来表现** - 回测结果仅供参考
2. **幸存者偏差** - 回测的股票可能已经退市
3. **过拟合风险** - 避免过度优化参数
4. **交易成本** - 已包含手续费和滑点，但可能不完整
5. **数据质量** - 依赖 Tushare 数据准确性

---

## 📞 故障排查

### 问题 1: 无交易信号
**原因：** 策略参数保守或市场震荡
**解决：** 调整策略参数或延长回测周期

### 问题 2: 飞书通知失败
**检查：**
```bash
PYTHONPATH=src python3 experiments/test_feishu.py
```

### 问题 3: 数据获取失败
**检查：**
```bash
PYTHONPATH=src python3 -c "
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
loader = TushareDataLoader('你的 token')
bars = list(loader.load_bars('002022.SZ'))
print(f'获取到{len(bars)}条数据')
"
```

---

**最后更新：** 2026-03-15 02:15
**状态：** ✅ 功能完整，已发送飞书报告
