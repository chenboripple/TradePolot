# TradePilot 策略配置文档

## 📊 三策略组合 (MA + RSI + BB)

---

## 1️⃣ Moving Average Cross (双均线交叉)

**策略类型：** 趋势跟踪

**原理：**
- 快线 (MA5) 上穿慢线 (MA20) → **金叉买入**
- 快线 (MA5) 下穿慢线 (MA20) → **死叉卖出**

**参数：**
```yaml
ma_cross:
  fast: 5    # 快线周期
  slow: 20   # 慢线周期
```

**优点：**
- ✅ 简单清晰，易于理解
- ✅ 趋势行情表现优秀
- ✅ 及时捕捉大趋势

**缺点：**
- ❌ 震荡行情频繁假信号
- ❌ 信号滞后（均线本质）

**适合行情：** 单边上涨/下跌

---

## 2️⃣ RSI (相对强弱指标)

**策略类型：** 超买超卖反转

**原理：**
- RSI < 30 → **超卖买入**（价格过低，可能反弹）
- RSI > 70 → **超买卖出**（价格过高，可能回调）

**参数：**
```yaml
rsi:
  period: 14     # 计算周期
  oversold: 30   # 超卖线
  overbought: 70 # 超买线
```

**优点：**
- ✅ 适合震荡行情
- ✅ 提前预警反转
- ✅ 参数稳定（经典 14/30/70）

**缺点：**
- ❌ 强趋势中可能持续超买/超卖
- ❌ 不适用于单边行情

**适合行情：** 横盘震荡

---

## 3️⃣ Bollinger Bands (布林带)

**策略类型：** 波动率回归

**原理：**
- 价格触及下轨 → **买入**（超卖反弹）
- 价格触及上轨 → **卖出**（超买回调）

**参数：**
```yaml
bollinger:
  period: 20      # 周期
  std_dev: 2.0    # 标准差倍数
```

**指标说明：**
- **上轨** = 中轨 + 2 倍标准差
- **中轨** = 20 日简单移动平均
- **下轨** = 中轨 - 2 倍标准差
- **带宽** = 上轨 - 下轨（波动率指标）

**优点：**
- ✅ 自动适应市场波动
- ✅ 结合趋势 + 波动率
- ✅ 可识别突破/回归两种逻辑

**缺点：**
- ❌ 极端行情可能失效
- ❌ 需要足够数据（至少 20 根 K 线）

**适合行情：** 震荡 + 突破

---

## 🎯 策略组合优势

### 互补性分析

| 策略 | 擅长行情 | 不擅长行情 | 与其他策略相关性 |
|------|---------|-----------|----------------|
| **MA Cross** | 趋势市 | 震荡市 | 低 (与 RSI/BB) |
| **RSI** | 震荡市 | 趋势市 | 低 (与 MA/BB) |
| **BB** | 震荡 + 突破 | 极端行情 | 中 (与 RSI) |

### 信号融合逻辑（统一口径 · strict 状态投票）

> 2026-09 起，dashboard / monitor / 回测共用 `signals/` 单一实现，投票语义统一为
> **状态票 + strict 规则**。此前三处实现语义互异（dashboard majority、monitor 买入
> 分支不查 sell_count、combo_vote strict），同数据结论矛盾，现已收敛。

每个组件在每根 bar 上输出**状态票**（不是只在交叉当根触发的边沿信号）：

| 组件 | 看涨(BUY) 条件 | 看跌(SELL) 条件 | 不投票 |
|------|---------------|----------------|--------|
| `ma` | 快线 > 慢线 | 快线 < 慢线 | 相等 / 数据不足 |
| `rsi` | RSI ≤ oversold | RSI ≥ overbought | 区间内 / 数据不足 |
| `bollinger` | 收盘 ≤ 下轨 | 收盘 ≥ 上轨 | 带内 / 带宽为 0 |
| `macd` | DIF > DEA | DIF < DEA | 相等 / 数据不足 |
| `trend` | 短 > 中 > 长 | 短 < 中 < 长 | 其余 / 数据不足 |
| `donchian` | 收盘破前窗口高点 | 收盘破前窗口低点 | 通道内 / 数据不足 |

聚合规则（`vote_threshold` 默认 2，夹取到 `[1, 组件数]`）：

```
- 同向票 ≥ 阈值 且 反向票 == 0  → 🔴 买入 / 🟢 卖出
- 有票但多空并存，或票数不足阈值 → 🟡 分歧 (CONFLICT)
- 所有组件都不投票              → ⚪ 观望 (HOLD)
```

**关键差异**：2 买 1 卖在旧 dashboard majority 下判"买入"，统一口径判 **CONFLICT**
（存在反向票即不一致）。信号 = recommendation **进入** BUY/SELL 的转移点，dashboard
展示状态本身、monitor 通知与回测撮合消费转移事件，二者派生自同一决策序列。

### ⚠️ combo 组件语义 ≠ 单策略类语义

`signals/` 的投票组件与 `strategies/` 下的单策略类**不是同一套语义**，结论不可互换：

| 维度 | `signals/` 投票组件（生产口径） | `strategies/` 单策略类（含 legacy combo） |
|------|-------------------------------|------------------------------------------|
| 触发方式 | **状态**：条件成立期间持续记票 | **边沿**：仅交叉/突破的当根返回 side |
| 组合计票 | 对组件状态计数 | `ComboVoteStrategy` 对子策略边沿信号计数 |
| 信号定义 | recommendation 转移点 | 子策略边沿事件同根巧合 |
| MA 相等 | 不投票（中性） | 旧 dashboard 曾判 SELL |
| 用途 | dashboard / monitor / Web·CLI 回测 | 单策略回测、walk-forward、`experiments/` |

`ComboVoteStrategy`（legacy）已标 deprecated，仅为 `experiments/` 兼容保留，行为不变；
生产请用 `signals.evaluate_symbol` 与回测适配器 `signals.ComboVoteStateStrategy`。

### 回测撮合规则（A7 统一引擎）

`backtest/engine.py` 是全系统唯一撮合实现，市场微观结构规则由可注入的
`backtest/rules.py::MarketRules` 控制，调用方按标的推导：

| 规则 | 默认 | 说明 |
|------|------|------|
| 涨跌幅限制 | 按板块 | `price_limit_for_symbol`：创业板 300/301、科创板 688/689 → ±20%；北交所 `.BJ` → ±30%；主板 → ±10%。**不识别 ST/*ST（±5%）**，需精确请显式传 `MarketRules(price_limit_pct=...)` |
| 涨停拦截 | 开 | 开盘价（next_open）或收盘价（close）触及涨停 → 买不进；触及跌停 → 卖不出，记入 `skipped_fills` |
| close 模式拦截 | 开 | A7 起 `close` 撮合也拦截涨跌停（旧版不拦截，偏乐观）；`enforce_limit_on_close=False` 可关闭对照 |
| T+1 | 开 | 当日买入禁止当日卖出，按 bar 日历日比较（日线天然满足，分钟线实际生效） |
| 成交量参与率 | 关 | `max_volume_participation`（如 0.1）设定后单根 bar 成交不超过其成交量 ×参与率，超出截断记录 |
| 一手 | 100 股 | 成交股数向下取整到 `lot_size` 整数倍 |

> ⚠️ **`Signal.strength` 不参与仓位计算**：引擎一律按 `RiskConfig.max_position_pct`
> 与可用现金决定买入股数，strength 仅用于展示/通知排序。把信号强度映射为仓位是
> D 阶段"模型期望收益 → 仓位"的工作，本期刻意不做，以保持历史回测可比性。

### 回测记录落库（A8）

`tradepilot backtest` 与 `tradepilot walkforward` **默认把结果写入 `backtest_results`**
（`--no-save` 退出），与 Web 端同表同格式（共用 `backtest/serialize.py`），前端历史回放
可直接消费 CLI 记录：

| 字段 | 说明 |
|------|------|
| `user_id` | CLI 记录为 `NULL`；`list_user_backtests` 按 user_id 过滤，故不进任何用户列表 |
| `run_kind` | `backtest` \| `walkforward` |
| `params_json` | 入参快照（days/strategy/execution/splits/warmup/select_by/profile/params…） |
| `profile_source` | 画像来源：`explicit`/`system`/`config:名字`/`default`（walk-forward 单策略仍记 `cli`），详见 A5 |
| `result_json` | 回测完整结果（权益曲线/成交/指标 + A5 provenance），walk-forward 此列为空 |
| `report_json` | walk-forward 全量分段报告（每段训练/测试下标、最佳参数、样本内外指标） |

`tradepilot backtests list [--kind backtest|walkforward]` 查看 CLI 落库记录。

### 回测策略解析与参数传递（A5）

改造前 Web/CLI 回测对 5 个单策略一律**无参默认构造**（`cls()`），既不读
`config.strategy_profiles`，也不读用户在 Web 保存的系统策略——**监控实际跑的画像从未被
回测验证过**，且 `api/app.py` 与 `cli.py` 各写一份策略注册表，互相漂移。A5 把"解析哪个
策略、用哪套参数、来源是什么"收敛成单一函数 `signals/backtest_profile.py::resolve_backtest_strategy`，
Web 端点与 CLI 共同消费，并输出 **provenance** 进响应与落库。

**第 6 个回测策略 `combo_vote`**：回测可选策略由 5 个单策略扩为 `ma / rsi / macd /
bollinger / donchian / combo_vote`，其中 `combo_vote` 走与 dashboard/monitor 完全相同的
`signals` 统一投票口径（`ComboVoteStateStrategy`），从此"监控喊的单"能被同一套参数回测。

解析优先级（高 → 低）：

| 优先级 | 入口 | `profile_source` | 说明 |
|--------|------|------------------|------|
| 1 显式 params | Web `params`；CLI `-p key=value` / `--vote-threshold` | `explicit` | 按策略白名单校验后构造（单策略 `cls(**params)`；combo → ProfileSpec） |
| 2 `profile=system` | Web `profile:"system"`；CLI `--profile system` | `system` | 读 `user_store.get_system_strategy(symbol)` 参数 → combo_vote；无则诚实落回缺省链 |
| 3 `profile=名字` | Web `profile:"<名>"`；CLI `--profile <名>` | `config:名字` | 读 `config.strategy_profiles[名字]` → combo_vote；未知名 → 报错 |
| 4 缺省 | 不给 params/profile | `default` | 单策略 = 类默认参数（与改造前 `cls()` 一致）；combo_vote = 缺省画像链（config symbols 绑定 → DB system → 全局默认三件套 `DEFAULT_PROFILE`） |

> **profile 比 strategy 下拉更强**：给定 profile 时一律解析为 `combo_vote`（一个 profile
> 本就是完整投票规格）；params 最显式，优先级最高。全局默认三件套 `DEFAULT_PROFILE` 抽成
> `backtest_profile.py` 单一常量，monitor 的 `_default_profile` 改为引用它，消除双份硬编码。

入口对照：

| 端 | 参数传递 | 画像 | 校验失败 |
|----|----------|------|----------|
| Web | `BacktestRequest.params`（全 Optional 字段 + 白名单 `model_validator`） | `BacktestRequest.profile` | HTTP 422 |
| CLI backtest | `--param/-p key=value`（可多次）、`--vote-threshold 1~3` | `--profile system\|<名>` | 打印错误 + 退出码 2 |
| CLI walkforward | `--grid-json '{"fast":[5,10]}'`（键须在白名单内）覆盖内置网格 | `--profile`（仅 combo_vote 解析基准画像，网格在其上叠加） | 打印错误 + 退出码 2 |

**provenance 透明化**：`profile_source` + `strategy_params`（实际生效参数摘要）既进
`/api/backtest` 响应与 `result_json`（前端结果卡片显示"这次到底跑了哪套参数、来自哪里"），
也写 `backtest_results` 的 v12 列，供历史回放核对。

**四方一致性**（`tests/test_app_backtest.py` + `tests/test_backtest_profile.py` 钉死）：
策略注册表 `BACKTEST_STRATEGIES` == 请求 `Literal` == 标签表 == `params_schema` 键集；且
`params_schema` 每个 `default` == 对应策略类 `inspect.signature` 默认值（杜绝 schema 与实现漂移）。
`/api/meta/backtest-options` 下发 `strategies[].params_schema` 与 `profiles`，前端据此**动态
渲染参数表单**（combo_vote 展开阈值 + 高级参数折叠区），不再硬编码字段。

### 复权混接防护（A9）

前复权（qfq）序列以"拉取时最新交易日"为锚，上游每次除权都会整段重算历史。增量刷新
若回看窗口太短，会把新锚数据接到旧锚存量上，在接点产生虚假跳空污染信号与盈亏。防护链：

- **检测**：`data/adjustment.py::detect_rebase` 比对库内存量与新拉取序列重叠日的 close 比值，
  连续≥2日方向一致、幅度近似恒定 → 判上游 qfq 重算；
- **自愈**：`stock_service.refresh` 命中后**全量重拉**（扩窗覆盖全部存量历史）整段覆盖 upsert，
  回看窗口由 `data.refresh_overlap_days`（默认 30）配置；
- **溯源**：`daily_bars` 新增 `adjust`/`adj_anchor_date`/`data_version`（`source|anchor|utc_ts`）三列，
  供信号台账与 ML 数据集引用做前视/混接溯源；
- **巡检**：`tradepilot data audit --adjust` 用已有 `pct_chg` 列离线校验全库 close 环比一致性，
  报告存量混接点。

### 信号台账（B2 · schema v13）

改造前系统只在通知里"喊单"，从不记录喊过什么、后来对没对——胜率/期望无从谈起，更无法
防"几乎不交易刷高胜率"。`storage/signal_ledger.py` 把每一次决策落成一行可追溯、可回填、
可统计的记录，闭合**预测 → 验证**回路：

```
   决策当时（T 日）              未来 bar 入库后               汇总
   record_signal/        ──▶    backfill_outcomes     ──▶    signal_stats
   record_decision              （用 B1 horizon_label        （coverage / 胜率 /
   （label_status=pending）       回填 fwd_net_return）          平均净收益）
```

**口径与 [`docs/prediction-target.md`](docs/prediction-target.md) 完全一致**：T 日收盘决策 →
entry=open[T+1] → 持有 `horizon`（默认 5）交易日 → exit=open[T+1+horizon]，净收益镜像
`backtest/costs.py::CostModel`。

v13 新增两张表：

| 表 | 用途 |
|----|------|
| `signal_ledger` | 一行一条决策：标的·日·来源·建议·票数·组件 / 模型字段（p_win·expected_ret·downside_mae）/ 回填结果（entry·exit·fwd_net_return·fwd_mae·fwd_ret_aux·label_status）。冲突键 `UNIQUE(symbol, trade_date, source, provisional)` |
| `kv_store` | 通用键值表，monitor 收盘例程记"上次执行日"等状态，防重启重复跑 |

关键语义（防台账被误用为粉饰工具）：

- **幂等 upsert**：monitor 重启重复记录同一信号不产生重复行；冲突时只更新决策/模型字段，
  **绝不触碰已回填的结果列**（`fwd_*`/`exit_price`/`label_status`/`filled_at`），重复记录不会
  把已完成的回填重置。
- **provisional 0/1 各一条**：盘中预估 bar 与收盘确认 bar 分别落库，互不覆盖。
- **回填诚实三态**：决策日在库且未来 bar 足够 → `filled`；不足以走完整 horizon → 保持
  `pending`（待更多数据，**绝不截断尾价冒充**）；决策日缺 bar → `bad_data`。
- **coverage 护栏**：`signal_stats` 必输出 `coverage = 有可执行信号(BUY/SELL)的标的·日单元数
  / 全部已评估单元数`；胜率/平均净收益**仅在 filled 行上计算**，pending/bad_data 不计入。

写入方与 CLI：

| 命令 | 作用 |
|------|------|
| `tradepilot backtest <symbol> --write-signals` | 把统一 combo 口径（默认画像）的历史 BUY 事件批量写入台账，并**把本次 bars 落库**使信号能用同源 bar 立即回填；带 `backtest_id` 关联（`--no-save` 时为空） |
| `tradepilot signals backfill [--symbol] [--aux-horizon 10]` | 用 DB 已有日线 + B1 标签回填 pending 行的前瞻净收益/下行风险（含 10 日辅助标签 `fwd_ret_aux`） |
| `tradepilot signals stats [--source]` | 输出 coverage / 胜率 / 平均净收益 / 状态与建议分布（含偏差免责声明） |
| `tradepilot signals list [--status] [--source] [--symbol] [--limit]` | 读回台账行 |

> A3 monitor 收盘例程（`provisional 0/1` 各一条 + `kv_store` 防重）落地后将成为第二个写入方，
> 与 `--write-signals` 共用 `record_decision` 入口。
>
> ⚠️ 台账绩效是**历史信号的事后验证**，含样本内/幸存者偏差，不构成收益承诺；可晋升的可信
> 基线须以 D 阶段严格 OOS 评估为准。

### 市场数据基建（C1/C2 · schema v14）

改造前指数历史**从不落库**：每次回测的基准对比都实时拉 Tushare，离线即降级、无 token 即
空白；市场宽度（涨跌家数/涨跌停）更是每轮现算、用完即弃，无法回看"昨天是普涨还是普跌"。
C1/C2 把这两类市场级时序落进独立表，为基准对比、市场总览、（D 阶段）市场环境特征提供
**DB 优先、离线可重建**的数据底座。

v14 新增两张表：

| 表 | 用途 |
|----|------|
| `index_daily` | 四大指数（沪深300 / 上证 / 深证成指 / 创业板指）日线 OHLC·pct_chg·amount·vol。冲突键 `UNIQUE(index_code, trade_date)` |
| `market_daily` | 每日市场宽度快照：advancers·decliners·unchanged·limit_up·limit_down·total_amount·up_ratio。主键 `trade_date`（同日重复写覆盖为终态） |

**C1 指数日线**走三级降级链（复刻 `stock_service.refresh_quotes` 的 tier 模式，每层失败记
`errors`、全部失败才 raise）：

```
tushare pro.index_daily  ──▶  akshare index_zh_a_hist  ──▶  akshare stock_zh_index_daily（新浪）
（有 token 时的首选）          （东财，中文列名）              （全历史，本地按区间裁剪）
```

消费方（`app.py` 基准对比、CLI `--benchmark`）统一改走 `load_index_bars(index_code, ...)`：
**DB 优先**，仅当库内完全缺该区间且有 token/网络时才**有界补拉一次**；补拉失败则诚实返回
库内现有（可能为空），**绝不抛错**——保证离线环境 benchmark 优雅降级而非崩溃。

**C2 市场宽度**——⚠️ **诚实面对现实：没有免费的历史宽度 API**。akshare/tushare 免费档都
不提供"某天有多少只涨停/多少只上涨"的历史序列，因此 `market_daily` 采用**增量积累制**：

> monitor 收盘例程（C5）/ `tradepilot data refresh --market`（C4）用**当日** `stock_quotes`
> 终态快照经 `aggregate_breadth` 聚合后写入。每跑一天积一天，历史**无法事后批量回填**。

这带来一个必须写明的**数据局限**：

- 新部署的库，`market_daily` 从空表开始，只有"启用之后"的交易日才有宽度记录；
- 因此任何依赖市场宽度的 D 阶段特征，对**缺失日一律 NaN + 组降级**（见特征管道），
  绝不用相邻日插值或常数填充造假；
- 想要更长的宽度历史，只能靠持续运行积累，或接入付费数据源——本项目不假装能免费回填。

**涨跌停口径修复**（顺带修掉一个长期 bug）：改造前市场总览用 `±9.8%` 一刀切判定涨跌停，
把创业板/科创板（±20%）、北交所（±30%）的正常大涨误判成"涨停"。`aggregate_breadth` 改用
A7 的 `price_limit_for_symbol(symbol)` **分板块判定**（主板 10% / 创业板·科创板 20% /
北交所 30%），带 `_LIMIT_HIT_TOLERANCE_PP=0.2` 的**绝对**容差。

> 为什么是绝对 0.2pp、而不是引擎撮合用的相对 `0.998`？两者作用对象不同：引擎比较的是
> **价格**（`price >= prev_close*(1+pct)*0.998`），而宽度分类比较的是 **change_pct**。
> A 股价格四舍五入到 0.01 元会让低价股的真实涨停显示成 ~9.95%，相对阈值会漏掉它们；
> 绝对 0.2pp 既精确保留了主板原口径（阈值 9.8），又把创业板/科创板修正到 19.8、北交所 29.8。
> 这是刻意不统一的两处容差，详见 `data/market_service.py` 注释。

### 行业数据基建（C3/C4/C5 · schema v14）

改造前个股只有 `stock_basic` 带的一个**静态行业标签**（"银行"/"小金属"），既无行业指数行情、
也无成分股归属，D 阶段"个股 vs 所属行业板块"的相对强弱特征根本无从算起。C3 把东财行业板块
落进三张独立表，C4 给出 CLI/只读 API，C5 把它挂进 monitor 收盘例程（默认关）。

v14 再增三张表（与 C1/C2 同属 v14，不另起版本号）：

| 表 | 用途 |
|----|------|
| `industry_boards` | 板块登记表：`board_code`（主键，如 BK0475）·`board_name`（如"银行"）·`source='em'`。name↔code 映射的唯一来源 |
| `industry_board_bars` | 板块日线 OHLC·pct_chg·amount·turnover_rate。冲突键 `UNIQUE(board_code, trade_date)` |
| `industry_membership` | 成分股归属：主键 `(board_code, symbol)` + `as_of` 观察日 |

**东财为唯一主源**（探查确认 tushare 120 积分档无免费行业指数历史）：`stock_board_industry_name_em`
（板块登记）/ `stock_board_industry_hist_em`（板块日线）/ `stock_board_industry_cons_em`（成分股）。
两个必须写明的接口坑：

- `hist_em` 与 `cons_em` 的入参是板块**名称**而非代码——服务内部用 `industry_boards` 登记的
  name↔code 映射桥接：拉数传 name、落库记 code。
- `cons_em` 返回 **6 位裸代码**（"600000"），经 `StockDataService.normalize_symbol` 补后缀
  （→"600000.SH"）；malformed 跳过。列名映射集中在 `data/industry_service.py` 顶部常量表，
  东财改列名只需改一处。

**部分成功 + 显式 RefreshReport**（沿用 C1 tier 降级哲学）：`refresh_for_symbols(symbols)` 只为
股票池所属板块拉数据——先解析 symbol→板块（**DB 成分快照优先**，兜底用 `stock_catalog.industry`
静态标签按板块名**模糊匹配**），板块去重后逐板块拉，每板块独立 try/except + 限流 sleep（可配
`data.industry_rate_limit_delay`，默认 0.5s）。报告含 `boards_refreshed/boards_failed/
symbols_unresolved`——**解析不出的 symbol 诚实记入 unresolved，绝不臆造板块归属**；失败板块的
行业特征在 D 阶段自动 NaN 降级。

> ⚠️ **成分股是"最新单快照"而非逐日历史**：`industry_membership` 主键 `(board_code, symbol)`
> 决定同一 (板块, 个股) 只存最新 `as_of`，**不是 point-in-time**。个股调仓换板块后旧归属被覆盖。
> D 阶段读取时按 `as_of ≤ trade_date` 取最近快照，并在数据集 manifest 标
> **`industry_point_in_time=False`** 前视警告——诚实承认"用今天的板块归属回看历史"存在轻微
> 幸存者/前视偏差，而非假装能重建任意历史日的成分。

**C4 入口**（纯读 DB、零网络）：

- CLI：`tradepilot data refresh [--market] [--industry] [--pool config|watchlist] [--days N]`
  ——`--market` 刷指数+宽度，`--industry` 按股票池刷板块，两个 flag 至少给一个（否则 exit 2）。
- API（登录可见）：`GET /api/market/history`、`/api/market/breadth`、`/api/industry/boards`、
  `/api/industry/boards/{code}/bars`、`/api/industry/boards/{code}/members`。**只读端点一律
  `fetch=False`**——空库时诚实返回空，绝不触发网络补拉（`test_history_never_fetches_on_empty_db`
  钉死此约束）。前端本期不强制消费，D 阶段环境面板预留。

**C5 收盘例程接入**（在 A3 finalize 末尾追加 `_finalize_market_data`，全部独立 try/except，
单项失败不影响个股收盘重评/通知）：① `refresh_quotes` 终态 → `record_market_breadth` 写
`market_daily`；② `MarketDataService.refresh_indexes` 刷 4 指数；③ 可选行业增量（`monitor.refresh_industry`
默认 false，开启后按 config 股票池调 `refresh_for_symbols`）。

### ML 特征管道与数据集（D1/D2 · schema v15）

C 阶段把指数/宽度/行业落库后，D1 才有原料把"市场环境"喂进个股信号。D1（`ml/features.py`）
建特征、D2（`ml/dataset.py`）建数据集，两者共同遵守一条**铁律**：

> **训练与线上打分调同一个函数 `symbol_feature_frame`**——同一 (symbol, trade_date) 在
> `build_dataset`（训练）与 D5 `scoring`（线上）算出的特征逐列一致，从根上杜绝 train/serve
> 偏斜。特征**只依赖 DB 可重建字段**（日线/指数/宽度/板块），不掺任何运行时才有的状态。

**四组特征，`--groups` 可开关**（路线图"每次只加一组验证增益"的机制保障）：

| 组 | 代表特征 | 来源 |
|----|----------|------|
| `signal` | `buy_count`/`sell_count`/`vote_threshold`、`rec_*` one-hot、各组件 `comp_*`(±1/0)、`state_age`、`max_strength` | 直接调 A 阶段 `signals/`——规则引擎输出即特征 |
| `price_volume` | `ret_1/5/20`、`close_ma5/20_ratio`、`rsi14`、`bb_pos`/`bb_width`(+60日分位)、`vol_ratio`、`amount_z20`、`breakout_20`、`atr_pct`(+60日均值比)、`high_low_range` | 复用 `indicators.py`，与 dashboard/monitor/回测**逐位一致** |
| `market` | `idx_ret_5/20`、`idx_close_ma20_ratio`、`idx_vol20`、`up_ratio`(+ma5)、`limit_up_count`、`total_amount_z` | C1 `index_daily` + C2 `market_daily` |
| `industry` | `board_ret_5/20`、`board_close_ma20_ratio`、`rel_strength_5/20`(个股−板块) | C3 `industry_board_bars` + `industry_membership` |

**因果性是硬约束**：每个特征在 T 日只用 ≤T 的数据；标签用 T+1 开盘（见 `docs/prediction-target.md`）。
`assert_no_lookahead` 是开发期探针——把序列**尾部 10 根 bar 换成极端值**（交替 ×10/×0.1，
避免比例特征对均匀缩放的尺度不变性掩盖泄漏），断言 T≤n−11 的所有特征行**逐列不变**；
尾行确因扰动剧变则证明探针非平凡。缺组**绝不插补/造假**：market/industry 无数据时填 NaN
并置 `_has_market=0`/`_has_industry=0` 标志列，让下游模型自己识别"这行没有市场环境信息"。

**D2 数据集构建** `build_dataset(symbols, start, end, horizon=5, aux_horizon=10, groups,
profile_resolver, ...) -> DatasetManifest`，每标的一条流水线：

1. `load_daily_bars` → 无数据则 `rejected`（reason `no_data`）；
2. **A9 复权审计门**：`audit_series(rows, tol=0.01).clean == False` → 拒入
   （reason `rebase_suspect@<date>(diff=<x>)`），混接嫌疑的序列绝不进训练集；
3. 解析 `ProfileSpec`（`profile_resolver` 缺省走 `backtest_profile.default_profile`）；
4. `symbol_feature_frame` 出特征 + `label_series(5)`/`label_series(10)` 出标签；
5. 丢标签缺失行、按 `[start,end]` 过滤、`label_win` 转 int。

产物 = `data/ml/<dataset_id>.csv.gz`（pandas gzip，零新依赖）+ `<dataset_id>.manifest.json`，
**`dataset_id` = 帧内容 + 参数的 sha256 前 12 位**，故重复构建幂等命中同一 ID。manifest 落
**v15 新表 `ml_datasets`**（`register_dataset` 按 dataset_id upsert；列表/dict 字段以 JSON 列
编码，`load_dataset_manifest`/`list_datasets` 解码回原生），记全 provenance：行数/正例率、
日期范围、`max_trade_date` 新鲜度戳、symbols、groups、cost_model、**每标的 profile 解析快照**
（kind/vote_threshold/组件 + board_code）、**每标的 data_version**（溯源到 A9 的
`source|anchor|ts`）、`industry_point_in_time=False` 前视警告（承接 C3 单快照局限）。

**CLI**（零网络、纯读 DB + 写文件）：

```
tradepilot ml build-dataset --pool config|watchlist|catalog [--symbols A,B] \
    [--start --end --horizon --aux-horizon --groups signal,price_volume,market,industry] \
    [--index-code 000300.SH --out-dir data/ml --no-register]
```

`--pool catalog` 取全库有日线的标的；`--symbols` 覆盖池；非法组或空池 exit 2。

> ⚠️ **诚实定位**：本地数据停在 2026-04-17、观察池仅 2 只，D 阶段验收**以 `tests/synth.py`
> 合成夹具跑通全管道为准**（3 标的×300 天+指数+板块）。在数据恢复并扩池前，任何由此产出的
> 数据集/模型都只是**机制验证**，不是可交易信号——D3 `promote` 门禁会以样本量/新鲜度/OOS
> Brier 三道闸默认拒绝晋升，UI 标 `demo`/`stale`。

### ML 模型训练与样本外评估（D3/D4 · schema v15）

D2 产出数据集后，D3（`ml/models.py` + `ml/registry.py`）训练模型、D4（`ml/evaluate.py`）评估。
两者共守一条**懒加载铁律**：

> **三个模块顶层只 import numpy**，sklearn/joblib 一律在函数内 `_require_sklearn()` 惰性加载。
> 未安装时 `dashboard`/`monitor`/`serve` 的信号链路照常运行，只有 `ml train`/`ml promote` 报
> 清晰错误（CLI exit 3）。该契约由 `tests/test_models.py::LazySklearnContractTest` 用 **AST 扫描
> 模块顶层 import 语句**钉住——不是靠文档约定。

**训练协议：锚定滚动 walk-forward**（复用 B3 `rolling_splits`，purge/embargo 已在切分层处理）。
每折：在 `train_idx` 上拟合 → 在 `val_idx` 上选超参 / 拟合校准器 → 只预测 `test_idx`；把所有折
的 OOS 预测拼成完整样本外序列。**模型选择永远看不到 test**，对外服役的工件 = 末折的
model + calibrator。

| kind | 模型 | NaN 处理 | 校准 |
|------|------|----------|------|
| `logreg` | `LogisticRegression(max_iter=1000)`，`C∈{0.01,0.1,1}` 按 val 折 Brier 选 | `SimpleImputer(median, add_indicator=True) + StandardScaler` Pipeline | `CalibratedClassifierCV(method="sigmoid", cv="prefit")` 在 val 折拟合 |
| `hgb` | `HistGradientBoostingClassifier(max_iter=200, lr=0.06, max_leaf_nodes=15, l2=1.0)`，val 折早停 | **原生容忍**——D1 缺组置 NaN 无需插补 | 同上 |

目标 `win5`（分类·5 日胜）/ `ret5`（回归·5 日净收益）/ `mae5`（回归·5 日最大不利偏移 =
下行风险）。**只有分类做校准**（回归无概率可言）；回归摘要给 MAE/R²，分类给 AUC/Brier/ECE/
覆盖率@阈值。

**晋升门禁（4 道闸，`promote(model_id, force=False)`）**：

| 门 | 条件 | 不过的后果 |
|----|------|-----------|
| `min_samples` | `n_rows ≥ 800` | 质量门 → force 落 **`demo`** |
| `min_positives` | `n_positive ≥ 80` | 质量门 → force 落 **`demo`** |
| `max_staleness` | 数据集 `max_trade_date` 距今 ≤ 120 天 | 新鲜度门 → force 仍可 **`promoted`** 但 `stale=true` |
| `beats_base_rate` | 分类：OOS Brier < 常数基线 Brier `p̄(1−p̄)` | 质量门 → force 落 **`demo`** |

全过 → `status='promoted'`、`stale=false`，并**退役同 (target, horizon) 的旧在位模型**
（`retire_promoted_models`）。不过且未 force → 维持 `candidate`、CLI **exit 1**。不过但 force →
质量门任一失败即 `demo`（演示工件，`load_promoted_model` 默认**不返回**，需 `include_demo=True`），
仅新鲜度失败则 `promoted` + `stale`。状态枚举：`candidate | promoted | retired | demo`。

**工件落盘** `data/ml/models/<model_id>/`：`model.joblib` + `calibrator.joblib` + `meta.json`
（provenance：dataset_id、feature_groups、n_features、split_protocol、sklearn_version、status、
stale）+ **`oos.npz`**（`oos_idx`/`p_oos`/`y_oos`/`net_ret`/`rec_buy`/`trade_dates`）。
`model_id = f"{kind}-{target}-{sha256[:10]}"`，hash 含 `trained_at` 故每次训练唯一；元数据落
**v15 新表 `ml_models`**（`register_model` 按 model_id upsert，四个 `*_json` 列编码
feature_groups/selected_params/metrics/warnings）。

**D4 评估：完全免 sklearn**——只读 `oos.npz` + `meta.json`，用 B4 `calibration.py` 的 numpy
纯函数算完所有指标（`tests/test_evaluate.py` 刻意只写这两个文件、不写 `model.joblib`，证明
评估链路不依赖 joblib）。三段报告：

1. **判别**：AUC / LogLoss / Brier vs 常数基线 Brier；
2. **校准**：logit 空间 slope/intercept、ECE、10 桶 reliability；
3. **决策对比表**（直接回答"模型过滤比不过滤好多少"）：同一标签、同一 OOS 折上并列四方——
   `rule_baseline`（规则引擎全部 BUY 票，`rec_buy`）｜`model_thr{0.50,0.55,0.60}`（模型过滤
   `p ≥ thr`）｜`buy_hold`（全买）｜`index`（000300 同窗 horizon 日收益，缺指数行则省略并警告）。
   每行列覆盖率 / 胜率 / 平均净收益 / 每信号期望 / 样本数。

**警告区诚实标注**（`render_text` 单列一节）。硬警告带 `⚠️` 前缀：数据滞后（stale）｜演示模型
（demo）｜OOS 样本偏少（`n_oos<200`，指标方差大）｜正例率失衡（`base_rate` 落在 0.2–0.8 之外，
AUC/Brier 须结合覆盖率看）｜OOS 单一类别（AUC 无定义）｜未跑赢常数基线｜行业 point-in-time
缺失（轻微前视）。两条**口径提示**用 `（…）` 而非 `⚠️`（是说明而非缺陷）：省略指数行
（`index_daily` 无同窗数据）｜OOS 折内规则引擎零 BUY 票（`rule_baseline` 退化为空）。

**CLI**：

```
tradepilot ml datasets                                  # 列出已注册数据集（train 需要 --dataset）
tradepilot ml train --dataset <id> [--model logreg|hgb] [--target win5|ret5|mae5] \
    [--splits 5 --embargo 2 --val-ratio 0.2 --threshold 0.5] [--models-dir] [--no-register]
tradepilot ml list [--status candidate|promoted|retired|demo]
tradepilot ml promote <model_id> [--force] [--min-samples --min-positives --max-staleness]
tradepilot ml eval --model <id> [--threshold 0.7 ...] [--index-code 000300.SH] [--json path]
```

退出码约定：工件/数据集/模型不存在 → **2**，sklearn 未安装 → **3**，门禁拒绝晋升 → **1**。

> ⚠️ **诚实定位（重要）**：合成夹具是**随机游走弱信号**，logreg 在其上 OOS AUC≈0.56、
> Brier≈0.2536 **高于**常数基线 0.2500 → `beats_base_rate` 不过 → 默认拒晋升，`--force` 也只能
> 落 `demo` + `stale=true`。**这是护栏正确工作，不是 bug**。指标质量类断言必须用
> `synth.ml_frame`（逻辑斯谛真值链路，logreg 可达 AUC 0.82 / Brier 0.171 < 基线 0.244）；
> `seed_market_db` 只用于 CLI 管道与集成测试。数据恢复并扩池后重跑 pipeline，才会产生第一个
> 真正可晋升（`promoted`）的模型。

### ML 预估消费与全管道（D5/D6）

D3/D4 把模型训练出来、评估明白，D5（`ml/scoring.py`）让**看板与监控真正用上它**，D6
（`ml/pipeline.py` + `tradepilot ml pipeline`）把 D1–D5 串成一键回归入口。

**核心区分：`vote_ratio` ≠ `p_win`（两个数并排显示，永不互相冒充）**

| 量 | 来源 | 含义 | 缺位时 |
|----|------|------|--------|
| `vote_ratio` | A1 规则投票（`buy_count / 组件数`） | 规则票占比，**不是概率** | 恒有（无需模型） |
| `p_win` | D5 在位分类模型 | 5 日胜的**校准概率** | 无 promoted 模型 → `forecast=null` |

A2 已把 `confidence` 一词全链路废除；D5 只**追加** `p_win`，不回潮 `confidence`。看板
`market_detail` 多一个 `"forecast": {...} | null` 字段；前端有 `forecast` 就显示"模型预估(5日)：
胜率 58% · 期望净收益 +1.2% · 参考下行 −3.1%"，没有就只显示"方向一致度 x%（规则票，非概率）"。

**`SignalScorer.try_load()` 懒加载 + 优雅回退**：sklearn 未装 / 无 `promoted` 模型 / 工件缺失 →
返回 `None`，看板监控照常出规则信号（`forecast=null`），绝不因 ML 缺席而崩。`demo` 模型默认
**不取**（需 `include_demo=True`），取到 `stale`/`demo` 时在 `warnings` 里显式打出。

**train/serve 一致性（D 阶段头号风险，硬测试钉死）**：`score_symbol` 内部构建特征行时，与
`build_dataset` **调用同一个 `assemble_dataset_frames`**，并读取数据集 manifest 里的**训练画像
快照**（`_spec_snapshot`）复现训练时的 `ProfileSpec`——不是用当前 config 的画像。
`tests/test_scoring.py::TrainServeConsistencyTest` 逐 (symbol, trade_date) 逐列 `np.allclose`
断言 scorer 的特征向量 == 数据集那一行，且 `assertEqual(checked, manifest.n_rows)` 防空跑。

> 🐛 **修了一个真 train/serve 偏斜 bug**：`signals/profile.py::_component_params` 曾忽略 canonical
> 组件字典里的 `params` 子节（`{"name","kind","params":{...}}`，正是 `_spec_snapshot` 写进 manifest
> 的形态），导致 D5 serve 端把**默认 MA5/20+RSI14** 的票喂给一个按 **MA5/10+RSI7** 训练的模型——
> 静默偏斜、零报错。修复：解析前把 `params` 子节抬平进容器，再走原有优先级链（嵌套节 → params →
> 直接键 → 扁平前缀键 → 默认）。回归测试 `test_signals_voting.py::test_components_nested_params_roundtrip`。

**监控台账回填（B2 预留列落地）**：收盘例程 `_finalize_after_close` 给每个确认信号算 forecast，
`SignalNotifier.send` 经 `extra_info['forecast']` 把 `ScoreResult.to_dict()` 交给飞书卡片渲染
（`feishu.py` 认这个键，画出 🧠 模型预估块）；同时把 `model_id / p_win / expected_ret /
downside_mae` 四列写进 `signal_ledger`。**预估是增强项**：其序列化单独 try/except，坏掉的
`to_dict()` 只会让这条通知不带预估，**绝不吞掉整条信号通知**（`monitor/main.py`；回归测试
`test_monitor_daily.py::ForecastPlumbingTest`、`test_watchlist_and_feishu.py::FeishuForecastCardTest`）。

**D6 一键管道 `tradepilot ml pipeline`**（`ml/pipeline.py`，不 import click、进度走 `progress`
回调，故可脱离 CliRunner 直接测）：

```
tradepilot ml pipeline --pool config|watchlist|catalog [--symbols A,B] [--start --end] \
    [--horizon 5 --aux-horizon 10] [--groups signal,price_volume,market,industry] \
    [--models logreg,hgb] [--target win5] [--aux-targets ret5,mae5] \
    [--splits 5 --embargo 2 --val-ratio 0.2 --threshold 0.5 ...] \
    [--out-dir --models-dir --report-dir reports] [--promote] [--force]
```

串起 build-dataset → train（kind×target 全组合，各独立 candidate）→ eval（**只对分类目标出决策
对比表**，回归的 AUC/Brier 无意义）→〔可选〕promote → markdown 报告（`reports/ml-pipeline-<dataset_id>.md`）。
退出码沿用 D3/D4：**2** = 空池/非法 kind/非法组/缺工件，**3** = sklearn 未装，**0** = 跑通
（哪怕门禁未过——报告照写）。

> ⚠️ **默认不晋升（`promote_models=False`）**：一次回归跑悄悄换掉看板/监控正在用的在位模型是
> **事故**，不是便利。要动在位模型必须显式 `--promote`，且仍受 D3 四道门禁约束。报告尾部固定一段
> **诚实定位**声明 + 每个数字都标了数据集新鲜度与 OOS 样本数，缺失一律渲染 `n/a`（绝不写成 0），
> 空子集（如 OOS 折内规则零 BUY 票的 `rule_baseline`）渲染 `—`（不写 `0.00%`，那会被读成"测过了、
> 收益为零"）。详见 `docs/ml-signal-quality.md`。

### 监控统一切日线（A3）

改造前 monitor 与网页/CLI 回测**口径分裂**：监控每标的每轮拉近 5 天 1min 线（N 次网络/轮、
分钟线不落库），而回测在日线上跑——**监控实际跑的策略从未被回测验证过**；且个股通知只看
strongest_signal、买入分支不查 sell_count（2 买 1 卖也喊买）、每轮重复推送。A3 把监控收敛到
与回测/看板同源的**日线统一口径**：

```
每轮（盘中 interval_seconds=300）：
  refresh_quotes()  ← 全市场一次性快照（替代 N 次/轮分钟拉取）
     │
     ▼  逐标的
  load_daily_bars(qfq 历史) + 快照合成 provisional bar
     │  ├─ 复权防护：快照原始 pre_close vs 库内 qfq last close 偏离 >0.5%
     │  │            → 判当日除权 → scale_ohlc 缩放历史对齐 + 打标 rebased
     │  │            → 列入收盘强制全量重拉名单（衔接 A9）
     │  └─ signals.evaluate_symbol（strict 状态投票，与回测/看板同一实现）
     ▼
  仅"当日新 decision_events"（转移点落在最后一根 bar）触发个股通知
     │  去重键 (symbol, side, trade_date, provisional) → 盘中/收盘各一次、同日不重发
     ▼
  写信号台账 provisional=1（盘中预估）

收盘例程（finalize_after，默认 15:05，每交易日一次，kv_store 防重启重复）：
  refresh 个股日线（A9 检测）→ 确认 bar 重评 → 收盘确认通知 → 写台账 provisional=0
```

关键语义：

- **切日线**：`load_minute_bars` 退出信号链路（测试断言零调用）；历史 = `load_daily_bars`
  （SQLite qfq），当日未收盘 = `stock_quotes` 快照合成 **provisional bar**（`monitor/daily_eval.py`
  纯函数编排，可离线单测）。网络调用 N 次/轮 → **1 次/轮**。数据门槛改日线 **<60 根跳过**。
- **通知门控修复**：2 买 1 卖经 strict 投票判 **CONFLICT**（存在反向票）→ 无边沿事件 → **不推送**
  （旧实现会喊买）；strongest_signal 降级为消息内"触发组件"注解，不再驱动门控。
- **provisional 双通道**：盘中预估 bar 标 `provisional=True`、通知带"⏳ 盘中预估，以收盘确认为准"；
  收盘确认 bar 标 `provisional=False`、通知带"✅ 收盘确认"。二者去重键不同，各推送一次。
- **收盘例程**：`StockDataService.refresh`（含 A9 复权检测）逐标的重拉日线 → 确认重评 → 写台账
  （`record_decision(provisional=0, data_version=…)`，与 B2 `--write-signals` 共用入口）→
  `kv_store['monitor.last_finalize']` 记执行日防重启重复跑。
- **价格预警降级**（`monitor/price_alert.py`，默认关）：分钟级即时提醒不再进信号投票，降级为
  **规则化非投票**预警——日内涨跌幅超阈值 / 逼近涨跌停（用 A7 `price_limit_for_symbol` 分板块，
  替代旧 ±9.8% 一刀切）/ 穿越参考价；独立去重键 `(symbol, kind, trade_date)`，与信号通道**互不
  串扰**；消息前缀 `⚡价格预警（非交易信号）`，明确不含买卖建议。
- **画像路由收敛**：删 `_run_combo_vote_profile/_run_grid_combo_profile/_run_combo_profile/
  _run_rsi_profile/_run_macd_profile/_run_ma_profile` 六套分支，全部走 `signals` 单一实现；
  **`breakout` 是唯一豁免**（保留旧路径，标 TODO：以 donchian+rsi 组件表达后并入）。
- `monitor.bar_freq` 默认改 **`daily`**；读到 `1min` 打 warning 解释新语义（分钟线已降级为价格预警）。

新增配置键（`monitor:` 节，全部 `.get` 防御式读取，缺失走默认）：

| 键 | 默认 | 说明 |
|----|------|------|
| `bar_freq` | `daily` | 仅用于识别旧配置并告警，不再驱动取数 |
| `finalize_after` | `"15:05"` | 收盘例程触发时刻（HH:MM），每交易日一次 |
| `refresh_industry` | `false` | 收盘是否顺带增量刷新行业（C 阶段挂钩） |
| `price_alert.enabled` | `false` | 价格预警总开关（默认关，开启不改信号行为） |
| `price_alert.pct_threshold` | `0.05` | 日内涨跌幅触发阈值（比例） |
| `price_alert.near_limit_pct` | `0.02` | 距涨/跌停触发带宽（比例） |
| `price_alert.reference_prices` | `{}` | symbol→参考价（上穿触发） |

### 预期效果

> ⚠️ 下表为**示意性**经验区间，非样本外验证结果，含前视/幸存者偏差风险，不可作为
> 收益承诺。可信基线须以 `tradepilot walkforward`（A6 修复后）与 D 阶段 OOS 评估为准。

| 配置 | 覆盖率 | 胜率 | 最大回撤 |
|------|-------|------|---------|
| 仅 MA | ~60% | 45% | -25% |
| MA + RSI | ~80% | 55% | -18% |
| **MA + RSI + BB** | **~90%** | **60%** | **-15%** |

---

## 📝 使用示例

### 启动监控

```bash
cd /Users/ripple/work\ space/TradePilot
source .venv/bin/activate
tradepilot monitor        # 或只检查单只标的：tradepilot monitor 600309.SH
```

### 测试策略（归档脚本）

```bash
cd /Users/ripple/work\ space/TradePilot
source .venv/bin/activate
PYTHONPATH=src python3 experiments/test_strategies.py
```

### 输出示例

```
📋 综合信号分析
======================================================================

买入信号：2/3
卖出信号：0/3

🟢 综合建议：BUY (多数策略看涨)
```

---

## 🔧 参数调优建议

### 保守型（低频交易）

```yaml
ma_cross:
  fast: 10   # 增大周期，减少假信号
  slow: 30

rsi:
  period: 21     # 更长周期
  oversold: 25   # 更严格的超卖线
  overbought: 75 # 更严格的超买线

bollinger:
  period: 26
  std_dev: 2.5   # 更宽的带，减少触发
```

### 激进型（高频交易）

```yaml
ma_cross:
  fast: 3    # 更敏感
  slow: 10

rsi:
  period: 9      # 更短周期
  oversold: 35   # 更宽松的超卖线
  overbought: 65 # 更宽松的超买线

bollinger:
  period: 14
  std_dev: 1.8   # 更窄的带，增加触发
```

### 默认推荐（均衡型）

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

---

## ⚠️ 风险提示

1. **历史表现不代表未来** - 回测数据仅供参考
2. **黑天鹅事件** - 极端行情所有策略可能同时失效
3. **过拟合风险** - 不要过度优化参数
4. **交易成本** - 高频交易需考虑手续费
5. **流动性风险** - 小盘股可能无法及时成交

---

## 📚 参考资料

- **MA Cross**: 《技术分析》- 约翰·墨菲
- **RSI**: 《RSI 指标实战技法》- 威尔斯·怀尔德
- **Bollinger Bands**: 《布林带》- 约翰·布林格

---

**最后更新：** 2026-09-06
**版本：** v2.2（统一投票口径 + 监控切日线 A3 + 回测策略解析/参数传递 A5 + 信号台账 B2 / schema v13）
