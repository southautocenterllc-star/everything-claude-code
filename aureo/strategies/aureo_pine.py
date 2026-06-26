"""AUREO Pine strategy ported to Python.

Replica la lógica del aureo_strategy.pine validado en Windows
(parametros 'balanced' calibrados con grid search 2026-05-19):

Long entry:
    Close > EMA20 > EMA50 > EMA200   (trend up)
    RSI(14) in [rsi_long_min, rsi_long_max]
    No posición abierta (pyramiding=0)

Short entry:
    Close < EMA20 < EMA50 < EMA200   (trend down)
    RSI(14) in [rsi_short_min, rsi_short_max]
    No posición abierta

Exits por barra (calculados al entrar, no se mueven):
    SL  = entry ± atr_sl_mult  * ATR(14)
    TP1 = entry ± atr_tp1_mult * ATR(14)   (cierre parcial 50%)
    TP2 = entry ± atr_tp2_mult * ATR(14)   (cierre restante 50%)

Pyramiding=0 lo enforce el engine de backtest, no esta función.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class PineParams:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    rsi_long_min: float = 30.0
    rsi_long_max: float = 75.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 55.0
    atr_sl_mult: float = 1.2
    atr_tp1_mult: float = 3.0
    atr_tp2_mult: float = 6.0


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    # Wilder's smoothing
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def generate_signals(df: pd.DataFrame, params: PineParams | None = None) -> pd.DataFrame:
    """Devuelve un DataFrame con columnas:
    [close, ema_fast, ema_mid, ema_slow, rsi, atr, signal, sl, tp1, tp2]

    signal: +1 long entry, -1 short entry, 0 nada (en esa barra).
    """
    p = params or PineParams()
    out = pd.DataFrame(index=df.index)
    out["close"] = df["close"]
    out["ema_fast"] = ema(df["close"], p.ema_fast)
    out["ema_mid"] = ema(df["close"], p.ema_mid)
    out["ema_slow"] = ema(df["close"], p.ema_slow)
    out["rsi"] = rsi(df["close"], p.rsi_period)
    out["atr"] = atr(df, p.atr_period)

    trend_up = (out["close"] > out["ema_fast"]) & (out["ema_fast"] > out["ema_mid"]) & (out["ema_mid"] > out["ema_slow"])
    trend_dn = (out["close"] < out["ema_fast"]) & (out["ema_fast"] < out["ema_mid"]) & (out["ema_mid"] < out["ema_slow"])

    rsi_ok_long = out["rsi"].between(p.rsi_long_min, p.rsi_long_max, inclusive="both")
    rsi_ok_short = out["rsi"].between(p.rsi_short_min, p.rsi_short_max, inclusive="both")

    long_sig = trend_up & rsi_ok_long
    short_sig = trend_dn & rsi_ok_short

    sig = np.where(long_sig, 1, np.where(short_sig, -1, 0))
    out["signal"] = sig

    direction = pd.Series(sig, index=out.index)
    sl = np.where(direction == 1, out["close"] - p.atr_sl_mult * out["atr"],
                  np.where(direction == -1, out["close"] + p.atr_sl_mult * out["atr"], np.nan))
    tp1 = np.where(direction == 1, out["close"] + p.atr_tp1_mult * out["atr"],
                   np.where(direction == -1, out["close"] - p.atr_tp1_mult * out["atr"], np.nan))
    tp2 = np.where(direction == 1, out["close"] + p.atr_tp2_mult * out["atr"],
                   np.where(direction == -1, out["close"] - p.atr_tp2_mult * out["atr"], np.nan))
    out["sl"] = sl
    out["tp1"] = tp1
    out["tp2"] = tp2

    return out
