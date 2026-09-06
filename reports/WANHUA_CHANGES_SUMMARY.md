# 万华化学 (600309.SH) 四窗口策略搜索 - 变更总结


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

**日期**: 2026-03-19  
**执行者**: 自动化研究脚本

---

## 📊 研究概述

对万华化学 (600309.SH) 进行了四窗口深度策略搜索，测试了 88 个策略变体，涵盖：
- MA 交叉策略 (9 个变体)
- RSI 策略 (10 个变体)
- 布林带策略 (8 个变体)
- Donchian 突破策略 (10 个变体)
- Dual Thrust 策略 (8 个变体)
- ATR 通道策略 (8 个变体)
- MACD 策略 (15 个变体)
- 均值回归策略 (8 个变体)
- 趋势过滤组合策略 (6 个变体)
- Grid Combo 组合策略 (6 个变体)

**测试窗口**: 1 年、6 个月、3 个月、1 个月  
**权重配置**: 1y(50%)、6m(25%)、3m(20%)、1m(5%)

---

## 🏆 最佳策略

**策略名称**: `MA_5_15` (MA 交叉策略)

**参数**:
- 快线 MA: 5 日
- 慢线 MA: 15 日

**四窗口表现**:

| 窗口 | 收益率 | 最大回撤 | 交易次数 | 胜率 | Sharpe |
|------|--------|----------|----------|------|--------|
| 1y | +36.91% | -11.41% | 8 | 75.0% | 1.36 |
| 6m | +14.17% | -9.24% | 4 | 75.0% | 1.04 |
| 3m | +8.48% | -8.08% | 2 | 100.0% | 1.14 |
| 1m | -1.84% | -2.93% | 1 | 0.0% | -2.08 |

**综合评分**: 47.43 (88 个策略中排名第 1)

---

## 📝 修改文件清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `research_wanhua_4window.py` | 四窗口研究脚本 (19.7KB) |
| `data/backtest/wanhua_4window_research.json` | 详细回测数据 (88 个策略) |
| `reports/wanhua_4window_research_summary.md` | 简要总结 |
| `reports/WANHUA_4WINDOW_FINAL_SUMMARY.md` | 最终报告 |
| `reports/WANHUA_CHANGES_SUMMARY.md` | 本变更总结 |

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `config.yaml` | 更新 600309.SH 策略配置，新增 ma_600309 策略画像 |
| `src/ripple_tradePilot/monitor/main.py` | 新增 `_run_ma_profile()` 方法，支持 MA 策略类型 |

---

## 🔧 config.yaml 变更详情

### 策略画像部分

**新增**:
```yaml
# 万华化学 - 原配置 (保留用于回退)
grid_combo_600309_v1:
  kind: "grid_combo"
  ma: { fast: 10, slow: 34 }
  rsi: { period: 16, oversold: 35, overbought: 65 }
  bb: { period: 26, std_dev: 1.8 }

# 万华化学 - 优化配置 (2026-03-19 四窗口深度研究推荐)
ma_600309:
  kind: "ma"
  ma: { fast: 5, slow: 15 }
```

**更新记录**:
```yaml
# 更新记录：
# - 2026-03-19 - 科华生物策略优化 (MA slow: 20→15，基于深度研究)
# - 2026-03-19 - 安凯客车策略优化 (Grid→RSI，基于四窗口研究：1y 收益 -16%→+21%，回撤 28%→7%)
# - 2026-03-19 - 万华化学策略优化 (Grid Combo→MA，基于四窗口研究：简化策略，四窗口综合评分最高)
# - 2026-03-19 - 泛海微策略优化 (MACD(12,20,7)，基于四窗口研究：1y 收益 +29.46%，回撤 19.23%)
```

### 标的配置部分

**变更前**:
```yaml
- code: "600309.SH"
  name: "万华化学"
  strategy_profile: "grid_combo_600309"
  notify_on: ["BUY", "SELL"]
```

**变更后**:
```yaml
- code: "600309.SH"
  name: "万华化学"
  strategy_profile: "ma_600309"
  notify_on: ["BUY", "SELL"]
```

---

## 🔧 monitor/main.py 变更详情

**新增方法**: `_run_ma_profile()`

```python
def _run_ma_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
    """运行 MA 交叉策略"""
    ma_cfg = profile.get('ma', {})
    ma_strategy = MovingAverageCross(
        fast=ma_cfg.get('fast', 5),
        slow=ma_cfg.get('slow', 20),
    )
    
    latest_bar = bars[-1]
    ma_strategy.reset()
    for bar in bars[:-1]:
        ma_strategy.on_bar(bar)
    ma_signal = ma_strategy.on_bar(latest_bar)
    
    signals = {'ma': ma_signal}
    strongest_signal = ('ma', ma_signal) if ma_signal.side else None
    
    if ma_signal.side == Side.BUY:
        recommendation = '🟢 买入'
    elif ma_signal.side == Side.SELL:
        recommendation = '🔴 卖出'
    else:
        recommendation = '⚪ 观望'
    
    return signals, strongest_signal, recommendation
```

**更新调度逻辑**:
```python
elif profile_kind == 'ma':
    signals, strongest_signal, recommendation = self._run_ma_profile(profile, bars)
```

---

## ✅ 603039.SH 配置确认

**泛海微 (603039.SH)** 配置已验证正确：

```yaml
- code: "603039.SH"
  name: "泛海微"
  strategy_profile: "breakout_603039"

breakout_603039:
  kind: "macd"
  macd:
    fast: 12
    slow: 20
    signal: 7
    zero_cross: false
```

**四窗口表现** (来自 603039_SH_4window_research.json):
- 1y: +29.46% (DD: -19.23%, Trades: 8, Sharpe: 0.97)
- 6m: +12.74% (DD: -18.13%, Trades: 5, Sharpe: 0.88)
- 3m: +30.58% (DD: -7.02%, Trades: 1, Sharpe: 3.12)
- 1m: 0.00% (无交易)

该配置可被 `monitor/main.py` 与 `monitor_brief.py` 正常使用。

---

## 📈 策略对比

### 原策略 (grid_combo_600309_v1)
- **类型**: Grid Combo (MA + RSI + BB 投票组合)
- **参数**: MA(10,34) + RSI(16,35,65) + BB(26,1.8)
- **历史表现** (2025-03-15 至 2026-03-15):
  - 收益率: +45.09%
  - 交易次数: 8
  - 胜率: 75.0%
- **缺点**: 策略复杂，参数多，维护成本高

### 新策略 (ma_600309)
- **类型**: 纯 MA 交叉
- **参数**: MA(5,15)
- **1 年回测表现**:
  - 收益率: +36.91%
  - 最大回撤: -11.41%
  - 交易次数: 8
  - 胜率: 75.0%
  - Sharpe: 1.36
- **优点**: 策略简单，参数少，四窗口表现均衡

### 对比结论

| 指标 | 原策略 | 新策略 | 变化 |
|------|--------|--------|------|
| 1y 收益率 | +45.09% | +36.91% | -8.18% |
| 1y 交易次数 | 8 | 8 | 0 |
| 1y 胜率 | 75.0% | 75.0% | 0 |
| 6m 收益率 | N/A | +14.17% | ✓ |
| 3m 收益率 | N/A | +8.48% | ✓ |
| 1m 收益率 | N/A | -1.84% | 小幅亏损 |
| 策略复杂度 | 高 | 低 | 简化 |
| 综合评分 | N/A | 47.43 | 88 策略中第 1 |

**建议**: 新策略虽然 1y 绝对收益略低，但策略更简单、四窗口表现均衡、综合评分最高，推荐切换。

---

## 🚀 部署步骤

1. **配置文件已更新**: `config.yaml` 已修改，600309.SH 使用新策略 `ma_600309`

2. **监控代码已更新**: `monitor/main.py` 已添加 MA 策略支持

3. **验证配置**:
   ```bash
   cd "/Users/ripple/work space/ripple_tradePilot"
   python3 -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c['symbols'][1])"
   ```

4. **重启监控** (如正在运行):
   ```bash
   # 停止现有监控进程
   # 重新启动
   python3 monitor_brief.py
   ```

5. **监控日志**: 观察 `logs/tradepilot.log` 确认策略加载正常

---

## 📋 后续建议

1. **监控 1m 窗口**: 若 1 个月窗口连续 2 个月收益 < -10%，重新评估策略
2. **季度回顾**: 每季度运行一次四窗口研究，确保策略持续有效
3. **风控设置**: 建议设置 15% 最大回撤止损
4. **备选策略**: 保留 grid_combo_600309_v1 作为回退选项

---

*变更总结生成时间：2026-03-19 01:40*
