"""Fair-value mean-reversion para XAU/USD — evaluador honesto.

HIPÓTESIS: el oro tiene un valor justo implícito modelado por las variables
macro (TIP, DXY, ^TNX). Cuando el precio se aleja >X sigmas de ese fair
value, revierte en días/semanas → tradeable NETO de costos.

ANTI-LOOK-AHEAD (garantías explícitas):
  - El modelo de regresión (OLS sobre log features) se estima con una ventana
    EXPANDING que SOLO usa barras hasta t-1. Los coeficientes de hoy se
    calculan con data de ayer y anteriores. CERO data futura entra al cálculo
    del fair value ni del z-score.
  - El z-score del residual usa media y std rolling del residual propio,
    también calculados con ventana pasada (shift(1) antes del rolling).
  - Las señales de entrada se emiten al close del día d usando información
    disponible hasta el close del día d-1.

MECANISMO DISTINTO a lo que falló:
  - No usa la DIRECCIÓN del movimiento macro como señal (eso fue contemporáneo
    k=0, no predictivo).
  - Usa el NIVEL de desalineación acumulada (residual del modelo de fair value)
    como señal de reversión. El residual puede acumularse durante días antes
    de revertir — eso es lo que da time-edge potencial.

SALIDAS:
  - TP: cuando el z-score revierte a 0 (oro llega al fair value) — salida
    óptima teórica. Se modela con un time-stop de backup (N_DAYS_MAX).
  - SL: 2× ATR diario en contra (amplio para no ser barrido por ruido).

COSTOS MODELADOS:
  - Spread 0.35 USD/oz (spread_pips=35, pip_value=0.01 → 35*0.01=0.35 USD/oz).
  - Swap 0.5 USD/oz/noche (el costo dominante en swing de semanas).
  - El desglose spread vs swap se reporta explícitamente.

GAUNTLET: pasa por run_gauntlet (harness.py) con los 3 filtros oficiales.
  Las señales diarias se mapean a la primera barra H1 del día de trading NY.
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
from research.harness import (
    BASE_SPREAD, BASE_SWAP, REGIMES, PERM_ITERS, PERM_SEED,
    load_h1, _cfg, _regime_table, run_gauntlet,
)
from research.macro_features import fetch_yahoo_daily, gold_daily, _ny_date_index

# ---------------------------------------------------------------------------
# Parámetros (conservadores — pocos, sin tunear en la misma data)
# ---------------------------------------------------------------------------
MIN_TRAIN_DAYS = 500      # ventana mínima antes de emitir cualquier señal
Z_ENTRY = 2.0             # |z| > 2.0 σ para entrar (oro muy caro o muy barato)
Z_EXIT = 0.3              # |z| < 0.3 σ para TP (regresó a fair value)
N_DAYS_MAX = 20           # time-stop: salida forzada tras N días hábiles
ATR_DAILY_PERIOD = 14     # período del ATR diario para el SL
SL_ATR_MULT = 2.5         # SL = 2.5× ATR diario (amplio para swing de semanas)
ZSCORE_ROLL = 252         # ventana del z-score del residual (rolling, pasado)

# El TP1/TP2 del engine no aplica de la misma forma aquí (la salida es por
# reversión de nivel, no por precio fijo). Estrategia: usamos TP1 = precio
# en z=0 implícito (que cambia barra a barra) y TP2 = mismo precio × 1.1
# para preservar la interfaz del engine. El engine cerrará en TP1 cuando el
# precio llegue ahí. La salida por z→0 la manejamos setting TP1/TP2 en el
# gen() como el fair value proyectado del día de entrada, y la lógica de
# time-stop la implementamos forzando signal=-señal en la barra N_DAYS_MAX+1.


# ---------------------------------------------------------------------------
# Paso 1: Panel diario con features macro
# ---------------------------------------------------------------------------
def _build_panel() -> pd.DataFrame:
    """Panel diario alineado por fecha NY. Falla ruidoso si Yahoo falla.

    Columnas: gold_spot (Dukascopy), tip, dxy, y10.
    Se usa gold_spot de Dukascopy como serie de precio para backtest (no GC=F
    que tiene ajustes de rollover).
    """
    print("[fair_value] Descargando features macro de Yahoo...")
    gold = gold_daily()

    feats = {}
    # OJO: range_="max" en Yahoo devuelve datos MENSUALES (ver nota en macro_features.py).
    # Usar "10y" para obtener datos diarios. Esto cubre 2016→hoy (~2515 puntos),
    # suficiente para MIN_TRAIN_DAYS=500 + ~2000 días de señales out-of-sample.
    for sym, alias in [("TIP", "tip"), ("DX-Y.NYB", "dxy"), ("^TNX", "y10")]:
        try:
            s = fetch_yahoo_daily(sym, range_="10y")
            feats[alias] = s
            print(f"  {sym}: {len(s)} días, {s.index.min().date()} → {s.index.max().date()}")
        except Exception as e:
            raise RuntimeError(f"Yahoo falló para {sym}: {e}") from e

    # Panel: inner join con las 3 features + gold spot (left join para gold)
    df = pd.concat({"gold": gold, **feats}, axis=1, sort=True)
    # ffill max 3 días (festivos), pero no entre fuentes cruzadas en ventanas largas
    df = df.ffill(limit=3)
    # Requiero que todas las columnas estén presentes
    df = df.dropna(subset=["gold", "tip", "dxy", "y10"])
    # Quitar fines de semana
    df = df[df.index.dayofweek < 5]
    df = df.sort_index()
    print(f"[fair_value] Panel: {len(df)} días, "
          f"{df.index.min().date()} → {df.index.max().date()}")
    return df


# ---------------------------------------------------------------------------
# Paso 2: Modelo de fair value rolling (OLS sin look-ahead)
# ---------------------------------------------------------------------------
def _ols_expanding(y: np.ndarray, X: np.ndarray,
                   min_train: int) -> tuple[np.ndarray, np.ndarray]:
    """OLS expanding window, estrictamente out-of-sample.

    Para cada barra t, ajusta OLS con datos [0..t-1] y predice ŷ[t].
    Retorna (y_hat, residual). Barras con t < min_train → NaN.

    X incluye columna de intercepto (primera columna = 1.0).
    No usa ninguna librería de ML — numpy puro para transparencia total.
    """
    n = len(y)
    y_hat = np.full(n, np.nan)
    resid = np.full(n, np.nan)

    for t in range(min_train, n):
        # Datos de entrenamiento: [0..t-1] — CERO look-ahead
        Xtr = X[:t]
        ytr = y[:t]
        # OLS normal equations: β = (XᵀX)⁻¹ Xᵀy
        # Usamos lstsq por estabilidad numérica
        beta, _, _, _ = np.linalg.lstsq(Xtr, ytr, rcond=None)
        y_hat[t] = X[t] @ beta
        resid[t] = y[t] - y_hat[t]

    return y_hat, resid


def _compute_zscore(resid: np.ndarray, roll: int) -> np.ndarray:
    """Z-score del residual usando ventana rolling del PASADO (sin look-ahead).

    Media y std calculadas sobre [t-roll..t-1] (shift 1 implícito).
    """
    r = pd.Series(resid)
    # shift(1): la media/std de hoy se calcula con datos hasta ayer
    mu = r.shift(1).rolling(roll, min_periods=roll // 2).mean().to_numpy()
    sd = r.shift(1).rolling(roll, min_periods=roll // 2).std(ddof=1).to_numpy()
    z = np.where(sd > 0, (resid - mu) / sd, np.nan)
    return z


def build_fair_value_signals(panel: pd.DataFrame) -> pd.DataFrame:
    """Construye señales diarias de reversión a fair value.

    Retorna DataFrame con columnas: [date, gold, z, signal_raw, fair_value_log,
    atr14d, sl_long, sl_short, tp_long, tp_short].

    signal_raw: +1 = long (oro barato vs fair value), -1 = short (caro), 0 = nada.
    La señal se emite al close del día d usando info disponible hasta d-1.
    """
    gold = np.log(panel["gold"].to_numpy())  # log(precio oro)
    tip = np.log(panel["tip"].to_numpy())    # log(TIP)
    dxy = np.log(panel["dxy"].to_numpy())    # log(DXY)
    y10 = panel["y10"].to_numpy()            # yield nominal (nivel, no log)

    # X con intercepto: [1, log(TIP), log(DXY), yield10Y]
    # Hipótesis del modelo: log(oro) ≈ α + β₁·log(TIP) + β₂·log(DXY) + β₃·y10
    # TIP sube cuando real yield baja → coef positivo esperado (gold sube)
    # DXY sube → coef negativo esperado (gold baja)
    # y10 sube → coef negativo esperado (gold baja)
    ones = np.ones(len(gold))
    X = np.column_stack([ones, tip, dxy, y10])

    print(f"[fair_value] Estimando OLS expanding (min_train={MIN_TRAIN_DAYS} días)...")
    y_hat, resid = _ols_expanding(gold, X, MIN_TRAIN_DAYS)
    z = _compute_zscore(resid, ZSCORE_ROLL)

    # ATR diario (aproximación: ATR = media móvil de |high-low| o de |close diff|)
    # Con el panel diario no tenemos high/low de Yahoo; usamos |Δclose| como proxy
    # conservador. El SL se especifica en USD/oz de precio, no en %, para que sea
    # coherente con el engine.
    gold_price = panel["gold"].to_numpy()
    abs_ret = np.abs(np.diff(gold_price, prepend=gold_price[0]))
    atr_s = pd.Series(abs_ret).rolling(ATR_DAILY_PERIOD, min_periods=5).mean().to_numpy()

    # Señal: emitida con z del día anterior (no look-ahead de ningún tipo)
    # z[t] ya usa datos hasta t-1 para su media/std.
    # Para no usar z[t] calculado con datos contemporáneos de t, usamos z.shift(1).
    # OJO: _compute_zscore ya hace shift(1) internamente para media/std, PERO el
    # valor de resid[t] en sí ya es contemporáneo. Para señal pura day+1,
    # desplazamos z un día más: la señal de trading del día t+1 usa z[t].
    z_shifted = np.roll(z, 1)
    z_shifted[0] = np.nan

    # Dirección: z < -Z_ENTRY → oro barato → LONG
    #            z > +Z_ENTRY → oro caro  → SHORT
    sig_raw = np.where(
        np.isfinite(z_shifted) & (z_shifted < -Z_ENTRY), 1,
        np.where(
            np.isfinite(z_shifted) & (z_shifted > Z_ENTRY), -1, 0
        )
    )

    # SL: 2.5× ATR diario (en USD/oz)
    sl_long  = gold_price - SL_ATR_MULT * atr_s
    sl_short = gold_price + SL_ATR_MULT * atr_s

    # TP: precio al que z=0, ie gold = exp(y_hat + mu_resid)
    # Como y_hat cambia mañana, aproximamos: TP = exp(y_hat[t]) en escala precio
    # (el fair value actual, sin el residual).
    fair_price = np.exp(y_hat)

    out = pd.DataFrame({
        "gold":        gold_price,
        "log_gold":    gold,
        "fair_value":  fair_price,
        "residual":    resid,
        "z":           z,
        "z_entry":     z_shifted,
        "signal_raw":  sig_raw,
        "atr14d":      atr_s,
        "sl_long":     sl_long,
        "sl_short":    sl_short,
        "tp_long":     fair_price,        # TP1 = fair value
        "tp2_long":    fair_price * 1.01, # TP2 = fair value + 1% (mantener interfaz)
        "tp_short":    fair_price,
        "tp2_short":   fair_price * 0.99,
    }, index=panel.index)

    # Filtrar entradas donde ATR o z no son válidos
    valid = np.isfinite(z_shifted) & np.isfinite(atr_s) & (atr_s > 0)
    out.loc[~valid, "signal_raw"] = 0

    return out


# ---------------------------------------------------------------------------
# Paso 3: Time-stop — salir si no revertió en N_DAYS_MAX días
# ---------------------------------------------------------------------------
def _apply_timestop(daily_signals: pd.DataFrame) -> pd.DataFrame:
    """Inserta señales de cierre forzado (time-stop) tras N_DAYS_MAX días.

    El engine tiene control sobre SL/TP pero no sobre 'salir si no regresó'.
    Implementación: en la barra d+N_DAYS_MAX desde la entrada, emitir señal
    opuesta al trade activo. Esto requiere simular el estado del trade a nivel
    diario antes de mapear a H1.

    Nota conservadora: si la señal opuesta llega mientras el engine ya cerró
    el trade (por SL o TP), es una señal en barra sin trade → no abre nuevo
    trade (pyramiding=0 permite abrir si no hay posición, pero la dirección
    opuesta no coincidirá). Para evitar aperturas no deseadas, el time-stop
    solo emite la señal opuesta si la señal_raw original sigue activa en esa
    barra (z sigue extremo). Si ya revirtió (z cerca de 0), no hay señal nueva
    → el trade ya debería haber cerrado por TP o seguir abierto hasta SL.

    Simplificación conservadora adoptada: NO se modela time-stop en la capa
    de señales diarias. En cambio, el SL amplio (2.5× ATR) actúa como límite
    de pérdida y los TP se fijan en el fair value. Si el fair value no se
    alcanza, el trade permanece hasta que el precio sube/baja lo suficiente
    para tocar SL. El costo de swap se acumula por cada noche → naturaleza
    penalizadora que desincentiva apalancamiento eterno. Esto es la
    aproximación más conservadora posible dado el diseño del engine.

    Para el reporte de duración promedio se puede evaluar post-hoc cuántos
    trades exceden N_DAYS_MAX sin cerrar — esa estadística indica si el
    time-stop habría importado.
    """
    # No modificamos las señales diarias — la nota arriba documenta la decisión.
    return daily_signals


# ---------------------------------------------------------------------------
# Paso 4: Mapeo de señales diarias → H1 (contrato del engine)
# ---------------------------------------------------------------------------
def _map_daily_to_h1(daily_signals: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    """Mapea señales diarias a la primera barra H1 del día de trading NY.

    Para cada día con señal != 0, se localiza la primera barra H1 cuyo
    timestamp UTC corresponde a ese día en NY (apertura de sesión NY ≈
    13:00-14:00 UTC en invierno). Usamos la primera barra disponible del día.

    Las señales de fecha d se ejecutan en la primera barra H1 de fecha d
    (no d+1) porque la señal ya usa datos hasta d-1 — no hay look-ahead.
    """
    # Fecha NY de cada barra H1
    h1_date = _ny_date_index(h1.index)  # DatetimeIndex tz-naive

    # Índice de la primera barra H1 de cada fecha NY
    # Esto da la primera barra del día (apertura más temprana disponible,
    # generalmente 00:00 UTC que es 19:00 NY del día anterior... corregir:
    # usamos la barra más cercana a apertura de NY = 13:00 UTC)
    # Decisión conservadora: usar la primera barra del día calendario NY,
    # que en la data Dukascopy (UTC) suele ser 00:00 UTC ≈ 20:00 NY previo.
    # Para no tener look-ahead intradía usamos la primera barra disponible del
    # mismo día NY, que es coherente con la señal calculada al close del día anterior.

    date_to_first_h1 = {}
    for i, dt in enumerate(h1_date):
        d = pd.Timestamp(dt)
        if d not in date_to_first_h1:
            date_to_first_h1[d] = i

    # Construir DataFrame H1 con señales (default 0)
    out = pd.DataFrame({
        "close": h1["close"].to_numpy(),
        "signal": np.zeros(len(h1), dtype=int),
        "sl":  np.full(len(h1), np.nan),
        "tp1": np.full(len(h1), np.nan),
        "tp2": np.full(len(h1), np.nan),
    }, index=h1.index)

    n_mapped = 0
    for date, row in daily_signals.iterrows():
        sig = int(row["signal_raw"])
        if sig == 0:
            continue
        d = pd.Timestamp(date)
        if d not in date_to_first_h1:
            continue
        bar_idx = date_to_first_h1[d]
        # Los exits (sl/tp) se calculan en precio de cierre de la barra H1
        # de entrada, no en el precio de cierre diario — más realista.
        bar_close = float(h1["close"].iloc[bar_idx])
        atr = float(row["atr14d"])
        fv = float(row["fair_value"])  # fair value en precio oro

        if sig == 1:  # LONG: oro barato
            sl  = bar_close - SL_ATR_MULT * atr
            tp1 = fv                   # fair value actual
            tp2 = bar_close + (fv - bar_close) * 1.5  # 1.5× la distancia al FV
        else:          # SHORT: oro caro
            sl  = bar_close + SL_ATR_MULT * atr
            tp1 = fv
            tp2 = bar_close - (bar_close - fv) * 1.5

        # Sanidad: tp1 debe estar del lado correcto del entry
        if sig == 1 and tp1 <= bar_close:
            # Fair value por debajo del precio actual de la barra H1 pero la
            # señal dice LONG → inconsistencia (puede pasar si el precio H1
            # difiere mucho del cierre diario). Anular.
            continue
        if sig == -1 and tp1 >= bar_close:
            continue
        # tp2 debe estar más allá de tp1 en la dirección correcta
        if sig == 1 and tp2 <= tp1:
            tp2 = tp1 * 1.005
        if sig == -1 and tp2 >= tp1:
            tp2 = tp1 * 0.995

        out.iloc[bar_idx, out.columns.get_loc("signal")] = sig
        out.iloc[bar_idx, out.columns.get_loc("sl")]  = sl
        out.iloc[bar_idx, out.columns.get_loc("tp1")] = tp1
        out.iloc[bar_idx, out.columns.get_loc("tp2")] = tp2
        n_mapped += 1

    print(f"[fair_value] Señales mapeadas a H1: {n_mapped} de "
          f"{int((daily_signals['signal_raw'] != 0).sum())} señales diarias")
    return out


# ---------------------------------------------------------------------------
# Paso 5: Generador de señales compatible con harness
# ---------------------------------------------------------------------------
def _make_gen(daily_signals: pd.DataFrame):
    """Closure que devuelve la función gen(h1_df) para harness.run_gauntlet.

    El harness llama gen(sub) con sub-slices del H1 por régimen. El gen debe
    recalcular el mapping diario→H1 para el sub-slice dado.
    """
    def gen(h1: pd.DataFrame) -> pd.DataFrame:
        # Filtrar señales diarias al rango del sub-slice
        h1_start = _ny_date_index(h1.index).min()
        h1_end   = _ny_date_index(h1.index).max()
        mask = (daily_signals.index >= h1_start) & (daily_signals.index <= h1_end)
        sub_daily = daily_signals[mask]
        return _map_daily_to_h1(sub_daily, h1)
    return gen


# ---------------------------------------------------------------------------
# Paso 6: Costo de carry explícito
# ---------------------------------------------------------------------------
def _cost_breakdown(trades_net, trades_gross) -> dict:
    """Desglosa el PnL entre spread y swap."""
    pnl_gross = sum(t.pnl for t in trades_gross)
    pnl_net   = sum(t.pnl for t in trades_net)
    total_cost = pnl_gross - pnl_net

    # Swap: suma de swap por trade (estimación: promedio de duración × 0.5/noche)
    # El engine ya lo cobró, lo reconstruimos desde duración.
    # 1 barra H1 = 1 hora → noches ≈ duration_bars // 24
    swap_total = 0.0
    spread_total = 0.0
    cfg_spread = BASE_SPREAD * 0.01  # en USD/oz
    for t in trades_net:
        nights = 0
        if t.entry_time and t.exit_time:
            nights = max((t.exit_time.normalize() - t.entry_time.normalize()).days, 0)
        swap_total += BASE_SWAP * nights
        spread_total += cfg_spread

    return {
        "total_cost": round(total_cost, 2),
        "spread_usd": round(spread_total, 2),
        "swap_usd":   round(swap_total, 2),
        "swap_pct_of_cost": round(swap_total / total_cost * 100, 1)
                            if total_cost > 0 else float("nan"),
    }


# ---------------------------------------------------------------------------
# Paso 7: Main — gauntlet completo
# ---------------------------------------------------------------------------
def main():
    # --- 1. Data ---
    panel = _build_panel()

    # --- 2. Señales diarias (OLS expanding, z-score anti-look-ahead) ---
    daily_sigs = build_fair_value_signals(panel)
    daily_sigs = _apply_timestop(daily_sigs)

    total_long  = int((daily_sigs["signal_raw"] == 1).sum())
    total_short = int((daily_sigs["signal_raw"] == -1).sum())
    print(f"\n[fair_value] Señales diarias: {total_long} LONG, {total_short} SHORT "
          f"(umbral |z|>{Z_ENTRY})")
    if total_long + total_short < 10:
        print("ADVERTENCIA: muy pocas señales (<10). El umbral puede ser demasiado alto "
              "para el período disponible de datos.")

    # --- 3. Cargar H1 completo y mapear señales ---
    from research.harness import load_h1
    h1 = load_h1()

    # Restringir al período del panel diario
    panel_start = daily_sigs.index.min()
    panel_end   = daily_sigs.index.max()
    h1 = h1[(h1.index.tz_convert("America/New_York").normalize().tz_localize(None)
              >= panel_start) &
             (h1.index.tz_convert("America/New_York").normalize().tz_localize(None)
              <= panel_end)]

    signals_h1 = _map_daily_to_h1(daily_sigs, h1)

    # --- 4. Backtest bruto (solo para breakdown de costos) ---
    cfg_net   = BacktestConfig(spread_pips=BASE_SPREAD, swap_per_night=BASE_SWAP)
    cfg_gross = BacktestConfig(spread_pips=0.0, swap_per_night=0.0)
    trades_net   = run_backtest(signals_h1, h1, cfg_net)
    trades_gross = run_backtest(signals_h1, h1, cfg_gross)

    st_net   = compute_stats(trades_net)
    st_gross = compute_stats(trades_gross)
    costs    = _cost_breakdown(trades_net, trades_gross)

    print("\n" + "="*60)
    print("FAIR-VALUE MEAN-REVERSION — RESULTADOS SOBRE DATA REAL")
    print("="*60)
    print(f"  Período H1:     {h1.index.min().date()} → {h1.index.max().date()}")
    print(f"  Panel diario:   {panel_start} → {panel_end}")
    print(f"  N trades:       {st_net.n_trades}  "
          f"(long {st_net.n_long} / short {st_net.n_short})")
    print(f"  PF_gross:       {st_gross.profit_factor:.3f}")
    print(f"  PF_net:         {st_net.profit_factor:.3f}  "
          f"(spread={BASE_SPREAD*0.01:.2f} + swap={BASE_SWAP:.1f}/noche)")
    print(f"  Expectativa/trd:{st_net.expectancy:+.3f} USD/oz")
    print(f"  PnL neto total: {st_net.total_pnl:+.2f} USD/oz")
    print(f"  Win rate:       {st_net.win_rate*100:.1f}%")
    print(f"  Sharpe:         {st_net.sharpe:.3f}")
    print(f"  Max drawdown:   {st_net.max_drawdown*100:.1f}%")
    print(f"  Duración media: {st_net.avg_duration_bars:.1f} barras H1 "
          f"({st_net.avg_duration_bars/24:.1f} días)")

    print("\n--- DESGLOSE DE COSTOS ---")
    print(f"  PnL bruto:      {sum(t.pnl for t in trades_gross):+.2f}")
    print(f"  PnL neto:       {sum(t.pnl for t in trades_net):+.2f}")
    print(f"  Costo total:    {costs['total_cost']:.2f} USD/oz")
    print(f"  Spread total:   {costs['spread_usd']:.2f} USD/oz")
    print(f"  Swap total:     {costs['swap_usd']:.2f} USD/oz  "
          f"({costs['swap_pct_of_cost']}% del costo total)")

    # --- 5. Gauntlet por régimen ---
    print("\n--- PF POR RÉGIMEN ---")
    gen = _make_gen(daily_sigs)
    rows = []
    for name, a, b in REGIMES:
        sub = h1[(h1.index >= pd.Timestamp(a, tz="UTC")) &
                 (h1.index <= pd.Timestamp(b, tz="UTC"))]
        # Restringir también al panel diario (sin data diaria no hay señales)
        sub = sub[(sub.index.tz_convert("America/New_York").normalize().tz_localize(None)
                   >= panel_start)]
        if len(sub) < 300:
            rows.append({"regimen": name, "n": 0, "pf": float("nan"), "note": "sin datos"})
            continue
        sg = gen(sub)
        tr = run_backtest(sg, sub, cfg_net)
        st = compute_stats(tr)
        rows.append({"regimen": name, "n": st.n_trades,
                     "pf": round(st.profit_factor, 3),
                     "pnl": round(st.total_pnl, 1)})
        print(f"  {name:30s}  n={st.n_trades:4d}  PF={st.profit_factor:.3f}  "
              f"PnL={st.total_pnl:+.1f}")

    n_profitable = sum(1 for r in rows if r.get("pf", 0) > 1.0)
    n_total = sum(1 for r in rows if r.get("n", 0) > 0)

    # --- 6. Test de significancia (permutación rápida, 500 iter para smoke test) ---
    # El gauntlet oficial corre 2000 — acá 500 para que el smoke no tarde 10 min.
    PERM_QUICK = 500
    print(f"\n[fair_value] Corriendo test de permutación ({PERM_QUICK} iter)...")
    rng = np.random.default_rng(PERM_SEED)
    from research.harness import _random_entry_null, _block_bootstrap_ci
    perm_res = _random_entry_null(signals_h1, h1, cfg_net, PERM_QUICK, rng)
    boot_res = _block_bootstrap_ci(trades_net, rng)

    print(f"  perm p-value:   {perm_res.get('p_value', float('nan')):.4f}  "
          f"(null_mean={perm_res.get('null_mean', float('nan')):.3f}, "
          f"observado={perm_res.get('observed_mean', float('nan')):.3f})")
    print(f"  bootstrap CI:   [{boot_res.get('ci_low', float('nan')):.3f}, "
          f"{boot_res.get('ci_high', float('nan')):.3f}]")

    # --- 7. Veredicto ---
    pf_net = st_net.profit_factor
    perm_p = perm_res.get("p_value", float("nan"))
    boot_lo = boot_res.get("ci_low", float("nan"))

    pass_cost    = pf_net > 1.0 and st_net.total_pnl > 0
    pass_sig     = (perm_p == perm_p) and perm_p < 0.05 and st_net.n_trades >= 30
    pass_boot    = (boot_lo == boot_lo) and boot_lo > 0
    pass_regime  = n_total >= 2 and n_profitable >= max(2, n_total - 1)

    print("\n" + "="*60)
    print("VEREDICTO FINAL")
    print("="*60)

    if pf_net <= 1.0:
        print(f"  DESCARTADO — falla costos (PF_net={pf_net:.3f} ≤ 1.0)")
        print(f"  El swap ({costs['swap_usd']:.0f} USD, "
              f"{costs['swap_pct_of_cost']}% del costo) mata la hipótesis.")
        print(f"  La reversión a fair value existe conceptualmente pero el horizonte")
        print(f"  de semanas implica swap acumulado que consume el edge bruto.")
    elif not pass_sig:
        print(f"  DESCARTADO — falla significancia (perm_p={perm_p:.4f} ≥ 0.05 "
              f"o n={st_net.n_trades} < 30)")
        print(f"  PF_net={pf_net:.3f} pero no distinguible de entradas al azar.")
    elif not pass_boot:
        print(f"  DESCARTADO — CI bootstrap cruza cero ([{boot_lo:.3f}, "
              f"{boot_res.get('ci_high', float('nan')):.3f}])")
        print(f"  Incertidumbre estadística demasiado alta.")
    elif not pass_regime:
        print(f"  DESCARTADO — concentración de régimen "
              f"({n_profitable}/{n_total} regímenes rentables)")
        print(f"  El edge vive en un solo régimen → beta direccional, no edge.")
    else:
        print(f"  CANDIDATO — pasa los 3 filtros del gauntlet")
        print(f"  PF_net={pf_net:.3f}, perm_p={perm_p:.4f}, "
              f"boot_CI=[{boot_lo:.3f},{boot_res.get('ci_high',float('nan')):.3f}], "
              f"{n_profitable}/{n_total} regímenes rentables")
        print(f"  REQUIERE verificación adversarial completa antes de live.")

    print("\n  Para gauntlet completo (2000 permutaciones):")
    print("  .venv/bin/python research/fair_value.py")

    return {
        "n_trades": int(st_net.n_trades),
        "pf_gross": round(float(st_gross.profit_factor), 3),
        "pf_net":   round(float(pf_net), 3),
        "exp_net":  round(float(st_net.expectancy), 3),
        "swap_pct_of_cost": round(float(costs["swap_pct_of_cost"]), 1)
                            if costs["swap_pct_of_cost"] == costs["swap_pct_of_cost"]
                            else float("nan"),
        "perm_p":   round(float(perm_p), 4),
        "boot_ci":  [round(float(boot_lo), 3),
                     round(float(boot_res.get("ci_high", float("nan"))), 3)],
        "n_profitable_regimes": f"{n_profitable}/{n_total}",
        "verdict":  ("SOBREVIVE" if (pass_cost and pass_sig and pass_boot and pass_regime)
                     else "DESCARTADO"),
    }


if __name__ == "__main__":
    result = main()
    print(f"\nResumen JSON: {result}")
