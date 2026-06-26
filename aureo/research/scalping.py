"""Scalping multi-timeframe XAU/USD — sesgo H4, entrada 15M (idea de Andrés).

H4 manda la DIRECCIÓN (tendencia), 15M dispara la ENTRADA. Salidas cortas
basadas en ATR(15M) → los trades resuelven rápido (intradía), así se ESQUIVA el
swap (el costo que mató al swing). El enemigo acá es el SPREAD: muchas
operaciones, cada una paga 0.35 USD/oz. Por eso se testea con spread realista,
sin piedad, por el mismo gauntlet (costos + significancia + régimen).

Cada `gen(m15) -> signals` produce el contrato del engine (signal/sl/tp1/tp2)
sobre velas de 15M. El H4 se resamplea del M15 y se reproyecta (ffill) sobre los
15M — SIN look-ahead: el sesgo H4 de la barra t usa solo H4 ya cerrado.

Uso:
    .venv/bin/python -m research.scalping            # corre el gauntlet sobre M15
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestConfig
from strategies.aureo_pine import atr, ema, rsi
from research.harness import run_gauntlet, summarize

M15_PATH = ROOT / "data" / "dukascopy" / "XAUUSD-M15-all.parquet"


def load_m15(start=None, end=None) -> pd.DataFrame:
    df = pd.read_parquet(M15_PATH)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]
    return df


def h4_bias(m15: pd.DataFrame, fast=50, slow=200) -> pd.Series:
    """Sesgo de tendencia en H4 (+1 alcista / -1 bajista / 0 mixto), reproyectado
    a la grilla de 15M con SHIFT para usar solo H4 YA cerrado (anti look-ahead)."""
    h4 = m15.resample("4h").agg({"open": "first", "high": "max",
                                 "low": "min", "close": "last"}).dropna()
    ef, es = ema(h4["close"], fast), ema(h4["close"], slow)
    up = (h4["close"] > ef) & (ef > es)
    dn = (h4["close"] < ef) & (ef < es)
    bias = pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=h4.index)
    bias = bias.shift(1)  # solo H4 cerrado en/antes de la barra previa
    return bias.reindex(m15.index, method="ffill").fillna(0)


def _exits(out, m15, sl_mult, tp1_mult, tp2_mult, atr_period=14):
    a = atr(m15, atr_period).to_numpy()
    c = m15["close"].to_numpy()
    d = out["signal"].to_numpy()
    out["sl"] = np.where(d == 1, c - sl_mult * a, np.where(d == -1, c + sl_mult * a, np.nan))
    out["tp1"] = np.where(d == 1, c + tp1_mult * a, np.where(d == -1, c - tp1_mult * a, np.nan))
    out["tp2"] = np.where(d == 1, c + tp2_mult * a, np.where(d == -1, c - tp2_mult * a, np.nan))
    bad = ~np.isfinite(out["sl"].to_numpy())
    s = out["signal"].to_numpy().copy(); s[bad & (s != 0)] = 0
    out["signal"] = s
    return out


def _frame(m15):
    out = pd.DataFrame(index=m15.index)
    out["close"] = m15["close"]
    return out


# --------------------------------------------------------------------------- #
# variantes (pocos parámetros, salidas cortas de scalping)
# --------------------------------------------------------------------------- #
def scalp_rsi_pullback(m15: pd.DataFrame) -> pd.DataFrame:
    """En tendencia H4, comprar la corrección 15M: RSI cruza al alza 45 desde
    abajo (long) / a la baja 55 desde arriba (short), en dirección del sesgo."""
    bias = h4_bias(m15).to_numpy()
    r = rsi(m15["close"], 14)
    cross_up = (r.shift(1) < 45) & (r >= 45)
    cross_dn = (r.shift(1) > 55) & (r <= 55)
    long_sig = (bias == 1) & cross_up.to_numpy()
    short_sig = (bias == -1) & cross_dn.to_numpy()
    out = _frame(m15)
    out["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    return _exits(out, m15, 1.2, 1.5, 2.5)


def scalp_micro_breakout(m15: pd.DataFrame) -> pd.DataFrame:
    """En tendencia H4, entrar al romper el extremo de las últimas 12 velas 15M
    (3h) en la dirección del sesgo (momentum intradía)."""
    bias = h4_bias(m15).to_numpy()
    hi = m15["high"].rolling(12).max().shift(1)
    lo = m15["low"].rolling(12).min().shift(1)
    brk_up = (m15["close"] > hi).to_numpy()
    brk_dn = (m15["close"] < lo).to_numpy()
    long_sig = (bias == 1) & brk_up
    short_sig = (bias == -1) & brk_dn
    out = _frame(m15)
    out["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    return _exits(out, m15, 1.2, 1.5, 3.0)


def scalp_ema_pullback(m15: pd.DataFrame) -> pd.DataFrame:
    """En tendencia H4, entrar cuando el 15M retrocede a la EMA20 y cierra de
    nuevo a favor del sesgo (pullback a la media)."""
    bias = h4_bias(m15).to_numpy()
    e20 = ema(m15["close"], 20)
    touch_below = (m15["low"] <= e20) & (m15["close"] > e20)
    touch_above = (m15["high"] >= e20) & (m15["close"] < e20)
    long_sig = (bias == 1) & touch_below.to_numpy()
    short_sig = (bias == -1) & touch_above.to_numpy()
    out = _frame(m15)
    out["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    return _exits(out, m15, 1.0, 1.5, 2.5)


SCALP_BATTERY = {
    "scalp_rsi_pullback": scalp_rsi_pullback,
    "scalp_micro_breakout": scalp_micro_breakout,
    "scalp_ema_pullback": scalp_ema_pullback,
}


def main() -> int:
    pd.set_option("display.width", 200); pd.set_option("display.max_columns", 30)
    m15 = load_m15()
    print(f"M15: {len(m15)} velas  {m15.index.min().date()} → {m15.index.max().date()}")
    # Scalping = intradía → swap 0 base; spread realista 0.35 (=35) MANDA.
    cfg = BacktestConfig(spread_pips=35, pip_value=0.01, swap_per_night=0.0)
    print("Costos: spread 0.35 USD/oz, swap 0 (intradía). El spread es el juez.\n")

    results = []
    for name, gen in SCALP_BATTERY.items():
        sg = gen(m15)
        n = int((sg["signal"] != 0).sum())
        r = run_gauntlet(name, gen, m15, cfg=cfg, perm_iters=0)
        results.append(r)
        print(f"{name:22s} señales={n:6d} trades={r.n_trades:5d} "
              f"PF_net={r.pf_net:6.3f} gross={r.pf_gross:6.3f} "
              f"boot_lo={r.boot_ci_low:+7.3f} reg={r.n_regimes_profitable}/{r.n_regimes_total} "
              f"| {r.verdict}")

    print("\n" + "=" * 90)
    print(summarize(results).to_string(index=False))
    survivors = [r for r in results if r.pf_net > 1.0 and r.total_pnl_net > 0]
    print(f"\nSupervivientes de COSTOS: {[r.name for r in survivors] or 'NINGUNO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
