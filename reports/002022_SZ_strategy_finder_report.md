# 002022.SZ 科华生物 strategy research


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

- Generated: 2026-03-23 22:05:11
- Weights: 3m=0.45, 6m=0.3, 1m=0.2, 1y=0.05
- Current profile: grid_combo_002022

## Best candidate

- Name: `RSI_6_20_60`
- Suggested profile: `002022_rsi_6_20_60`
- Score: 13.93
- 3m: 9.50% | DD 1.07% | trades 3
- 6m: 12.23% | DD 7.37% | trades 7
- 1m: 4.40% | DD 0.95% | trades 1
- 1y: 10.80% | DD 7.37% | trades 11

## Top candidates

1. `RSI_6_20_60` | score 13.93 | 3m 9.50% | 6m 12.23% | 1y 10.80%
2. `BB_26_2.0` | score -4.81 | 3m -0.12% | 6m 7.75% | 1y 16.38%
3. `MA_5_20` | score -17.54 | 3m -1.14% | 6m 2.24% | 1y 25.98%

## Suggested config patch

See JSON patch artifact generated alongside this report.
