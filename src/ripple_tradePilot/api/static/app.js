const state = {
  dashboard: null,
  user: null,
  strategies: [],
  backtests: [],
  allStocks: null,
  stockQuery: "",
  stockSortKey: "",
  stockSortDir: "desc",
  stockPage: 1,
  stockPageSize: 100,
  stockFilters: {
    exchange: "",
    board: "",
    industry: "",
    area: "",
    status: "",
    watch: "",
  },
  assetClass: "stock",
  selectedSymbol: null,
  currentMarket: null,
  selectedStrategyId: "",
  appliedStrategyId: "",
  detailRequest: 0,
  view: "overview",
  range: 120,
  indicators: { ma: true, bb: true, volume: true },
  hoverIndex: null,
  authMode: "login",
  pendingView: null,
  editingStrategyId: null,
  backtest: null,
  backtestHover: null,
  backtestChartGeometry: null,
  marketOverview: null,
  autoRefresh: { timer: null, lastRun: 0, busy: false },
};

const elements = {
  refresh: document.querySelector("#refresh-button"),
  generatedAt: document.querySelector("#generated-at"),
  systemDot: document.querySelector("#system-dot"),
  watchlist: document.querySelector("#watchlist"),
  watchlistTitle: document.querySelector("#watchlist-title"),
  watchlistCount: document.querySelector("#watchlist-count"),
  dataAlert: document.querySelector("#data-alert"),
  workspace: document.querySelector("#market-workspace"),
  emptyMarket: document.querySelector("#empty-market"),
  chart: document.querySelector("#market-chart"),
  chartTooltip: document.querySelector("#chart-tooltip"),
  chartEmpty: document.querySelector("#chart-empty"),
  detailStrategy: document.querySelector("#detail-strategy"),
  detailDefaultStrategy: document.querySelector("#detail-default-strategy"),
  strategyCalculating: document.querySelector("#strategy-calculating"),
  accountButton: document.querySelector("#account-button"),
  accountMenu: document.querySelector("#account-menu"),
  accountName: document.querySelector("#account-name"),
  accountAvatar: document.querySelector("#account-avatar"),
  authDialog: document.querySelector("#auth-dialog"),
  authForm: document.querySelector("#auth-form"),
  authError: document.querySelector("#auth-error"),
  strategyDialog: document.querySelector("#strategy-dialog"),
  strategyForm: document.querySelector("#strategy-form"),
  strategyError: document.querySelector("#strategy-error"),
  strategyAssetClass: document.querySelector("#strategy-asset-class"),
  strategyStockField: document.querySelector("#strategy-stock-field"),
  strategyStockSymbol: document.querySelector("#strategy-stock-symbol"),
  strategyFutureField: document.querySelector("#strategy-future-field"),
  strategyFutureSymbol: document.querySelector("[name=future_symbol]"),
  strategyDialogKicker: document.querySelector("#strategy-dialog-kicker"),
  strategyDialogTitle: document.querySelector("#strategy-dialog-title"),
  strategySubmit: document.querySelector("#strategy-submit"),
  stockDialog: document.querySelector("#stock-dialog"),
  stockForm: document.querySelector("#stock-form"),
  stockError: document.querySelector("#stock-error"),
  addStockButton: document.querySelector("#add-stock-button"),
  stockTable: document.querySelector("#stock-table"),
  stockEmpty: document.querySelector("#stock-empty"),
  stockSearch: document.querySelector("#stock-search"),
  stockPagination: document.querySelector("#stock-pagination"),
  stockPagePrev: document.querySelector("#stock-page-prev"),
  stockPageNext: document.querySelector("#stock-page-next"),
  stockPageInfo: document.querySelector("#stock-page-info"),
  stockCatalogStatus: document.querySelector("#stock-catalog-status"),
  syncStocks: document.querySelector("#sync-stocks-button"),
  refreshQuotes: document.querySelector("#refresh-quotes-button"),
  stockFilters: document.querySelectorAll("[data-stock-filter]"),
  stockFilterReset: document.querySelector("#stock-filter-reset"),
  backtestForm: document.querySelector("#backtest-form"),
  backtestSubmit: document.querySelector("#backtest-submit"),
  backtestError: document.querySelector("#backtest-error"),
  backtestResult: document.querySelector("#backtest-result"),
  backtestNotes: document.querySelector("#backtest-notes"),
  backtestChart: document.querySelector("#backtest-chart"),
  backtestChartTooltip: document.querySelector("#backtest-chart-tooltip"),
  backtestChartEmpty: document.querySelector("#backtest-chart-empty"),
  autoRefreshToggle: document.querySelector("#auto-refresh-toggle"),
  autoRefreshInterval: document.querySelector("#auto-refresh-interval"),
  marketOverviewStatus: document.querySelector("#market-overview-status"),
  marketOverviewStale: document.querySelector("#market-overview-stale"),
  marketOverviewRefresh: document.querySelector("#market-overview-refresh"),
  marketOverviewSource: document.querySelector("#market-overview-source"),
  marketOverviewTime: document.querySelector("#market-overview-time"),
  marketOverviewBody: document.querySelector("#market-overview-body"),
  marketOverviewIndices: document.querySelector("#market-overview-indices"),
  marketOverviewBar: document.querySelector("#market-overview-bar"),
  marketOverviewBreadthCounts: document.querySelector("#market-overview-breadth-counts"),
  marketOverviewBreadthLimits: document.querySelector("#market-overview-breadth-limits"),
  marketOverviewTurnover: document.querySelector("#market-overview-turnover"),
  marketOverviewSentiment: document.querySelector("#market-overview-sentiment"),
  marketOverviewMovers: document.querySelector("#market-overview-movers"),
  detailBacktest: document.querySelector("#detail-backtest-button"),
  backtestChartLegend: document.querySelector("#backtest-chart-legend"),
  toastContainer: document.querySelector("#toast-container"),
};

const recommendationLabels = { BUY: "偏多", SELL: "偏空", HOLD: "观望", CONFLICT: "分歧" };
const voteLabels = { BUY: "偏多", SELL: "偏空", HOLD: "中性" };
const assetLabels = { stock: "股票", future: "期货" };
// 回测策略/撮合模式/画像选项：由 /api/meta/backtest-options 下发（后端单一来源），
// 这里只留与后端默认值一致的兜底项，接口失败时表单仍可用默认策略提交。
// strategies[].params_schema 用于动态渲染参数输入（A5）。
const backtestMeta = {
  strategies: [{ value: "rsi", label: "RSI 反转" }],
  executions: [{ value: "next_open", label: "次日开盘撮合" }],
  profiles: [],
};

// A5 provenance：profile_source → 友好中文（结果卡片说明"参数来自哪里"）
const PROFILE_SOURCE_LABELS = { explicit: "显式参数", system: "系统策略", default: "缺省画像" };
// 参数键 → 中文名（与 STRATEGIES.md 口径一致）；未登记的键回退原始名
const PARAM_LABELS = {
  fast: "快线", slow: "慢线", signal: "信号线", window: "通道窗口", std_dev: "标准差倍数",
  period: "周期", oversold: "超卖", overbought: "超买",
  vote_threshold: "投票阈值", ma_fast: "MA 快线", ma_slow: "MA 慢线",
  rsi_period: "RSI 周期", rsi_oversold: "RSI 超卖", rsi_overbought: "RSI 超买",
  bb_period: "布林周期", bb_std: "布林标准差",
};

function backtestStrategyLabel(value) {
  return backtestMeta.strategies.find((item) => item.value === value)?.label ?? value;
}

function backtestExecutionLabel(value) {
  return backtestMeta.executions.find((item) => item.value === value)?.label ?? value;
}

function paramLabel(name) {
  return PARAM_LABELS[name] ?? name;
}

function profileSourceLabel(source) {
  if (!source) return "";
  if (source.startsWith("config:")) return `配置画像 ${source.slice("config:".length)}`;
  return PROFILE_SOURCE_LABELS[source] ?? source;
}

function strategyParamsSchema(value) {
  return backtestMeta.strategies.find((item) => item.value === value)?.params_schema ?? [];
}

function populateBacktestFormOptions() {
  const strategySelect = elements.backtestForm.elements.strategy;
  const executionSelect = elements.backtestForm.elements.execution;
  const keepStrategy = strategySelect.value;
  const keepExecution = executionSelect.value;
  strategySelect.innerHTML = backtestMeta.strategies
    .map((item) => `<option value="${escapeHtml(item.value)}"${item.value === keepStrategy ? " selected" : ""}>${escapeHtml(item.label)}</option>`)
    .join("");
  executionSelect.innerHTML = backtestMeta.executions
    .map((item) => `<option value="${escapeHtml(item.value)}"${item.value === keepExecution ? " selected" : ""}>${escapeHtml(item.label)}</option>`)
    .join("");
  populateBacktestProfileOptions();
  renderBacktestParams();
}

// 画像下拉：system + config 画像名；仅 combo_vote 可用（单策略无画像概念，禁用并清空）
function populateBacktestProfileOptions() {
  const select = elements.backtestForm.elements.profile;
  if (!select) return;
  const keep = select.value;
  const options = [{ value: "", label: "缺省解析链" }, ...backtestMeta.profiles];
  select.innerHTML = options
    .map((item) => `<option value="${escapeHtml(item.value)}"${item.value === keep ? " selected" : ""}>${escapeHtml(item.label)}</option>`)
    .join("");
  syncBacktestProfileState();
}

function syncBacktestProfileState() {
  const select = elements.backtestForm.elements.profile;
  if (!select) return;
  const isCombo = elements.backtestForm.elements.strategy.value === "combo_vote";
  select.disabled = !isCombo;
  if (!isCombo) select.value = "";
}

// 按所选策略的 params_schema 动态渲染参数输入。留空 = 用画像/缺省链解析（provenance 才诚实），
// 填写 = 显式覆盖（→ profile_source=explicit）。combo_vote 把 vote_threshold 置顶、三件套折叠。
function renderBacktestParams() {
  const container = document.querySelector("#backtest-params");
  if (!container) return;
  syncBacktestProfileState();
  const strategy = elements.backtestForm.elements.strategy.value;
  const schema = strategyParamsSchema(strategy);
  if (!schema.length) {
    container.innerHTML = "";
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const field = (f) => `
    <label class="bt-param">
      <span>${escapeHtml(paramLabel(f.name))}</span>
      <input type="number" data-param="${escapeHtml(f.name)}" inputmode="decimal"
        placeholder="默认 ${escapeHtml(String(f.default))}"
        ${f.min !== undefined ? `min="${escapeHtml(String(f.min))}"` : ""}
        ${f.max !== undefined ? `max="${escapeHtml(String(f.max))}"` : ""}
        step="${f.type === "int" ? "1" : "any"}">
    </label>`;
  if (strategy === "combo_vote") {
    const threshold = schema.filter((f) => f.name === "vote_threshold");
    const advanced = schema.filter((f) => f.name !== "vote_threshold");
    container.innerHTML = `
      <div class="bt-param-row">${threshold.map(field).join("")}</div>
      <details class="bt-param-advanced">
        <summary>高级参数（可选 · MA/RSI/布林带三件套）</summary>
        <div class="bt-param-row">${advanced.map(field).join("")}</div>
      </details>`;
  } else {
    container.innerHTML = `<div class="bt-param-row">${schema.map(field).join("")}</div>`;
  }
}

// 收集用户实际填写的参数（留空跳过）；全空 → null，让后端走画像/缺省链而非误判 explicit
function collectBacktestParams() {
  const params = {};
  document.querySelectorAll("#backtest-params [data-param]").forEach((input) => {
    const raw = input.value.trim();
    if (raw === "") return;
    const num = Number(raw);
    params[input.dataset.param] = Number.isNaN(num) ? raw : num;
  });
  return Object.keys(params).length ? params : null;
}

async function fetchBacktestOptions() {
  try {
    const payload = await apiRequest("/api/meta/backtest-options");
    if (payload.strategies?.length) backtestMeta.strategies = payload.strategies;
    if (payload.executions?.length) backtestMeta.executions = payload.executions;
    if (payload.profiles?.length) backtestMeta.profiles = payload.profiles;
  } catch {
    // 元数据接口失败时保留兜底选项，不打断页面
  }
  populateBacktestFormOptions();
}
const AUTO_REFRESH_INTERVALS = { 60: 60000, 300: 300000 };

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatPercent(value, digits = 2, signed = false) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const percent = Number(value) * 100;
  const sign = signed && percent > 0 ? "+" : "";
  return `${sign}${percent.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

function recClass(value) {
  if (value === "BUY") return "rec-buy";
  if (value === "SELL") return "rec-sell";
  if (value === "CONFLICT") return "rec-conflict";
  return "rec-hold";
}

function shortDate(value) {
  if (!value) return "--";
  const parts = String(value).split("-");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : value;
}

function showToast(message, kind = "info") {
  const toast = document.createElement("div");
  toast.className = kind === "error" ? "toast toast-error" : "toast toast-info";
  toast.textContent = message;
  elements.toastContainer.appendChild(toast);
  // 自动消失：先淡出再移除节点
  window.setTimeout(() => {
    toast.classList.add("toast-out");
    window.setTimeout(() => toast.remove(), 300);
  }, 3200);
}

async function apiRequest(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      message = typeof payload.detail === "string" ? payload.detail : message;
    } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function fetchCurrentUser() {
  const payload = await apiRequest("/api/auth/me");
  state.user = payload.user;
  renderAuthState();
}

async function loadProtectedData() {
  if (!state.user) {
    state.strategies = [];
    state.backtests = [];
    renderStrategies();
    renderBacktests();
    renderMarket();
    return;
  }
  try {
    const [strategies, backtests] = await Promise.all([
      apiRequest("/api/strategies"),
      apiRequest("/api/backtests"),
    ]);
    state.strategies = strategies.items;
    state.backtests = backtests.items;
    renderStrategies();
    renderBacktests();
    renderMarket();
  } catch (error) {
    if (error.status === 401) {
      state.user = null;
      renderAuthState();
      return;
    }
    throw error;
  }
}

async function fetchStockCatalog() {
  const payload = await apiRequest("/api/stocks");
  state.allStocks = payload.items;
  populateStockFilters();
  populateStrategyStocks();
  populateSymbolSuggestions();
  renderStockCatalog();
}

function populateSymbolSuggestions() {
  const datalist = document.querySelector("#symbol-options");
  const stocks = state.allStocks ?? [];
  if (!datalist) return;
  // 观察池优先，截断条目数避免 5000+ 选项拖慢浏览器联想
  const watched = stocks.filter((item) => item.is_watched);
  const rest = stocks.filter((item) => !item.is_watched);
  datalist.innerHTML = [...watched, ...rest]
    .slice(0, 1500)
    .map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.name)}</option>`)
    .join("");
}

function populateStrategyStocks() {
  const selected = elements.strategyStockSymbol.value;
  const stocks = state.allStocks ?? [];
  elements.strategyStockSymbol.innerHTML = [
    '<option value="">从全部数据池选择</option>',
    ...stocks.map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)} · ${escapeHtml(item.name)}</option>`),
  ].join("");
  if (stocks.some((item) => item.symbol === selected)) {
    elements.strategyStockSymbol.value = selected;
  }
}

function updateStrategySymbolField() {
  const isStock = elements.strategyAssetClass.value === "stock";
  elements.strategyStockField.hidden = !isStock;
  elements.strategyFutureField.hidden = isStock;
  elements.strategyStockSymbol.required = isStock;
  elements.strategyFutureSymbol.required = !isStock;
}

async function openStrategyDialog(strategy = null) {
  elements.strategyError.hidden = true;
  elements.strategyForm.reset();
  state.editingStrategyId = strategy?.id ?? null;
  if (state.allStocks === null) await fetchStockCatalog();
  populateStrategyStocks();

  const editing = Boolean(strategy);
  elements.strategyDialogKicker.textContent = editing ? "EDIT STRATEGY" : "NEW STRATEGY";
  elements.strategyDialogTitle.textContent = editing ? "编辑策略" : "新建策略";
  elements.strategySubmit.textContent = editing ? "保存修改" : "保存策略";
  elements.strategyAssetClass.disabled = editing;
  elements.strategyStockSymbol.disabled = editing;
  elements.strategyFutureSymbol.disabled = editing;

  if (strategy) {
    const params = strategy.parameters ?? {};
    elements.strategyForm.elements.name.value = strategy.name;
    elements.strategyAssetClass.value = strategy.asset_class;
    elements.strategyStockSymbol.value = strategy.asset_class === "stock" ? strategy.symbol : "";
    elements.strategyFutureSymbol.value = strategy.asset_class === "future" ? strategy.symbol : "";
    elements.strategyForm.elements.profile.value = strategy.profile;
    elements.strategyForm.elements.visibility.value = strategy.visibility;
    elements.strategyForm.elements.ma_fast.value = params.ma_fast ?? 5;
    elements.strategyForm.elements.ma_slow.value = params.ma_slow ?? 20;
    elements.strategyForm.elements.rsi_period.value = params.rsi_period ?? 14;
    elements.strategyForm.elements.rsi_oversold.value = params.rsi_oversold ?? 30;
    elements.strategyForm.elements.rsi_overbought.value = params.rsi_overbought ?? 70;
    elements.strategyForm.elements.bb_period.value = params.bb_period ?? 20;
    elements.strategyForm.elements.bb_std.value = params.bb_std ?? 2;
    elements.strategyForm.elements.vote_threshold.value = params.vote_threshold ?? 2;
  }
  updateStrategySymbolField();
  if (!editing && !state.allStocks.length) {
    elements.strategyError.textContent = "请先到全部数据页面同步股票清单";
    elements.strategyError.hidden = false;
  }
  elements.strategyDialog.showModal();
}

function renderAuthState() {
  const authenticated = Boolean(state.user);
  elements.accountButton.hidden = authenticated;
  elements.accountMenu.hidden = !authenticated;
  elements.accountName.textContent = state.user
    ? `${state.user.username} · ${state.user.role === "admin" ? "管理员" : "普通用户"}`
    : "";
  elements.accountAvatar.textContent = state.user?.role === "admin" ? "A" : "U";
  document.querySelectorAll(".guest-content").forEach((element) => { element.hidden = authenticated; });
  document.querySelectorAll(".authenticated-content").forEach((element) => { element.hidden = !authenticated; });
}

function setAuthMode(mode) {
  state.authMode = mode;
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.authTab === mode);
  });
  document.querySelector("#auth-submit").textContent = mode === "register" ? "注册并登录" : "登录";
  document.querySelector("#auth-password").autocomplete = mode === "register" ? "new-password" : "current-password";
  elements.authError.hidden = true;
}

function openAuth(mode = "login", pendingView = null) {
  state.pendingView = pendingView;
  setAuthMode(mode);
  if (!elements.authDialog.open) elements.authDialog.showModal();
  document.querySelector("#auth-username").focus();
}

async function fetchDashboard() {
  elements.refresh.classList.add("is-loading");
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`API ${response.status}`);
    state.dashboard = await response.json();
    elements.systemDot.classList.add("is-online");
    elements.generatedAt.textContent = `更新 ${new Date(state.dashboard.generated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;

    const available = marketsForAsset();
    if (state.selectedSymbol && !available.some((item) => item.symbol === state.selectedSymbol)) {
      state.selectedSymbol = null;
      state.selectedStrategyId = "";
      state.appliedStrategyId = "";
    }
    state.currentMarket = available.find((item) => item.symbol === state.selectedSymbol) ?? null;
    renderAll();
    if (state.currentMarket && state.view === "detail" && state.selectedStrategyId) {
      await loadMarketDetail();
    }
  } catch (error) {
    elements.generatedAt.textContent = "连接失败";
    elements.systemDot.classList.remove("is-online");
    elements.dataAlert.hidden = false;
    elements.dataAlert.textContent = `监控数据加载失败：${error.message}`;
  } finally {
    elements.refresh.classList.remove("is-loading");
  }
}

function marketsForAsset() {
  return (state.dashboard?.markets ?? []).filter((item) => item.asset_class === state.assetClass);
}

function minutesSince(isoValue) {
  const parsed = new Date(isoValue);
  if (Number.isNaN(parsed.getTime())) return null;
  return Math.max(0, Math.round((Date.now() - parsed.getTime()) / 60000));
}

function marketOverviewSourceLabel(source) {
  if (source === null || source === undefined || source === "") return null;
  const labels = { mx: "妙想行情", sina: "新浪行情", akshare: "AkShare 行情", snapshot: "本地快照" };
  const parts = String(source)
    .split("+")
    .map((part) => labels[part] || part)
    .filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

async function fetchMarketOverview() {
  if (!state.user) return;
  try {
    const payload = await apiRequest("/api/market/overview");
    state.marketOverview = payload.data ?? null;
  } catch (error) {
    if (error.status === 401) {
      state.user = null;
      renderAuthState();
      return;
    }
    state.marketOverview = null;
  }
  renderMarketOverview();
}

function renderMarketOverview() {
  const data = state.marketOverview;
  elements.marketOverviewStatus.hidden = data !== null;
  elements.marketOverviewBody.hidden = data === null;
  if (!data) {
    elements.marketOverviewStale.hidden = true;
    elements.marketOverviewSource.hidden = true;
    elements.marketOverviewTime.textContent = "";
    elements.marketOverviewMovers.hidden = true;
    return;
  }

  const quoteTime = data.quote_time ? String(data.quote_time).slice(11, 16) : "--";
  elements.marketOverviewTime.textContent = `数据时间 ${quoteTime}`;
  elements.marketOverviewStale.hidden = !data.stale;
  if (data.stale) {
    const ageMinutes = data.quote_time ? minutesSince(data.quote_time) : null;
    elements.marketOverviewStale.textContent = ageMinutes === null
      ? "数据较旧"
      : ageMinutes >= 24 * 60
        ? `数据较旧 · ${Math.round(ageMinutes / 1440)} 天前`
        : ageMinutes >= 60
          ? `数据较旧 · ${Math.round(ageMinutes / 60)} 小时前`
          : `数据较旧 · ${ageMinutes} 分钟前`;
  }
  const sourceLabel = marketOverviewSourceLabel(data.source);
  elements.marketOverviewSource.hidden = sourceLabel === null;
  elements.marketOverviewSource.textContent = sourceLabel ?? "";

  const indices = data.indices ?? [];
  elements.marketOverviewIndices.hidden = indices.length === 0;
  elements.marketOverviewIndices.innerHTML = indices.map((item) => {
    // change_pct 已是百分点口径（0.48 = 0.48%），不能再乘 100
    const pct = Number(item.change_pct);
    const hasPct = Number.isFinite(pct);
    const tone = hasPct && pct > 0 ? "rec-buy" : hasPct && pct < 0 ? "rec-sell" : "rec-hold";
    const change = Number(item.change);
    const changeText = Number.isFinite(change) ? `${change > 0 ? "+" : ""}${formatNumber(change)}` : "--";
    const pctText = hasPct ? `${pct > 0 ? "+" : ""}${formatNumber(pct)}%` : "--";
    return `<div class="market-overview-index">
      <span>${escapeHtml(item.name)}</span>
      <strong>${formatNumber(item.price)}</strong>
      <small class="${tone}">${changeText}　${pctText}</small>
    </div>`;
  }).join("");

  const breadth = data.breadth ?? {};
  const up = Number(breadth.up) || 0;
  const flat = Number(breadth.flat) || 0;
  const down = Number(breadth.down) || 0;
  const total = Number(breadth.total) || up + flat + down;
  elements.marketOverviewBar.innerHTML = total > 0
    ? [
      ["up", "上涨", up],
      ["flat", "平", flat],
      ["down", "下跌", down],
    ]
      .filter(([, , count]) => count > 0)
      .map(([kind, label, count]) => `<span class="market-overview-seg-${kind}" style="width:${((count / total) * 100).toFixed(2)}%" title="${label} ${count.toLocaleString("zh-CN")}"></span>`)
      .join("")
    : "";
  elements.marketOverviewBreadthCounts.innerHTML = `<span class="rec-buy">上涨 ${up.toLocaleString("zh-CN")}</span> / 平 ${flat.toLocaleString("zh-CN")} / <span class="rec-sell">下跌 ${down.toLocaleString("zh-CN")}</span>`;
  elements.marketOverviewBreadthLimits.textContent = `涨停 ${Number(breadth.limit_up) || 0} · 跌停 ${Number(breadth.limit_down) || 0}`;

  const turnover = data.turnover;
  elements.marketOverviewTurnover.textContent = turnover === null || turnover === undefined
    ? "--"
    : `${formatNumber(turnover / 1e8, 0)} 亿元`;

  const sentiment = data.sentiment?.label ?? "--";
  const sentimentTone = sentiment === "偏强" ? "is-strong" : sentiment === "偏弱" ? "is-weak" : "is-neutral";
  elements.marketOverviewSentiment.className = `market-overview-value ${sentimentTone}`;
  const upRatio = data.sentiment?.up_ratio;
  elements.marketOverviewSentiment.textContent = upRatio === null || upRatio === undefined
    ? sentiment
    : `${sentiment} · 上涨占比 ${formatPercent(upRatio, 0)}`;

  // 涨跌幅榜：领涨 / 领跌，各最多 5 条，空数组时隐藏整个区块
  const movers = data.movers ?? {};
  const moverLists = [
    ["领涨", movers.gainers ?? []],
    ["领跌", movers.losers ?? []],
  ];
  const hasMovers = moverLists.some(([, rows]) => rows.length > 0);
  elements.marketOverviewMovers.hidden = !hasMovers;
  elements.marketOverviewMovers.innerHTML = hasMovers
    ? moverLists.map(([title, rows]) => {
      if (!rows.length) return "";
      return `<div class="market-overview-mover-list">
        <h3>${title}</h3>
        ${rows.slice(0, 5).map((row) => {
          // change_pct 为百分点口径（5.2 = 5.2%），不能再乘 100
          const hasPct = row.change_pct !== null && row.change_pct !== undefined && Number.isFinite(Number(row.change_pct));
          const pct = Number(row.change_pct);
          const tone = hasPct && pct > 0 ? "rec-buy" : hasPct && pct < 0 ? "rec-sell" : "rec-hold";
          const pctText = hasPct ? `${pct > 0 ? "+" : ""}${formatNumber(pct)}%` : "--";
          return `<div class="market-overview-mover-row"><span>${escapeHtml(row.name)}</span><strong class="${tone}">${pctText}</strong></div>`;
        }).join("")}
      </div>`;
    }).join("")
    : "";
}

function renderAll() {
  renderAuthState();
  renderAssetButtons();
  renderWatchlist();
  renderMarket();
  renderStrategies();
  renderBacktests();
  if (state.allStocks !== null) renderStockCatalog();
  renderSystem();
}

function renderAssetButtons() {
  document.querySelectorAll(".asset-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.asset === state.assetClass);
  });
  elements.watchlistTitle.textContent = `${assetLabels[state.assetClass]}观察池`;
  elements.addStockButton.hidden = !state.user || state.assetClass !== "stock";
}

function renderWatchlist() {
  const markets = marketsForAsset();
  const configured = state.dashboard?.summary?.by_asset?.[state.assetClass]?.configured ?? markets.length;
  elements.watchlistCount.textContent = String(configured);
  if (!markets.length) {
    elements.watchlist.innerHTML = `<div class="watch-empty">暂无可用${assetLabels[state.assetClass]}行情</div>`;
    return;
  }

  elements.watchlist.innerHTML = markets.map((item) => {
    const canManage = Boolean(state.user && item.asset_class === "stock");
    const actions = canManage ? `<span class="watch-actions">
      <button class="watch-action" data-refresh-symbol="${escapeHtml(item.symbol)}" title="更新日线" aria-label="更新 ${escapeHtml(item.name)} 日线">↻</button>
      <button class="watch-action" data-remove-symbol="${escapeHtml(item.symbol)}" title="移出观察池" aria-label="移除 ${escapeHtml(item.name)}">×</button>
    </span>` : "";
    // change_pct 为百分点口径，红涨绿跌，缺失时显示 "--"
    const hasChangePct = item.change_pct !== null && item.change_pct !== undefined;
    const changePct = Number(item.change_pct);
    const changePctText = hasChangePct ? `${changePct > 0 ? "+" : ""}${formatNumber(item.change_pct)}%` : "--";
    const changePctClass = hasChangePct && changePct > 0 ? "rec-buy" : hasChangePct && changePct < 0 ? "rec-sell" : "rec-hold";
    return `<div class="watch-row ${canManage ? "has-actions" : ""}">
      <button class="watch-item ${item.symbol === state.selectedSymbol ? "is-active" : ""}" data-symbol="${escapeHtml(item.symbol)}">
        <span class="watch-item-top">
          <strong>${escapeHtml(item.name)}</strong>
          <span>${formatNumber(item.price)}<span class="watch-item-change ${changePctClass}">${changePctText}</span></span>
        </span>
        <span class="watch-item-bottom">
          <span>${escapeHtml(item.symbol)}</span>
          <span class="side-${item.recommendation.toLowerCase()}">${recommendationLabels[item.recommendation]}</span>
        </span>
      </button>
      ${actions}
    </div>`;
  }).join("");

  elements.watchlist.querySelectorAll("[data-symbol]").forEach((button) => {
    button.addEventListener("click", () => selectSymbol(button.dataset.symbol));
  });
  elements.watchlist.querySelectorAll("[data-refresh-symbol]").forEach((button) => {
    button.addEventListener("click", () => refreshStock(button));
  });
  elements.watchlist.querySelectorAll("[data-remove-symbol]").forEach((button) => {
    button.addEventListener("click", () => removeStock(button.dataset.removeSymbol));
  });
}

async function refreshStock(button) {
  button.disabled = true;
  button.classList.add("is-loading");
  try {
    await apiRequest(`/api/watchlist/${encodeURIComponent(button.dataset.refreshSymbol)}/refresh`, { method: "POST" });
    state.selectedSymbol = button.dataset.refreshSymbol;
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
  }
}

async function removeStock(symbol) {
  if (!window.confirm(`确认将 ${symbol} 移出观察池？`)) return;
  try {
    await apiRequest(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
    if (state.selectedSymbol === symbol) state.selectedSymbol = null;
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderStockCatalog() {
  const stocks = state.allStocks ?? [];
  const query = state.stockQuery.trim().toLowerCase();
  const matchesValue = (value, selected) => {
    if (!selected) return true;
    if (selected === "__missing__") return !String(value || "").trim();
    return String(value || "") === selected;
  };
  const stockStatus = (item) => {
    if (item.price_kind === "unavailable") return "unavailable";
    if (item.freshness === "stale") return "stale";
    return item.price_kind;
  };
  const filtered = stocks.filter((item) => {
    const queryMatches = !query || (
      `${item.symbol} ${item.name} ${item.exchange} ${item.board} ${item.industry} ${item.area}`
        .toLowerCase()
        .includes(query)
    );
    const watchStatus = item.is_watched ? "watched" : "archived";
    return queryMatches
      && matchesValue(item.exchange, state.stockFilters.exchange)
      && matchesValue(item.board || item.market, state.stockFilters.board)
      && matchesValue(item.industry, state.stockFilters.industry)
      && matchesValue(item.area, state.stockFilters.area)
      && (!state.stockFilters.status || stockStatus(item) === state.stockFilters.status)
      && (!state.stockFilters.watch || watchStatus === state.stockFilters.watch);
  });
  // 排序：名称按中文排序，价格/涨跌按数值排序，缺失值始终排在最后
  const sortKey = state.stockSortKey;
  if (sortKey) {
    const sortDir = state.stockSortDir === "asc" ? 1 : -1;
    filtered.sort((left, right) => {
      if (sortKey === "name") {
        return sortDir * String(left.name || "").localeCompare(String(right.name || ""), "zh-CN");
      }
      const leftValue = left[sortKey];
      const rightValue = right[sortKey];
      const leftMissing = leftValue === null || leftValue === undefined || Number.isNaN(Number(leftValue));
      const rightMissing = rightValue === null || rightValue === undefined || Number.isNaN(Number(rightValue));
      if (leftMissing && rightMissing) return 0;
      if (leftMissing) return 1;
      if (rightMissing) return -1;
      return sortDir * (Number(leftValue) - Number(rightValue));
    });
  }
  // 分页：作用在筛选与排序后的结果上，页码越界时收敛到有效范围
  const totalPages = Math.max(1, Math.ceil(filtered.length / state.stockPageSize));
  state.stockPage = Math.min(Math.max(1, state.stockPage), totalPages);
  const page = state.stockPage;
  const visible = filtered.slice((page - 1) * state.stockPageSize, page * state.stockPageSize);

  const hasActiveFilters = Boolean(query) || Object.values(state.stockFilters).some(Boolean);
  elements.stockEmpty.hidden = filtered.length > 0;
  elements.stockEmpty.textContent = hasActiveFilters ? "没有符合筛选条件的股票" : "暂无股票数据";
  elements.stockPagination.hidden = filtered.length === 0;
  elements.stockPageInfo.textContent = `第 ${page}/${totalPages} 页 · 共 ${filtered.length.toLocaleString("zh-CN")} 条`;
  elements.stockPagePrev.disabled = page <= 1;
  elements.stockPageNext.disabled = page >= totalPages;
  elements.stockCatalogStatus.textContent = stocks.length
    ? hasActiveFilters
      ? `已筛选 ${filtered.length.toLocaleString("zh-CN")} / 数据库 ${stocks.length.toLocaleString("zh-CN")} 只`
      : `数据库共 ${stocks.length.toLocaleString("zh-CN")} 只股票`
    : "尚未同步股票清单";
  elements.stockFilterReset.disabled = !hasActiveFilters;
  // 表头排序指示：当前排序列标注升/降箭头
  document.querySelectorAll("[data-stock-sort]").forEach((th) => {
    const active = th.dataset.stockSort === state.stockSortKey;
    th.classList.toggle("is-sorted-asc", active && state.stockSortDir === "asc");
    th.classList.toggle("is-sorted-desc", active && state.stockSortDir === "desc");
  });
  elements.stockTable.innerHTML = visible.map((item) => {
    const exchangeLabels = { SSE: "上交所", SZSE: "深交所", BSE: "北交所" };
    const hasChange = item.change_pct !== null && item.change_pct !== undefined;
    const changeClass = hasChange ? (Number(item.change_pct) >= 0 ? "rec-buy" : "rec-sell") : "";
    const changeText = hasChange
      ? `${Number(item.change_pct) >= 0 ? "+" : ""}${formatNumber(item.change_pct)}%`
      : "--";
    const freshness = item.price_kind === "realtime"
      ? (item.freshness === "fresh" ? "实时快照" : "快照滞后")
      : item.price_kind === "daily"
        ? (item.freshness === "fresh" ? "日线收盘" : "日线滞后")
        : "暂无行情";
    const priceTime = item.price_time
      ? String(item.price_time).replace("T", " ").slice(0, 16)
      : "--";
    const exchange = exchangeLabels[item.exchange] || item.exchange || "--";
    const board = item.board || item.market || "--";
    let action = '<span class="owner-label">登录后管理</span>';
    if (state.user) {
      action = item.is_watched
        ? `<button class="table-action stock-action is-remove" data-catalog-remove="${escapeHtml(item.symbol)}">移出观察池</button>`
        : `<button class="primary-button stock-action" data-catalog-add="${escapeHtml(item.symbol)}">加入观察池</button>`;
    }
    return `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.symbol)}${item.is_default ? " · 默认" : ""}</span></td>
      <td class="symbol-cell"><strong>${escapeHtml(exchange)} / ${escapeHtml(board)}</strong><span>上市 ${escapeHtml(item.list_date || "--")}</span></td>
      <td class="symbol-cell"><strong>${escapeHtml(item.industry || "--")}</strong><span>${escapeHtml(item.area || "--")}</span></td>
      <td>${formatNumber(item.price)}</td>
      <td class="${changeClass}">${changeText}</td>
      <td>${escapeHtml(priceTime)}</td>
      <td><span class="visibility-badge">${freshness}</span></td>
      <td><span class="visibility-badge ${item.is_watched ? "is-watched" : ""}">${item.is_watched ? "观察中" : "已归档"}</span></td>
      <td>${action}</td>
    </tr>`;
  }).join("");
  elements.stockTable.querySelectorAll("[data-catalog-add]").forEach((button) => {
    button.addEventListener("click", () => addCatalogStock(button));
  });
  elements.stockTable.querySelectorAll("[data-catalog-remove]").forEach((button) => {
    button.addEventListener("click", () => removeStock(button.dataset.catalogRemove));
  });
}

function populateStockFilters() {
  const stocks = state.allStocks ?? [];
  const dynamicKeys = ["exchange", "board", "industry", "area"];
  const allLabels = {
    exchange: "全部交易所",
    board: "全部板块",
    industry: "全部行业",
    area: "全部地域",
  };
  const valueLabels = {
    SSE: "上交所",
    SZSE: "深交所",
    BSE: "北交所",
    __missing__: "未标注",
  };
  dynamicKeys.forEach((key) => {
    const select = Array.from(elements.stockFilters).find(
      (item) => item.dataset.stockFilter === key
    );
    const values = new Set();
    stocks.forEach((item) => {
      const raw = key === "board" ? item.board || item.market : item[key];
      values.add(String(raw || "").trim() || "__missing__");
    });
    const options = Array.from(values).sort((left, right) => {
      const order = { SSE: 1, SZSE: 2, BSE: 3, __missing__: 99 };
      if (key === "exchange") {
        return (order[left] || 50) - (order[right] || 50);
      }
      return left.localeCompare(right, "zh-CN");
    });
    select.innerHTML = [
      `<option value="">${allLabels[key]}</option>`,
      ...options.map(
        (value) => `<option value="${escapeHtml(value)}">${escapeHtml(valueLabels[value] || value)}</option>`
      ),
    ].join("");
    if (options.includes(state.stockFilters[key])) {
      select.value = state.stockFilters[key];
    } else {
      state.stockFilters[key] = "";
    }
  });
}

async function addCatalogStock(button) {
  button.disabled = true;
  try {
    const result = await apiRequest("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol: button.dataset.catalogAdd }),
    });
    state.assetClass = "stock";
    state.selectedSymbol = result.item.symbol;
    state.selectedStrategyId = "";
    state.appliedStrategyId = "";
    await fetchDashboard();
    await fetchStockCatalog();
    await switchView("detail");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function syncStockCatalog() {
  elements.syncStocks.disabled = true;
  elements.syncStocks.textContent = "正在同步...";
  try {
    const result = await apiRequest("/api/stocks/refresh", { method: "POST" });
    await Promise.all([fetchStockCatalog(), fetchDashboard()]);
    elements.stockCatalogStatus.textContent = `已同步 ${result.data.count.toLocaleString("zh-CN")} 只股票`;
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.syncStocks.disabled = false;
    elements.syncStocks.textContent = "同步股票清单";
  }
}

async function refreshStockQuotes() {
  elements.refreshQuotes.disabled = true;
  elements.refreshQuotes.textContent = "正在刷新...";
  try {
    const result = await apiRequest("/api/stocks/quotes/refresh", { method: "POST" });
    await fetchStockCatalog();
    elements.stockCatalogStatus.textContent = `实时行情 ${result.data.count.toLocaleString("zh-CN")} 只 · ${result.data.quote_time.replace("T", " ")}`;
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.refreshQuotes.disabled = false;
    elements.refreshQuotes.textContent = "刷新实时行情";
  }
}

function strategiesForCurrentMarket() {
  if (!state.currentMarket) return [];
  return state.strategies.filter((item) => (
    !item.is_system
    && item.symbol === state.currentMarket.symbol
    && item.asset_class === state.currentMarket.asset_class
  ));
}

function renderDetailStrategy() {
  const market = state.currentMarket;
  if (!market) {
    elements.detailStrategy.innerHTML = "";
    return;
  }
  const strategies = strategiesForCurrentMarket();
  if (state.selectedStrategyId && !strategies.some((item) => String(item.id) === state.selectedStrategyId)) {
    state.selectedStrategyId = "";
  }
  const defaultStrategyId = market.default_strategy_id === null
    || market.default_strategy_id === undefined
    ? ""
    : String(market.default_strategy_id);
  const systemStrategy = state.strategies.find(
    (item) => item.is_system && item.symbol === market.symbol
  );
  const systemLabel = systemStrategy
    ? `${systemStrategy.profile} · ${systemStrategy.owner}`
    : `${market.system_strategy_profile || "系统策略"} · 系统`;
  elements.detailStrategy.innerHTML = [
    `<option value="">${escapeHtml(systemLabel)}${defaultStrategyId === "" ? " · 默认" : ""}</option>`,
    ...strategies.map((item) => {
      const owner = item.is_owner ? "我的" : item.owner;
      const defaultLabel = String(item.id) === defaultStrategyId ? " · 默认" : "";
      return `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(owner)}${defaultLabel}</option>`;
    }),
  ].join("");
  elements.detailStrategy.value = state.selectedStrategyId;
  const isCurrentDefault = state.selectedStrategyId === defaultStrategyId;
  elements.detailDefaultStrategy.hidden = !state.user;
  elements.detailDefaultStrategy.disabled = isCurrentDefault;
  elements.detailDefaultStrategy.textContent = isCurrentDefault ? "当前默认" : "设为默认";
}

async function setDefaultStrategy() {
  const symbol = state.selectedSymbol;
  if (!symbol || !state.user) return;
  elements.detailDefaultStrategy.disabled = true;
  elements.detailDefaultStrategy.textContent = "保存中...";
  try {
    await apiRequest(`/api/watchlist/${encodeURIComponent(symbol)}/default-strategy`, {
      method: "PATCH",
      body: JSON.stringify({
        strategy_id: state.selectedStrategyId ? Number(state.selectedStrategyId) : null,
      }),
    });
    await fetchDashboard();
  } catch (error) {
    showToast(error.message, "error");
    renderDetailStrategy();
  }
}

async function loadMarketDetail() {
  const symbol = state.selectedSymbol;
  if (!symbol) return;
  const request = ++state.detailRequest;
  const query = new URLSearchParams({ limit: String(state.range) });
  if (state.selectedStrategyId) query.set("strategy_id", state.selectedStrategyId);
  else query.set("system_strategy", "true");
  elements.detailStrategy.disabled = true;
  elements.strategyCalculating.hidden = false;
  try {
    const market = await apiRequest(`/api/markets/${encodeURIComponent(symbol)}?${query}`);
    if (request !== state.detailRequest || symbol !== state.selectedSymbol) return;
    state.currentMarket = market;
    state.appliedStrategyId = state.selectedStrategyId;
    state.hoverIndex = null;
    renderMarket();
  } catch (error) {
    if (request !== state.detailRequest) return;
    state.selectedStrategyId = state.appliedStrategyId;
    renderDetailStrategy();
    elements.dataAlert.hidden = false;
    elements.dataAlert.textContent = `策略趋势计算失败：${error.message}`;
  } finally {
    if (request === state.detailRequest) {
      elements.detailStrategy.disabled = false;
      elements.strategyCalculating.hidden = true;
    }
  }
}

function renderMarket() {
  const market = state.currentMarket;
  elements.workspace.hidden = !market;
  elements.emptyMarket.hidden = Boolean(market);
  if (!market) {
    elements.chartTooltip.hidden = true;
    elements.dataAlert.hidden = true;
    return;
  }

  renderDetailStrategy();
  elements.dataAlert.hidden = market.freshness !== "stale";
  if (market.freshness === "stale") {
    elements.dataAlert.textContent = `${assetLabels[market.asset_class]}行情存在滞后，当前展示的是数据库中最近一次更新数据，请勿按实时行情使用。`;
  }

  document.querySelector("#instrument-class").textContent = assetLabels[market.asset_class];
  document.querySelector("#instrument-exchange").textContent = market.exchange || "--";
  document.querySelector("#instrument-name").textContent = market.name;
  document.querySelector("#instrument-symbol").textContent = market.symbol;
  document.querySelector("#instrument-price").textContent = formatNumber(market.price);

  const change = document.querySelector("#instrument-change");
  change.className = market.change >= 0 ? "price-up" : "price-down";
  change.textContent = `${market.change >= 0 ? "+" : ""}${formatNumber(market.change)}  ${market.change_pct >= 0 ? "+" : ""}${formatNumber(market.change_pct)}%`;

  const freshness = document.querySelector("#freshness-badge");
  freshness.className = `freshness-badge ${market.freshness === "stale" ? "is-stale" : ""}`;
  freshness.textContent = market.freshness === "stale" ? `滞后 ${market.lag_days} 天` : "数据正常";

  // 一键回测按钮：仅登录且当前为股票标的时可见（回测表单按股票代码口径）
  elements.detailBacktest.hidden = !state.user || market.asset_class !== "stock";

  const recommendation = document.querySelector("#recommendation");
  recommendation.className = recClass(market.recommendation);
  recommendation.textContent = recommendationLabels[market.recommendation] ?? market.recommendation;
  // vote_ratio 是规则票占比（非概率）；文案与 title 都明确标注，避免被误读成胜率
  document.querySelector("#vote-ratio").textContent = `规则票占比 ${market.vote_ratio}%（非概率）`;
  document.querySelector("#decision-reason").textContent = market.reason;

  const voteNames = { ma: "均线趋势", rsi: "RSI 区间", bollinger: "布林位置", macd: "MACD 状态", trend: "均线排列", donchian: "唐奇安通道" };
  document.querySelector("#vote-grid").innerHTML = Object.entries(market.votes).map(([name, vote]) => `
    <div class="vote-item"><span>${voteNames[name] ?? name}</span><strong class="${recClass(vote)}">${voteLabels[vote] ?? vote}</strong></div>
  `).join("");

  const indicators = market.indicators;
  document.querySelector("#indicator-row").innerHTML = [
    [`MA${market.parameters.ma_fast}`, indicators.ma_fast],
    [`MA${market.parameters.ma_slow}`, indicators.ma_slow],
    [`RSI${market.parameters.rsi_period}`, indicators.rsi],
    ["布林中轨", indicators.bb_middle],
  ].map(([label, value]) => `<div class="indicator-item"><span>${label}</span><strong>${formatNumber(value)}</strong></div>`).join("");

  renderSignals(market.signals);
  drawChart();
}

function renderSignals(signals) {
  document.querySelector("#signal-count").textContent = `${signals.length} 条`;
  const list = document.querySelector("#signal-list");
  if (!signals.length) {
    list.innerHTML = '<div class="signal-empty">当前数据区间没有明确方向变化</div>';
    return;
  }
  list.innerHTML = signals.slice(0, 5).map((signal) => `
    <div class="signal-item">
      <time>${shortDate(signal.date)}</time>
      <strong class="${recClass(signal.side)}">${recommendationLabels[signal.side]}</strong>
      <span>${escapeHtml(signal.reason)}</span>
    </div>
  `).join("");
}

function renderStrategies() {
  const strategies = state.strategies.filter((item) => item.asset_class === state.assetClass);
  const body = document.querySelector("#strategy-table");
  const empty = document.querySelector("#strategy-empty");
  if (!state.user) {
    body.innerHTML = "";
    return;
  }
  document.querySelector("#strategy-subtitle").textContent = `${assetLabels[state.assetClass]} · 我的策略与开放策略`;
  empty.hidden = strategies.length > 0;
  body.innerHTML = strategies.map((item) => {
    const params = item.parameters ?? {};
    const parameterText = `MA ${params.ma_fast ?? "--"}/${params.ma_slow ?? "--"} · RSI ${params.rsi_period ?? "--"} · BB ${params.bb_period ?? "--"}/${params.bb_std ?? "--"}`;
    const action = item.is_owner
      ? `<div class="strategy-actions"><button class="table-action" data-edit-strategy="${item.id}">编辑</button><button class="table-action" data-strategy-id="${item.id}" data-visibility="${item.visibility}">${item.visibility === "public" ? "设为私有" : "设为开放"}</button></div>`
      : "--";
    return `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.profile)} · ${escapeHtml(item.symbol)}</span></td>
      <td><span class="owner-label">${escapeHtml(item.owner)}</span></td>
      <td><span class="market-tag">${assetLabels[item.asset_class]}</span></td>
      <td class="parameter-cell">${escapeHtml(parameterText)}</td>
      <td><span class="visibility-badge ${item.visibility === "public" ? "is-public" : ""}">${item.visibility === "public" ? "开放" : "私有"}</span></td>
      <td class="${recClass(item.recommendation)}">${recommendationLabels[item.recommendation] ?? "--"}</td>
      <td>${action}</td>
    </tr>`;
  }).join("");
  body.querySelectorAll("[data-strategy-id]").forEach((button) => {
    button.addEventListener("click", () => toggleStrategyVisibility(button));
  });
  body.querySelectorAll("[data-edit-strategy]").forEach((button) => {
    button.addEventListener("click", () => {
      const strategy = state.strategies.find(
        (item) => String(item.id) === button.dataset.editStrategy
      );
      if (strategy) openStrategyDialog(strategy);
    });
  });
}

function renderBacktests() {
  const backtests = state.backtests.filter((item) => item.asset_class === state.assetClass);
  const body = document.querySelector("#backtest-table");
  const empty = document.querySelector("#backtest-empty");
  if (!state.user) {
    body.innerHTML = "";
    return;
  }
  empty.hidden = backtests.length > 0;
  body.innerHTML = backtests.map((item) => `
    <tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name || item.symbol)}</strong><span>${escapeHtml(item.strategy_name || "未关联策略")} · ${escapeHtml(item.symbol)}</span></td>
      <td>${escapeHtml(item.start_date || "--")} — ${escapeHtml(item.end_date || "--")}</td>
      <td class="${Number(item.total_return) >= 0 ? "rec-buy" : "rec-sell"}">${formatNumber(item.total_return)}%</td>
      <td>${formatNumber(item.max_drawdown)}%</td>
      <td>${formatNumber(item.win_rate)}%</td>
      <td>${escapeHtml(item.total_trades ?? 0)}</td>
      <td>${escapeHtml(item.created_at || "--")}</td>
      <td><div class="strategy-actions">
        <button class="table-action" data-replay-backtest="${item.id}" title="查看此记录保存的权益曲线与成交明细">回放</button>
        <button class="table-action" data-rerun-backtest="${item.id}" title="按此记录参数重新执行回测">重跑</button>
        <button class="table-action is-danger" data-delete-backtest="${item.id}" title="删除此回测记录">删除</button>
      </div></td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-replay-backtest]").forEach((button) => {
    button.addEventListener("click", () => replayBacktest(Number(button.dataset.replayBacktest)));
  });
  body.querySelectorAll("[data-rerun-backtest]").forEach((button) => {
    button.addEventListener("click", () => rerunBacktest(Number(button.dataset.rerunBacktest)));
  });
  body.querySelectorAll("[data-delete-backtest]").forEach((button) => {
    button.addEventListener("click", () => deleteBacktest(Number(button.dataset.deleteBacktest)));
  });
}

async function replayBacktest(id) {
  elements.backtestError.hidden = true;
  try {
    const response = await apiRequest(`/api/backtests/${id}`);
    state.backtest = response.data;
    state.backtestHover = null;
    renderBacktestResult();
    elements.backtestResult.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showToast(error.message, "error");
  }
}

function rerunBacktest(id) {
  const record = state.backtests.find((item) => item.id === id);
  if (!record) return;
  const fields = elements.backtestForm.elements;
  fields.symbol.value = record.symbol;
  fields.strategy.value = record.strategy_key;
  // A5：策略键变了 → 重渲染参数输入，避免把上一个策略的参数键提交上去（白名单 422）。
  // 参数/画像不做精确还原（列表行不含完整 strategy_params），rerun 按缺省链解析。
  renderBacktestParams();
  fields.bars.value = record.bar_count;
  fields.execution.value = record.execution;
  elements.backtestForm.requestSubmit();
}

async function deleteBacktest(id) {
  if (!window.confirm("确认删除这条回测记录？")) return;
  try {
    await apiRequest(`/api/backtests/${id}`, { method: "DELETE" });
    await loadProtectedData();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function runBacktest(event) {
  event.preventDefault();
  elements.backtestError.hidden = true;
  const fields = elements.backtestForm.elements;
  const submit = elements.backtestSubmit;
  submit.disabled = true;
  submit.textContent = "回测运行中...";
  try {
    const payload = {
      symbol: fields.symbol.value.trim(),
      strategy: fields.strategy.value,
      bars: Number(fields.bars.value),
      execution: fields.execution.value,
      benchmark: fields.benchmark.checked,
    };
    // A5：仅在用户实际填写时附带 params/profile（留空 → 后端走画像/缺省链，provenance 才诚实）
    const explicitParams = collectBacktestParams();
    if (explicitParams) payload.params = explicitParams;
    const profileValue = fields.profile ? fields.profile.value : "";
    if (fields.strategy.value === "combo_vote" && profileValue) payload.profile = profileValue;
    const response = await apiRequest("/api/backtest", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.backtest = response.data;
    state.backtestHover = null;
    renderBacktestResult();
  } catch (error) {
    elements.backtestError.textContent = `回测失败：${error.message}`;
    elements.backtestError.hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "开始回测";
  }
}

function setBacktestMetric(selector, text, tone) {
  const element = document.querySelector(selector);
  element.textContent = text;
  element.className = tone === "up" ? "rec-buy" : tone === "down" ? "rec-sell" : "";
}

function renderBacktestResult() {
  const data = state.backtest;
  if (!data) return;
  elements.backtestResult.hidden = false;
  document.querySelector("#backtest-result-title").textContent = `${data.symbol} · ${backtestStrategyLabel(data.strategy)}`;
  document.querySelector("#backtest-result-meta").textContent = `${backtestExecutionLabel(data.execution)} · ${data.bar_count} 根 K 线`;

  // A5 provenance：参数来源 + 实际生效参数摘要（combo 显示阈值+组件，单策略显示扁平参数）
  const provenance = document.querySelector("#backtest-provenance");
  if (provenance) {
    const parts = [];
    if (data.profile_source) parts.push(`参数来源：${profileSourceLabel(data.profile_source)}`);
    const sp = data.strategy_params;
    if (sp && typeof sp === "object") {
      if (sp.vote_threshold !== undefined) {
        const comps = sp.components && typeof sp.components === "object" ? Object.keys(sp.components).join("/") : "";
        parts.push(`投票阈值 ${sp.vote_threshold}${comps ? ` · 组件 ${comps}` : ""}`);
      } else {
        const flat = Object.entries(sp).map(([key, val]) => `${paramLabel(key)}=${val}`).join(" · ");
        if (flat) parts.push(flat);
      }
    }
    provenance.textContent = parts.join("　|　");
    provenance.hidden = parts.length === 0;
  }

  // 基准图例：仅在请求了基准且数据可用时展示（return 为小数口径，用 formatPercent）
  const benchmark = data.benchmark;
  const benchmarkVisible = Boolean(benchmark?.available && (benchmark.curve ?? []).length);
  elements.backtestChartLegend.hidden = !benchmarkVisible;
  elements.backtestChartLegend.innerHTML = benchmarkVisible
    ? `<span class="legend-item"><i class="legend-swatch legend-equity"></i>策略净值</span><span class="legend-item"><i class="legend-swatch legend-benchmark"></i>沪深300 ${formatPercent(benchmark.return, 2, true)}</span>`
    : "";

  const notes = [];
  if (data.halted_by_drawdown) {
    notes.push(["alert", "已触发回撤闸门：净值回撤达到风控阈值后回测提前停止，后续信号不再撮合。"]);
  }
  if (data.skipped_fills > 0) {
    notes.push(["info", `涨跌停拦截：${data.skipped_fills} 笔委托因涨停/跌停无法成交，已按废单处理。`]);
  }
  if (!notes.length) {
    notes.push(["info", "本次回测未触发涨跌停拦截，也未触发回撤闸门。"]);
  }
  elements.backtestNotes.innerHTML = notes
    .map(([kind, text]) => `<div class="data-alert ${kind === "info" ? "note-info" : ""}">${escapeHtml(text)}</div>`)
    .join("");

  const metrics = data.metrics ?? {};
  const trades = data.trades ?? {};
  setBacktestMetric("#bt-total-return", formatPercent(metrics.total_return, 2, true), Number(metrics.total_return) >= 0 ? "up" : "down");
  setBacktestMetric("#bt-annual-return", formatPercent(metrics.annual_return, 2, true), Number(metrics.annual_return) >= 0 ? "up" : "down");
  setBacktestMetric("#bt-max-drawdown", formatPercent(metrics.max_drawdown), Number(metrics.max_drawdown) < 0 ? "down" : "");
  setBacktestMetric("#bt-sharpe", formatNumber(metrics.sharpe), "");
  setBacktestMetric("#bt-total-fees", `${formatNumber(trades.total_fees)} 元`, "");
  setBacktestMetric("#bt-num-trades", String(trades.num_trades ?? 0), "");
  setBacktestMetric("#bt-win-rate", formatPercent(trades.win_rate, 1), "");
  setBacktestMetric("#bt-avg-return", formatPercent(trades.avg_return_per_trade, 2, true), Number(trades.avg_return_per_trade) >= 0 ? "up" : "down");
  setBacktestMetric("#bt-best-trade", formatPercent(trades.best_trade, 2, true), Number(trades.best_trade) >= 0 ? "up" : "down");
  setBacktestMetric("#bt-worst-trade", formatPercent(trades.worst_trade, 2, true), Number(trades.worst_trade) >= 0 ? "up" : "down");

  const fills = data.fills ?? [];
  document.querySelector("#backtest-fill-count").textContent = `${fills.length} 笔`;
  document.querySelector("#backtest-fill-table").innerHTML = fills.length
    ? fills.map((fill) => `
      <tr>
        <td>${escapeHtml(fill.date)}</td>
        <td class="${fill.side === "BUY" ? "side-buy" : "side-sell"}">${fill.side === "BUY" ? "买入" : "卖出"}</td>
        <td>${formatNumber(fill.quantity, 0)}</td>
        <td>${formatNumber(fill.price)}</td>
        <td>${formatNumber(fill.fee)}</td>
      </tr>
    `).join("")
    : '<tr><td colspan="5">本次回测没有产生成交</td></tr>';

  const curve = data.equity_curve ?? [];
  document.querySelector("#backtest-chart-range").textContent = curve.length >= 2
    ? `${curve[0].date} — ${curve[curve.length - 1].date}`
    : "--";
  document.querySelector("#backtest-disclaimer").textContent = data.disclaimer ?? "";
  requestAnimationFrame(drawEquityChart);
}

function drawEquityChart() {
  const canvas = elements.backtestChart;
  const frame = canvas.parentElement;
  const curve = state.backtest?.equity_curve ?? [];
  if (elements.backtestResult.hidden) return;
  if (frame.clientWidth === 0) return;
  if (!curve.length) {
    elements.backtestChartEmpty.hidden = false;
    elements.backtestChartEmpty.textContent = "暂无权益曲线";
    return;
  }
  elements.backtestChartEmpty.hidden = true;

  const width = frame.clientWidth;
  const height = frame.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const margin = { top: 18, right: 70, bottom: 26, left: 14 };
  const chartWidth = width - margin.left - margin.right;
  const chartHeight = height - margin.top - margin.bottom;
  const values = curve.map((point) => point.equity);
  let valueMin = Math.min(...values);
  let valueMax = Math.max(...values);
  const valuePadding = Math.max((valueMax - valueMin) * 0.1, valueMax * 0.005);
  valueMin -= valuePadding;
  valueMax += valuePadding;
  const yAt = (value) => margin.top + ((valueMax - value) / (valueMax - valueMin || 1)) * chartHeight;
  const xAt = (index) => margin.left + (curve.length <= 1 ? chartWidth / 2 : (index / (curve.length - 1)) * chartWidth);

  context.strokeStyle = "#e5eaee";
  context.fillStyle = "#7d8993";
  context.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.lineWidth = 1;
  for (let grid = 0; grid <= 4; grid += 1) {
    const y = margin.top + chartHeight * grid / 4;
    context.beginPath();
    context.moveTo(margin.left, y + 0.5);
    context.lineTo(width - margin.right, y + 0.5);
    context.stroke();
    const value = valueMax - (valueMax - valueMin) * grid / 4;
    context.fillText(formatNumber(value, 0), width - margin.right + 8, y + 3);
  }

  const baseline = values[0];
  context.strokeStyle = "#a8b1b9";
  context.setLineDash([4, 3]);
  context.beginPath();
  context.moveTo(margin.left, yAt(baseline) + 0.5);
  context.lineTo(width - margin.right, yAt(baseline) + 0.5);
  context.stroke();
  context.setLineDash([]);

  context.beginPath();
  curve.forEach((point, index) => {
    const x = xAt(index);
    const y = yAt(point.equity);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = "#006a60";
  context.lineWidth = 1.6;
  context.stroke();
  context.lineTo(xAt(curve.length - 1), margin.top + chartHeight);
  context.lineTo(xAt(0), margin.top + chartHeight);
  context.closePath();
  context.fillStyle = "rgba(0, 106, 96, .08)";
  context.fill();

  // 叠加沪深300基准：基准 value（首点 1.0）按权益首点缩放映射到同一 Y 轴；
  // 两条曲线长度可能不同，X 轴各按 index/(len-1) 归一化对齐
  const benchmark = state.backtest?.benchmark;
  const benchmarkCurve = benchmark?.available ? (benchmark.curve ?? []) : [];
  if (benchmarkCurve.length) {
    const baseEquity = curve[0].equity;
    const benchmarkXAt = (index) => margin.left + (benchmarkCurve.length <= 1
      ? chartWidth / 2
      : (index / (benchmarkCurve.length - 1)) * chartWidth);
    context.beginPath();
    benchmarkCurve.forEach((point, index) => {
      const x = benchmarkXAt(index);
      const y = yAt(baseEquity * Number(point.value));
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.strokeStyle = "#a8b1b9";
    context.lineWidth = 1.3;
    context.setLineDash([4, 3]);
    context.stroke();
    context.setLineDash([]);
  }

  const labelCount = Math.min(5, curve.length);
  context.fillStyle = "#7d8993";
  for (let labelIndex = 0; labelIndex < labelCount; labelIndex += 1) {
    const index = Math.round((curve.length - 1) * labelIndex / Math.max(labelCount - 1, 1));
    const x = xAt(index);
    context.fillText(shortDate(curve[index].date), Math.min(Math.max(x - 14, margin.left), width - margin.right - 28), height - 8);
  }

  if (state.backtestHover !== null && curve[state.backtestHover]) {
    const x = xAt(state.backtestHover);
    context.strokeStyle = "#7f8a93";
    context.setLineDash([3, 3]);
    context.beginPath();
    context.moveTo(x, margin.top);
    context.lineTo(x, margin.top + chartHeight);
    context.stroke();
    context.setLineDash([]);
    context.fillStyle = "#006a60";
    context.beginPath();
    context.arc(x, yAt(curve[state.backtestHover].equity), 3, 0, Math.PI * 2);
    context.fill();
  }

  state.backtestChartGeometry = { margin, chartWidth, curve };
}

async function autoRefreshTick() {
  if (state.autoRefresh.busy) return;
  state.autoRefresh.busy = true;
  state.autoRefresh.lastRun = Date.now();
  try {
    await fetchDashboard();
    if (state.user) await fetchMarketOverview();
    if (state.user) await loadProtectedData();
    if (state.allStocks !== null) await fetchStockCatalog();
  } catch (_) {
    // 各请求内部已有错误提示，这里仅防止定时器中断
  } finally {
    state.autoRefresh.busy = false;
  }
}

function syncAutoRefreshTimer() {
  if (state.autoRefresh.timer) {
    window.clearInterval(state.autoRefresh.timer);
    state.autoRefresh.timer = null;
  }
  if (!elements.autoRefreshToggle.checked || document.hidden) return;
  const intervalMs = AUTO_REFRESH_INTERVALS[Number(elements.autoRefreshInterval.value)] ?? AUTO_REFRESH_INTERVALS[60];
  state.autoRefresh.timer = window.setInterval(autoRefreshTick, intervalMs);
}

async function toggleStrategyVisibility(button) {
  button.disabled = true;
  try {
    const visibility = button.dataset.visibility === "public" ? "private" : "public";
    await apiRequest(`/api/strategies/${button.dataset.strategyId}/visibility`, {
      method: "PATCH",
      body: JSON.stringify({ visibility }),
    });
    await loadProtectedData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderSystem() {
  const system = state.dashboard?.system ?? {};
  const assetSummary = state.dashboard?.summary?.by_asset?.[state.assetClass] ?? {};
  document.querySelector("#system-grid").innerHTML = [
    ["API 服务", system.api === "online" ? "运行中" : "异常", "FastAPI dashboard"],
    ["行情存储", system.data_source || "--", `${assetSummary.available ?? 0} 个标的可用`],
    ["数据库", system.database || "--", "行情、用户、策略与回测"],
    ["当前市场", assetLabels[state.assetClass], `${assetSummary.configured ?? 0} 个已配置`],
  ].map(([label, value, note]) => `
    <div class="system-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></div>
  `).join("");

  const errors = (system.errors ?? []).filter((item) => !item.asset_class || item.asset_class === state.assetClass);
  document.querySelector("#error-count").textContent = `${errors.length} 项`;
  document.querySelector("#error-list").innerHTML = errors.length
    ? errors.map((item) => `<div class="error-item"><strong>${escapeHtml(item.symbol)}</strong><span>${escapeHtml(item.error)}</span></div>`).join("")
    : '<div class="signal-empty">当前市场没有数据读取异常</div>';
}

function chartBars() {
  return (state.currentMarket?.bars ?? []).slice(-state.range);
}

function drawChart() {
  const canvas = elements.chart;
  const frame = canvas.parentElement;
  const bars = chartBars();
  if (!bars.length || frame.clientWidth === 0) {
    elements.chartEmpty.hidden = false;
    elements.chartEmpty.textContent = "暂无图表数据";
    return;
  }
  elements.chartEmpty.hidden = true;

  const width = frame.clientWidth;
  const height = frame.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const margin = { top: 20, right: 64, bottom: 26, left: 14 };
  const volumeHeight = state.indicators.volume ? 62 : 0;
  const chartBottom = height - margin.bottom - volumeHeight;
  const chartWidth = width - margin.left - margin.right;
  const chartHeight = chartBottom - margin.top;
  const candleStep = chartWidth / bars.length;
  const candleWidth = Math.max(2, Math.min(9, candleStep * 0.62));

  const priceValues = bars.flatMap((bar) => {
    const values = [bar.low, bar.high];
    if (state.indicators.bb) values.push(bar.bb_lower, bar.bb_upper);
    return values.filter((value) => value !== null && value !== undefined);
  });
  let priceMin = Math.min(...priceValues);
  let priceMax = Math.max(...priceValues);
  const pricePadding = Math.max((priceMax - priceMin) * 0.08, priceMax * 0.005);
  priceMin -= pricePadding;
  priceMax += pricePadding;
  const priceY = (value) => margin.top + ((priceMax - value) / (priceMax - priceMin || 1)) * chartHeight;
  const xAt = (index) => margin.left + candleStep * index + candleStep / 2;

  context.strokeStyle = "#e5eaee";
  context.fillStyle = "#7d8993";
  context.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.lineWidth = 1;
  for (let grid = 0; grid <= 4; grid += 1) {
    const y = margin.top + chartHeight * grid / 4;
    context.beginPath();
    context.moveTo(margin.left, y + 0.5);
    context.lineTo(width - margin.right, y + 0.5);
    context.stroke();
    const price = priceMax - (priceMax - priceMin) * grid / 4;
    context.fillText(formatNumber(price), width - margin.right + 8, y + 3);
  }

  const signalMap = new Map((state.currentMarket.signals ?? []).map((item) => [item.date, item.side]));
  bars.forEach((bar, index) => {
    const x = xAt(index);
    const color = bar.close >= bar.open ? "#c23845" : "#12805c";
    context.strokeStyle = color;
    context.fillStyle = color;
    context.beginPath();
    context.moveTo(x, priceY(bar.high));
    context.lineTo(x, priceY(bar.low));
    context.stroke();
    const bodyTop = priceY(Math.max(bar.open, bar.close));
    const bodyBottom = priceY(Math.min(bar.open, bar.close));
    context.fillRect(x - candleWidth / 2, bodyTop, candleWidth, Math.max(bodyBottom - bodyTop, 1));

    const signal = signalMap.get(bar.date);
    if (signal) {
      context.fillStyle = signal === "BUY" ? "#c23845" : "#12805c";
      context.beginPath();
      const markerY = signal === "BUY" ? priceY(bar.low) + 10 : priceY(bar.high) - 10;
      context.arc(x, markerY, 3, 0, Math.PI * 2);
      context.fill();
    }
  });

  function drawLine(key, color, lineWidth = 1.3, dash = []) {
    context.strokeStyle = color;
    context.lineWidth = lineWidth;
    context.setLineDash(dash);
    context.beginPath();
    let active = false;
    bars.forEach((bar, index) => {
      const value = bar[key];
      if (value === null || value === undefined) {
        active = false;
        return;
      }
      const x = xAt(index);
      const y = priceY(value);
      if (!active) context.moveTo(x, y);
      else context.lineTo(x, y);
      active = true;
    });
    context.stroke();
    context.setLineDash([]);
  }

  if (state.indicators.ma) {
    drawLine("ma_fast", "#176b87", 1.5);
    drawLine("ma_slow", "#b57920", 1.5);
  }
  if (state.indicators.bb) {
    drawLine("bb_upper", "#9b7ab2", 1, [4, 3]);
    drawLine("bb_middle", "#a8b1b9", 1, [2, 3]);
    drawLine("bb_lower", "#9b7ab2", 1, [4, 3]);
  }

  if (state.indicators.volume) {
    const volumeTop = chartBottom + 12;
    const maxVolume = Math.max(...bars.map((bar) => bar.volume), 1);
    bars.forEach((bar, index) => {
      const x = xAt(index);
      const barHeight = (bar.volume / maxVolume) * (volumeHeight - 18);
      context.fillStyle = bar.close >= bar.open ? "rgba(194,56,69,.35)" : "rgba(18,128,92,.35)";
      context.fillRect(x - candleWidth / 2, height - margin.bottom - barHeight, candleWidth, barHeight);
    });
    context.fillStyle = "#8c98a3";
    context.fillText("VOL", margin.left, volumeTop + 5);
  }

  const labelCount = Math.min(5, bars.length);
  context.fillStyle = "#7d8993";
  for (let index = 0; index < labelCount; index += 1) {
    const barIndex = Math.round((bars.length - 1) * index / Math.max(labelCount - 1, 1));
    const label = shortDate(bars[barIndex].date);
    const x = xAt(barIndex);
    context.fillText(label, Math.min(x - 14, width - margin.right - 28), height - 8);
  }

  if (state.hoverIndex !== null && bars[state.hoverIndex]) {
    const x = xAt(state.hoverIndex);
    context.strokeStyle = "#7f8a93";
    context.setLineDash([3, 3]);
    context.beginPath();
    context.moveTo(x, margin.top);
    context.lineTo(x, height - margin.bottom);
    context.stroke();
    context.setLineDash([]);
  }

  state.chartGeometry = { margin, chartWidth, candleStep, bars, width, height };
}

// 详情页一键回测：把当前标的填入回测表单，切到回测视图并自动提交
async function runDetailBacktest() {
  const symbol = state.selectedSymbol ?? state.currentMarket?.symbol;
  if (!state.user || !symbol) return;
  elements.backtestForm.elements.symbol.value = symbol;
  await switchView("backtests");
  elements.backtestForm.requestSubmit();
}

async function selectSymbol(symbol) {
  const market = marketsForAsset().find((item) => item.symbol === symbol) ?? null;
  state.selectedSymbol = symbol;
  state.selectedStrategyId = market?.default_strategy_id === null
    || market?.default_strategy_id === undefined
    ? ""
    : String(market.default_strategy_id);
  state.appliedStrategyId = state.selectedStrategyId;
  state.hoverIndex = null;
  state.currentMarket = market;
  renderWatchlist();
  renderMarket();
  await switchView("detail");
}

function switchAsset(assetClass) {
  state.assetClass = assetClass;
  state.hoverIndex = null;
  state.selectedSymbol = null;
  state.selectedStrategyId = "";
  state.appliedStrategyId = "";
  state.currentMarket = null;
  renderAll();
}

async function switchView(view) {
  state.view = view;
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("is-active", section.id === `view-${view}`));
  if (view === "detail") requestAnimationFrame(drawChart);
  if (view === "stocks") await fetchStockCatalog();
  if (view === "overview") {
    renderMarketOverview();
    if (state.user) await fetchMarketOverview();
  }
  if (state.user && ["strategies", "backtests"].includes(view)) await loadProtectedData();
}

document.querySelectorAll(".asset-button").forEach((button) => button.addEventListener("click", () => switchAsset(button.dataset.asset)));
document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelectorAll("[data-auth-mode]").forEach((button) => {
  button.addEventListener("click", () => openAuth(button.dataset.authMode, state.view));
});
document.querySelectorAll("[data-auth-tab]").forEach((button) => {
  button.addEventListener("click", () => setAuthMode(button.dataset.authTab));
});
document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.range = Number(button.dataset.range);
    document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
    if (state.selectedSymbol && state.range > (state.currentMarket?.bars?.length ?? 0)) {
      await loadMarketDetail();
    }
    state.hoverIndex = null;
    renderMarket();
  });
});
elements.detailStrategy.addEventListener("change", async () => {
  state.selectedStrategyId = elements.detailStrategy.value;
  await loadMarketDetail();
});
elements.detailDefaultStrategy.addEventListener("click", setDefaultStrategy);
document.querySelectorAll("[data-indicator]").forEach((input) => {
  input.addEventListener("change", () => {
    state.indicators[input.dataset.indicator] = input.checked;
    drawChart();
  });
});

elements.refresh.addEventListener("click", async () => {
  await fetchDashboard();
  if (state.user) await fetchMarketOverview();
  if (state.view === "stocks") await fetchStockCatalog();
});
elements.marketOverviewRefresh.addEventListener("click", async () => {
  if (!state.user) return;
  elements.marketOverviewRefresh.disabled = true;
  elements.marketOverviewRefresh.classList.add("is-loading");
  try {
    await fetchMarketOverview();
  } finally {
    elements.marketOverviewRefresh.disabled = false;
    elements.marketOverviewRefresh.classList.remove("is-loading");
  }
});
document.querySelectorAll('input[list="symbol-options"]').forEach((input) => {
  input.addEventListener("focus", () => {
    if (state.allStocks === null) fetchStockCatalog().catch(() => {});
  });
});
elements.autoRefreshToggle.addEventListener("change", () => {
  elements.autoRefreshInterval.disabled = !elements.autoRefreshToggle.checked;
  syncAutoRefreshTimer();
  if (elements.autoRefreshToggle.checked && !document.hidden) autoRefreshTick();
});
elements.autoRefreshInterval.addEventListener("change", syncAutoRefreshTimer);
document.addEventListener("visibilitychange", () => {
  if (!elements.autoRefreshToggle.checked) return;
  if (document.hidden) {
    syncAutoRefreshTimer();
    return;
  }
  const intervalMs = AUTO_REFRESH_INTERVALS[Number(elements.autoRefreshInterval.value)] ?? AUTO_REFRESH_INTERVALS[60];
  if (Date.now() - state.autoRefresh.lastRun >= intervalMs) autoRefreshTick();
  syncAutoRefreshTimer();
});
elements.backtestForm.addEventListener("submit", runBacktest);
// A5：切换策略 → 按新策略的 params_schema 重渲染参数输入，并同步画像下拉可用态
elements.backtestForm.elements.strategy.addEventListener("change", renderBacktestParams);
elements.detailBacktest.addEventListener("click", runDetailBacktest);
elements.syncStocks.addEventListener("click", syncStockCatalog);
elements.refreshQuotes.addEventListener("click", refreshStockQuotes);
elements.stockSearch.addEventListener("input", () => {
  state.stockQuery = elements.stockSearch.value;
  state.stockPage = 1;
  renderStockCatalog();
});
elements.stockFilters.forEach((select) => {
  select.addEventListener("change", () => {
    state.stockFilters[select.dataset.stockFilter] = select.value;
    state.stockPage = 1;
    renderStockCatalog();
  });
});
elements.stockFilterReset.addEventListener("click", () => {
  state.stockQuery = "";
  state.stockPage = 1;
  elements.stockSearch.value = "";
  Object.keys(state.stockFilters).forEach((key) => {
    state.stockFilters[key] = "";
  });
  elements.stockFilters.forEach((select) => {
    select.value = "";
  });
  renderStockCatalog();
});
document.querySelectorAll("[data-stock-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.stockSort;
    if (state.stockSortKey === key) {
      state.stockSortDir = state.stockSortDir === "asc" ? "desc" : "asc";
    } else {
      state.stockSortKey = key;
      // 名称默认升序，数值列默认降序
      state.stockSortDir = key === "name" ? "asc" : "desc";
    }
    state.stockPage = 1;
    renderStockCatalog();
  });
});
elements.stockPagePrev.addEventListener("click", () => {
  state.stockPage -= 1;
  renderStockCatalog();
});
elements.stockPageNext.addEventListener("click", () => {
  state.stockPage += 1;
  renderStockCatalog();
});
elements.accountButton.addEventListener("click", () => openAuth("login"));
document.querySelector("#gate-public-button").addEventListener("click", async () => {
  if (state.selectedSymbol) {
    await switchView("detail");
    return;
  }
  const firstMarket = marketsForAsset()[0];
  if (firstMarket) await selectSymbol(firstMarket.symbol);
  else await switchView("stocks");
});
document.querySelector("#logout-button").addEventListener("click", async () => {
  await apiRequest("/api/auth/logout", { method: "POST" });
  state.user = null;
  state.strategies = [];
  state.backtests = [];
  state.allStocks = null;
  state.marketOverview = null;
  state.selectedStrategyId = "";
  state.appliedStrategyId = "";
  await fetchDashboard();
  // 退出后与游客首屏口径一致：停在详情页但已无可用标的时，回退到公开数据
  if (state.view === "detail" && !state.currentMarket) {
    const firstMarket = marketsForAsset()[0];
    if (firstMarket) await selectSymbol(firstMarket.symbol);
    else await switchView("stocks");
  }
});
elements.addStockButton.addEventListener("click", () => {
  elements.stockError.hidden = true;
  elements.stockDialog.showModal();
  elements.stockForm.elements.symbol.focus();
});
document.querySelector("#new-strategy-button").addEventListener("click", () => openStrategyDialog());
elements.strategyAssetClass.addEventListener("change", updateStrategySymbolField);
elements.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.authError.hidden = true;
  const payload = {
    username: elements.authForm.elements.username.value.trim(),
    password: elements.authForm.elements.password.value,
  };
  try {
    const result = await apiRequest(`/api/auth/${state.authMode}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.user = result.user;
    elements.authDialog.close();
    elements.authForm.reset();
    renderAuthState();
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
    await loadProtectedData();
    await fetchMarketOverview();
    if (state.pendingView) await switchView(state.pendingView);
    state.pendingView = null;
  } catch (error) {
    elements.authError.textContent = error.message;
    elements.authError.hidden = false;
  }
});
elements.stockForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.stockError.hidden = true;
  const submit = elements.stockForm.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    const result = await apiRequest("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol: elements.stockForm.elements.symbol.value.trim() }),
    });
    state.selectedSymbol = result.item.symbol;
    state.assetClass = "stock";
    state.selectedStrategyId = "";
    state.appliedStrategyId = "";
    elements.stockDialog.close();
    elements.stockForm.reset();
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
    await switchView("detail");
  } catch (error) {
    elements.stockError.textContent = error.message;
    elements.stockError.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
elements.strategyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.strategyError.hidden = true;
  const form = new FormData(elements.strategyForm);
  const payload = {
    name: form.get("name"),
    asset_class: form.get("asset_class"),
    symbol: form.get("asset_class") === "stock"
      ? form.get("stock_symbol")
      : form.get("future_symbol"),
    profile: form.get("profile"),
    visibility: form.get("visibility"),
    parameters: {
      ma_fast: Number(form.get("ma_fast")),
      ma_slow: Number(form.get("ma_slow")),
      rsi_period: Number(form.get("rsi_period")),
      rsi_oversold: Number(form.get("rsi_oversold")),
      rsi_overbought: Number(form.get("rsi_overbought")),
      bb_period: Number(form.get("bb_period")),
      bb_std: Number(form.get("bb_std")),
      vote_threshold: Number(form.get("vote_threshold")),
    },
  };
  try {
    const editing = state.editingStrategyId !== null;
    const url = editing ? `/api/strategies/${state.editingStrategyId}` : "/api/strategies";
    const requestPayload = editing
      ? {
        name: payload.name,
        profile: payload.profile,
        visibility: payload.visibility,
        parameters: payload.parameters,
      }
      : payload;
    await apiRequest(url, {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify(requestPayload),
    });
    elements.strategyDialog.close();
    state.editingStrategyId = null;
    elements.strategyForm.reset();
    await loadProtectedData();
    await fetchDashboard();
  } catch (error) {
    elements.strategyError.textContent = error.message;
    elements.strategyError.hidden = false;
  }
});
function hoverChartAt(clientX) {
  const geometry = state.chartGeometry;
  if (!geometry) return;
  const rect = elements.chart.getBoundingClientRect();
  const x = clientX - rect.left;
  const index = Math.max(0, Math.min(geometry.bars.length - 1, Math.floor((x - geometry.margin.left) / geometry.candleStep)));
  state.hoverIndex = index;
  const bar = geometry.bars[index];
  elements.chartTooltip.hidden = false;
  elements.chartTooltip.style.left = `${Math.min(x + 14, rect.width - 174)}px`;
  elements.chartTooltip.style.top = "18px";
  elements.chartTooltip.innerHTML = `${escapeHtml(bar.date)}<br>开 ${formatNumber(bar.open)}　高 ${formatNumber(bar.high)}<br>低 ${formatNumber(bar.low)}　收 ${formatNumber(bar.close)}<br>量 ${formatNumber(bar.volume, 0)}`;
  drawChart();
}

function clearChartHover() {
  state.hoverIndex = null;
  elements.chartTooltip.hidden = true;
  drawChart();
}

function hoverEquityAt(clientX) {
  const geometry = state.backtestChartGeometry;
  if (!geometry || !geometry.curve.length) return;
  const rect = elements.backtestChart.getBoundingClientRect();
  const x = clientX - rect.left;
  const ratio = geometry.curve.length <= 1 ? 0.5 : (x - geometry.margin.left) / geometry.chartWidth;
  const index = Math.max(0, Math.min(geometry.curve.length - 1, Math.round(ratio * (geometry.curve.length - 1))));
  state.backtestHover = index;
  const point = geometry.curve[index];
  const baseEquity = geometry.curve[0].equity;
  const changePct = baseEquity ? (point.equity / baseEquity - 1) * 100 : 0;
  elements.backtestChartTooltip.hidden = false;
  elements.backtestChartTooltip.style.left = `${Math.min(x + 14, rect.width - 174)}px`;
  elements.backtestChartTooltip.style.top = "18px";
  elements.backtestChartTooltip.innerHTML = `${escapeHtml(point.date)}<br>净值 ${formatNumber(point.equity)}<br>区间收益 ${changePct >= 0 ? "+" : ""}${formatNumber(changePct)}%`;
  drawEquityChart();
}

function clearEquityHover() {
  state.backtestHover = null;
  elements.backtestChartTooltip.hidden = true;
  drawEquityChart();
}

function bindChartPointerEvents(canvas, onMove, onClear) {
  canvas.addEventListener("mousemove", (event) => onMove(event.clientX));
  canvas.addEventListener("mouseleave", onClear);
  // 触屏：单指按住拖动即显示十字光标与提示，抬手隐藏（与主流行情 App 一致）；
  // preventDefault + CSS touch-action:none 阻止拖动图表时页面跟随滚动
  canvas.addEventListener("touchstart", (event) => {
    event.preventDefault();
    if (event.touches.length) onMove(event.touches[0].clientX);
  }, { passive: false });
  canvas.addEventListener("touchmove", (event) => {
    event.preventDefault();
    if (event.touches.length) onMove(event.touches[0].clientX);
  }, { passive: false });
  canvas.addEventListener("touchend", onClear);
  canvas.addEventListener("touchcancel", onClear);
}

bindChartPointerEvents(elements.chart, hoverChartAt, clearChartHover);
bindChartPointerEvents(elements.backtestChart, hoverEquityAt, clearEquityHover);

new ResizeObserver(() => {
  if (state.view === "detail") drawChart();
}).observe(elements.chart.parentElement);

new ResizeObserver(() => {
  if (!elements.backtestResult.hidden) drawEquityChart();
}).observe(elements.backtestChart.parentElement);

async function bootstrap() {
  try {
    await fetchCurrentUser();
    await fetchDashboard();
    await fetchBacktestOptions();
    if (state.user) {
      await loadProtectedData();
      await fetchMarketOverview();
    } else {
      // 游客首屏直接落到公开数据：优先观察池详情（/api/dashboard、/api/markets 均匿名可用），
      // 无行情时退到全部数据目录；注册墙只保留在市场总览/策略/回测等账号功能上
      const firstMarket = marketsForAsset()[0];
      if (firstMarket) await selectSymbol(firstMarket.symbol);
      else await switchView("stocks");
    }
  } catch (error) {
    elements.generatedAt.textContent = "连接失败";
    elements.dataAlert.hidden = false;
    elements.dataAlert.textContent = `应用初始化失败：${error.message}`;
  }
}

bootstrap();
