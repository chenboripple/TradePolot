# TradePilot 项目分析与优化建议

## 当前定位

项目已经具备策略、行情加载、回测、监控和飞书通知的基本能力，但整体仍处于研究原型向服务化产品过渡的阶段。核心包位于 `src/ripple_tradePilot`，根目录同时保留了大量一次性研究、回测和诊断脚本，运行入口及配置格式尚未完全统一。

## P0：立即处理

### 密钥治理

本地 `config.yaml` 包含真实 Tushare Token 和飞书 Webhook。该文件当前未被 Git 跟踪，但此前 `.gitignore` 没有保护它。应立即轮换已经暴露过的飞书 Webhook，并检查 Git 历史、终端日志和备份中是否存在旧密钥。Docker 方案已将 `config.yaml`、`.env` 排除，并支持环境变量覆盖。

### 自动交易边界

当前系统适合作为研究和信号提示工具，不应直接接入真实下单。策略缺少独立的风控闸门、订单幂等、持仓对账、熔断、交易日历异常处理和完整审计日志。接入实盘前需将“信号生成”和“订单执行”拆成两个独立服务，并要求人工或风控规则确认。

## P1：近期优化

### 配置统一

~~项目同时使用 `feishu.webhook_url` 和 `notifiers.feishu.webhook` 两套结构~~（已统一为 `notifiers.feishu.{enabled,webhook,secret,dashboard_url}`，`TRADEPILOT_CONFIG` 可指定路径）。后续可进一步引入带版本号的配置模型，用 Pydantic 做启动时校验。

### 测试体系

`tests/` 已建立离线测试体系（策略/回测引擎/API/监控通知/数据清洗，CI 在 push/PR 上运行 `pytest tests/ -q`）；根目录访问真实接口的手工 `test_*.py` 已归档至 `experiments/`。后续可补充：带固定行情夹具的回归基线、显式标记的外部接口集成测试层。

### 依赖可复现性

`requirements.txt` 与 `pyproject.toml` 重复维护且版本范围较宽，没有 lock 文件。建议保留 `pyproject.toml` 为唯一依赖来源，用 `uv lock` 或 `pip-tools` 生成锁定文件，并由 Dependabot/Renovate 定期提交升级 PR。

### 异步与限流

监控循环使用 `asyncio.gather`，但 Tushare 和通知调用仍是同步阻塞请求，多个标的不会真正并发，并可能阻塞优雅退出。应使用线程池封装同步 SDK，或统一采用异步客户端，同时增加全局限流、指数退避和熔断。

### 状态持久化

信号去重状态只保存在内存中，容器重启后可能重复通知。回测结果散落在 JSON、CSV 和 SQLite 中。建议将运行状态、通知幂等键和策略版本统一存入 SQLite/PostgreSQL，并为数据表增加迁移机制。

## P2：结构治理

### 代码边界

~~将根目录研究脚本迁入 `experiments/`~~（已完成，38 个脚本归档，见 `experiments/README.md`；根目录仅保留 `monitor_brief.py`、`heartbeat_tradepilot.py`、`install.py`、`setup.py`）。~~CLI 中多个命令仍为 TODO~~（已清理，`tradepilot` 各命令均连接真实服务）。剩余：生成数据和报告移出源码树；`src/data` 不应存储运行期数据库。

### 可观测性

当前主要依赖文本日志。建议添加结构化 JSON 日志、任务耗时、行情延迟、接口错误率、最近成功检查时间和通知成功率指标；API 健康检查应区分存活与就绪状态。

### 回测可信度

统一回测引擎已落地：次日开盘撮合（消除前视）、涨跌停拦截、佣金/印花税/滑点、回撤闸门，并提供 walk-forward 样本外验证（`tradepilot walkforward`）；历史研究报告已逐份加注可信度横幅，研究期脚本归档至 `experiments/`。剩余：停牌、除权复权、成交量约束的固定测试；记录策略与数据版本。

### 元数据一致性

~~README 中仓库名写成了 `TradePolot`~~（已修正）。许可证已统一为 MIT：README、`pyproject.toml` classifier 与 `LICENSE` 文件三处一致。

## 已落地的部署改进

- 非 root、只读根文件系统、移除 capabilities。
- API 与监控拆分为独立容器，并配置重启策略和健康检查。
- 数据及输出目录持久化，不随镜像更新丢失。
- GitHub Actions 自动发布 amd64/arm64 镜像。
- Watchtower 仅更新带标签的 TradePilot 服务，支持滚动重启和旧镜像清理。
- 本地构建与生产镜像部署采用同一份 Compose 服务定义。

## 已落地（2026-09 复核）

- **断链修复**：monitor 配置路径、定期报告发送（原构建卡片后丢弃）、游客首屏公开行情链路。
- **可信度与口径**：持仓日夏普口径及文档纠偏、A 股红涨绿跌全仓统一（`REC_BUY/REC_SELL/REC_HOLD` 常量）、监控信号附止损/止盈参考位、行情脏值中位数过滤、24 份历史报告加注可信度横幅。
- **体验打磨**：飞书 interactive 卡片 + 监控台跳转按钮、K 线/权益曲线触摸十字线、回测表单选项由 `/api/meta/backtest-options` 后端驱动、CLI 输出脱敏飞书密钥。
- **结构治理**：38 个研究脚本归档 `experiments/`（含 README 口径警示）、文档失效引用与旧仓库路径批量修正、测试扩至 138 个并由 CI 执行。
