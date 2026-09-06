#!/usr/bin/env python3
"""
TradePilot 安装脚本
支持 macOS、Linux、Windows
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path


def run_cmd(cmd, check=True):
    """运行命令"""
    print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result


def install():
    """安装 TradePilot"""
    project_root = Path(__file__).parent.absolute()
    
    print("🚀 TradePilot 安装程序")
    print(f"📁 项目目录: {project_root}")
    
    # 检查 Python 版本
    py_version = sys.version_info
    if py_version < (3, 9):
        print("❌ 需要 Python 3.9 或更高版本")
        sys.exit(1)
    print(f"✅ Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    
    # 检查 pip
    if not shutil.which("pip"):
        print("❌ 未找到 pip，请先安装")
        sys.exit(1)
    print("✅ pip 已安装")
    
    # 安装依赖
    print("\n📦 安装依赖...")
    run_cmd(f"{sys.executable} -m pip install -r {project_root / 'requirements.txt'}")
    
    # 设置 PYTHONPATH
    print("\n🔧 设置环境...")
    env_file = Path.home() / ".tradepilot" / "env.sh"
    env_content = f"""# TradePilot 环境变量
export PYTHONPATH="{project_root}/src:$PYTHONPATH"
export PATH="{project_root}/scripts:$PATH"
"""
    env_file.parent.mkdir(exist_ok=True)
    env_file.write_text(env_content)
    print(f"✅ 环境变量文件: {env_file}")
    print("请运行: source ~/.tradepilot/env.sh")
    
    # 创建配置目录
    config_dir = Path.home() / ".tradepilot"
    config_dir.mkdir(exist_ok=True)
    print(f"✅ 配置目录: {config_dir}")
    
    # 创建配置模板
    config_file = config_dir / "config.yaml"
    if not config_file.exists():
        config_template = """# TradePilot 用户配置
# 设置环境变量或使用此配置文件

# Tushare 配置
tushare:
  token: ""  # 或设置环境变量 TUSHARE_TOKEN
  cache_dir: "data/cache"
  rate_limit_delay: 1.5

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
  # A3 起信号统一在日线评估；旧 "1min" 已废弃（读到非 daily 值会告警并按日线运行）
  bar_freq: "daily"
  finalize_after: "15:05"   # 收盘例程触发时刻（HH:MM），每交易日一次
  refresh_industry: false   # 收盘是否顺带增量刷新行业板块（C 阶段，默认关）
  trading_hours:
    start: "09:30"
    end: "15:00"
  check_non_trading: false
  # 可选价格预警（规则化、非投票信号，默认关；与信号通道分开去重）
  price_alert:
    enabled: false
    pct_threshold: 0.05      # |日内涨跌幅| ≥ 此比例 → 急拉/急跌
    near_limit_pct: 0.02     # 距涨/跌停 ≤ 此比例 → 逼近涨/跌停（按板块 limit）
    reference_prices: {}     # symbol → 参考价（上穿触发）

# 监控标的
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    strategy_profile: "rsi_002022"
    notify_on: ["BUY", "SELL"]
"""
        config_file.write_text(config_template, encoding='utf-8')
        print(f"✅ 配置文件已创建: {config_file}")
    else:
        print(f"⚠️ 配置文件已存在: {config_file}")
    
    print("\n✅ 安装完成!")
    print("\n使用方法:")
    print("  source ~/.tradepilot/env.sh  加载环境变量")
    print("  tradepilot --help            显示帮助")
    print("  tradepilot init              初始化配置")
    print("  tradepilot config            查看配置")
    print("  tradepilot backtest SYMBOL   运行回测")
    print("  tradepilot monitor SYMBOL    监控股票")
    
    print("\n配置说明:")
    print("  1. 编辑配置文件: ~/.tradepilot/config.yaml")
    print("  2. 或使用环境变量:")
    print("     export TUSHARE_TOKEN=your_token")
    print("     export FEISHU_WEBHOOK_SECRET=your_secret")


def update():
    """更新 TradePilot"""
    project_root = Path(__file__).parent.absolute()
    print("🔄 更新 TradePilot...")
    run_cmd(f"{sys.executable} -m pip install --upgrade -e {project_root}")
    print("✅ 更新完成")


def uninstall():
    """卸载 TradePilot"""
    print("🗑️  卸载 TradePilot...")
    run_cmd(f"{sys.executable} -m pip uninstall ripple_tradePilot -y")
    print("✅ 卸载完成")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='TradePilot 安装程序')
    parser.add_argument('--update', action='store_true', help='更新')
    parser.add_argument('--uninstall', action='store_true', help='卸载')
    args = parser.parse_args()
    
    if args.uninstall:
        uninstall()
    elif args.update:
        update()
    else:
        install()
