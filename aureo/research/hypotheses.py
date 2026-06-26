"""Batería de hipótesis de edge para XAU/USD H1.

Cada hipótesis es `gen(df) -> signals_df` con columnas [close, signal, sl, tp1,
tp2], el mismo contrato que consume backtest.engine. Todas usan salidas basadas
en ATR (comparables entre sí) y POCOS parámetros (anti-overfitting). El gauntlet
(research/harness.py) decide cuáles sobreviven costos + significancia + régimen.

Convención de dirección: +1 long, -1 short, 0 nada. El SL/TP se coloca según la
dirección. Una posición a la vez la enforce el engine (pyramiding=0).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.aureo_pine import PineParams, atr, ema, generate_signals, rsi


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _base(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["close"] = df["close"]
    return out


def _attach_exits(out: pd.DataFrame, df: pd.DataFrame, atr_s: pd.Series,
                  sl_mult: float, tp1_mult: float, tp2_mult: float) -> pd.DataFrame:
    d = out["signal"].to_numpy()
    c = df["close"].to_numpy()
    a = atr_s.to_numpy()
    out["sl"] = np.where(d == 1, c - sl_mult * a,
                         np.where(d == -1, c + sl_mult * a, np.nan))
    out["tp1"] = np.where(d == 1, c + tp1_mult * a,
                          np.where(d == -1, c - tp1_mult * a, np.nan))
    out["tp2"] = np.where(d == 1, c + tp2_mult * a,
                          np.where(d == -1, c - tp2_mult * a, np.nan))
    # exits indefinidos (atr NaN) → anular la señal de esa barra
    bad = ~np.isfinite(out["sl"].to_numpy())
    sig = out["signal"].to_numpy().copy()
    sig[bad & (sig != 0)] = 0
    out["signal"] = sig
    return out


# --------------------------------------------------------------------------- #
# hipótesis
# --------------------------------------------------------------------------- #
def balanced_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """Control: la 'balanced' que el backtest oficial declaró sin edge."""
    s = generate_signals(df, PineParams())
    return s[["close", "signal", "sl", "tp1", "tp2"]]


def _new_extreme(df, lookback=20):
    hi = df["high"].rolling(lookback).max()
    lo = df["low"].rolling(lookback).min()
    prev_hi = hi.shift(1)
    prev_lo = lo.shift(1)
    new_high = (df["close"] > prev_hi) & (df["close"].shift(1) <= prev_hi)
    new_low = (df["close"] < prev_lo) & (df["close"].shift(1) >= prev_lo)
    return new_high.fillna(False), new_low.fillna(False)


def breakout_fade_up(df: pd.DataFrame) -> pd.DataFrame:
    """Pista de la data: los breakouts AL ALZA se desvanecen → SHORT en nuevo
    máximo de 20 barras (mean-reversion)."""
    out = _base(df)
    new_high, _ = _new_extreme(df, 20)
    out["signal"] = np.where(new_high, -1, 0)
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.2, 3.0, 6.0)


def breakout_follow_up(df: pd.DataFrame) -> pd.DataFrame:
    """Control opuesto: LONG en nuevo máximo de 20 barras (momentum)."""
    out = _base(df)
    new_high, _ = _new_extreme(df, 20)
    out["signal"] = np.where(new_high, 1, 0)
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.2, 3.0, 6.0)


def breakout_follow_down(df: pd.DataFrame) -> pd.DataFrame:
    """Pista de la data: los breakouts A LA BAJA continúan → SHORT en nuevo
    mínimo de 20 barras (momentum bajista)."""
    out = _base(df)
    _, new_low = _new_extreme(df, 20)
    out["signal"] = np.where(new_low, -1, 0)
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.2, 3.0, 6.0)


def breakout_fade_down(df: pd.DataFrame) -> pd.DataFrame:
    """Control opuesto: LONG en nuevo mínimo de 20 barras (rebote)."""
    out = _base(df)
    _, new_low = _new_extreme(df, 20)
    out["signal"] = np.where(new_low, 1, 0)
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.2, 3.0, 6.0)


def rsi_meanrev(df: pd.DataFrame) -> pd.DataFrame:
    """Counter-trend puro: long RSI<30, short RSI>70."""
    out = _base(df)
    r = rsi(df["close"], 14)
    sig = np.where(r < 30, 1, np.where(r > 70, -1, 0))
    out["signal"] = sig
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.5, 2.0, 4.0)


def rsi_meanrev_trendfilter(df: pd.DataFrame) -> pd.DataFrame:
    """Comprar la corrección en tendencia: long RSI<35 sobre EMA200;
    short RSI>65 bajo EMA200."""
    out = _base(df)
    r = rsi(df["close"], 14)
    e200 = ema(df["close"], 200)
    up = df["close"] > e200
    long_sig = (r < 35) & up
    short_sig = (r > 65) & (~up)
    out["signal"] = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.5, 2.0, 4.0)


def session_open_momentum(df: pd.DataFrame) -> pd.DataFrame:
    """En la apertura de Londres (08:00 UTC) y NY (13:00 UTC), entrar en la
    dirección del movimiento de las 3 barras previas."""
    out = _base(df)
    hour = df.index.hour
    mom = df["close"].pct_change(3)
    is_open = np.isin(hour, [8, 13])
    sig = np.where(is_open & (mom > 0), 1, np.where(is_open & (mom < 0), -1, 0))
    out["signal"] = sig
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.2, 2.0, 4.0)


def opening_range_breakout(df: pd.DataFrame) -> pd.DataFrame:
    """ORB Londres: rango de la 1ª hora (08:00 UTC); en 09:00 romper ese rango
    define dirección del día."""
    out = _base(df)
    h = df.index.hour
    sig = np.zeros(len(df), dtype=int)
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    # rango de la barra de las 08:00 → señal en la barra de las 09:00
    for i in range(1, len(df)):
        if h[i] == 9 and h[i - 1] == 8:
            if close[i] > high[i - 1]:
                sig[i] = 1
            elif close[i] < low[i - 1]:
                sig[i] = -1
    out["signal"] = sig
    a = atr(df, 14)
    return _attach_exits(out, df, a, 1.2, 2.0, 4.0)


def donchian_trend(df: pd.DataFrame) -> pd.DataFrame:
    """Turtle-ish: long en ruptura de máximo de 55 barras, short en mínimo de 55."""
    out = _base(df)
    new_high, new_low = _new_extreme(df, 55)
    out["signal"] = np.where(new_high, 1, np.where(new_low, -1, 0))
    a = atr(df, 14)
    return _attach_exits(out, df, a, 2.0, 3.0, 6.0)


def vol_filtered_balanced(df: pd.DataFrame) -> pd.DataFrame:
    """La 'balanced' pero SOLO cuando la volatilidad (ATR/precio) está por debajo
    de su mediana móvil — hipótesis: el trend-follower débil funciona en calma."""
    s = generate_signals(df, PineParams()).copy()
    a = atr(df, 14)
    vol = (a / df["close"])
    med = vol.rolling(500, min_periods=100).median()
    calm = (vol <= med).to_numpy()
    sig = s["signal"].to_numpy().copy()
    sig[~calm] = 0
    s["signal"] = sig
    return s[["close", "signal", "sl", "tp1", "tp2"]]


BATTERY = {
    "balanced_baseline": balanced_baseline,
    "breakout_fade_up": breakout_fade_up,
    "breakout_follow_up": breakout_follow_up,
    "breakout_follow_down": breakout_follow_down,
    "breakout_fade_down": breakout_fade_down,
    "rsi_meanrev": rsi_meanrev,
    "rsi_meanrev_trendfilter": rsi_meanrev_trendfilter,
    "session_open_momentum": session_open_momentum,
    "opening_range_breakout": opening_range_breakout,
    "donchian_trend": donchian_trend,
    "vol_filtered_balanced": vol_filtered_balanced,
}
