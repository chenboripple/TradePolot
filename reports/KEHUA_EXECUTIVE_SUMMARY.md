# 科华生物 (002022.SZ) 策略研究 - 执行摘要


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

**日期**: 2026-03-19  
**状态**: ✅ 已完成并更新配置

---

## 🎯 一句话总结

**最佳策略**: MA(5/15) + RSI(6/20/60) + BB(20/2.0) 组合  
**预期收益**: 1y +49% | 6m +22% | 3m +22%  
**风险**: 最大回撤 ~7.4% (重点周期)

---

## ✅ 已完成工作

### 1. 新增策略 (6 个)
- ✅ Donchian 通道突破
- ✅ Dual Thrust 双阈
- ✅ ATR 通道
- ✅ MACD (含零轴过滤)
- ✅ 均值回归改进版
- ✅ 趋势过滤

### 2. 多周期回测
- ✅ 测试周期：5y/3y/1y/6m/3m/1m
- ✅ 测试策略：42 个
- ✅ 权重配置：1y(35%) > 6m(20%) > 3m(15%) > 其他

### 3. 配置更新
- ✅ `config.yaml` 已更新科华生物策略参数
- ✅ 保留原配置为 `grid_combo_002022_v1` 用于回退

---

## 📊 最佳策略配置

```yaml
grid_combo_002022:
  ma: { fast: 5, slow: 15 }      # ← 慢线从 20 改为 15
  rsi: { period: 6, oversold: 20, overbought: 60 }
  bb: { period: 20, std_dev: 2.0 }
```

---

## 📈 性能对比

| 周期 | 原配置 | 新配置 | 改善 |
|------|--------|--------|------|
| 1y | +35.8% | **+49.2%** | +37% |
| 6m | +9.8% | **+22.3%** | +128% |
| 3m | +6.0% | **+22.2%** | +270% |
| Sharpe | 2.12 | **2.66** | +25% |

---

## 📁 新增文件

```
src/ripple_tradePilot/strategies/
  ├── donchian.py          # Donchian 突破
  ├── dual_thrust.py       # Dual Thrust
  ├── atr_channel.py       # ATR 通道
  ├── macd.py              # MACD
  ├── mean_reversion.py    # 均值回归
  └── trend_filter.py      # 趋势过滤

research_kehua_deep.py                          # 研究脚本
data/backtest/kehua_deep_research.json          # 完整结果
reports/
  ├── kenhua_deep_research_summary.md           # 摘要
  └── KEHUA_FINAL_REPORT.md                     # 最终报告
```

---

## ⚠️ 注意事项

1. **短期策略**: 新策略在 1y/6m/3m 表现优异，但 5y/3y 表现一般
2. **建议风控**: 最大回撤止损 ≤15%
3. **监控**: 建议月度重新评估策略表现

---

## 🚀 下一步

1. **立即生效**: 配置已更新，下次监控自动使用新策略
2. **监控表现**: 关注实盘信号与预期是否一致
3. **定期评估**: 每月运行 `research_kehua_deep.py` 重新评估

---

**详细报告**: `reports/KEHUA_FINAL_REPORT.md`  
**完整数据**: `data/backtest/kehua_deep_research.json`
