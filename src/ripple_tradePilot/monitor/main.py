"""TradePilot 实时监控主程序（A3：统一切日线 + 通知口径修复）。

轮询节奏：每轮**一次性**刷新全市场快照（``StockDataService.refresh_quotes``，陈旧/盘中才真拉），
逐标的用 ``monitor.daily_eval`` 在**日线**上评估（库内 qfq 历史 + 当日快照合成 provisional bar），
只有"当日新进入 BUY/SELL 的边沿事件"才触发个股通知；收盘后（``finalize_after``，默认 15:05）跑
一次收盘例程：refresh 个股日线（含 A9 复权防护）→ 确认 bar 重评 → 收盘确认通知 → 写信号台账
（provisional=0）→ ``kv_store`` 防重启重复。

改造要点（对照旧实现）：
- **切日线**：删除每标的每轮拉近 5 天 1min 线（N 次网络/轮 → 1 次/轮）；``load_minute_bars``
  退出信号链路，分钟级即时提醒降级为可选 ``price_alert``（默认关，独立去重通道）。
- **统一口径**：删除 ``_run_combo_vote_profile/_run_grid_combo_profile/_run_combo_profile/
  _run_rsi_profile/_run_macd_profile/_run_ma_profile`` 六套画像分支，全部收敛到 ``signals`` 单一
  实现（strict 状态投票）；``breakout`` 是唯一豁免（保留旧路径，标 TODO）。
- **通知门控**：旧实现只看 strongest_signal、每轮重复推送且买入分支不查 sell_count（2买1卖也喊买）；
  现只由过阈值的当日新边沿事件触发，2买1卖经 strict 投票判 CONFLICT 不推送。去重键
  ``(symbol, side, trade_date, provisional)``，盘中/收盘各一次、同日不重发。
- **台账闭环**：盘中写 provisional=1，收盘写 provisional=0，衔接 B2 ``signal_ledger`` 回填前瞻收益。
"""

import asyncio
import logging
import signal as sys_signal
import sys
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..config_loader import get_vote_threshold, load_config, resolve_config_path
from ..data.stock_service import StockDataService
from ..data.tushare_loader import TushareDataLoader
from ..models.types import Bar, Side, Signal
from ..notifiers.feishu import FeishuWebhookNotifier
from ..signals.backtest_profile import default_profile
from ..signals.profile import ProfileSpec, UnsupportedProfileKindError, parse_profile
from ..storage.database import load_daily_bars, load_stock_quotes, stock_catalog_names
from ..storage.signal_ledger import kv_get, kv_set, record_decision
from ..storage.user_store import get_system_strategy
from .daily_eval import (
    MIN_DAILY_BARS,
    DailyEvaluation,
    build_daily_series,
    evaluate_symbol_daily,
    today_str,
)
from .price_alert import (
    PriceAlert,
    PriceAlertConfig,
    evaluate_price_alerts,
    parse_price_alert_config,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("TradePilot")

# 推荐语统一 A 股口径：红=买/涨，绿=卖/跌（展示层常量；signals 内部用 BUY/SELL/HOLD/CONFLICT 编码）
# （与 Web 端 --buy:#ba1a1a、K 线红涨绿跌、飞书信号卡 red=BUY/green=SELL 一致；
# 修改文案时务必同步 monitor_brief.py 等消费方，推荐直接引用这三个常量）
REC_BUY = '🔴 买入'
REC_SELL = '🟢 卖出'
REC_HOLD = '⚪ 观望'
REC_CONFLICT = '🟡 观望'

# 快照陈旧阈值（复刻 api/app.py 的市场总览自动刷新口径）
SNAPSHOT_MAX_AGE = timedelta(minutes=5)
# 收盘例程 kv_store 防重键
FINALIZE_KV_KEY = "monitor.last_finalize"
DEFAULT_FINALIZE_AFTER = "15:05"


def _display_recommendation(code: Optional[str], vote_threshold: Optional[int] = None) -> str:
    """signals 内部编码 → A 股口径展示文案（emoji 只出现在展示层）。"""
    if code == "BUY":
        return REC_BUY
    if code == "SELL":
        return REC_SELL
    if code == "CONFLICT":
        if vote_threshold:
            return f'{REC_CONFLICT} (信号冲突，需{vote_threshold}票)'
        return f'{REC_CONFLICT} (信号冲突)'
    return REC_HOLD


def _latest_quote_time(rows: List[Dict[str, Any]]) -> Optional[datetime]:
    """一批快照行里最新的 quote_time（ISO 秒）；解析失败/空 → None。"""
    latest: Optional[datetime] = None
    for row in rows:
        raw = row.get("quote_time")
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if latest is None or moment > latest:
            latest = moment
    return latest


class SignalNotifier:
    """信号通知器（个股边沿信号 + 价格预警，两条独立去重通道）。"""

    def __init__(self, config: dict, feishu=None,
                 stop_loss_pct: float = 0.08, take_profit_pct: float = 0.20):
        self.config = config
        self.feishu = feishu  # FeishuWebhookNotifier 实例（可选）
        # 风控提示价参数（与回测引擎 RiskConfig 同口径），仅用于估算参考止损/止盈位
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        # 去重：信号键 (symbol, side, trade_date, provisional)；价格预警键 (symbol, kind, trade_date)
        self._sent_signals = set()
        self._sent_alerts = set()

    def _risk_hints(self, side: Side, price: float) -> Optional[dict]:
        """按风控参数估算买入信号的参考止损/止盈价（只读提示，不下单）。"""
        if side != Side.BUY or price <= 0:
            return None
        return {
            'stop_loss': price * (1 - self.stop_loss_pct),
            'take_profit': price * (1 + self.take_profit_pct),
        }

    def should_notify(self, symbol: str, side: Side, trade_date: str, provisional: bool) -> bool:
        """该 (symbol, side, trade_date, provisional) 是否尚未通知过（盘中/收盘各算一次）。"""
        return (symbol, side.value, str(trade_date), bool(provisional)) not in self._sent_signals

    def record(self, symbol: str, side: Side, trade_date: str, provisional: bool) -> None:
        self._sent_signals.add((symbol, side.value, str(trade_date), bool(provisional)))

    def should_alert(self, symbol: str, kind: str, trade_date: str) -> bool:
        """价格预警去重（与信号通道分开，互不串扰）。"""
        return (symbol, kind, str(trade_date)) not in self._sent_alerts

    def record_alert(self, symbol: str, kind: str, trade_date: str) -> None:
        self._sent_alerts.add((symbol, kind, str(trade_date)))

    @staticmethod
    def _forecast_text(forecast: Any) -> str:
        """D5 预估的文本块（控制台/微信/钉钉模板用）；无预估 → 空串。

        与 ``vote_text`` **并列而非合并**：票占比不是概率、模型胜率才是概率，两者挤在一行
        必被误读成同一个东西。缺 ``ret5``/``mae5`` 在位模型时对应段直接省略（不写 0.00%——
        那会被读成"预测不涨不跌"）。
        """
        if forecast is None or getattr(forecast, "p_win", None) is None:
            return ""
        parts = [f"胜率 {forecast.p_win:.1%}"]
        if forecast.expected_net_return is not None:
            parts.append(f"期望净收益 {forecast.expected_net_return:+.2%}")
        if forecast.downside_mae is not None:
            parts.append(f"参考下行 {forecast.downside_mae:.2%}")
        flag = "（演示模型，非可信在位）" if forecast.status == "demo" else ""
        return (
            f"\n🧠 模型预估（{forecast.horizon_days}日 · 基准 {forecast.as_of}）："
            f"{' · '.join(parts)}{flag}"
        )

    def send(self, symbol: str, name: str, side: Side, price: float, strategy: str, bar: Bar,
             provisional: bool = False, trigger_components: Tuple[str, ...] = (),
             buy_count: Optional[int] = None, sell_count: Optional[int] = None,
             vote_threshold: Optional[int] = None, forecast: Any = None):
        """发送个股信号通知（仅由当日新边沿事件触发；strongest_signal 降级为"触发组件"注解）。

        ``forecast`` 为 D5 :class:`ml.scoring.ScoreResult`（或 None）：仅收盘例程传入。
        """
        hints = self._risk_hints(side, price)
        vote_text = ""
        if buy_count is not None and sell_count is not None and vote_threshold:
            vote_text = f"\n🗳️ 投票：看涨 {buy_count} / 看跌 {sell_count}（阈值 {vote_threshold}，无反向票才成立）"
        trigger_text = ""
        if trigger_components:
            trigger_text = f"\n🔧 触发组件：{', '.join(trigger_components)}"
        provisional_text = "\n⏳ 盘中预估（未收盘，以收盘确认为准）" if provisional else "\n✅ 收盘确认"
        forecast_text = self._forecast_text(forecast)
        risk_block = ""
        if hints:
            risk_block = (
                "\n🎯 风控参考位（仅估算，不下单）：\n"
                f"• 止损：{hints['stop_loss']:.2f} 元（-{self.stop_loss_pct:.0%}）\n"
                f"• 止盈：{hints['take_profit']:.2f} 元（+{self.take_profit_pct:.0%}）\n"
            )
        message = f"""
🚨 交易信号提醒
━━━━━━━━━━━━━━━━
📊 标的：{name} ({symbol})
📈 信号：{side.value}
💰 价格：{price:.2f} 元
📉 画像：{strategy}
⏰ 交易日：{bar.timestamp.strftime('%Y-%m-%d')}{provisional_text}{vote_text}{trigger_text}{forecast_text}
━━━━━━━━━━━━━━━━

📝 信号详情：
• 开盘：{bar.open:.2f}
• 最高：{bar.high:.2f}
• 最低：{bar.low:.2f}
• 收盘：{bar.close:.2f}
• 成交量：{bar.volume/10000:.1f}万手
{risk_block}
⚠️ 风险提示：
• 本信号仅供参考
• 请自行判断是否交易
• 投资有风险，入市需谨慎
━━━━━━━━━━━━━━━━
"""

        # 控制台输出
        if self.config.get('console', {}).get('enabled', True):
            print(message)
            logger.info(f"信号：{symbol} {side.value} @ {price:.2f}（{'盘中' if provisional else '收盘'}）")

        # 飞书信号通知（个股信号必须进飞书，而不仅是定期汇总）
        if self.feishu is not None:
            try:
                extra_info = dict(hints or {})
                extra_info['provisional'] = provisional
                note_parts = []
                if buy_count is not None and sell_count is not None and vote_threshold:
                    note_parts.append(f"投票 {buy_count}涨/{sell_count}跌（阈值 {vote_threshold}）")
                if trigger_components:
                    note_parts.append("触发组件：" + ", ".join(trigger_components))
                if note_parts:
                    extra_info['note'] = "；".join(note_parts)
                # D5：预估块交给飞书卡片渲染（feishu.py 认 extra_info['forecast']）。
                # 单独 try——预估是增强项，它序列化出错不该让整条信号通知被外层 except 吞掉
                if forecast is not None and hasattr(forecast, "to_dict"):
                    try:
                        extra_info['forecast'] = forecast.to_dict()
                    except Exception as e:
                        logger.warning(f"{symbol} 预估块序列化失败，通知照发但不带预估：{e}")
                self.feishu.send(
                    symbol=symbol, name=name, side=side,
                    price=price, strategy=strategy, bar=bar,
                    extra_info=extra_info,
                )
            except Exception as e:
                logger.error(f"飞书信号通知失败：{e}")

        # 企业微信 / 钉钉（可选，沿用旧文本模板）
        if self.config.get('wechat', {}).get('enabled', False):
            self._send_wechat(symbol, name, side, price, strategy, bar)
        if self.config.get('dingtalk', {}).get('enabled', False):
            self._send_dingtalk(symbol, name, side, price, strategy, bar)

    def send_price_alert(self, alert: PriceAlert):
        """发送价格预警（非交易信号，独立通道；前缀已在 alert.message 内）。"""
        if self.config.get('console', {}).get('enabled', True):
            print(alert.message)
            logger.info(f"价格预警：{alert.symbol} {alert.kind} @ {alert.price:.2f}")
        if self.feishu is not None:
            try:
                self.feishu._send_text({"msg_type": "text", "content": {"text": alert.message}})
            except Exception as e:
                logger.error(f"飞书价格预警发送失败：{e}")

    def _send_wechat(self, symbol: str, name: str, side: Side, price: float, strategy: str, bar: Bar):
        """发送企业微信通知"""
        import httpx

        webhook = self.config['wechat']['webhook']
        color = "warning" if side == Side.BUY else "comment"
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"""## 🚨 交易信号提醒

> 📊 标的：{name} ({symbol})
> 📈 信号：**{side.value}**
> 💰 价格：{price:.2f} 元
> 📉 画像：{strategy}

**信号详情：**
- 开盘：{bar.open:.2f}
- 最高：{bar.high:.2f}
- 最低：{bar.low:.2f}
- 收盘：{bar.close:.2f}

⚠️ 投资有风险，入市需谨慎"""
            }
        }
        try:
            response = httpx.post(webhook, json=payload, timeout=10)
            if response.status_code == 200:
                logger.info("企业微信通知发送成功")
            else:
                logger.error(f"企业微信通知失败：{response.text}")
        except Exception as e:
            logger.error(f"发送企业微信通知异常：{e}")

    def _send_dingtalk(self, symbol: str, name: str, side: Side, price: float, strategy: str, bar: Bar):
        """发送钉钉通知"""
        import httpx

        webhook = self.config['dingtalk']['webhook']
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"交易信号：{name}",
                "text": f"""## 🚨 交易信号提醒

> 📊 标的：{name} ({symbol})
> 📈 信号：**{side.value}**
> 💰 价格：{price:.2f} 元
> 📉 画像：{strategy}

**信号详情：**
- 开盘：{bar.open:.2f}
- 最高：{bar.high:.2f}
- 最低：{bar.low:.2f}
- 收盘：{bar.close:.2f}

⚠️ 投资有风险，入市需谨慎"""
            }
        }
        try:
            response = httpx.post(webhook, json=payload, timeout=10)
            result = response.json()
            if result.get('errcode') == 0:
                logger.info("钉钉通知发送成功")
            else:
                logger.error(f"钉钉通知失败：{result}")
        except Exception as e:
            logger.error(f"发送钉钉通知异常：{e}")


class MarketMonitor:
    """市场行情监控器（日线信号 + 收盘例程）。"""

    def __init__(self, config_path: Optional[str] = None):
        # 统一路径解析：显式参数 > TRADEPILOT_CONFIG > ~/.tradepilot/config.yaml > ./config.yaml
        resolved_path = resolve_config_path(config_path)
        self.config = load_config(str(resolved_path))

        # 数据加载器：仅用于 is_trade_day（交易日历）与 monitor_brief 的日线兜底；信号链路不再拉分钟线
        ts_token = self.config.get('tushare', {}).get('token', '')
        rate_limit = self.config.get('tushare', {}).get('rate_limit_delay', 1.5)
        self.data_loader = TushareDataLoader(ts_token, rate_limit_delay=rate_limit)
        # 快照/收盘刷新走 StockDataService（懒加载，便于测试注入 Fake）
        self._stock_service: Optional[StockDataService] = None

        # 风控提示价参数（与回测引擎 RiskConfig 同口径，可被 config 的 risk 段覆盖）
        risk_cfg = self.config.get('risk', {}) or {}
        self._stop_loss_pct = float(risk_cfg.get('stop_loss_pct', 0.08))
        self._take_profit_pct = float(risk_cfg.get('take_profit_pct', 0.20))

        # 飞书通知器（个股信号 + 定期报告 + 价格预警）
        feishu_config = self.config.get('notifiers', {}).get('feishu', {})
        if feishu_config.get('enabled', False):
            webhook_url = feishu_config.get('webhook', '')
            if webhook_url:
                self.feishu_notifier = FeishuWebhookNotifier(
                    webhook_url=webhook_url,
                    secret=feishu_config.get('secret'),
                    dashboard_url=feishu_config.get('dashboard_url') or None,
                )
                logger.info("✅ 飞书通知器已初始化")
            else:
                logger.warning(
                    "⚠️ 飞书通知已启用但未配置 webhook，通知将不会发送。"
                    "请设置 notifiers.feishu.webhook 或环境变量 FEISHU_WEBHOOK_URL"
                )
                self.feishu_notifier = None
        else:
            self.feishu_notifier = None

        self.notifier = SignalNotifier(
            self.config.get('notifiers', {}),
            feishu=self.feishu_notifier,
            stop_loss_pct=self._stop_loss_pct,
            take_profit_pct=self._take_profit_pct,
        )

        # 按标的的策略画像
        self._configured_strategy_profiles = self.config.get('strategy_profiles', {})
        self.strategy_profiles: Dict[str, dict] = {}
        self._refresh_strategy_profiles()
        logger.info(f"✅ 已加载策略画像：{', '.join(sorted(self.strategy_profiles.keys())) or '（无，使用默认三件套）'}")

        # 观察池联动 + 全局默认投票阈值（与看板共用 config_loader.get_vote_threshold）
        self._use_watchlist = self.config.get('monitor', {}).get('use_watchlist', True)
        self._vote_default = get_vote_threshold(self.config)
        # 全局默认三件套画像：引用 signals.backtest_profile.DEFAULT_PROFILE 单一常量
        # （A5：与 Web/CLI 回测缺省解析链共用同一份定义，消除双份硬编码）
        self._default_profile = default_profile(self._vote_default)

        # 监控状态
        self._running = False
        self._check_interval = self.config.get('monitor', {}).get('interval_seconds', 300)
        self._report_interval = self.config.get('monitor', {}).get('report_interval_seconds', 3600)
        self._last_report_at: Optional[datetime] = None
        self._check_count = 0
        self._today = today_str()
        # 当日疑似除权（rebase）标的：收盘强制全量重拉名单（衔接 A9）
        self._rebase_watch: set = set()

        # A3：信号统一切日线。bar_freq 仅用于识别旧配置并告警，不再驱动取数。
        monitor_cfg = self.config.get('monitor', {}) or {}
        self._bar_freq = monitor_cfg.get('bar_freq', 'daily')
        if str(self._bar_freq).lower() != 'daily':
            logger.warning(
                f"monitor.bar_freq={self._bar_freq!r} 已废弃：A3 起信号统一在**日线**上评估"
                "（库内 qfq 历史 + 当日快照合成 provisional bar）。分钟级即时提醒降级为可选"
                "价格预警（monitor.price_alert.enabled，默认关）。本次按日线运行。"
            )
        # 收盘例程时刻（默认 15:05）
        self._finalize_after = self._parse_time(
            monitor_cfg.get('finalize_after', DEFAULT_FINALIZE_AFTER), time(15, 5)
        )
        # 价格预警（默认关）
        self._price_alert_config: PriceAlertConfig = parse_price_alert_config(monitor_cfg)
        # 收盘是否顺带增量刷新行业（C 阶段；默认关）
        self._refresh_industry = bool(monitor_cfg.get('refresh_industry', False))

    @staticmethod
    def _parse_time(value: Any, default: time) -> time:
        try:
            return time.fromisoformat(str(value))
        except (TypeError, ValueError):
            return default

    def _get_stock_service(self) -> StockDataService:
        if self._stock_service is None:
            self._stock_service = StockDataService()
        return self._stock_service

    def _collect_symbols(self) -> List[dict]:
        """汇总监控标的：config.yaml 的 symbols + Web 观察池（去重）。"""
        symbols = list(self.config.get('symbols', []))
        known = {str(s.get('code', '')).upper() for s in symbols}

        if not self._use_watchlist:
            return symbols

        try:
            from ..storage.user_store import list_all_watched_symbols
            watched = list_all_watched_symbols()
        except Exception as e:
            logger.warning(f"读取观察池失败，仅监控 config 标的：{e}")
            return symbols

        for item in watched:
            code = str(item.get('symbol', '')).upper()
            if not code or code in known:
                continue
            known.add(code)
            profile_name = f"观察池:{code}"
            profile = dict(self._default_profile)
            try:
                override = get_system_strategy(code, 'stock')
                if override and override.get('parameters'):
                    profile.update(override['parameters'])
                    profile.setdefault('kind', 'combo_vote')
            except Exception as e:
                logger.debug(f"读取 {code} 系统策略失败，使用默认画像：{e}")
            self.strategy_profiles[profile_name] = profile
            symbols.append({
                'code': code,
                'name': item.get('name') or code,
                'strategy_profile': profile_name,
                'from_watchlist': True,
            })

        return symbols

    def _refresh_strategy_profiles(self):
        self.strategy_profiles = {
            name: dict(profile)
            for name, profile in self._configured_strategy_profiles.items()
        }
        for symbol in self.config.get('symbols', []):
            profile_name = symbol.get('strategy_profile')
            if not profile_name:
                continue
            override = get_system_strategy(symbol['code'], 'stock')
            if override is None:
                continue
            configured = self.strategy_profiles.get(profile_name, {})
            self.strategy_profiles[profile_name] = {
                **override['parameters'],
                'kind': configured.get('kind', 'combo_vote'),
            }

    def _resolve_profile(self, profile_name: Optional[str], symbol: str) -> Tuple[dict, str]:
        """解析标的画像 → ``(profile_dict, source_label)``。

        命中 strategy_profiles → ``(画像, 'config:名字')``；否则回退默认三件套 ``(_, 'default')``。
        观察池标的的画像已由 ``_collect_symbols`` 注册进 strategy_profiles。
        """
        if profile_name and profile_name in self.strategy_profiles:
            return self.strategy_profiles[profile_name], f"config:{profile_name}"
        return dict(self._default_profile), "default"

    def is_trading_time(self) -> bool:
        """判断是否在 A 股交易时间（盘中轮询窗口）。"""
        now = datetime.now()
        if not self.data_loader.is_trade_day(now):
            return False
        current = now.time()
        trading_config = self.config.get('monitor', {}).get('trading_hours', {})
        start = self._parse_time(trading_config.get('start', '09:30'), time(9, 30))
        end = self._parse_time(trading_config.get('end', '15:00'), time(15, 0))
        in_day_session = start <= current <= end
        in_lunch_break = time(11, 30) < current < time(13, 0)
        return in_day_session and not in_lunch_break

    # ------------------------------------------------------------------
    # 快照（每轮一次，替代每标的拉分钟线）
    # ------------------------------------------------------------------
    def _refresh_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """刷新全市场快照 → ``{symbol: quote_row}``。盘中且陈旧才真拉，失败回退库内既有快照。"""
        rows: List[Dict[str, Any]] = []
        try:
            rows = load_stock_quotes()
            latest = _latest_quote_time(rows)
            stale = latest is None or (datetime.now() - latest) > SNAPSHOT_MAX_AGE
            if stale:
                self._get_stock_service().refresh_quotes()
                rows = load_stock_quotes()
        except Exception as e:
            logger.warning(f"快照刷新失败，使用库内既有快照评估：{e}")
            try:
                rows = load_stock_quotes()
            except Exception:
                rows = []
        return {str(row.get('symbol', '')).upper(): dict(row) for row in rows if row.get('symbol')}

    def _symbol_names(self) -> Dict[str, str]:
        try:
            return {str(k).upper(): v for k, v in stock_catalog_names().items()}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 单标的评估
    # ------------------------------------------------------------------
    async def check_symbol(self, symbol_config: dict, results: list = None,
                           quotes: Optional[Dict[str, Dict[str, Any]]] = None,
                           write_ledger: bool = False):
        """检查单个标的（日线统一口径；按股票使用专属策略画像）。

        ``quotes``：本轮一次性刷新的全市场快照 ``{symbol: row}``（缺省则不合成 provisional bar，
        仅在库内日线上评估）。``write_ledger``：监控轮询传 True 写盘中 provisional 台账；CLI 单次
        检查默认 False（不污染台账）。
        """
        symbol = str(symbol_config['code']).upper()
        name = symbol_config.get('name') or symbol
        profile_name = symbol_config.get('strategy_profile')
        profile, source = self._resolve_profile(profile_name, symbol)
        quote = (quotes or {}).get(symbol)

        # breakout 是唯一豁免（parse_profile 不支持 → 旧路径，标 TODO）
        try:
            parse_profile(profile, default_threshold=self._vote_default, source=source)
            breakout = False
        except UnsupportedProfileKindError:
            breakout = True
        except ValueError as e:
            logger.error(f"{symbol} 策略画像解析失败（{profile_name}）：{e}")
            if results is not None:
                results.append({'code': symbol, 'name': name, 'price': 0,
                                'recommendation': '❌ 错误', 'buy_count': 0, 'sell_count': 0})
            return

        if breakout:
            self._check_breakout(symbol, name, profile_name, profile, results, quote)
            return

        try:
            evaluation = evaluate_symbol_daily(
                symbol, profile, quote=quote, trade_date=self._today,
                default_threshold=self._vote_default, source=source,
            )
        except Exception as e:
            logger.error(f"检查 {symbol} 失败：{e}", exc_info=True)
            if results is not None:
                results.append({'code': symbol, 'name': name, 'price': 0,
                                'recommendation': '❌ 错误', 'buy_count': 0, 'sell_count': 0})
            return

        if evaluation.insufficient:
            logger.warning(
                f"日线数据不足：{symbol}（{evaluation.n_bars} 根 < {MIN_DAILY_BARS}），跳过。"
                "请先运行 tradepilot data refresh 补历史。"
            )
            return

        if evaluation.rebased:
            self._rebase_watch.add(symbol)
            logger.warning(
                f"{symbol} 当日疑似除权（rebase_factor={evaluation.rebase_factor:.4f}），"
                "已按比值缩放历史对齐快照基准，并列入收盘强制全量重拉名单。"
            )

        self._consume_evaluation(symbol, name, profile_name, evaluation, results,
                                 write_ledger=write_ledger, data_version=None)

    def _consume_evaluation(self, symbol: str, name: str, profile_name: Optional[str],
                            evaluation: DailyEvaluation, results: Optional[list],
                            *, write_ledger: bool, data_version: Optional[str],
                            forecast: Any = None):
        """统一口径评估结果 → 通知门控 + 台账 + 汇总记录（盘中/收盘共用）。

        ``forecast`` 是 D5 :class:`ml.scoring.ScoreResult`（或 None）：仅收盘例程传入——
        盘中不打分，因为特征只认**已入库的收盘 bar**，用 provisional bar 打分会给出一个
        基准日是昨天的预估，反而误导。
        """
        decision = evaluation.decision
        recommendation = _display_recommendation(decision.recommendation, decision.vote_threshold)

        # 通知门控：仅当日**新**边沿事件触发个股通知（strongest_signal 降级为触发组件注解）
        event = evaluation.new_event
        if event is not None:
            side = event.side
            if self.notifier.should_notify(symbol, side, evaluation.trade_date, evaluation.provisional):
                self.notifier.send(
                    symbol=symbol, name=name, side=side, price=evaluation.latest_price,
                    strategy=f"{profile_name or 'default'}", bar=evaluation.latest_bar,
                    provisional=evaluation.provisional,
                    trigger_components=evaluation.trigger_components,
                    buy_count=decision.buy_count, sell_count=decision.sell_count,
                    vote_threshold=decision.vote_threshold,
                    forecast=forecast,
                )
                self.notifier.record(symbol, side, evaluation.trade_date, evaluation.provisional)
            else:
                logger.info(
                    f"跳过重复通知：{symbol} {side.value}"
                    f"（{evaluation.trade_date} provisional={evaluation.provisional}）"
                )

        # 台账：盘中 provisional=1（预估），收盘 provisional=0（确认）
        if write_ledger:
            self._record_ledger(symbol, evaluation, profile_name,
                                data_version=data_version, forecast=forecast)

        if results is not None:
            results.append({
                'code': symbol, 'name': name, 'price': evaluation.latest_price,
                'recommendation': recommendation,
                'buy_count': decision.buy_count, 'sell_count': decision.sell_count,
            })

        logger.info(
            f"{symbol} 信号：BUY={decision.buy_count}/SELL={decision.sell_count}"
            f"（阈值 {decision.vote_threshold}）→ {recommendation}"
            f"{'［盘中预估］' if evaluation.provisional else ''}"
        )

    def _record_ledger(self, symbol: str, evaluation: DailyEvaluation,
                       profile_name: Optional[str], data_version: Optional[str] = None,
                       forecast: Any = None):
        """把决策写入 B2 信号台账（provisional 跟随评估：盘中=1，收盘确认=0）。

        有 D5 预估时一并回填 ``model_id``/``p_win``/``expected_ret``/``downside_mae`` 四列
        （B2 建表时已预留）——台账因此既能算规则票的胜率，也能**按 model_id 分组算模型的
        实际命中率**，这是 D 阶段闭环的最后一环。
        """
        if evaluation.decision is None:
            return
        try:
            record_decision(
                symbol, evaluation.decision,
                source="monitor",
                provisional=1 if evaluation.provisional else 0,
                profile_name=profile_name or "default",
                horizon=forecast.horizon_days if forecast is not None else 5,
                data_version=data_version,
                trade_date=evaluation.trade_date,
                model_id=forecast.model_id if forecast is not None else None,
                p_win=forecast.p_win if forecast is not None else None,
                expected_ret=forecast.expected_net_return if forecast is not None else None,
                downside_mae=forecast.downside_mae if forecast is not None else None,
            )
        except Exception as e:
            logger.debug(f"{symbol} 台账写入失败（不影响监控）：{e}")

    def _forecast_for(self, symbol: str) -> Any:
        """D5：取在位 ML 模型对该标的的预估；不可用则 ``None``（通知不带预估块）。

        打分器走 :func:`ml.scoring.get_scorer` 的进程级缓存——每轮只多一次廉价 DB 查询，
        在位 model_id 未变则复用已反序列化的工件。sklearn 未装 / 无 promoted 模型 / 工件
        损坏 / 任何异常都退化为 ``None``：**ML 支线绝不影响收盘例程**。
        """
        try:
            from ..ml.scoring import get_scorer

            scorer = get_scorer()
            if scorer is None:
                return None
            return scorer.score_symbol(symbol)
        except Exception as e:
            logger.debug(f"{symbol} ML 预估不可用（不影响监控）：{e}")
            return None

    def _check_breakout(self, symbol: str, name: str, profile_name: Optional[str],
                        profile: dict, results: Optional[list], quote: Optional[Dict[str, Any]]):
        """breakout 画像豁免路径（TODO: 以 donchian+rsi 组件表达后并入统一口径）。"""
        from ..signals.facade import coerce_bars
        try:
            db_rows = load_daily_bars(symbol)
            rows, provisional, rebased, factor = build_daily_series(
                db_rows, quote, trade_date=self._today
            )
            bars = coerce_bars(rows)
            if len(bars) < MIN_DAILY_BARS:
                logger.warning(f"日线数据不足：{symbol}（{len(bars)} < {MIN_DAILY_BARS}），跳过 breakout。")
                return
            if rebased:
                self._rebase_watch.add(symbol)
            signals, strongest_signal, recommendation = self._run_breakout_profile(profile, bars)
            latest_bar = bars[-1]
            if strongest_signal:
                _strategy_name, signal = strongest_signal
                if signal.side and self.notifier.should_notify(symbol, signal.side, self._today, provisional):
                    self.notifier.send(
                        symbol=symbol, name=name, side=signal.side, price=latest_bar.close,
                        strategy=f"{profile_name or 'default'}:breakout", bar=latest_bar,
                        provisional=provisional,
                    )
                    self.notifier.record(symbol, signal.side, self._today, provisional)
            if results is not None:
                buy_count = sum(1 for s in signals.values() if s.side == Side.BUY)
                sell_count = sum(1 for s in signals.values() if s.side == Side.SELL)
                results.append({
                    'code': symbol, 'name': name, 'price': latest_bar.close,
                    'recommendation': recommendation, 'buy_count': buy_count, 'sell_count': sell_count,
                })
        except Exception as e:
            logger.error(f"检查 {symbol}（breakout）失败：{e}", exc_info=True)
            if results is not None:
                results.append({'code': symbol, 'name': name, 'price': 0,
                                'recommendation': '❌ 错误', 'buy_count': 0, 'sell_count': 0})

    def _run_breakout_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        """突破画像（唯一保留的旧路径；breakout 暂不表达为投票组件）。"""
        from ..strategies.rsi import RSI

        window = profile.get('breakout_window', 20)
        rsi_period = profile.get('rsi_period', 6)
        buy_rsi_min = profile.get('buy_rsi_min', 55)
        exit_ma = profile.get('exit_ma', 10)
        sell_rsi_max = profile.get('sell_rsi_max', 45)

        latest_bar = bars[-1]
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        highest_prev = max(highs[-(window + 1):-1]) if len(highs) > window else None
        ma_exit = sum(closes[-exit_ma:]) / exit_ma if len(closes) >= exit_ma else None

        rsi_strategy = RSI(period=rsi_period, oversold=30, overbought=70)
        for bar in bars[:-1]:
            rsi_strategy.on_bar(bar)
        rsi_signal = rsi_strategy.on_bar(latest_bar)
        latest_rsi = rsi_strategy.get_current_rsi()

        side = None
        strength = 0.0
        if highest_prev is not None and latest_rsi is not None:
            if latest_bar.close > highest_prev and latest_rsi > buy_rsi_min:
                side = Side.BUY
                strength = 1.0 + min((latest_rsi - buy_rsi_min) / 20.0, 0.5)
            elif ma_exit is not None and (latest_bar.close < ma_exit or latest_rsi < sell_rsi_max):
                side = Side.SELL
                strength = 1.0

        signal = Signal(timestamp=latest_bar.timestamp, side=side, strength=strength)
        signals = {'breakout': signal, 'rsi_filter': rsi_signal}
        strongest_signal = ('breakout', signal) if signal.side else None
        recommendation = REC_BUY if signal.side == Side.BUY else REC_SELL if signal.side == Side.SELL else REC_HOLD
        return signals, strongest_signal, recommendation

    # ------------------------------------------------------------------
    # 价格预警（默认关，独立通道）
    # ------------------------------------------------------------------
    def _dispatch_price_alerts(self, quotes: Dict[str, Dict[str, Any]]):
        if not self._price_alert_config.enabled or not quotes:
            return
        try:
            alerts = evaluate_price_alerts(
                list(quotes.values()), config=self._price_alert_config, names=self._symbol_names()
            )
        except Exception as e:
            logger.warning(f"价格预警评估失败：{e}")
            return
        for alert in alerts:
            if self.notifier.should_alert(alert.symbol, alert.kind, self._today):
                self.notifier.send_price_alert(alert)
                self.notifier.record_alert(alert.symbol, alert.kind, self._today)

    # ------------------------------------------------------------------
    # 定期汇总报告
    # ------------------------------------------------------------------
    async def send_periodic_report(self, results: list):
        """发送定期监控报告到飞书（interactive 卡片）。"""
        if not self.feishu_notifier:
            return

        buy_stocks = [r for r in results if r.get('recommendation') == REC_BUY]
        sell_stocks = [r for r in results if r.get('recommendation') == REC_SELL]
        hold_stocks = [r for r in results if '观望' in r.get('recommendation', '')]

        content = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "📊 TradePilot 监控报告"},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**监控时间：** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                                f"**监控标的：** {len(results)}只\n"
                                f"**检查次数：** {self._check_count}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                ],
            },
        }

        if buy_stocks:
            buy_text = "\n".join([f"• {r['name']} ({r['code']}) - {r['price']:.2f}元" for r in buy_stocks])
            content["card"]["elements"].append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"🔴 **买入信号** ({len(buy_stocks)}只):\n{buy_text}"},
            })
        if sell_stocks:
            sell_text = "\n".join([f"• {r['name']} ({r['code']}) - {r['price']:.2f}元" for r in sell_stocks])
            content["card"]["elements"].append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"🟢 **卖出信号** ({len(sell_stocks)}只):\n{sell_text}"},
            })
        if hold_stocks:
            content["card"]["elements"].append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"⚪ 观望：{len(hold_stocks)}只（无明确信号）"},
            })

        if getattr(self.feishu_notifier, "dashboard_url", None):
            content["card"]["elements"].append({"tag": "hr"})
            content["card"]["elements"].append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📈 打开监控台查看图表"},
                    "url": self.feishu_notifier.dashboard_url,
                    "type": "primary",
                }],
            })

        content["card"]["elements"].append({"tag": "hr"})
        content["card"]["elements"].append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": "⚠️ 投资有风险，入市需谨慎。本监控仅供参考，不构成投资建议。",
            }],
        })

        if self.feishu_notifier.send_card(content):
            logger.info("✅ 飞书定期报告已发送")
        else:
            logger.error("飞书定期报告发送失败（详见飞书通知器日志）")

    def _should_send_report(self, results: list) -> bool:
        """定期报告限频：默认每小时最多一条，出现买卖信号时立即发送。"""
        has_signal = any(r.get('recommendation') in (REC_BUY, REC_SELL) for r in results)
        if has_signal:
            return True
        if self._last_report_at is None:
            return True
        return (datetime.now() - self._last_report_at).total_seconds() >= self._report_interval

    # ------------------------------------------------------------------
    # 收盘例程（finalize_after，默认 15:05；每交易日一次，kv_store 防重启重复）
    # ------------------------------------------------------------------
    async def _maybe_finalize(self):
        now = datetime.now()
        if now.time() < self._finalize_after:
            return
        if not self.data_loader.is_trade_day(now):
            return
        today = today_str(now)
        if kv_get(FINALIZE_KV_KEY) == today:
            logger.debug(f"收盘例程今日（{today}）已执行，跳过。")
            return
        try:
            await self._finalize_after_close(today)
        except Exception as e:
            logger.error(f"收盘例程异常：{e}", exc_info=True)
            return  # 异常时不写 kv，下一轮重试
        kv_set(FINALIZE_KV_KEY, today)
        logger.info(f"✅ 收盘例程完成（{today}），已记 kv_store 防重复。")

    async def _finalize_after_close(self, today: str):
        """收盘确认：refresh 个股日线（A9 防护）→ 确认 bar 重评 → 收盘确认通知 → 写台账(0)。"""
        logger.info("🌙 收盘例程启动：刷新个股日线 → 确认重评 → 收盘通知 → 写台账")
        self._today = today
        self._refresh_strategy_profiles()
        symbols = self._collect_symbols()
        service = self._get_stock_service()
        results: list = []

        for sym_config in symbols:
            symbol = str(sym_config['code']).upper()
            name = sym_config.get('name') or symbol
            profile_name = sym_config.get('strategy_profile')
            profile, source = self._resolve_profile(profile_name, symbol)

            # 1) refresh 个股日线（含 A9 detect_rebase + 全量重拉）；疑似除权标的扩窗强制重拉
            data_version: Optional[str] = None
            try:
                initial_days = 1095 if symbol in self._rebase_watch else 365
                report = service.refresh(symbol, initial_days=initial_days)
                data_version = report.get('data_version')
                if report.get('rebased'):
                    logger.warning(f"{symbol} 收盘刷新检测到复权漂移，已全量重拉整段覆盖。")
            except Exception as e:
                logger.error(f"{symbol} 收盘日线刷新失败（用库内既有数据重评）：{e}")

            # 2) breakout 豁免不入台账/统一通知
            try:
                parse_profile(profile, default_threshold=self._vote_default, source=source)
            except UnsupportedProfileKindError:
                continue
            except ValueError as e:
                logger.error(f"{symbol} 画像解析失败，跳过收盘重评：{e}")
                continue

            # 3) 确认 bar 重评（库内已含今日收盘 → provisional=False）
            try:
                evaluation = evaluate_symbol_daily(
                    symbol, profile, quote=None, trade_date=today,
                    default_threshold=self._vote_default, source=source,
                )
            except Exception as e:
                logger.error(f"{symbol} 收盘重评失败：{e}", exc_info=True)
                continue
            if evaluation.insufficient:
                logger.warning(f"{symbol} 收盘重评数据不足（{evaluation.n_bars} 根），跳过。")
                continue

            # 4) D5 ML 预估（仅收盘路径；失败/无在位模型 → None，通知不带预估块）
            #    放在重评之后、_consume_evaluation 之前，才能同时进通知卡片与台账四列
            forecast = self._forecast_for(symbol)

            self._consume_evaluation(symbol, name, profile_name, evaluation, results,
                                     write_ledger=True, data_version=data_version,
                                     forecast=forecast)

        self._rebase_watch.clear()

        # 5) 收盘汇总报告
        if results and self._should_send_report(results):
            await self.send_periodic_report(results)
            self._last_report_at = datetime.now()

        # 6) C5：市场级数据落库（宽度/指数/可选行业），独立于个股通知，单项失败不影响其他
        self._finalize_market_data(today, symbols)

    def _finalize_market_data(self, today: str, symbols: List[dict]) -> None:
        """C5：收盘例程的市场级数据落库（C2 宽度 → C1 指数 → 可选 C3 行业）。

        全部独立 try/except——单项失败不影响其他，也不影响已完成的个股收盘重评/通知。
        同步网络调用与上游 ``service.refresh`` 一致（收盘例程每交易日一次，非热路径）。
        """
        service = self._get_stock_service()

        # 1) refresh_quotes 终态 → aggregate_breadth 写 market_daily（C2）
        try:
            service.refresh_quotes()
        except Exception as e:
            logger.warning(f"收盘全市场快照刷新失败，用库内既有快照聚合宽度：{e}")
        try:
            from ..data.market_service import record_market_breadth

            rows = load_stock_quotes()
            if rows:
                breadth = record_market_breadth(today, rows, "snapshot")
                logger.info(
                    f"📊 市场宽度已落库（{today}）：上涨 {breadth['advancers']} / "
                    f"下跌 {breadth['decliners']} / 涨停 {breadth['limit_up']} / "
                    f"跌停 {breadth['limit_down']}"
                )
            else:
                logger.warning("收盘无全市场快照，跳过市场宽度落库")
        except Exception as e:
            logger.error(f"市场宽度聚合/落库失败：{e}", exc_info=True)

        # 2) refresh_index_daily（4 指数，三级降级链，部分成功）（C1）
        try:
            from ..data.market_service import MarketDataService

            report = MarketDataService().refresh_indexes()
            if report["failed"]:
                logger.warning(
                    f"指数日线刷新：成功 {len(report['refreshed'])} / "
                    f"失败 {len(report['failed'])}（失败指数特征 D 阶段 NaN 降级）"
                )
            else:
                logger.info(f"📈 指数日线已落库：{len(report['refreshed'])} 个指数")
        except Exception as e:
            logger.error(f"指数日线刷新失败：{e}", exc_info=True)

        # 3) 可选行业增量（C3，monitor.refresh_industry 默认 false）
        monitor_cfg = self.config.get("monitor", {}) if isinstance(self.config, dict) else {}
        if not monitor_cfg.get("refresh_industry", False):
            return
        try:
            from ..data.industry_service import IndustryDataService

            pool = [str(s.get("code")) for s in symbols if s.get("code")]
            if pool:
                report = IndustryDataService().refresh_for_symbols(pool)
                logger.info(
                    f"🏭 行业板块刷新：目标 {report['boards_total']} → "
                    f"成功 {len(report['boards_refreshed'])} / "
                    f"失败 {len(report['boards_failed'])}"
                )
        except Exception as e:
            logger.error(f"行业板块刷新失败：{e}", exc_info=True)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run_loop(self):
        """主监控循环（日线信号；盘中轮询 + 收盘例程）。"""
        logger.info("🚀 TradePilot 启动监控（日线信号口径）...")
        logger.info(f"📊 配置标的数：{len(self.config.get('symbols', []))}（观察池联动：{'开' if self._use_watchlist else '关'}）")
        logger.info(f"⏱️ 检查间隔：{self._check_interval}秒")
        logger.info(f"📝 汇总报告间隔：{self._report_interval}秒（有信号时立即发送）")
        logger.info(f"🕒 盘中窗口：09:30-15:00；收盘例程：{self._finalize_after.strftime('%H:%M')} 后每交易日一次")
        logger.info(f"📱 飞书通知：{'✅ 已启用' if self.feishu_notifier else '❌ 未启用'}")
        logger.info(f"⚡ 价格预警：{'✅ 已启用' if self._price_alert_config.enabled else '❌ 未启用（默认关）'}")

        self._running = True
        while self._running:
            results: list = []
            try:
                self._today = today_str()
                if self.is_trading_time():
                    self._refresh_strategy_profiles()
                    symbols = self._collect_symbols()
                    self._check_count += 1
                    logger.info(f"\n{'='*60}")
                    logger.info(f"📋 第 {self._check_count} 次监控 ({datetime.now().strftime('%H:%M')})，标的 {len(symbols)} 只")
                    logger.info(f"{'='*60}")

                    # 每轮一次性刷新全市场快照（替代每标的拉分钟线）
                    quotes = self._refresh_snapshot()

                    tasks = [
                        self.check_symbol(sym, results, quotes, write_ledger=True)
                        for sym in symbols
                    ]
                    await asyncio.gather(*tasks)

                    # 价格预警（默认关，独立通道）
                    self._dispatch_price_alerts(quotes)

                    if self._should_send_report(results):
                        await self.send_periodic_report(results)
                        self._last_report_at = datetime.now()
                else:
                    # 非盘中：到点跑收盘例程（每交易日一次）
                    await self._maybe_finalize()
                    next_trading_time = datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)
                    if datetime.now() >= next_trading_time:
                        next_trading_time += timedelta(days=1)
                    logger.info(f"⏸️  非盘中，下次检查：{next_trading_time.strftime('%Y-%m-%d %H:%M')}")

                await asyncio.sleep(self._check_interval)
            except Exception as e:
                logger.error(f"监控循环异常：{e}", exc_info=True)
                await asyncio.sleep(60)

    def stop(self):
        """停止监控"""
        self._running = False
        logger.info(f"🛑 监控已停止（共检查 {self._check_count} 次）")


async def main(config_path: Optional[str] = None):
    """主函数"""
    resolved_path = resolve_config_path(config_path)
    if not resolved_path.exists():
        logger.error(
            f"配置文件不存在：{resolved_path}"
            "（可设置 TRADEPILOT_CONFIG 环境变量，或先运行 tradepilot init）"
        )
        sys.exit(1)

    monitor = MarketMonitor(str(resolved_path))

    def signal_handler(sig, frame):
        logger.info("收到停止信号...")
        monitor.stop()

    sys_signal.signal(sys_signal.SIGINT, signal_handler)
    sys_signal.signal(sys_signal.SIGTERM, signal_handler)

    try:
        await monitor.run_loop()
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
