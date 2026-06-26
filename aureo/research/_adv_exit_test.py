"""Adversarial test: does a SHORT-horizon exit rescue a short-horizon edge that
the ATR-exit gauntlet declares dead? If yes -> the verdict is a false negative.

We add a time-based exit engine (exit after N bars at close, optional ATR SL),
build a FAIR random-entry null for it, and re-run the 3 filters (cost / null /
regime) on:
  - seasonality day-of-month 21-22 long (the reviewer's example)
  - rsi_meanrev (counter-trend)
both with (a) ATR exits [status quo] and (b) N-bar time exit.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import compute_stats
from research.harness import load_h1, REGIMES, BASE_SPREAD, BASE_SWAP
from strategies.aureo_pine import atr, rsi

SPREAD = BASE_SPREAD * 0.01  # convert pips->price like engine does
SWAP = BASE_SWAP


# ---------------- time-based exit backtest (own engine) ----------------
def run_time_exit(df, sig, hold_bars, spread=SPREAD, swap=SWAP, sl_atr=None,
                  atr_s=None):
    """Enter at close of signal bar, exit at close hold_bars later. One position
    at a time (pyramiding=0). Optional ATR stop loss checked intrabar.
    Returns list of pnl per trade with swap charged per night."""
    idx = df.index
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    a = atr_s.to_numpy() if atr_s is not None else None
    n = len(df)
    pnls = []
    times = []  # (entry_time, exit_time)
    i = 0
    while i < n:
        if sig[i] != 0 and np.isfinite(close[i]):
            d = int(sig[i])
            entry = close[i] + (spread / 2) * d
            sl = None
            if sl_atr is not None and a is not None and np.isfinite(a[i]):
                sl = entry - sl_atr * a[i] * d
            exit_j = min(i + hold_bars, n - 1)
            outp = None
            jhit = exit_j
            if sl is not None:
                for j in range(i + 1, exit_j + 1):
                    if d == 1 and low[j] <= sl:
                        outp = sl; jhit = j; break
                    if d == -1 and high[j] >= sl:
                        outp = sl; jhit = j; break
            if outp is None:
                outp = close[exit_j]; jhit = exit_j
            pnl = (outp - entry) * d - spread
            nights = max((idx[jhit].normalize() - idx[i].normalize()).days, 0)
            pnl -= swap * nights
            pnls.append(pnl)
            times.append((idx[i], idx[jhit]))
            i = jhit + 1  # one position at a time; no overlap
        else:
            i += 1
    return np.array(pnls), times


def pf(pnls):
    if len(pnls) == 0:
        return 0.0
    gp = pnls[pnls > 0].sum()
    gl = -pnls[pnls <= 0].sum()
    return gp / gl if gl > 0 else float("inf")


def random_entry_null_time(df, sig, hold_bars, n_iter=2000, seed=42,
                           sl_atr=None, atr_s=None):
    """Fair null: same #entries, same long/short split, same time-exit + costs,
    but entries at random eligible bars."""
    rng = np.random.default_rng(seed)
    n_long = int((sig == 1).sum())
    n_short = int((sig == -1).sum())
    ntot = n_long + n_short
    obs, _ = run_time_exit(df, sig, hold_bars, sl_atr=sl_atr, atr_s=atr_s)
    obs_mean = obs.mean() if len(obs) else 0.0
    n = len(df)
    eligible = np.arange(n - hold_bars - 1)
    means = np.empty(n_iter)
    for k in range(n_iter):
        s = np.zeros(n, dtype=int)
        picks = rng.choice(eligible, size=min(ntot, len(eligible)), replace=False)
        dirs = np.array([1] * n_long + [-1] * n_short)[:len(picks)]
        s[picks] = dirs
        p, _ = run_time_exit(df, s, hold_bars, sl_atr=sl_atr, atr_s=atr_s)
        means[k] = p.mean() if len(p) else 0.0
    pval = (1.0 + int((means >= obs_mean).sum())) / (1.0 + n_iter)
    return obs_mean, float(np.mean(means)), pval, len(obs)


def regime_pf_time(df, sigfn, hold_bars, sl_atr=None):
    rows = []
    for name, a, b in REGIMES:
        sub = df[(df.index >= pd.Timestamp(a, tz="UTC")) &
                 (df.index <= pd.Timestamp(b, tz="UTC"))]
        if len(sub) < 300:
            continue
        s = sigfn(sub)
        atr_s = atr(sub, 14) if sl_atr else None
        pnls, _ = run_time_exit(sub, s, hold_bars, sl_atr=sl_atr, atr_s=atr_s)
        rows.append((name, len(pnls), round(pf(pnls), 3), round(pnls.sum(), 1)))
    return rows


# ---------------- signal generators ----------------
def seasonality_sig(df, days=(21, 22), direction=1):
    dom = df.index.day
    return np.where(np.isin(dom, days), direction, 0).astype(int)


def rsi_sig(df):
    r = rsi(df["close"], 14).to_numpy()
    return np.where(r < 30, 1, np.where(r > 70, -1, 0)).astype(int)


def regime_pf_block(rows):
    prof = sum(1 for _, _, p, _ in rows if p > 1.0)
    pos = [pn for _, _, _, pn in rows if pn > 0]
    top = max(pos) / sum(pos) if pos and sum(pos) > 0 else float("nan")
    return prof, len(rows), top


if __name__ == "__main__":
    df = load_h1()
    print(f"Data {len(df)} bars {df.index.min().date()}->{df.index.max().date()}")
    atr14 = atr(df, 14)

    # ===== PART A: reproduce reviewer's gross seasonality claim =====
    print("\n=== A: seasonality day 21-22 LONG, gross PF by horizon (no costs) ===")
    for h in [1, 2, 4, 8, 24]:
        s = seasonality_sig(df)
        pnls, _ = run_time_exit(df, s, h, spread=0.0, swap=0.0)
        print(f"  hold={h:3d}b  n={len(pnls):4d}  PF_gross={pf(pnls):.3f}  "
              f"mean={pnls.mean():+.3f}  sum={pnls.sum():+.1f}")

    print("\n=== A2: same seasonality NET of base costs by horizon ===")
    for h in [1, 2, 4, 8, 24]:
        s = seasonality_sig(df)
        pnls, _ = run_time_exit(df, s, h)
        print(f"  hold={h:3d}b  n={len(pnls):4d}  PF_net={pf(pnls):.3f}  "
              f"mean={pnls.mean():+.3f}  sum={pnls.sum():+.1f}")
