#!/usr/bin/env python3
from __future__ import annotations
import sys, json, math
from pathlib import Path
from datetime import datetime
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

class GridCombo:
    def __init__(self, ma_fast, ma_slow, rsi_period, oversold, overbought, bb_period, bb_std, vote):
        self.ma = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        self.rsi = RSI(period=rsi_period, oversold=oversold, overbought=overbought)
        self.bb = BollingerBands(period=bb_period, std_dev=bb_std)
        self.meta = {'kind':'grid_combo','ma':[ma_fast,ma_slow],'rsi':[rsi_period,oversold,overbought],'bb':[bb_period,bb_std],'vote':vote}
        self.vote = vote
    def signal(self, bar, hist):
        if len(hist) < max(30, self.meta['ma'][1], self.meta['bb'][0]): return 'HOLD'
        self.ma.reset(); self.rsi.reset(); self.bb.reset()
        for p in hist[:-1]: self.ma.on_bar(p); self.rsi.on_bar(p); self.bb.on_bar(p)
        sigs = [self.ma.on_bar(bar), self.rsi.on_bar(bar), self.bb.on_bar(bar)]
        b = sum(1 for s in sigs if s.side == Side.BUY)
        s = sum(1 for s in sigs if s.side == Side.SELL)
        if b >= self.vote: return 'BUY'
        if s >= self.vote: return 'SELL'
        return 'HOLD'

class MACD:
    def __init__(self, fast, slow, signal_period):
        self.meta = {'kind':'macd','fast':fast,'slow':slow,'signal':signal_period}
        self.fast=fast; self.slow=slow; self.signal_period=signal_period
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
        pm, cm = macds[-2], macds[-1]
        ps, cs = self.ema(macds[:-1], self.signal_period), self.ema(macds, self.signal_period)
        if pm<=ps and cm>cs:return 'BUY'
        if pm>=ps and cm<cs:return 'SELL'
        return 'HOLD'

class BreakoutRSI:
    def __init__(self, window, rsi_period, buy_rsi, sell_rsi):
        self.window=window; self.rsi=RSI(period=rsi_period, oversold=30, overbought=70)
        self.meta={'kind':'breakout_rsi','window':window,'rsi_period':rsi_period,'buy_rsi':buy_rsi,'sell_rsi':sell_rsi}
        self.buy_rsi=buy_rsi; self.sell_rsi=sell_rsi
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
        self.meta={'kind':'mean_reversion','lookback':lookback,'entry_std':entry_std,'exit_std':exit_std}
        self.lookback=lookback; self.entry_std=entry_std; self.exit_std=exit_std
    def signal(self, bar, hist):
        closes=[b.close for b in hist]
        if len(closes)<self.lookback:return 'HOLD'
        w=closes[-self.lookback:]; mean=float(np.mean(w)); std=float(np.std(w))
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
        peak=max(peak,v); mdd=max(mdd,(peak-v)/peak*100 if peak else 0)
    wr=sum(1 for t in trades if t>0)/len(trades)*100 if trades else 0.0
    rs=[]
    for i in range(1,len(curve)):
        if curve[i-1]: rs.append((curve[i]-curve[i-1])/curve[i-1])
    sharpe=0.0
    if rs:
        avg=float(np.mean(rs)); std=float(np.std(rs)); sharpe=(avg/std*math.sqrt(252)) if std>0 else 0.0
    return {'total_return':ret,'max_drawdown':mdd,'total_trades':len(trades),'win_rate':wr,'sharpe':sharpe}

def score(res):
    t=res['total_trades']
    trade_bonus = 8 if 4<=t<=20 else 4 if 2<=t<=30 else -10 if t==0 else -2
    return res['total_return'] - 0.6*res['max_drawdown'] + 5*res['sharpe'] + trade_bonus

def build_candidates(profile):
    pm=profile.get('ma',{}); pr=profile.get('rsi',{}); pb=profile.get('bb',{})
    c=[]
    # current + two more sensitive variants
    c.append(GridCombo(pm.get('fast',10),pm.get('slow',30),pr.get('period',14),pr.get('oversold',30),pr.get('overbought',70),pb.get('period',20),pb.get('std_dev',2.0),2))
    c.append(GridCombo(max(3,pm.get('fast',10)-2),max(8,pm.get('slow',30)-10),max(4,pr.get('period',14)-4),20,60,20,1.8,2))
    c.append(GridCombo(pm.get('fast',10),pm.get('slow',30),max(4,pr.get('period',14)-2),25,65,pb.get('period',20),pb.get('std_dev',2.0),2))
    c.append(GridCombo(pm.get('fast',10),pm.get('slow',30),max(4,pr.get('period',14)-4),20,60,pb.get('period',20),pb.get('std_dev',2.0),1))
    # macd family
    for x in [(8,21,6),(10,24,8),(12,26,9)]: c.append(MACD(*x))
    # breakout
    for x in [(10,6,55,45),(20,6,55,45),(20,8,60,40)]: c.append(BreakoutRSI(*x))
    # mean reversion
    for x in [(20,2.0,0.5),(20,2.5,0.5),(30,2.0,1.0)]: c.append(MeanRev(*x))
    return c

def main():
    loader=TushareDataLoader(TOKEN)
    symbols=CONFIG.get('symbols',[])
    profiles=CONFIG.get('strategies',{}).get('profiles',{})
    report=[]
    start='20210101'; end=datetime.now().strftime('%Y%m%d')
    print('='*100)
    print('TradePilot 监控池 5 年策略调研（精简版）')
    print('='*100, flush=True)
    for sym in symbols:
        code=sym['code']; name=sym.get('name',code); profile_name=sym['strategy_profile']; profile=profiles.get(profile_name,{})
        bars=list(loader.load_bars(code,start,end))
        print(f'\n{name} ({code}) | bars={len(bars)} | profile={profile_name}', flush=True)
        results=[]
        for strat in build_candidates(profile):
            res=backtest(strat,bars)
            results.append({'meta':strat.meta,'result':res,'score':score(res)})
        results.sort(key=lambda x:x['score'], reverse=True)
        best=results[0]; current=results[0]
        for r in results:
            if r['meta'].get('kind')=='grid_combo' and r['meta'].get('vote')==2:
                pm=profile.get('ma',{}); pr=profile.get('rsi',{}); pb=profile.get('bb',{})
                if r['meta'].get('ma')==[pm.get('fast'),pm.get('slow')] and r['meta'].get('rsi')==[pr.get('period'),pr.get('oversold'),pr.get('overbought')] and r['meta'].get('bb')==[pb.get('period'),pb.get('std_dev')]:
                    current=r; break
        print(f"  当前: 收益{current['result']['total_return']:.2f}% 回撤{current['result']['max_drawdown']:.2f}% 交易{current['result']['total_trades']} 夏普{current['result']['sharpe']:.2f}", flush=True)
        print(f"  推荐: {best['meta']} | 收益{best['result']['total_return']:.2f}% 回撤{best['result']['max_drawdown']:.2f}% 交易{best['result']['total_trades']} 夏普{best['result']['sharpe']:.2f}", flush=True)
        report.append({'code':code,'name':name,'current':current,'best':best,'top3':results[:3]})
    out=ROOT/'data'/'backtest'/'monitor_pool_5y_research_fast.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('\n结果已保存:', out, flush=True)

if __name__=='__main__':
    main()
