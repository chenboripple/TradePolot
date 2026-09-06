# 科华生物 (002022.SZ) 深度策略研究 - 最终报告


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

**研究日期**: 2026-03-19  
**研究目标**: 寻找对科华生物更合适、相对敏感且尽可能高收益的策略

---

## 📋 执行摘要

本次研究对科华生物 (002022.SZ) 进行了深度策略分析，测试了 **42 个策略**，覆盖 **6 个时间周期**（5y/3y/1y/6m/3m/1m），重点加权近期表现（1y 35%、6m 20%、3m 15%）。

### 🏆 核心发现

**最佳策略**: `Grid_5_15_6_20_60_20_2.0_1`

这是一个组合策略，配置如下：
- **MA 交叉**: fast=5, slow=15
- **RSI**: period=6, oversold=20, overbought=60
- **布林带**: period=20, std_dev=2.0
- **投票阈值**: 1 (任一策略信号即可交易)

---

## 📊 最佳策略详细表现

### 各周期表现

| 周期 | 收益率 | 最大回撤 | 交易次数 | 胜率 | Sharpe | 最终资金 |
|------|--------|----------|----------|------|--------|----------|
| **5y** | -49.91% | 74.10% | 68 | 54.4% | -0.37 | ¥50,089 |
| **3y** | -7.14% | 49.23% | 39 | 59.0% | 0.04 | ¥92,858 |
| **1y** | **+49.19%** | 7.38% | 16 | 75.0% | 2.66 | ¥149,192 |
| **6m** | **+22.26%** | 7.40% | 9 | 88.9% | 3.36 | ¥122,256 |
| **3m** | **+22.15%** | 2.85% | 4 | 100.0% | 3.99 | ¥122,153 |
| **1m** | **+6.75%** | 0.72% | 1 | 100.0% | 6.89 | ¥106,755 |

### 关键洞察

1. **短期表现优异**: 1y/6m/3m 周期均取得显著正收益，Sharpe 比率 > 2.5
2. **长期表现疲软**: 5y/3y 周期表现不佳，说明策略更适合短期波段
3. **风险控制良好**: 重点周期 (1y/6m/3m) 最大回撤控制在 7.4% 以内
4. **高胜率**: 近期周期胜率 75%-100%，信号质量高

---

## 🔄 与当前配置对比

### 当前配置 (config.yaml)

```yaml
grid_combo_002022:
  kind: "grid_combo"
  ma: { fast: 5, slow: 20 }
  rsi: { period: 6, oversold: 20, overbought: 60 }
  bb: { period: 20, std_dev: 2.0 }
```

### 推荐新配置

```yaml
grid_combo_002022:
  kind: "grid_combo"
  ma: { fast: 5, slow: 15 }      # 慢线从 20 改为 15，更敏感
  rsi: { period: 6, oversold: 20, overbought: 60 }  # 保持不变
  bb: { period: 20, std_dev: 2.0 }  # 保持不变
```

### 性能对比 (1y 周期)

| 指标 | 当前配置 | 推荐配置 | 改善 |
|------|----------|----------|------|
| 收益率 | ~35.8% | **+49.2%** | **+37.4%** |
| 最大回撤 | ~6.4% | 7.4% | -15.6% |
| 交易次数 | ~14 | 16 | +14.3% |
| 胜率 | ~64.3% | **75.0%** | **+16.6%** |
| Sharpe | ~2.12 | **2.66** | **+25.5%** |

> 注：当前配置数据来自 `data/backtest/002022_SZ_optimized_params.json`

---

## 🥈 Top 5 策略排名

| 排名 | 策略名称 | 类型 | 综合评分 | 1y 收益 | 6m 收益 | 3m 收益 |
|------|----------|------|----------|---------|---------|---------|
| 1 | `Grid_5_15_6_20_60_20_2.0_1` | 组合 | **52.82** | +49.19% | +22.26% | +22.15% |
| 2 | `Grid_5_20_6_20_60_20_2.0_1` | 组合 | 40.35 | +38.37% | +9.79% | +20.78% |
| 3 | `BB_20_2.0` | 布林带 | 31.10 | +32.93% | +11.55% | +12.89% |
| 4 | `MA_5_15` | MA 交叉 | 23.69 | +31.52% | +10.23% | +11.45% |
| 5 | `RSI_14_30_70` | RSI | 19.24 | +18.67% | +8.92% | +5.34% |

---

## 💡 策略接入 TradePilot 建议

### 方案 A: 直接更新配置 (推荐)

修改 `config.yaml` 中的策略配置：

```yaml
strategies:
  profiles:
    grid_combo_002022:
      kind: "grid_combo"
      ma: { fast: 5, slow: 15 }      # ← 修改这里
      rsi: { period: 6, oversold: 20, overbought: 60 }
      bb: { period: 20, std_dev: 2.0 }
```

**优点**: 简单直接，立即生效  
**风险**: 需要监控实盘表现

### 方案 B: 新增策略画像

在 `config.yaml` 中新增一个策略画像：

```yaml
strategies:
  profiles:
    grid_combo_002022:
      kind: "grid_combo"
      ma: { fast: 5, slow: 20 }
      rsi: { period: 6, oversold: 20, overbought: 60 }
      bb: { period: 20, std_dev: 2.0 }
    
    grid_combo_002022_v2:  # ← 新增
      kind: "grid_combo"
      ma: { fast: 5, slow: 15 }
      rsi: { period: 6, oversold: 20, overbought: 60 }
      bb: { period: 20, std_dev: 2.0 }
```

然后修改监控配置：

```yaml
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    strategy_profile: "grid_combo_002022_v2"  # ← 使用新配置
    notify_on: ["BUY", "SELL"]
```

**优点**: 保留原配置，可随时回退  
**风险**: 需要手动切换

### 方案 C: 多策略并行 (高级)

同时运行多个策略，根据市场状态动态切换：

```python
# 示例伪代码
if market_volatility < threshold:
    use_strategy("grid_combo_002022_v2")  # 敏感策略
else:
    use_strategy("grid_combo_002022")     # 保守策略
```

**优点**: 自适应市场  
**风险**: 实现复杂，需要额外开发

---

## ⚠️ 风险提示

1. **过拟合风险**: 最佳策略在 1y/6m/3m 表现优异，但 5y/3y 表现较差，可能存在短期过拟合
2. **市场变化**: 历史回测不代表未来表现，需持续监控
3. **建议风控**:
   - 单笔交易最大仓位：≤30%
   - 总最大回撤止损：≤15%
   - 定期 (月度) 重新评估策略表现

---

## 📁 新增文件清单

### 策略实现 (6 个)

| 文件 | 描述 |
|------|------|
| `src/ripple_tradePilot/strategies/donchian.py` | Donchian 通道突破策略 |
| `src/ripple_tradePilot/strategies/dual_thrust.py` | Dual Thrust 双阈策略 |
| `src/ripple_tradePilot/strategies/atr_channel.py` | ATR 通道策略 |
| `src/ripple_tradePilot/strategies/macd.py` | MACD 策略 (含零轴过滤) |
| `src/ripple_tradePilot/strategies/mean_reversion.py` | 均值回归改进版策略 |
| `src/ripple_tradePilot/strategies/trend_filter.py` | 趋势过滤策略及包装器 |

### 研究脚本 (1 个)

| 文件 | 描述 |
|------|------|
| `research_kehua_deep.py` | 多周期深度研究脚本 (可复用) |

### 结果文件 (2 个)

| 文件 | 描述 |
|------|------|
| `data/backtest/kehua_deep_research.json` | 完整回测结果 (JSON) |
| `reports/kehua_deep_research_summary.md` | 策略研究摘要 |
| `reports/KEHUA_FINAL_REPORT.md` | 本最终报告 |

---

## 🎯 下一步行动

1. **立即**: 更新 `config.yaml` 中的科华生物策略配置
2. **本周**: 在模拟盘/小仓位实盘测试新配置
3. **每月**: 重新运行 `research_kehua_deep.py` 评估策略表现
4. **季度**: 根据市场变化调整策略参数

---

**报告生成时间**: 2026-03-19 00:20:45  
**研究工具**: TradePilot 深度策略研究框架  
**数据源**: Tushare Pro
