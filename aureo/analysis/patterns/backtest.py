"""
backtest.py
Motor cuantitativo:
  - forward_returns: rendimiento futuro a N dias
  - evaluate_signals: para cada senal, mide si "el patron repite con ventaja"
    (win rate, retorno medio, muestra) en cada horizonte
  - event_study: reaccion promedio del activo alrededor de eventos macro
"""
import numpy as np
import pandas as pd

import config
from patterns import SIGNAL_COLUMNS


# Direccion de cada senal: +1 mide a favor de subida, -1 a favor de bajada.
# Imprescindible para no contar un short acertado (precio cae) como fallo.
DIRECCION = {
    "golden_cross": +1, "breakout_up": +1, "rsi_oversold_reversal": +1,
    "death_cross": -1, "breakout_down": -1, "rsi_overbought_reversal": -1,
    "bullish_divergence": +1, "bearish_divergence": -1,
    "bullish_engulfing": +1, "bearish_engulfing": -1,
    "gap_up": +1, "gap_down": -1,
}


def _permutation_test(r: np.ndarray, mask: np.ndarray, rng) -> dict:
    """
    Test de significancia por ROTACION CIRCULAR (circular-shift permutation).

    Mide si el retorno medio (con signo) de las fechas de la senal le gana al
    azar SIN romper la estructura temporal. La distribucion nula se construye
    dejando QUIETO el set exacto de fechas de la senal (preserva su clustering
    y su n) y rotando circularmente la serie de retornos con signo: offset 0 es
    la observada, offsets aleatorios son la nula.

    Por que rotacion y NO muestreo de fechas al azar: las entradas reales estan
    clusterizadas y sus ventanas fwd con h>1 se solapan -> estan correlacionadas
    entre si, asi que la media de n entradas reales tiene MAS varianza que la
    media de n fechas independientes. Una nula de fechas random queda demasiado
    angosta e infla la significancia (verificado: fallaba el random walk). La
    rotacion preserva en la nula TANTO el clustering de las entradas COMO la
    autocorrelacion de los retornos -> comparacion justa. Tambien mete el drift
    del activo dentro de la nula. Un t-stat ingenuo miente por la misma razon
    (solapamiento -> encoge el error estandar).
    """
    L = len(r)
    n = int(mask.sum())
    if n < 1 or L < 3 or n >= L:
        return {"p_value": float("nan"), "significativo": False}
    observed = float(r[mask].mean())
    pos = np.flatnonzero(mask)                              # (n,) fechas de la senal
    shifts = rng.integers(1, L, size=config.PERMUTATION_ITERS)   # offsets 1..L-1
    rolled_idx = (pos[None, :] - shifts[:, None]) % L       # (n_iter, n)
    null_means = r[rolled_idx].mean(axis=1)                 # (n_iter,)
    count = int(np.sum(null_means >= observed))
    # p empirico de una cola con correccion +1 (evita p=0 estadisticamente deshonesto)
    p = (1.0 + count) / (1.0 + config.PERMUTATION_ITERS)
    return {
        "p_value": float(p),
        "significativo": bool(p < config.PERMUTATION_ALPHA and n >= config.PERMUTATION_MIN_N),
    }


def forward_returns(df: pd.DataFrame, horizons=None) -> pd.DataFrame:
    """Agrega columnas fwd_<h> con el retorno a h dias hacia adelante."""
    horizons = horizons or config.FORWARD_HORIZONS
    out = df.copy()
    for h in horizons:
        out[f"fwd_{h}"] = out["Close"].shift(-h) / out["Close"] - 1
    return out


def evaluate_signals(df: pd.DataFrame, horizons=None) -> pd.DataFrame:
    """
    Para cada senal/horizonte: n, win_rate y retorno (CON SIGNO segun DIRECCION)
    + p-valor por permutacion. win_rate>0 = se movio a favor de la direccion de
    la senal (un short acierta cuando el precio cae).
    """
    horizons = horizons or config.FORWARD_HORIZONS
    df = forward_returns(df, horizons)
    rng = np.random.default_rng(config.PERMUTATION_SEED)   # reproducible
    rows = []

    for sig in SIGNAL_COLUMNS:
        if sig not in df.columns:
            continue
        if sig not in DIRECCION:
            raise KeyError(f"Senal sin direccion en DIRECCION: {sig}")
        direction = DIRECCION[sig]
        sig_mask = df[sig].fillna(False).to_numpy(dtype=bool)
        for h in horizons:
            fwd = df[f"fwd_{h}"].to_numpy(dtype=float)
            valid = ~np.isnan(fwd)                  # dias con retorno fwd definido
            r = direction * fwd[valid]              # retorno CON SIGNO, alineado
            m = sig_mask[valid]                     # fechas de la senal en region valida
            n = int(m.sum())
            if n == 0:
                continue
            entradas = r[m]
            test = _permutation_test(r, m, rng)
            rows.append({
                "senal": sig,
                "direccion": "long" if direction > 0 else "short",
                "horizonte_dias": h,
                "n": n,
                "win_rate": float((entradas > 0).mean()),
                "ret_medio": float(entradas.mean()),
                "ret_mediano": float(np.median(entradas)),
                "p_value": test["p_value"],
                "significativo": test["significativo"],
            })

    return pd.DataFrame(rows)


def evaluate_signals_walkforward(df: pd.DataFrame, horizons=None,
                                 train_frac: float = 0.7) -> pd.DataFrame:
    """
    Validacion walk-forward simple: parte la historia en IN-SAMPLE (primer
    train_frac) y OUT-OF-SAMPLE (resto) y evalua cada senal en ambos tramos.
    Si el 'edge' existe in-sample pero desaparece out-of-sample, probablemente
    era overfitting/ruido. Devuelve una tabla comparativa por senal y horizonte.
    """
    horizons = horizons or config.FORWARD_HORIZONS
    n = len(df)
    if n < 50:
        return pd.DataFrame()
    cut = int(n * train_frac)
    df_is = df.iloc[:cut]
    df_oos = df.iloc[cut:]

    ev_is = evaluate_signals(df_is, horizons)
    ev_oos = evaluate_signals(df_oos, horizons)

    keep = ["senal", "horizonte_dias", "n", "win_rate", "ret_medio"]
    a = ev_is[keep].rename(columns={
        "n": "n_is", "win_rate": "win_is", "ret_medio": "ret_is"})
    b = ev_oos[keep].rename(columns={
        "n": "n_oos", "win_rate": "win_oos", "ret_medio": "ret_oos"})
    merged = a.merge(b, on=["senal", "horizonte_dias"], how="outer")

    # 'consistente' = el signo del retorno medio se mantiene IS vs OOS
    def _consistent(row):
        ri, ro = row.get("ret_is"), row.get("ret_oos")
        if pd.isna(ri) or pd.isna(ro):
            return False
        return bool((ri > 0) == (ro > 0))

    merged["consistente"] = merged.apply(_consistent, axis=1)
    return merged.sort_values(["senal", "horizonte_dias"]).reset_index(drop=True)


def event_study(df: pd.DataFrame, event_dates, window: int = None) -> pd.DataFrame:
    """
    Estudio de eventos: alinea los retornos en una ventana [-window, +window]
    alrededor de cada evento y devuelve el promedio por offset.
    'event_dates' es una lista de fechas (str o Timestamp).

    Solo se consideran eventos que caen DENTRO del rango de datos (si un evento
    es anterior al primer dato lo mapeariamos erroneamente a la fila 0 y
    contaminaria el offset 0).
    """
    window = window or config.EVENT_WINDOW
    idx = df.index
    series = df["ret"]
    offsets = range(-window, window + 1)
    collected = {o: [] for o in offsets}

    if len(idx) == 0:
        return pd.DataFrame()
    lo, hi = idx[0], idx[-1]

    for ev in pd.to_datetime(list(event_dates)):
        if ev < lo or ev > hi:
            continue
        # primer dia de mercado en o despues del evento
        pos_arr = np.where(idx >= ev)[0]
        if len(pos_arr) == 0:
            continue
        pos = pos_arr[0]
        for o in offsets:
            p = pos + o
            if 0 <= p < len(series):
                collected[o].append(series.iloc[p])

    rows = []
    for o in offsets:
        vals = pd.Series(collected[o]).dropna()
        if len(vals) == 0:
            continue
        rows.append({
            "offset_dias": o,
            "ret_medio": float(vals.mean()),
            "win_rate": float((vals > 0).mean()),
            "n": int(len(vals)),
        })
    return pd.DataFrame(rows)


def event_study_by_type(df: pd.DataFrame, events, window: int = None) -> dict:
    """
    Igual que event_study pero separado por TIPO de evento.
    'events' es un DataFrame con columnas 'date' y 'event'.
    Devuelve {tipo_evento: DataFrame del estudio}.
    """
    if events is None or len(events) == 0:
        return {}
    out = {}
    for ev_type, grp in events.groupby("event"):
        study = event_study(df, grp["date"], window)
        if len(study) > 0:
            out[str(ev_type)] = study
    return out


def correlation_matrix(data_ind: dict) -> pd.DataFrame:
    """Correlacion de retornos diarios entre activos (contexto complementario)."""
    rets = {name: d["ret"] for name, d in data_ind.items()}
    return pd.DataFrame(rets).dropna().corr()
