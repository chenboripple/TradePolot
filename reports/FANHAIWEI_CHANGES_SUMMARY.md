# 泛海微 (603039.SH) 四窗口策略研究 - 修改文件清单


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

**日期**: 2026-03-19  
**研究目标**: 找到 1 年、6 个月、3 个月、1 个月四窗口综合最强的策略

---

## 📝 新增文件

| 文件 | 路径 | 说明 |
|------|------|------|
| `research_fanhaiwei_4window.py` | 项目根目录 | 四窗口策略搜索脚本 (288 个策略变体) |
| `compare_fanhaiwei_current_vs_best.py` | 项目根目录 | 当前策略 vs 最佳策略对比脚本 |
| `603039_SH_4window_research.json` | `data/backtest/` | 完整回测结果 JSON |
| `603039_SH_4window_research_summary.md` | `data/backtest/` | Top 10 策略详细报告 |
| `fanhaiwei_current_vs_best_comparison.md` | `reports/` | 策略对比报告 |
| `FANHAIWEI_EXECUTIVE_SUMMARY.md` | `reports/` | 执行摘要 (给用户的最终报告) |
| `FANHAIWEI_CHANGES_SUMMARY.md` | `reports/` | 本文档 (修改文件清单) |

---

## 🔧 修改文件

### 1. `config.yaml`

**修改内容**: 更新 603039.SH 的策略配置

**原配置**:
```yaml
breakout_603039:
  kind: "breakout"
  breakout_window: 20
  rsi_period: 6
  buy_rsi_min: 55
  exit_ma: 10
  sell_rsi_max: 45
```

**新配置**:
```yaml
# 泛海微 - 优化配置 (2026-03-19 四窗口深度研究推荐)
# 策略：MACD(12, 20, 7)
# 四窗口表现：1y: +29.46% (DD:19.23%) | 6m: +12.74% (DD:18.13%) | 3m: +30.58% (DD:7.02%) | 1m: 0.00%
# Sharpe: 0.97 (1y) | 交易次数：8 (1y), 5 (6m), 1 (3m), 0 (1m)
breakout_603039:
  kind: "macd"
  macd:
    fast: 12
    slow: 20
    signal: 7
    zero_cross: false
```

---

### 2. `src/ripple_tradePilot/monitor/main.py`

**修改内容**: 添加 MACD 策略支持

#### 2.1 添加导入
```python
from ripple_tradePilot.strategies.macd import MACD
```

#### 2.2 添加 `_run_macd_profile` 方法
```python
def _run_macd_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
    """运行 MACD 策略"""
    macd_cfg = profile.get('macd', {})
    macd_strategy = MACD(
        fast=macd_cfg.get('fast', 12),
        slow=macd_cfg.get('slow', 26),
        signal=macd_cfg.get('signal', 9),
        zero_cross=macd_cfg.get('zero_cross', False),
    )
    
    latest_bar = bars[-1]
    macd_strategy.reset()
    for bar in bars[:-1]:
        macd_strategy.on_bar(bar)
    macd_signal = macd_strategy.on_bar(latest_bar)
    
    signals = {'macd': macd_signal}
    strongest_signal = ('macd', macd_signal) if macd_signal.side else None
    
    if macd_signal.side == Side.BUY:
        recommendation = '🟢 买入'
    elif macd_signal.side == Side.SELL:
        recommendation = '🔴 卖出'
    else:
        recommendation = '⚪ 观望'
    
    return signals, strongest_signal, recommendation
```

#### 2.3 在 `check_symbol` 方法中添加 macd 分支
```python
elif profile_kind == 'macd':
    signals, strongest_signal, recommendation = self._run_macd_profile(profile, bars)
```

---

### 3. `monitor_brief.py`

**修改内容**: 添加 MACD 策略支持到日常监控

#### 修改 `_run_profile` 函数
```python
def _run_profile(monitor: MarketMonitor, profile: dict, bars: list):
    if profile.get('kind') == 'grid_combo':
        return monitor._run_grid_combo_profile(profile, bars)
    if profile.get('kind') == 'rsi':
        return monitor._run_rsi_profile(profile, bars)
    if profile.get('kind') == 'breakout':
        return monitor._run_breakout_profile(profile, bars)
    if profile.get('kind') == 'macd':  # ← 新增
        return monitor._run_macd_profile(profile, bars)
    return {}, None, '⚪ 观望'
```

---

## 📊 研究结果摘要

### 最佳策略
**MACD(12, 20, 7)** - 综合得分 11.7

### 四窗口表现
| 窗口 | 收益率 | 最大回撤 | 交易次数 | 夏普比率 |
|------|--------|----------|----------|----------|
| 1y   | +29.46% | -19.23% | 8        | 0.97     |
| 6m   | +12.74% | -18.13% | 5        | 0.88     |
| 3m   | +30.58% | -7.02%  | 1        | 3.12     |
| 1m   | 0.00%  | 0.00%    | 0        | 0.00     |

### 对比当前策略
- **当前策略**: 所有窗口 0 交易，完全失效
- **最佳策略**: 加权收益 +24.03%，平均夏普 1.24
- **提升**: 从 0% 到 +24.03% 加权收益

---

## ✅ 验证步骤

运行以下命令验证配置正确：

```bash
cd /Users/ripple/work\ space/ripple_tradePilot

# 1. 验证模块导入
python3 -c "
import sys
sys.path.insert(0, 'src')
from ripple_tradePilot.monitor.main import MarketMonitor
from ripple_tradePilot.strategies.macd import MACD
print('✅ MACD import OK')
print('✅ MarketMonitor import OK')
print('✅ _run_macd_profile exists:', hasattr(MarketMonitor, '_run_macd_profile'))
"

# 2. 运行对比回测
python3 compare_fanhaiwei_current_vs_best.py

# 3. 查看最终报告
cat reports/FANHAIWEI_EXECUTIVE_SUMMARY.md
```

---

## 🚀 下一步

1. **实盘验证**: 监控 MACD 策略实盘信号
2. **定期复核**: 每月运行一次四窗口回测
3. **风险控制**: 建议单票仓位不超过 20%

---

**修改完成时间**: 2026-03-19 01:20 GMT+8
