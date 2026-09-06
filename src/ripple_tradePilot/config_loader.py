"""
TradePilot 配置加载器
支持环境变量和配置文件

配置优先级（从高到低）：
1. 环境变量（如 TUSHARE_TOKEN）
2. 用户配置文件 ~/.tradepilot/config.yaml

注意：不提供代码默认值，必须由用户配置
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional

import yaml


# 用户配置目录
USER_CONFIG_DIR = Path.home() / ".tradepilot"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"


def resolve_config_path(explicit_path: Optional[str] = None) -> Path:
    """按统一优先级解析配置文件路径。

    优先级：显式参数 > TRADEPILOT_CONFIG 环境变量 > ~/.tradepilot/config.yaml > ./config.yaml

    Returns:
        第一个存在的候选路径；都不存在时返回优先级最高的候选（用于错误提示）。
    """
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_path = os.getenv('TRADEPILOT_CONFIG')
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(USER_CONFIG_FILE)
    candidates.append(Path('config.yaml'))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _normalize_feishu_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """统一两套飞书配置结构。

    历史上同时存在顶层 ``feishu.webhook_url/webhook_secret``（文档与 init 模板）
    和 ``notifiers.feishu.webhook/secret``（monitor 实际读取）两套结构，
    按文档配置的用户通知会静默失效。此处把旧结构合并进 notifiers.feishu，
    并在已配置 webhook 但未显式设置 enabled 时默认启用。
    """
    legacy = config.get('feishu') or {}
    if not isinstance(legacy, dict):
        legacy = {}
    notifiers = config.get('notifiers') or {}
    if not isinstance(notifiers, dict):
        notifiers = {}
    feishu = dict(notifiers.get('feishu') or {})

    if not feishu.get('webhook') and legacy.get('webhook_url'):
        feishu['webhook'] = legacy['webhook_url']
    if not feishu.get('secret') and legacy.get('webhook_secret'):
        feishu['secret'] = legacy['webhook_secret']
    if 'enabled' not in feishu and feishu.get('webhook'):
        feishu['enabled'] = True

    if feishu:
        notifiers['feishu'] = feishu
        config['notifiers'] = notifiers
    return config


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载配置，支持环境变量覆盖

    Args:
        config_path: 指定配置文件路径（可选，覆盖默认路径）

    Returns:
        配置字典

    Raises:
        FileNotFoundError: 配置文件不存在且未设置对应环境变量
    """
    # 1. 加载用户配置
    target_path = resolve_config_path(config_path)
    config = {}
    if target_path.exists():
        with open(target_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    
    # 2. 环境变量覆盖
    env_mappings = {
        # Tushare
        'TUSHARE_TOKEN': ['tushare', 'token'],
        'TUSHARE_CACHE_DIR': ['tushare', 'cache_dir'],
        'TUSHARE_RATE_LIMIT': ['tushare', 'rate_limit_delay'],
        
        # MX (东方财富妙想)
        'MX_APIKEY': ['mx', 'api_key'],
        
        # Feishu
        'FEISHU_WEBHOOK_URL': ['feishu', 'webhook_url'],
        'FEISHU_WEBHOOK_SECRET': ['feishu', 'webhook_secret'],
    }
    
    for env_var, key_path in env_mappings.items():
        value = os.getenv(env_var)
        if value:
            target = config
            for key in key_path[:-1]:
                if key not in target:
                    target[key] = {}
                target = target[key]
            target[key_path[-1]] = value

    notifier_env_mappings = {
        'FEISHU_WEBHOOK_URL': ['notifiers', 'feishu', 'webhook'],
        'FEISHU_WEBHOOK_SECRET': ['notifiers', 'feishu', 'secret'],
    }
    for env_var, key_path in notifier_env_mappings.items():
        value = os.getenv(env_var)
        if value:
            target = config
            for key in key_path[:-1]:
                target = target.setdefault(key, {})
            target[key_path[-1]] = value

    feishu_enabled = os.getenv('FEISHU_ENABLED')
    if feishu_enabled:
        enabled_values = {'1', 'true', 'yes', 'on'}
        config.setdefault('notifiers', {}).setdefault('feishu', {})['enabled'] = (
            feishu_enabled.strip().lower() in enabled_values
        )

    # 3. 统一两套飞书配置结构（旧 feishu.* → notifiers.feishu.*）
    config = _normalize_feishu_config(config)

    return config


def init_config(config_path: Optional[Path] = None):
    """初始化用户配置文件

    Args:
        config_path: 可选，指定生成位置；默认 ~/.tradepilot/config.yaml
    """
    target = Path(config_path) if config_path else USER_CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        print(f"⚠️  配置文件已存在: {target}")
        return

    default_config = """# TradePilot 用户配置
# 设置环境变量或使用此配置文件

# Tushare 配置
tushare:
  token: ""  # 或设置环境变量 TUSHARE_TOKEN
  cache_dir: "data/cache"
  rate_limit_delay: 1.5

# 日线数据刷新（A9 复权混接防护）
data:
  refresh_overlap_days: 30   # 增量刷新回看窗口；给复权基准漂移检测留足重叠样本
  full_refetch_days: 1095    # 检测到上游 qfq 重算时全量重拉的天数下限（约 3 年）

# 东方财富妙想配置（可选）
mx:
  api_key: ""  # 或设置环境变量 MX_APIKEY

# 飞书机器人配置（配好 webhook 即生效；旧的顶层 feishu.webhook_url 结构仍兼容）
notifiers:
  feishu:
    enabled: true   # 设为 false 关闭通知；环境变量 FEISHU_ENABLED 可覆盖
    webhook: ""     # 或设置环境变量 FEISHU_WEBHOOK_URL
    secret: ""      # 或设置环境变量 FEISHU_WEBHOOK_SECRET

# 监控配置
monitor:
  interval_seconds: 300
  bar_freq: "daily"          # A3：信号统一在日线评估；旧 "1min" 已废弃（读到会告警并按日线运行）
  finalize_after: "15:05"    # 收盘例程触发时刻（HH:MM）：刷新日线→确认重评→收盘通知→写台账
  refresh_industry: false    # 收盘是否顺带增量刷新行业板块（C 阶段，默认关）
  trading_hours:
    start: "09:30"
    end: "15:00"
  check_non_trading: false
  report_interval_seconds: 3600  # 无信号时的例行心跳间隔
  use_watchlist: true            # 是否联动用户观察池
  vote_threshold: 2              # combo_vote 默认投票阈值（1~3），监控与看板共用
  price_alert:                   # 可选价格预警（规则化、非投票信号，默认关；与信号通道分开去重）
    enabled: false
    pct_threshold: 0.05          # |日内涨跌幅| ≥ 此比例 → 急拉/急跌
    near_limit_pct: 0.02         # 距涨/跌停 ≤ 此比例 → 逼近涨/跌停（按板块 limit）
    reference_prices: {}         # symbol → 参考价（上穿触发）

# 监控标的（不填 strategy_profile 时使用默认 combo_vote 画像）
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    notify_on: ["BUY", "SELL"]
"""

    target.write_text(default_config, encoding='utf-8')
    print(f"✅ 配置文件已创建: {target}")
    print("请编辑配置文件设置您的 API Key")


def get_vote_threshold(config: Dict[str, Any]) -> int:
    """全局默认投票阈值（combo_vote 类策略：MA/RSI/BB 需要几票同向才出信号）。

    读取优先级：``monitor.vote_threshold`` > 顶层 ``vote_threshold`` > 默认 2。
    监控与看板共用此函数，保证回测/监控/展示口径一致；结果限制在 1~3。
    """
    from ripple_tradePilot.indicators import DEFAULT_VOTE_THRESHOLD

    monitor_cfg = config.get('monitor')
    raw = None
    if isinstance(monitor_cfg, dict):
        raw = monitor_cfg.get('vote_threshold')
    if raw is None:
        raw = config.get('vote_threshold')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_VOTE_THRESHOLD
    return max(1, min(3, value))


def get_tushare_token(config: Dict[str, Any]) -> str:
    """获取 Tushare Token"""
    token = config.get('tushare', {}).get('token', '')
    if not token:
        raise ValueError(
            "Tushare token not found. "
            "Set TUSHARE_TOKEN env var or add to ~/.tradepilot/config.yaml"
        )
    return token
