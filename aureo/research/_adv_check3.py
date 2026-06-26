"""Exit sweep: is there ANY ATR exit giving real gross edge surviving costs?"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research.harness import load_h1
from strategies.aureo_pine import atr, ema, rsi
from backtest.engine import run_backtest, BacktestConfig
from backtest.metrics import compute_stats

def make_gen(sl,tp1,tp2):
    def gen(df):
        out = pd.DataFrame(index=df.index); out["close"]=df["close"]
        r2=rsi(df["close"],2); e200=ema(df["close"],200); up=df["close"]>e200
        out["signal"]=np.where((r2<10)&up,1,np.where((r2>90)&(~up),-1,0))
        a=atr(df,14).to_numpy(); c=df["close"].to_numpy(); d=out["signal"].to_numpy()
        out["sl"]=np.where(d==1,c-sl*a,np.where(d==-1,c+sl*a,np.nan))
        out["tp1"]=np.where(d==1,c+tp1*a,np.where(d==-1,c-tp1*a,np.nan))
        out["tp2"]=np.where(d==1,c+tp2*a,np.where(d==-1,c-tp2*a,np.nan))
        bad=~np.isfinite(out["sl"].to_numpy()); s=d.copy(); s[bad&(s!=0)]=0; out["signal"]=s
        return out
    return gen

df=load_h1()
print(f"{'sl/tp1/tp2':16s} {'n':>5s} {'PFgross':>8s} {'PFnet':>7s} {'net_tot':>8s}")
for sl,tp1,tp2 in [(0.5,0.5,1.0),(1.0,0.5,1.0),(1.0,1.0,2.0),(2.0,1.0,2.0),
                   (0.5,1.0,1.5),(1.5,0.5,1.0),(3.0,1.0,2.0),(1.0,0.25,0.5)]:
    gen=make_gen(sl,tp1,tp2); sig=gen(df)
    g=run_backtest(sig,df,BacktestConfig(0.0,0.0))
    nt=run_backtest(sig,df,BacktestConfig(spread_pips=0.35,swap_per_night=0.5))
    sg=compute_stats(g); sn=compute_stats(nt)
    print(f"{sl}/{tp1}/{tp2:<8} {sn.n_trades:5d} {sg.profit_factor:8.4f} {sn.profit_factor:7.4f} {sn.total_pnl:+8.1f}")
