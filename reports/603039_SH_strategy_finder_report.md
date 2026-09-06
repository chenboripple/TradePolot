# 603039.SH 泛海微 strategy research


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

- Generated: 2026-03-23 22:05:12
- Weights: 3m=0.45, 6m=0.3, 1m=0.2, 1y=0.05
- Current profile: breakout_603039

## Best candidate

- Name: `DT_3_0.5_0.5`
- Suggested profile: `603039_dt_3_0_5_0_5`
- Score: 18.32
- 3m: 28.55% | DD 8.66% | trades 2
- 6m: 12.04% | DD 15.71% | trades 5
- 1m: -3.95% | DD 8.42% | trades 1
- 1y: 18.13% | DD 20.30% | trades 12

## Top candidates

1. `DT_3_0.5_0.5` | score 18.32 | 3m 28.55% | 6m 12.04% | 1y 18.13%
2. `MA_5_15` | score 13.73 | 3m 18.48% | 6m 12.84% | 1y -12.12%
3. `MA_3_13` | score 10.93 | 3m 15.77% | 6m 8.51% | 1y -0.88%

## Suggested config patch

See JSON patch artifact generated alongside this report.
