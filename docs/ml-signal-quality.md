# ML 信号质量模型（D 阶段总纲）

> 本文件是 D 阶段（特征 → 数据集 → 训练 → 评估 → 消费 → 一键管道）的**总纲与诚实定位声明**。
> 预测目标的逐字定义在 [`prediction-target.md`](prediction-target.md)；规则投票口径在
> `../STRATEGIES.md`。本文只讲 D 阶段把这些拼成"能回答『这次信号赚钱把握多大』的模型"时
> 必须守住的协议，以及**当前数据现实下它到底能产出什么**。

## 0. 一句话定位

> 在 A 股日线合成/历史数据上，训练一个**校准过的 5 日胜率分类器**，给看板/监控的规则信号
> 追加一个 `p_win`（与规则票占比 `vote_ratio` **并排显示、永不互相冒充**），并把"模型过滤
> 比不过滤每信号期望高多少、覆盖率掉多少"用样本外决策对比表量化出来。

**它不是自动交易器。** 系统全程只读信号、永不接下单；ML 层只产出**概率与期望**供人决策。

---

## 1. 模块地图与依赖分层

```
ml/
├── labels.py       # B1 前瞻标签（纯函数，镜像引擎成本口径）
├── splits.py       # B3 锚定滚动切分 + purge/embargo（纯函数）
├── calibration.py  # B4 Brier/ECE/slope/decision_value（numpy 纯函数）
├── features.py     # D1 四组特征（DataFrame→DataFrame 纯函数，只用 T 日收盘前信息）
├── dataset.py      # D2 build_dataset → csv.gz + manifest（落 ml_datasets 表）
├── models.py       # D3 train_model（logreg/hgb，懒加载 sklearn）
├── registry.py     # D3 工件落盘 + ml_models 表 + promote 四道门禁
├── evaluate.py     # D4 evaluate_model（免 sklearn，只读 oos.npz + meta.json）
├── scoring.py      # D5 SignalScorer：dashboard/monitor 的服务时入口（懒加载 + 优雅回退）
└── pipeline.py     # D6 run_pipeline：把上面串成一键回归/演示入口
```

**懒加载铁律**：`models / registry / evaluate / scoring / pipeline` **顶层只 import numpy 与本仓
模块**，sklearn/joblib 一律在函数内 `_require_sklearn()` 惰性加载。未安装 scikit-learn 时，
`import ripple_tradePilot.ml` 及 dashboard/monitor/serve 的信号链路**照常运行**，只有真去训练
（`ml train` / `ml pipeline`）才报清晰错误（CLI **exit 3**）。该契约由
`tests/test_models.py::LazySklearnContractTest` 用 **AST 扫描模块顶层 import** 钉死，不靠文档约定。

---

## 2. 特征：四组可独立开关（`ml/features.py`）

**铁律**：每个特征只用 **T 日收盘前可得**信息（日线/index/market/board 均 ≤ T），标签用 T+1
开盘（B1）；每个特征函数是 `(DataFrame→DataFrame)` 纯函数，行索引 `(symbol, trade_date)`，
**只依赖 DB 可重建字段**——这是 train/serve 一致性的根基。内置 `assert_no_lookahead` 开发期探针
（最后 10 根 bar 换极端值，断言 T ≤ n−11 行所有特征不变）。

| 组 | 代表列 | 来源 |
|----|--------|------|
| `signal` | `buy_count`/`sell_count`/`vote_threshold`/`rec_buy`/各组件 side one-hot/`state_age` | A1 `signals/` 规则引擎输出即特征 |
| `price_volume` | `ret_1/5/20`/`close/MA5−1`/`rsi14`/`bb_pos`/`bb_width`/`vol_ratio`/`amount_z20`/`breakout_20`/`atr_pct` | 个股日线 |
| `market` | `idx_ret_5/20`(000300)/`idx_close/MA20−1`/`up_ratio`(+ma5)/`limit_up_count`/`total_amount_z` | C1/C2 `index_daily`+`market_daily` |
| `industry` | `board_ret_5/20`/`board_close/MA20−1`/`stock_ret−board_ret` | C3 行业三表 |

`--groups` 可按组开关（路线图"每次只加一组验证增益"的机制保障）。缺组时 `market`/`industry`
列置 NaN，并写 `_has_market`/`_has_industry` 组可用标志列；`hgb` 原生吞 NaN，`logreg` 走中位数
插补 + 缺失指示列。

> ⚠️ **行业 point-in-time 缺失**：`industry_membership` 现状是**最新单快照**（非逐日历史成分），
> manifest 标 `industry_point_in_time=False` 并带前视警告。行业组增益须**谨慎解读**——含轻微
> 前视/幸存者偏差。

---

## 3. 数据集（`ml/dataset.py` + `ml_datasets` 表，schema v15）

`build_dataset(symbols, start, end, horizon=5, aux_horizon=10, groups, profile_resolver, db_path,
out_dir)`：逐标的 `load_daily_bars` → **A9 复权巡检不过的 symbol 拒入并记录**（`rejected`）→ 各组
特征 → `label_series`（5 主 + 10 辅 + MAE）→ 丢标签缺失行 → `data/ml/<dataset_id>.csv.gz`。

**manifest**（json + `ml_datasets` 表）记录可复现所需的一切：`dataset_id`（= **内容 hash 前 12 位**，
不含机器/时间因素 → 同库同参数重跑幂等）、行数/正例率、日期范围、`symbols`、`groups`、
`cost_model`、**profile 解析快照**（`_spec_snapshot`，见 §6 train/serve）、每 symbol `data_version`、
`max_trade_date`（**新鲜度戳**，晋升门禁靠它）、point-in-time 警告。

---

## 4. 训练协议：锚定滚动 walk-forward（`ml/models.py` + `ml/registry.py`）

复用 B3 `rolling_splits`（purge/embargo 在切分层处理）。每折：`train_idx` 拟合 → `val_idx` 选超参 /
拟合校准器 → **只预测 `test_idx`**；拼接所有折的 OOS 预测成完整样本外序列。**模型选择永远看不到
test**，对外服役的工件 = **末折**的 model + calibrator。

| kind | 模型 | NaN | 校准 |
|------|------|-----|------|
| `logreg` | `LogisticRegression(max_iter=1000)`，`C∈{0.01,0.1,1}` 按 val 折 Brier 选 | `SimpleImputer(median, add_indicator)+StandardScaler` | `CalibratedClassifierCV(method="sigmoid", cv="prefit")` 在 val 折拟合 |
| `hgb` | `HistGradientBoostingClassifier(max_iter=200, lr=0.06, max_leaf_nodes=15, l2=1.0)`，val 折早停 | **原生容忍** | 同上 |

目标：`win5`（分类·5 日胜）/ `ret5`（回归·5 日净收益）/ `mae5`（回归·5 日最大不利偏移 = 下行
风险）。**只有分类做校准**；回归摘要给 MAE/R²，分类给 AUC/Brier/ECE/覆盖率@阈值。

**工件** `data/ml/models/<model_id>/`：`model.joblib` + `calibrator.joblib` + `meta.json`（provenance）
+ `oos.npz`（`oos_idx`/`p_oos`/`y_oos`/`net_ret`/`rec_buy`/`trade_dates`）。
`model_id = f"{kind}-{target}-{sha256[:10]}"`，hash 含 `trained_at`——未显式给 `trained_at` 时用
**微秒**精度打戳，避免"同一秒内重训同数据集同协议"撞同一个 model_id（否则覆盖上轮工件、并把 DB 行
upsert 回 `candidate`，若上轮已 promote 等于悄悄摘掉在位模型）。元数据落 **v15 `ml_models`** 表。

---

## 5. 评估与晋升门禁

**D4 评估完全免 sklearn**——只读 `oos.npz` + `meta.json`，用 B4 numpy 纯函数算完所有指标。三段：
判别（AUC/LogLoss/Brier vs 常数基线 `p̄(1−p̄)`）｜校准（logit slope/intercept、ECE、10 桶
reliability）｜**决策对比表**：同一标签、同一 OOS 折上并列四方——`rule_baseline`（规则全部 BUY 票）｜
`model_thr{t}`（模型过滤 `p≥t`）｜`buy_hold`｜`index`（000300 同窗）。每行列覆盖率/胜率/平均净收益/
**每信号期望**/样本数——直接回答"模型过滤把每信号期望抬了多少、覆盖率掉多少"。

**晋升门禁（4 道闸，`promote(model_id, force=False)`）**：

| 门 | 条件 | 不过 + force 的后果 |
|----|------|---------------------|
| `min_samples` | `n_rows ≥ 800` | 质量门 → **`demo`** |
| `min_positives` | `n_positive ≥ 80` | 质量门 → **`demo`** |
| `max_staleness` | `max_trade_date` 距今 ≤ 120 天 | 新鲜度门 → **`promoted` 但 `stale=true`** |
| `beats_base_rate` | 分类：OOS Brier < 常数基线 Brier | 质量门 → **`demo`** |

全过 → `status='promoted'`、`stale=false`，并**退役同 (target,horizon) 的旧在位模型**。不过且未
force → 维持 `candidate`（CLI `ml promote` exit 1）。不过但 force → 质量门任一失败即 **`demo`**
（演示工件，`load_promoted_model` 默认**不返回**，需 `include_demo=True`），仅新鲜度失败则
`promoted`+`stale`。状态枚举：`candidate | promoted | retired | demo`。

---

## 6. 消费：看板 / 监控（`ml/scoring.py`，D5）

**`vote_ratio` ≠ `p_win`**——A1 规则票占比与 D5 模型概率**并排显示，永不互相冒充**。A2 已全链路
废除 `confidence` 一词；D5 只**追加** `p_win`，不回潮 `confidence`。

- **`SignalScorer.try_load()`**：懒加载 + 优雅回退。sklearn 未装 / 无 `promoted` 模型 / 工件缺失 →
  `None`，看板监控照常出规则信号（`forecast=null`）。`demo` 默认不取；`stale`/`demo` 取到时在
  `warnings` 里显式打出。
- **看板** `market_detail` 多 `"forecast": {...} | null`；前端有则显示"模型预估(5日)：胜率 · 期望
  净收益 · 参考下行"，无则只显示"方向一致度 x%（规则票，非概率）"。
- **监控**收盘例程给每个确认信号算 forecast，经 `extra_info['forecast']` 让飞书卡片渲染 🧠 预估块，
  并把 `model_id/p_win/expected_ret/downside_mae` 四列写进 `signal_ledger`（B2 预留列）。**预估是
  增强项**：其序列化单独 try/except，坏掉的 `to_dict()` 只让这条通知不带预估，**绝不吞掉整条信号通知**。

**train/serve 一致性（D 阶段头号风险）**：`score_symbol` 构建特征行时，与 `build_dataset` 调用
**同一个 `assemble_dataset_frames`**，并读取 manifest 里的**训练画像快照**复现训练时的 `ProfileSpec`
（不是用当前 config 的画像）。`tests/test_scoring.py::TrainServeConsistencyTest` 逐 (symbol,trade_date)
逐列 `np.allclose` 断言 scorer 特征向量 == 数据集那一行，且 `assertEqual(checked, manifest.n_rows)`
防空跑。

> 🐛 **本阶段修了一个真 train/serve 偏斜 bug**：`signals/profile.py::_component_params` 曾忽略
> canonical 组件字典里的 `params` 子节（`{"name","kind","params":{...}}`，正是 `_spec_snapshot` 写进
> manifest 的形态），导致 serve 端把**默认 MA5/20+RSI14** 的票喂给一个按 **MA5/10+RSI7** 训练的
> 模型——静默偏斜、零报错。回归测试 `test_signals_voting.py::test_components_nested_params_roundtrip`。

---

## 7. 一键管道（`ml/pipeline.py` + `tradepilot ml pipeline`，D6）

`run_pipeline(...)` 不 import click、不 print，进度走可选 `progress` 回调 → 可脱离 CliRunner 直接测。
串起 build-dataset → train（kind×target 全组合，各独立 candidate）→ eval（**只对分类目标出决策对比
表**，回归的 AUC/Brier 无意义）→〔可选〕promote → markdown 报告。

```
tradepilot ml pipeline --pool config|watchlist|catalog [--symbols A,B] [--start --end] \
    [--horizon 5 --aux-horizon 10] [--groups signal,price_volume,market,industry] \
    [--models logreg,hgb] [--target win5] [--aux-targets ret5,mae5] \
    [--splits 5 --embargo 2 --val-ratio 0.2 --threshold 0.5 ...] \
    [--out-dir --models-dir --report-dir reports] [--promote] [--force]
```

报告落 `reports/ml-pipeline-<dataset_id>.md`：数据集摘要（含新鲜度戳/正例率/成本口径）→ 模型清单
→ 每分类模型一张决策对比表 + 校准行 → 主模型可靠性分桶 → 晋升门禁 → 警告（去重汇总）→ 诚实定位。
缺失一律渲染 `n/a`（**绝不写成 0**）；空子集（如 OOS 折内规则零 BUY 票的 `rule_baseline`）渲染 `—`
（不写 `0.00%`，那会被读成"测过了、收益为零"）。退出码：**2** = 空池/非法 kind/非法组/缺工件，
**3** = sklearn 未装，**0** = 跑通（哪怕门禁未过——报告照写）。

> ⚠️ **默认不晋升（`promote_models=False`）**：一次回归跑悄悄换掉看板/监控正在用的在位模型是
> **事故**，不是便利。要动在位模型必须显式 `--promote`，且仍受 §5 四道门禁约束。

---

## 8. 诚实定位声明（最重要的一节）

> **在数据供给恢复（日线更新到近期）并扩充股票池之前，本系统产不出任何"可作为交易依据"的模型。**

三条硬现实：

1. **本地日线停在 2026-04-17、观察池仅 2 只、`daily_bars` 表曾长期空（只有 CSV）** → `max_staleness`
   门（≤120 天）**必触发**。
2. **合成夹具是随机游走弱信号** → logreg 在其上 OOS AUC≈0.56、Brier≈0.2536 **高于**常数基线
   0.2500 → `beats_base_rate` **不过** → 默认拒晋升，`--force` 也只能落 **`demo` + `stale=true`**。
3. 因此 D 阶段全部验收都基于**合成夹具上管道的正确性**（`tests/test_pipeline.py` /
   `test_cli_ml.py` / `test_scoring.py` / `test_models.py`），而非真实预测力。

**这是护栏正确工作，不是 bug。** 三层防"演示模型冒充可用模型"：`promote` 门禁 + `stale`/`demo`
标记 + UI/通知/报告 `warnings`。指标**质量**类断言必须用 `synth.ml_frame`（逻辑斯谛真值链路，
logreg 可达 AUC 0.82 / Brier 0.171 < 基线 0.244）；`seed_market_db` 只用于 CLI 管道与集成测试。

**数据恢复并扩池后，重跑 `tradepilot ml pipeline --pool config --promote`，才会产生第一个真正
可晋升（`promoted`）的模型。** 在那之前，看板上任何 `p_win` 都来自 `demo`/`stale` 工件，仅供
机制演示，**不可作为交易依据**。

---

## 9. 相关测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_models.py` | D3 训练度量质量（`synth.ml_frame` 真值链路）+ 懒加载 AST 契约（五模块） |
| `tests/test_registry.py` | D3 工件往返 + promote 四门精确判定 + model_id 唯一性 |
| `tests/test_evaluate.py` | D4 免 sklearn 评估 + 决策对比表 + 警告 + JSON 往返 |
| `tests/test_scoring.py` | D5 **train/serve 逐列一致性硬测试** + 优雅回退 + forecast 序列化 |
| `tests/test_pipeline.py` | D6 纯渲染层（缺失→`n/a`、空子集→`—`、分节）+ 真跑三次接线口径 |
| `tests/test_cli_ml.py` | D3/D4/D6 CLI 全链路 + 退出码 + 报告落盘（CliRunner，零网络） |
