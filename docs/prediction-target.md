# 预测目标与评估口径（B 阶段钉死）

> 本文件是全系统**唯一**的预测目标定义。标签（`ml/labels.py`）、切分（`ml/splits.py`）、
> 校准（`ml/calibration.py`）、特征管道（D1）、数据集（D2）、模型与评估（D3/D4）必须
> 与此处逐字一致；任何改动都要同步本文与对应纯函数测试。

## 1. 为什么需要这份文档

改造前 dashboard 把"投票占比"当 `confidence` 显示（2/3 票 → 66%），这是一个**规则
一致性**指标，不是概率，无法被校准、无法回答"这次信号赚钱的把握有多大"。本阶段把
预测目标改写成可验证、与回测引擎自洽的口径，并配套防泄漏切分与校准度量。

## 2. 预测目标（一句话）

> **T 日收盘后**，预测"**T+1 开盘买入、持有 H 个交易日、T+1+H 开盘卖出**"这一动作的
> **净收益为正的概率**（主目标 H=5，辅助 H=10），并给出**预期净收益**与**下行风险**。

时间轴（H=5）：

```
T 日收盘决策 ──▶ T+1 开盘买入(entry) ──▶ 持有 5 个交易日 ──▶ T+6 开盘卖出(exit)
   bar[i]            bar[i+1]                                  bar[i+6]
```

- `entry = open[i+1]`，`exit = open[i+1+H]`（H=5 → `open[i+6]`）。
- 用**开盘价进出**与引擎的 `next_open` 撮合、A 股 T+1 约束自洽——标签学到的目标正是
  回测/实盘能复现的动作，杜绝"用收盘价买当日"的前视。
- 决策点只用 **T 日收盘前**可得信息（D1 特征的 `assert_no_lookahead` 保证）。

## 3. 标签字段（`ml/labels.py::HorizonLabel`）

| 字段 | 定义 | 用途 |
|------|------|------|
| `entry` | `open[i+1]` | 买入基准价 |
| `exit` | `open[i+1+H]` | 卖出基准价 |
| `gross_return` | `exit/entry − 1` | 不含成本的毛收益 |
| `net_return` | 含完整往返成本的净收益（见 §4） | **主回归/期望目标** |
| `win` | `net_return > 0` | **主分类目标**（"净收益为正的概率"） |
| `mae` | `min(low[i+1..i+H]) / entry − 1`（≤0） | 下行风险（D 阶段回归目标） |
| `mfe` | `max(high[i+1..i+H]) / entry − 1`（≥0） | 持有期最大浮盈（波动率退出参考） |
| `complete` | 恒 `True`（不完整直接返回 `None`） | 契约明示 |

**bars 不足以走完整 H → 返回 `None` 丢弃**，绝不用尾部截断价冒充完整标签（否则最后
几根 bar 的标签系统性偏短、污染训练集）。`label_series(bars, H)` 返回与 `bars` 等长的
列表，尾部 `H+1` 个为 `None`。

## 4. 成本口径（与引擎共用 `CostModel`）

净收益**镜像** `backtest/costs.py::CostModel` 的比例成本，避免"标签收益"与"回测收益"
两套口径对不上：

```
每股买入成本 buy_ps  = entry × (1 + slippage) × (1 + fee_rate)
每股卖出净得 sell_ps = exit  × (1 − slippage) × (1 − fee_rate − stamp_duty)
net_return = sell_ps / buy_ps − 1
```

默认常量：`fee_rate=0.0003`（佣金万三）、`stamp_duty=0.0005`（印花税万五，仅卖出）、
`slippage=0.001`（滑点千一）。

**标签不计 `min_fee`（最低佣金 5 元）**：标签是**单位资金的无量纲 per-share 收益**，
`min_fee` 是与持仓规模相关的固定下限，属仓位/引擎层；放进 per-share 标签会引入规模
依赖、破坏可比性。回测引擎仍按实际股数计 `min_fee`，两者差额仅在小单时显现，由 D 阶段
决策价值评估（§6）在真实仓位口径下复核。

平价往返（`entry==exit`）的净损失 = `round_trip_cost_factor()`（滑点+佣金+印花税合成
成本因子，约 0.28%）——这是"信号必须跑赢的成本底线"。

## 5. 切分协议（`ml/splits.py`，防泄漏）

普通 K 折在金融时序上会泄漏：H 日前瞻标签让相邻样本共享未来信息，随机打乱更把"用未来
预测过去"合法化。采用**锚定式（expanding-window）滚动切分**：

- `rolling_splits(trade_dates, exit_dates, n_splits, val_ratio=0.2, embargo=2)`：把唯一
  交易日均分为 `n_splits+1` 段，第 0 段为训练种子，第 1..n_splits 段为 successive test 折；
  第 j 折的 train = 该折首日之前的全部样本。
- **purge（按每行自己的 `exit_date`）**：剔除 `exit_date >= test_start_date` 的训练样本
  ——这是 H 日标签跨边界重叠的精确处理（不是简单丢最后 H 行）。
- **embargo**：再空 `embargo` 个交易日，吸收残留序列依赖。
- **val** = 余下 train 的日期尾部 `val_ratio` 段（已随 train 一并 purge），用于早停/阈值/
  校准拟合。
- **按日期而非行号切分**：样本可多标的池化（同一 `trade_date` 多行），避免把同一天的
  不同标的拆进 train/test 两侧。
- `holdout_split(trade_dates, holdout_start)`：最终保留集**永不进 train/val**，所有调参
  结束后做一次性最终评估。

**核心不变量**（`tests/test_splits.py` 逐条钉住）：任何 train/val 样本的 `exit_date`
严格早于其测试折首日；train/val/test 两两互斥；test 折互不重叠且按时间递增。

## 6. 校准与决策价值（`ml/calibration.py`）

把"概率准不准"与"过阈值的信号值不值得下手"拆开度量：

**校准**（模型说 58% 胜率时实际是不是 58%）：

- `brier_score` = `mean((p−y)²)`，完美校准+完美判别 → 0；
- `log_loss` = 二元交叉熵；
- `reliability_table(y, p, n_bins=10)`：等宽分桶，逐桶比对平均预测 vs 实际正例率；
- `expected_calibration_error`（ECE）= Σ(桶占比 × |预测−实际|)；
- `calibration_slope_intercept`：logit 空间拟合 `y ~ sigmoid(a + b·logit(p))`，完美校准 →
  `b≈1, a≈0`；`b<1` 过度自信、`b>1` 不够自信（IRLS 自实现，不依赖 sklearn）。

**决策价值**（`decision_value_report(y, net_ret, p, threshold)` → `DecisionValue`）：在
`p ≥ threshold` 的过滤子集上输出——

- `coverage`：过滤后剩余信号占比（**关键护栏**，防"几乎不交易刷高胜率"的假象）；
- `win_rate`：子集实际正例率；
- `avg_net_return`（= `expectancy`）：**每信号净期望收益**；
- `avg_win` / `avg_loss`：子集内盈/亏单的平均净收益。

这正是路线图第 4 点"围绕净期望收益做决策 + 跟踪覆盖率"的实现。D4 评估报告在同一标签、
同一 OOS 折上对比"规则引擎全部 BUY 票（不过滤基线）| 模型过滤 p≥thr | 买入持有 | 指数"，
直接回答"模型过滤是否真的把期望收益做高、代价是覆盖率降了多少"。

## 7. 测试基准

`tests/synth.py::ml_frame` 提供逻辑斯谛真值链路：`p_true = sigmoid(X·coef + intercept)`、
`y ~ Bernoulli(p_true)`，故 `p_true` 是**构造性完美校准**。`tests/test_calibration.py`
据此断言 Brier ≈ `E[p(1−p)]`、log-loss ≈ 二元熵、ECE 小、slope≈1/intercept≈0；人为
锐化/钝化 logit 解析地推出 slope≈0.5/≈2，验证斜率语义。`tests/test_labels.py` 用常数价/
单跳/V 形序列手算净收益、MAE、MFE。

## 8. 诚实定位

本地数据停在 2026-04-17、观察池仅 2 只——**ML 阶段验收以合成夹具跑通全管道为准**。
真实模型工件在数据恢复并扩池前一律标注 `demo`/`stale`，受 D3 `promote` 门禁约束
（样本量/新鲜度/OOS Brier 不达标默认拒晋升），UI 带"数据滞后"警告。本文定义的是**口径**，
不是收益承诺；任何样本内高收益都含前视/幸存者偏差，须以 walk-forward（A6 修复后）与
D 阶段 OOS 评估为准。
