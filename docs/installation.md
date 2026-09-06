# TradePilot 安装指南

## 系统要求

- **Python**: 3.9 或更高版本
- **操作系统**: macOS、Linux、Windows
- **网络**: 需要访问 Tushare、AkShare 等数据接口

## 安装方法

### 方法一：使用安装脚本（推荐）

#### macOS / Linux

```bash
# 克隆仓库
git clone https://github.com/chenboripple/TradePilot.git
cd TradePilot

# 运行安装脚本
python3 install.py
```

#### Windows

```powershell
# 克隆仓库
git clone https://github.com/chenboripple/TradePilot.git
cd TradePilot

# 运行安装脚本
python install.py
```

### 方法二：使用 pip 安装

```bash
# 克隆仓库
git clone https://github.com/chenboripple/TradePilot.git
cd TradePilot

# 安装
pip install -e .
```

### 方法三：使用虚拟环境

#### macOS / Linux

```bash
# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
pip install -e .
```

#### Windows

```powershell
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
pip install -e .
```

## 配置

### 方式一：环境变量（推荐用于服务器部署）

#### macOS / Linux

```bash
# 临时设置（当前终端）
export TUSHARE_TOKEN="your_token"
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/..."
export FEISHU_WEBHOOK_SECRET="your_secret"

# 永久设置（添加到 ~/.bashrc 或 ~/.zshrc）
echo 'export TUSHARE_TOKEN="your_token"' >> ~/.bashrc
source ~/.bashrc
```

#### Windows

```powershell
# 临时设置（当前终端）
$env:TUSHARE_TOKEN="your_token"

# 永久设置（系统环境变量）
[Environment]::SetEnvironmentVariable("TUSHARE_TOKEN", "your_token", "User")
```

### 方式二：配置文件（推荐用于本地开发）

```bash
# 初始化配置文件
tradepilot init
```

编辑 `~/.tradepilot/config.yaml`：

```yaml
tushare:
  token: "your_token"
  cache_dir: "data/cache"
  rate_limit_delay: 1.5

# 飞书通知（配好 webhook 即生效；旧的顶层 feishu.webhook_url 结构仍兼容）
notifiers:
  feishu:
    enabled: true
    webhook: "https://open.feishu.cn/..."
    secret: "your_secret"

monitor:
  interval_seconds: 300
  bar_freq: "1min"
  trading_hours:
    start: "09:30"
    end: "15:00"

symbols:
  - code: "002022.SZ"
    name: "科华生物"
    strategy_profile: "rsi_002022"
    notify_on: ["BUY", "SELL"]
```

## 验证安装

```bash
# 检查版本
tradepilot version

# 查看配置
tradepilot config

# 显示帮助
tradepilot --help
```

## 更新

```bash
python install.py --update
```

## 卸载

```bash
python install.py --uninstall
```

## 常见问题

### 1. 安装失败：权限不足

**解决方案**：使用 `--user` 参数

```bash
pip install --user -e .
```

### 2. 找不到命令 `tradepilot`

**解决方案**：检查 PATH

```bash
# 查看安装路径
which tradepilot

# 如果没有，添加 Python 脚本目录到 PATH
export PATH="$HOME/.local/bin:$PATH"
```

### 3. Windows 下安装失败

**解决方案**：使用管理员权限运行 PowerShell

```powershell
# 以管理员身份运行 PowerShell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
python install.py
```

### 4. 依赖冲突

**解决方案**：使用虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## 平台特定说明

### macOS

- 需要 Xcode Command Line Tools（可选）
- 建议使用 Homebrew 安装 Python

### Linux

- 需要 Python 3.9+ 和 pip
- 某些发行版需要安装 `python3-dev` 包

### Windows

- 需要 Python 3.9+（从 python.org 安装）
- 建议使用 PowerShell 或命令提示符
- 可能需要安装 Microsoft C++ Build Tools
