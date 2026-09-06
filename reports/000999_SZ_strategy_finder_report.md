# 000999.SZ 华润三九 strategy research


> ⚠️ **可信度提示（2026-09 复核）**：本报告的高收益数字来自研究期脚本，存在前视撮合（当日收盘成交）、成本低估、参数在样本内寻优、窗口选择偏差等问题，**不可作为实盘预期**。统一口径回测（次日开盘撮合 + 涨跌停拦截 + 佣金/印花税/滑点 + 回撤闸门）请以 Web「回测记录」/ `tradepilot backtest` 与样本外验证为准。相关脚本已归档至 `experiments/`。

- Generated: 2026-03-23 22:05:13
- Weights: 3m=0.45, 6m=0.3, 1m=0.2, 1y=0.05
- Current profile: combo_000999

## Best candidate

- Name: `COMBO_MA5_15_RSI16_35_65_V1`
- Suggested profile: `000999_combo_ma5_15_rsi16_35_65_v1`
- Score: 9.87
- 3m: 6.26% | DD 2.93% | trades 2
- 6m: 10.55% | DD 3.05% | trades 4
- 1m: 0.91% | DD 0.12% | trades 1
- 1y: 13.32% | DD 9.00% | trades 8

## Top candidates

1. `COMBO_MA5_15_RSI16_35_65_V1` | score 9.87 | 3m 6.26% | 6m 10.55% | 1y 13.32%
2. `COMBO_RSI16_35_65_BB26_2.0_V1` | score 8.24 | 3m 5.36% | 6m 7.32% | 1y 6.44%
3. `RSI_16_35_65` | score 7.23 | 3m 5.10% | 6m 7.05% | 1y 6.84%

## Suggested config patch

See JSON patch artifact generated alongside this report.
