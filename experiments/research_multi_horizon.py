#!/usr/bin/env python3
from __future__ import annotations
import sys, json, math
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any
import yaml
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / 'src'))
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
TOKEN = CONFIG['tushare']['token']

HORIZONS = [
    ('5y', 365 * 5),
    ('3y', 365 * 3),
    ('1y', 365),
    ('6m', 183),
    ('3m', 90),
    ('1m', 30),
]
WEIGHTS = {
    '5y': 0.10,
    '3y': 0.15,
    '1y': 0.35,
    '6m': 0.20,
    '3m': 0.15,
    '1m': 0.05,
}

class GridCombo:
    def __init__(self, ma_fast, ma_slow, rsi_period, oversold, overbought, bb_period, bb_std, vote):
        self.ma = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi = RSI(period=rsi_period, oversold=oversold, overbought=overbought)
        self.bb = BollingerBands(period=bb_period, std_dev=bb_std)
        self.vote = vote
        self.meta = {'kind':'grid_combo','ma':[ma_fast,ma_slow],'rsi':[rsi_period,oversold,overbought],'bb':[bb_period,bb_std],'vote':vote}
    def signal(self, bar, hist):
        if len(hist) < max(30, self.meta['ma'][1], self.meta['bb'][0]): return 'HOLD'
        self.ma.reset(); self.rsi.reset(); self.bb.reset()
        for p in hist[:-1]:
            self.ma.on_bar(p); self.rsi.on_bar(p); self.bb.on_bar(p)
        sigs = [self.ma.on_bar(bar), self.rsi.on_bar(bar), self.bb.on_bar(bar)]
        b = sum(1 for s in sigs if s.side == Side.BUY)
        s = sum(1 for s in sigs if s.side == Side.SELL)
        if b >= self.vote: return 'BUY'
        if s >= self.vote: return 'SELL'
        return 'HOLD'

class MACD:
    def __init__(self, fast, slow, signal_period):
        self.fast=fast; self.slow=slow; self.signal_period=signal_period
        self.meta={'kind':'macd','fast':fast,'slow':slow,'signal':signal_period}
    def ema(self,data,p):
        if len(data)<p: return data[-1]
        m=2/(p+1); e=sum(data[:p])/p
        for x in data[p:]: e=(x-e)*m+e
        return e
    def signal(self, bar, hist):
        closes=[b.close for b in hist]
        if len(closes)<self.slow+self.signal_period+5:return 'HOLD'
        macds=[]
        for i in range(self.slow, len(closes)+1):
            w=closes[:i]
            macds.append(self.ema(w[-self.slow:],self.fast)-self.ema(w[-self.slow:],self.slow))
        if len(macds)<self.signal_period+2:return 'HOLD'
        pm,cm=macds[-2],macds[-1]
        ps,cs=self.ema(macds[:-1],self.signal_period),self.ema(macds,self.signal_period)
        if pm<=ps and cm>cs:return 'BUY'
        if pm>=ps and cm<cs:return 'SELL'
        return 'HOLD'

class BreakoutRSI:
    def __init__(self, window, rsi_period, buy_rsi, sell_rsi):
        self.window=window; self.rsi=RSI(period=rsi_period, oversold=30, overbought=70)
        self.buy_rsi=buy_rsi; self.sell_rsi=sell_rsi
        self.meta={'kind':'breakout_rsi','window':window,'rsi_period':rsi_period,'buy_rsi':buy_rsi,'sell_rsi':sell_rsi}
    def signal(self, bar, hist):
        if len(hist)<self.window+5:return 'HOLD'
        self.rsi.reset()
        for p in hist[:-1]: self.rsi.on_bar(p)
        self.rsi.on_bar(bar)
        rv=getattr(self.rsi,'_last_rsi',None)
        if rv is None:return 'HOLD'
        highs=[b.high for b in hist]; lows=[b.low for b in hist]
        if bar.close>max(highs[-(self.window+1):-1]) and rv>=self.buy_rsi:return 'BUY'
        if bar.close<min(lows[-(self.window+1):-1]) and rv<=self.sell_rsi:return 'SELL'
        return 'HOLD'

class MeanRev:
    def __init__(self, lookback, entry_std, exit_std):
        self.lookback=lookback; self.entry_std=entry_std; self.exit_std=exit_std
        self.meta={'kind':'mean_reversion','lookback':lookback,'entry_std':entry_std,'exit_std':exit_std}
    def signal(self, bar, hist):
        closes=[b.close for b in hist]
        if len(closes)<self.lookback:return 'HOLD'
        w=closes[-self.lookback:]
        mean=float(np.mean(w)); std=float(np.std(w))
        if std==0:return 'HOLD'
        z=(bar.close-mean)/std
        if z<=-self.entry_std:return 'BUY'
        if z>=self.exit_std:return 'SELL'
        return 'HOLD'

def backtest(strategy, bars):
    capital=100000.0; position=0; entry=0.0; trades=[]; curve=[capital]
    for i,bar in enumerate(bars):
        sig=strategy.signal(bar,bars[:i+1])
        curve.append(capital+position*bar.close if position else capital)
        if sig=='BUY' and position==0:
            shares=int(capital*0.95/bar.close/100)*100
            if shares>0:
                capital-=shares*bar.close*1.0003; position=shares; entry=bar.close
        elif sig=='SELL' and position>0:
            capital+=position*bar.close*0.9997; trades.append((bar.close-entry)*position); position=0
    final=capital+position*bars[-1].close if position else capital
    ret=(final-100000)/100000*100
    peak=100000.0; mdd=0.0
    for v in curve:
        peak=max(peak,v)
        mdd=max(mdd,(peak-v)/peak*100 if peak else 0)
    wr=sum(1 for t in trades if t>0)/len(trades)*100 if trades else 0.0
    rs=[]
    for i in range(1,len(curve)):
        if curve[i-1]: rs.append((curve[i]-curve[i-1])/curve[i-1])
    sharpe=0.0
    if rs:
        avg=float(np.mean(rs)); std=float(np.std(rs)); sharpe=(avg/std*math.sqrt(252)) if std>0 else 0.0
    return {'total_return':ret,'max_drawdown':mdd,'total_trades':len(trades),'win_rate':wr,'sharpe':sharpe}

def build_candidates(profile):
    pm=profile.get('ma',{}); pr=profile.get('rsi',{}); pb=profile.get('bb',{})
    c=[]
    # 更聚焦：每类少量强候选
    c += [
        GridCombo(pm.get('fast',10), pm.get('slow',30), pr.get('period',14), pr.get('oversold',30), pr.get('overbought',70), pb.get('period',20), pb.get('std_dev',2.0), 2),
        GridCombo(max(3, pm.get('fast',10)-2), max(8, pm.get('slow',30)-5), max(4, pr.get('period',14)-2), 25, 65, 20, 1.8, 2),
        GridCombo(pm.get('fast',10), pm.get('slow',30), 6, 20, 60, 26, 2.0, 1),
        GridCombo(5, 20, 6, 20, 60, 20, 2.0, 1),
        GridCombo(10, 30, 14, 30, 70, 26, 2.0, 1),
    ]
    for x in [(8,21,6),(10,24,8),(12,26,9)]: c.append(MACD(*x))
    for x in [(10,6,55,45),(20,6,55,45),(20,8,60,40)]: c.append(BreakoutRSI(*x))
    for x in [(20,2.0,0.5),(30,2.0,1.0),(30,2.5,1.0)]: c.append(MeanRev(*x))
    return c

def horizon_score(res, horizon):
    trades=res['total_trades']
    trade_bonus = 0
    if horizon in ('1y','6m','3m'):
        if 3 <= trades <= 18: trade_bonus = 6
        elif trades == 0: trade_bonus = -8
    elif horizon in ('5y','3y'):
        if 6 <= trades <= 60: trade_bonus = 6
        elif trades == 0: trade_bonus = -8
    return res['total_return'] - 0.5*res['max_drawdown'] + 4*res['sharpe'] + trade_bonus

def main():
    loader=TushareDataLoader(TOKEN)
    symbols=CONFIG.get('symbols',[])
    profiles=CONFIG.get('strategies',{}).get('profiles',{})
    now=datetime.now()
    report=[]
    print('='*110)
    print('TradePilot 多周期策略调研（1年权重更高）')
    print('='*110)
    for sym in symbols:
        code=sym['code']; name=sym.get('name',code); profile=profiles.get(sym['strategy_profile'],{})
        horizon_bars={}
        for key,days in HORIZONS:
            start=(now - timedelta(days=days)).strftime('%Y%m%d')
            end=now.strftime('%Y%m%d')
            horizon_bars[key]=list(loader.load_bars(code,start,end))
        print(f'\n{name} ({code})', flush=True)
        results=[]
        for strat in build_candidates(profile):
            per={}
            total=0.0
            ok=True
            for hk,_ in HORIZONS:
                bars=horizon_bars[hk]
                if len(bars) < 15:
                    ok=False; break
                r=backtest(strat,bars)
                per[hk]=r
                total += WEIGHTS[hk] * horizon_score(r, hk)
            if not ok: continue
            # 额外偏好：1y 不能太差
            total += 0.6 * per['1y']['total_return']
            total -= 0.2 * per['1y']['max_drawdown']
            results.append({'meta':strat.meta,'periods':per,'score':total})
        results.sort(key=lambda x:x['score'], reverse=True)
        best=results[0]
        print(f"  最优: {best['meta']}", flush=True)
        print(f"    5y {best['periods']['5y']['total_return']:.2f}% | 3y {best['periods']['3y']['total_return']:.2f}% | 1y {best['periods']['1y']['total_return']:.2f}% | 6m {best['periods']['6m']['total_return']:.2f}% | 3m {best['periods']['3m']['total_return']:.2f}% | 1m {best['periods']['1m']['total_return']:.2f}%", flush=True)
        report.append({'code':code,'name':name,'best':best,'top3':results[:3]})
    out=ROOT/'data'/'backtest'/'multi_horizon_research.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('\n结果已保存:', out)

if __name__=='__main__':
    main()
