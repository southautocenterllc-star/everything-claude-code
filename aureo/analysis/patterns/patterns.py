"""
patterns.py
Deteccion de los patrones que pediste:
  1) Estacionalidad / ciclos  -> seasonality()
  2) Setups tecnicos / senales -> technical_signals()
(La reaccion a eventos macro se mide en backtest.event_study)
"""
import pandas as pd

import config


# ---------------------------------------------------------------------------
# 1) ESTACIONALIDAD / CICLOS
# ---------------------------------------------------------------------------
def seasonality(df: pd.DataFrame) -> dict:
    """
    Devuelve estadisticas de estacionalidad a partir de los retornos diarios:
      - retorno promedio por mes del anio
      - retorno promedio por dia de la semana
      - mejor y peor mes
    """
    r = df["ret"].dropna()

    by_month = r.groupby(r.index.month).agg(["mean", "median", "std", "count"])
    by_month.index.name = "mes"

    by_dow = r.groupby(r.index.dayofweek).agg(["mean", "median", "count"])
    by_dow.index.name = "dia_semana"  # 0=Lun ... 4=Vie

    best_month = int(by_month["mean"].idxmax())
    worst_month = int(by_month["mean"].idxmin())

    return {
        "by_month": by_month,
        "by_dow": by_dow,
        "best_month": best_month,
        "worst_month": worst_month,
    }


# ---------------------------------------------------------------------------
# 2) SETUPS TECNICOS / SENALES
# ---------------------------------------------------------------------------
def technical_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega columnas booleanas con las senales detectadas (sin lookahead).
    """
    out = df.copy()
    fast, slow = out["SMA_FAST"], out["SMA_SLOW"]
    rsi = out["RSI"]

    # Cruces de medias
    out["golden_cross"] = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    out["death_cross"]  = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    # Reversion desde sobreventa / sobrecompra (RSI cruza el umbral)
    out["rsi_oversold_reversal"] = (
        (rsi.shift(1) < config.RSI_OVERSOLD) & (rsi >= config.RSI_OVERSOLD)
    )
    out["rsi_overbought_reversal"] = (
        (rsi.shift(1) > config.RSI_OVERBOUGHT) & (rsi <= config.RSI_OVERBOUGHT)
    )

    # Rupturas (breakouts): solo la TRANSICION False->True cuenta como senal
    # (el dia que ROMPE, no cada dia que se sostiene arriba -> no infla muestra).
    out["breakout_up"] = (
        (out["Close"] > out["PRIOR_HIGH"])
        & (out["Close"].shift(1) <= out["PRIOR_HIGH"].shift(1))
    )
    out["breakout_down"] = (
        (out["Close"] < out["PRIOR_LOW"])
        & (out["Close"].shift(1) >= out["PRIOR_LOW"].shift(1))
    )

    # Divergencias RSI (proxy simple, solo usa pasado -> sin lookahead):
    #   alcista  -> precio hace minimo mas bajo pero RSI sube (debilidad bajista)
    #   bajista  -> precio hace maximo mas alto pero RSI baja (debilidad alcista)
    w = config.DIVERGENCE_WINDOW
    out["bullish_divergence"] = (
        (out["Close"] < out["Close"].shift(w))
        & (rsi > rsi.shift(w))
        & (rsi.shift(w) < 40)
    )
    out["bearish_divergence"] = (
        (out["Close"] > out["Close"].shift(w))
        & (rsi < rsi.shift(w))
        & (rsi.shift(w) > 60)
    )

    # Patrones de vela envolvente (engulfing)
    o, c = out["Open"], out["Close"]
    po, pc = o.shift(1), c.shift(1)
    prev_bear, prev_bull = pc < po, pc > po
    cur_bull, cur_bear = c > o, c < o
    out["bullish_engulfing"] = prev_bear & cur_bull & (o <= pc) & (c >= po)
    out["bearish_engulfing"] = prev_bull & cur_bear & (o >= pc) & (c <= po)

    # Gaps respecto al cierre previo
    g = config.GAP_THRESHOLD
    out["gap_up"]   = out["Open"] > pc * (1 + g)
    out["gap_down"] = out["Open"] < pc * (1 - g)

    return out


SIGNAL_COLUMNS = [
    "golden_cross",
    "death_cross",
    "rsi_oversold_reversal",
    "rsi_overbought_reversal",
    "breakout_up",
    "breakout_down",
    "bullish_divergence",
    "bearish_divergence",
    "bullish_engulfing",
    "bearish_engulfing",
    "gap_up",
    "gap_down",
]
