#!/usr/bin/env python3
# 一次性研究脚本，归档于 experiments/，不在产品路径维护
"""诊断：评估窗口内各子策略信号分布 + 买入持有基准 + 阈值敏感性"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent / "src"))
import yaml

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.risk.manager import RiskConfig
from ripple_tradePilot.backtest.engine import run_backtest

ROOT = Path(__file__).parent
EVAL_DAYS = 91


def df_to_bars(df):
    return [Bar(timestamp=datetime.strptime(str(r['trade_date']), '%Y%m%d'),
                open=float(r['open']), high=float(r['high']), low=float(r['low']),
                close=float(r['close']), volume=float(r.get('vol', 0) or 0))
            for _, r in df.iterrows()]


class Combo:
    """可配置投票阈值的 combo_vote 复刻（复用同一子策略实现）"""
    def __init__(self, profile, threshold):
        self.ma = MovingAverageCross(fast=profile.get('ma_fast', 5), slow=profile.get('ma_slow', 20))
        self.rsi = RSI(period=profile.get('rsi_period', 14),
                       oversold=profile.get('rsi_oversold', 30), overbought=profile.get('rsi_overbought', 70))
        self.bb = BollingerBands(period=profile.get('bb_period', 20), std_dev=profile.get('bb_std', 2.0))
        self.threshold = threshold

    def on_bar(self, bar):
        sigs = {'ma': self.ma.on_bar(bar), 'rsi': self.rsi.on_bar(bar), 'bb': self.bb.on_bar(bar)}
        buys = [n for n, s in sigs.items() if s.side == Side.BUY]
        sells = [n for n, s in sigs.items() if s.side == Side.SELL]
        return buys, sells


def fetch_with_retry(loader, code, start, end, retries=4):
    import time
    for i in range(retries):
        df = loader.get_daily_bars(code, start, end)
        if df is not None and len(df) > 0:
            return df
        print(f"  数据拉取失败，重试 {i+1}/{retries}...")
        time.sleep(3)
    return None


def main():
    config = yaml.safe_load(open(ROOT / 'config.yaml', encoding='utf-8'))
    loader = TushareDataLoader(config['tushare']['token'],
                               rate_limit_delay=config['tushare'].get('rate_limit_delay', 1.5))
    profiles = config.get('strategy_profiles', {})
    today = datetime.now()
    eval_start = (today - timedelta(days=EVAL_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    fetch_start = today - timedelta(days=EVAL_DAYS + 370)

    for sym in config.get('symbols', []):
        code, name, pname = sym['code'], sym['name'], sym['strategy_profile']
        profile = profiles[pname]
        print(f"\n{'='*20} {code} {name} ({pname}) {'='*20}")
        df = fetch_with_retry(loader, code, fetch_start.strftime('%Y%m%d'), today.strftime('%Y%m%d'))
        if df is None or len(df) == 0:
            print(f"⚠️ {code} 数据获取失败，跳过")
            continue
        bars = df_to_bars(df)
        warm = [b for b in bars if b.timestamp < eval_start]
        evalb = [b for b in bars if b.timestamp >= eval_start]

        # 基准：买入持有
        bh = evalb[-1].close / evalb[0].open - 1
        print(f"评估窗口 {evalb[0].timestamp.date()}~{evalb[-1].timestamp.date()} ({len(evalb)} 日)")
        print(f"买入持有基准: {bh*100:+.2f}%  (开盘 {evalb[0].open:.2f} → 收盘 {evalb[-1].close:.2f})")

        # 诊断信号
        combo = Combo(profile, 2)
        for b in warm:
            combo.on_bar(b)
        buy_days, sell_days, conflict_days = [], [], []
        for b in evalb:
            buys, sells = combo.on_bar(b)
            if buys and sells:
                conflict_days.append((b.timestamp.date(), buys, sells))
            elif buys:
                buy_days.append((b.timestamp.date(), buys))
            elif sells:
                sell_days.append((b.timestamp.date(), sells))
        print(f"满足阈值2的买入日: {len(buy_days)}  卖出日: {len(sell_days)}  冲突日: {len(conflict_days)}")
        for d, src in buy_days[:10]:
            print(f"  🔴 {d} <- {src}")  # A 股口径：红=买
        for d, src in sell_days[:10]:
            print(f"  🟢 {d} <- {src}")
        for d, bs, ss in conflict_days[:10]:
            print(f"  🟡 {d} 冲突 buy={bs} sell={ss}")

        # 阈值敏感性：threshold=1 用项目引擎跑
        from ripple_tradePilot.strategies.combo_vote import ComboVoteStrategy
        for th in (2, 1):
            ma = MovingAverageCross(fast=profile.get('ma_fast', 5), slow=profile.get('ma_slow', 20))
            rsi = RSI(period=profile.get('rsi_period', 14), oversold=profile.get('rsi_oversold', 30),
                      overbought=profile.get('rsi_overbought', 70))
            bb = BollingerBands(period=profile.get('bb_period', 20), std_dev=profile.get('bb_std', 2.0))
            strat = ComboVoteStrategy([('ma', ma), ('rsi', rsi), ('bb', bb)], vote_threshold=th)
            for b in warm:
                strat.on_bar(b)
            res = run_backtest(strat, evalb, initial_cash=100000.0, fee_rate=0.0005,
                               risk_config=RiskConfig())
            final = res.equity_curve[-1]
            n_trades = len([f for f in res.fills if f.side == Side.SELL])
            holding = res.fills and res.fills[-1].side == Side.BUY
            print(f"threshold={th}: 期末 {final:,.0f} ({(final/100000-1)*100:+.2f}%), "
                  f"成交 {len(res.fills)} 笔(平仓 {n_trades}){'，期末持仓中' if holding else ''}")


if __name__ == '__main__':
    main()
