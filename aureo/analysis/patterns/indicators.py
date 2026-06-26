"""
indicators.py
Calculo de indicadores tecnicos (puro pandas/numpy, reproducible y sin lookahead).
"""
import pandas as pd

import config


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """RSI de Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve una copia del DataFrame con los indicadores agregados."""
    out = df.copy()
    out["ret"] = out["Close"].pct_change()
    out["SMA_FAST"] = out["Close"].rolling(config.SMA_FAST).mean()
    out["SMA_SLOW"] = out["Close"].rolling(config.SMA_SLOW).mean()
    out["RSI"] = _rsi(out["Close"], config.RSI_PERIOD)
    out["ATR"] = _atr(out, config.ATR_PERIOD)
    # Maximo/minimo del periodo PREVIO (shift 1 para evitar lookahead)
    out["PRIOR_HIGH"] = out["Close"].rolling(config.BREAKOUT_WINDOW).max().shift(1)
    out["PRIOR_LOW"] = out["Close"].rolling(config.BREAKOUT_WINDOW).min().shift(1)
    return out
