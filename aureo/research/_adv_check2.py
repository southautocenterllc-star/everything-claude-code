"""Probe: realistic swap, gross edge decomposition, anti-beta test."""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research.harness import load_h1
from research._adv_check import mr_fast_rsi2
from backtest.engine import run_backtest, BacktestConfig
from backtest.metrics import compute_stats


def pf(trades):
    return compute_stats(trades).profit_factor if trades else 0.0

def tot(trades):
    return compute_stats(trades).total_pnl if trades else 0.0


df = load_h1()
sig = mr_fast_rsi2(df)

# 1) sweep swap levels (flat) to find breakeven
print("=== SWAP SWEEP (flat, spread=0.35) ===")
for sw in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
    cfg = BacktestConfig(spread_pips=0.35, swap_per_night=sw)
    tr = run_backtest(sig, df, cfg)
    print(f"swap={sw:.2f}  PF_net={pf(tr):.4f}  total={tot(tr):+.1f}")

# 2) spread-only (no swap) to isolate
print("\n=== SPREAD ONLY (no swap) ===")
for sp in [0.0, 0.1, 0.2, 0.35]:
    cfg = BacktestConfig(spread_pips=sp, swap_per_night=0.0)
    tr = run_backtest(sig, df, cfg)
    print(f"spread={sp:.2f}  PF_net={pf(tr):.4f}  total={tot(tr):+.1f}")

# 3) trade durations: how many nights does this MR strategy actually hold?
cfg = BacktestConfig(spread_pips=0.35, swap_per_night=0.5)
tr = run_backtest(sig, df, cfg)
nights = []
for t in tr:
    if t.entry_time is not None and t.exit_time is not None:
        nights.append(max((t.exit_time.normalize()-t.entry_time.normalize()).days,0))
nights = np.array(nights)
print(f"\n=== HOLDING NIGHTS (n={len(nights)}) ===")
print(f"mean nights={nights.mean():.2f}  median={np.median(nights):.0f}  "
      f"pct 0 nights={np.mean(nights==0)*100:.1f}%  max={nights.max()}")

# 4) realistic rate-based swap: gold swap ~ funding rate. Long pays, short can earn.
# Approx: swap_per_night = price * (rate/360). Use Fed funds proxy by year-ish.
# Simpler: the reviewer claims rate-based swap RAISES PF to 0.989. Test a
# direction-aware swap where short positions EARN carry (realistic for sells).
print("\n=== DIRECTION-AWARE SWAP (long pays 0.5, short earns 0.2) ===")
tr = run_backtest(sig, df, BacktestConfig(spread_pips=0.35, swap_per_night=0.0))
# manual swap
g_prof=0.0; g_loss=0.0; total=0.0
for t in tr:
    n = max((t.exit_time.normalize()-t.entry_time.normalize()).days,0) if t.exit_time is not None and t.entry_time is not None else 0
    sw = 0.5 if t.direction==1 else -0.2  # short earns
    p = t.pnl - sw*n
    total += p
    if p>0: g_prof+=p
    else: g_loss+= -p
print(f"PF_net={g_prof/g_loss if g_loss>0 else float('inf'):.4f}  total={total:+.1f}")

# count long vs short
nl = sum(1 for t in tr if t.direction==1); ns=sum(1 for t in tr if t.direction==-1)
print(f"long trades={nl}  short trades={ns}")
