"""Adversarial check: mr_fast_rsi2 (Connors RSI2 ultrashort MR)."""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.aureo_pine import atr, ema, rsi
from research.harness import load_h1, run_gauntlet, _cfg, REGIMES
from backtest.engine import run_backtest, BacktestConfig
from backtest.metrics import compute_stats


def _base(df):
    out = pd.DataFrame(index=df.index)
    out["close"] = df["close"]
    return out


def _attach_exits(out, df, atr_s, sl_mult, tp1_mult, tp2_mult):
    d = out["signal"].to_numpy()
    c = df["close"].to_numpy()
    a = atr_s.to_numpy()
    out["sl"] = np.where(d == 1, c - sl_mult * a, np.where(d == -1, c + sl_mult * a, np.nan))
    out["tp1"] = np.where(d == 1, c + tp1_mult * a, np.where(d == -1, c - tp1_mult * a, np.nan))
    out["tp2"] = np.where(d == 1, c + tp2_mult * a, np.where(d == -1, c - tp2_mult * a, np.nan))
    bad = ~np.isfinite(out["sl"].to_numpy())
    sig = out["signal"].to_numpy().copy()
    sig[bad & (sig != 0)] = 0
    out["signal"] = sig
    return out


def mr_fast_rsi2(df):
    """RSI(2)<10 long over EMA200 / >90 short under EMA200, exits 0.5/1.0 ATR."""
    out = _base(df)
    r2 = rsi(df["close"], 2)
    e200 = ema(df["close"], 200)
    up = df["close"] > e200
    long_sig = (r2 < 10) & up
    short_sig = (r2 > 90) & (~up)
    out["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    a = atr(df, 14)
    # exits 0.5 / 1.0 ATR short. tp2 needed too; use 1.0 and 1.0? Spec says 0.5/1.0
    # interpret: sl=0.5? Let's try sl=1.0, tp1=0.5, tp2=1.0 (short MR exit)
    return _attach_exits(out, df, a, 1.0, 0.5, 1.0)


if __name__ == "__main__":
    df = load_h1()
    print(f"Data: {len(df)} velas  {df.index.min().date()} -> {df.index.max().date()}\n")

    r = run_gauntlet("mr_fast_rsi2", mr_fast_rsi2, df, perm_iters=2000)
    print("=== GAUNTLET mr_fast_rsi2 ===")
    print(f"n_trades   : {r.n_trades}")
    print(f"PF_net     : {r.pf_net}")
    print(f"PF_gross   : {r.pf_gross}")
    print(f"perm_p     : {r.perm_p}")
    print(f"boot_lo    : {r.boot_ci_low}  boot_hi: {r.boot_ci_high}")
    print(f"reg ok     : {r.n_regimes_profitable}/{r.n_regimes_total}  top%={r.pct_pnl_top_regime}")
    print(f"total_pnl  : {r.total_pnl_net}")
    print(f"VERDICT    : {r.verdict}")
    for n in r.notes:
        print("   -", n)
    print("\n=== REGIMES ===")
    print(r.regimes.to_string(index=False))
