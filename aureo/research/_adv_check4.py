from __future__ import annotations
import sys
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research.harness import load_h1, run_gauntlet
from strategies.aureo_pine import atr, ema, rsi

def make_gen(sl,tp1,tp2):
    def gen(df):
        out=pd.DataFrame(index=df.index); out["close"]=df["close"]
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
for tag,(sl,tp1,tp2) in [("best_0.5/1.0/1.5",(0.5,1.0,1.5)),("alt_1.5/0.5/1.0",(1.5,0.5,1.0))]:
    r=run_gauntlet(tag, make_gen(sl,tp1,tp2), df, perm_iters=2000)
    print(f"\n### {tag}")
    print(f"n={r.n_trades} PFnet={r.pf_net} PFgross={r.pf_gross} perm_p={r.perm_p} "
          f"boot_lo={r.boot_ci_low} reg={r.n_regimes_profitable}/{r.n_regimes_total} top={r.pct_pnl_top_regime}")
    print("VERDICT:", r.verdict)
    for n in r.notes: print("  -",n)
    print(r.regimes.to_string(index=False))
