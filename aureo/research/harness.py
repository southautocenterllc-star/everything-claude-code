"""Harness de research de edges para AUREO — el GAUNTLET.

Filosofía (lección dura del proyecto): un número de backtest no vale nada hasta
que sobrevive a TRES filtros, en este orden, sobre data real con costos reales:

  1. COSTOS   — spread 0.35 USD/oz + swap 0.5 USD/oz/noche (el swap domina en
                swing de XAU). Si no gana NETO, se descarta. Punto.
  2. SIGNIFICANCIA — ¿la LÓGICA de la señal le gana al azar? Null por entradas
                aleatorias: mismas N entradas, misma proporción long/short,
                MISMAS reglas de salida y costos, pero en barras al azar. Si la
                señal no le gana a tirar dardos con el mismo exit, no hay edge,
                hay exposición. (+ block-bootstrap del PnL por trade como 2º lente.)
  3. ROBUSTEZ POR RÉGIMEN — el edge no puede vivir en UN solo régimen. Se mide
                PF por bloque macro y por año; si el 100% del profit viene del
                toro 2023-26, es direccionalidad disfrazada (eso mató a la
                'balanced').

Este módulo NO inventa estrategias; ejecuta el gauntlet sobre cualquier función
que produzca el DF de señales del engine (signal/sl/tp1/tp2). Las hipótesis
viven en research/hypotheses.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestConfig, Trade, run_backtest
from backtest.metrics import compute_stats

DATA_PATH = ROOT / "data" / "dukascopy" / "XAUUSD-H1-all.parquet"

# Escenario de costos BASE — no negociable, todo backtest corre con esto.
# OJO UNIDADES (bug cazado por el panel adversarial 2026-06-03): el engine cobra
# spread_pips * pip_value, con pip_value=0.01. Un spread realista de XAU es
# ~0.35 USD/oz ⇒ spread_pips=35 (NO 0.35, que sería 0.0035 USD/oz, 100x menos).
# El backtest oficial del 2026-06-02 (PF balanced 0.874) usó 35 correctamente;
# este harness lo tenía mal (0.35) y daba números optimistas. Ya corregido.
BASE_SPREAD = 35.0       # spread_pips → 35 * 0.01 = 0.35 USD/oz reales
BASE_SWAP = 0.5          # USD/oz/noche

# Regímenes macro del oro (bloques amplios para no sobre-ajustar el etiquetado).
REGIMES = [
    ("2008-2012_bull_gfc", "2008-01-01", "2012-09-30"),
    ("2012-2015_bear",     "2012-10-01", "2015-12-31"),
    ("2016-2018_range",    "2016-01-01", "2018-12-31"),
    ("2019-2022_bull_covid", "2019-01-01", "2022-12-31"),
    ("2023-2026_bull",     "2023-01-01", "2026-12-31"),
]

PERM_ITERS = 2000
PERM_SEED = 42


def load_h1(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    df = pd.read_parquet(DATA_PATH)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]
    return df


def _cfg(spread: float = BASE_SPREAD, swap: float = BASE_SWAP) -> BacktestConfig:
    return BacktestConfig(spread_pips=spread, swap_per_night=swap)


def _pf(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    return compute_stats(trades).profit_factor


def _mean_pnl(trades: list[Trade]) -> float:
    return float(np.mean([t.pnl for t in trades])) if trades else 0.0


def _random_entry_null(signals: pd.DataFrame, df: pd.DataFrame, cfg: BacktestConfig,
                       n_iter: int, rng: np.random.Generator) -> dict:
    """Null por entradas aleatorias. Conserva: nº de entradas pre-pyramiding por
    dirección y las reglas de salida (SL/TP se recalculan en la barra random con
    el MISMO offset relativo que tenía la señal real, para no regalar/quitar R).

    Construye, por iteración, un DF de señales con las mismas entradas pero en
    barras al azar (donde sl/tp1/tp2 estén definidos), lo corre por el engine con
    costos y guarda el mean PnL por trade. p = P(null_mean >= observado)."""
    sig = signals["signal"].to_numpy()
    long_idx = np.flatnonzero(sig == 1)
    short_idx = np.flatnonzero(sig == -1)
    n_long, n_short = len(long_idx), len(short_idx)

    # barras elegibles = donde la estrategia define exits (atr válido)
    valid = signals[["sl", "tp1", "tp2"]].notna().all(axis=1).to_numpy()
    eligible = np.flatnonzero(valid)
    if len(eligible) < (n_long + n_short) or (n_long + n_short) == 0:
        return {"p_value": float("nan"), "null_mean": float("nan"), "n_iter": 0}

    # offsets relativos (en múltiplos de precio) de sl/tp respecto al close, para
    # replicar la geometría del trade en la barra random.
    close = signals["close"].to_numpy() if "close" in signals else df["close"].to_numpy()
    sl_off = (signals["sl"].to_numpy() - close)
    tp1_off = (signals["tp1"].to_numpy() - close)
    tp2_off = (signals["tp2"].to_numpy() - close)
    # offset "típico" por dirección: mediana de los offsets reales de esa dirección
    def _typ(off, idx):
        return float(np.nanmedian(off[idx])) if len(idx) else np.nan
    geom = {
        1: (_typ(sl_off, long_idx), _typ(tp1_off, long_idx), _typ(tp2_off, long_idx)),
        -1: (_typ(sl_off, short_idx), _typ(tp1_off, short_idx), _typ(tp2_off, short_idx)),
    }

    observed = _mean_pnl(run_backtest(signals, df, cfg))
    cl = df["close"].to_numpy()
    null_means = np.empty(n_iter)
    base = pd.DataFrame(index=signals.index)
    for k in range(n_iter):
        s = np.zeros(len(sig), dtype=int)
        sl = np.full(len(sig), np.nan)
        tp1 = np.full(len(sig), np.nan)
        tp2 = np.full(len(sig), np.nan)
        picks = rng.choice(eligible, size=n_long + n_short, replace=False)
        for j, p in enumerate(picks):
            d = 1 if j < n_long else -1
            s[p] = d
            g = geom[d]
            sl[p] = cl[p] + g[0]
            tp1[p] = cl[p] + g[1]
            tp2[p] = cl[p] + g[2]
        rnd = base.assign(signal=s, sl=sl, tp1=tp1, tp2=tp2)
        null_means[k] = _mean_pnl(run_backtest(rnd, df, cfg))

    count = int(np.sum(null_means >= observed))
    p = (1.0 + count) / (1.0 + n_iter)
    return {"p_value": float(p), "null_mean": float(np.mean(null_means)),
            "observed_mean": float(observed), "n_iter": n_iter}


def _block_bootstrap_ci(trades: list[Trade], rng: np.random.Generator,
                        n_iter: int = 2000, block: int = 10,
                        alpha: float = 0.05) -> dict:
    """IC del mean PnL por trade vía block bootstrap (preserva autocorrelación
    de rachas). Si el IC inferior es >0, el mean es robustamente positivo."""
    pnls = np.array([t.pnl for t in trades])
    n = len(pnls)
    if n < 20:
        return {"ci_low": float("nan"), "ci_high": float("nan"), "mean": _mean_pnl(trades)}
    n_blocks = int(np.ceil(n / block))
    means = np.empty(n_iter)
    for k in range(n_iter):
        starts = rng.integers(0, n, size=n_blocks)
        sample = np.concatenate([np.take(pnls, range(s, s + block), mode="wrap")
                                 for s in starts])[:n]
        means[k] = sample.mean()
    return {"ci_low": float(np.quantile(means, alpha / 2)),
            "ci_high": float(np.quantile(means, 1 - alpha / 2)),
            "mean": float(pnls.mean())}


def _regime_table(gen, full: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """PF y PnL por régimen macro. `gen` es la función generadora de señales."""
    rows = []
    for name, a, b in REGIMES:
        sub = full[(full.index >= pd.Timestamp(a, tz="UTC")) &
                   (full.index <= pd.Timestamp(b, tz="UTC"))]
        if len(sub) < 300:
            continue
        sg = gen(sub)
        tr = run_backtest(sg, sub, cfg)
        st = compute_stats(tr)
        # pnl_r: PnL en RETORNO por unidad (pnl/precio_entrada), invariante a la
        # escala de precio. Medir concentración en USD crudos sesga a la era de
        # precio/ATR alto (oro $800→$2400) → falsos negativos. (PF ya es
        # scale-invariante, no se toca.)
        pnl_r = float(sum(t.pnl / t.entry_price for t in tr if t.entry_price))
        rows.append({"regimen": name, "n_trades": st.n_trades,
                     "pf": round(st.profit_factor, 3),
                     "pnl": round(st.total_pnl, 1),
                     "pnl_r": round(pnl_r, 4),
                     "win_rate": round(st.win_rate, 3)})
    return pd.DataFrame(rows)


@dataclass
class GauntletResult:
    name: str
    n_trades: int
    pf_net: float
    pf_gross: float
    expectancy_net: float
    total_pnl_net: float
    sharpe: float
    max_dd: float
    perm_p: float
    boot_ci_low: float
    boot_ci_high: float
    regimes: pd.DataFrame
    n_regimes_profitable: int
    n_regimes_total: int
    pct_pnl_top_regime: float
    verdict: str = ""
    notes: list[str] = field(default_factory=list)


def run_gauntlet(name: str, gen, full: pd.DataFrame,
                 cfg: BacktestConfig | None = None,
                 perm_iters: int = PERM_ITERS) -> GauntletResult:
    """Corre el gauntlet completo sobre una hipótesis. `gen(df)->signals_df`."""
    cfg = cfg or _cfg()
    rng = np.random.default_rng(PERM_SEED)

    signals = gen(full)
    trades_net = run_backtest(signals, full, cfg)
    trades_gross = run_backtest(signals, full, _cfg(spread=0.0, swap=0.0))
    st = compute_stats(trades_net)
    st_gross = compute_stats(trades_gross)

    perm = _random_entry_null(signals, full, cfg, perm_iters, rng) \
        if (trades_net and perm_iters > 0) else {"p_value": float("nan")}
    boot = _block_bootstrap_ci(trades_net, rng) if trades_net else \
        {"ci_low": float("nan"), "ci_high": float("nan")}

    reg = _regime_table(gen, full, cfg)
    n_prof = int((reg["pf"] > 1.0).sum()) if len(reg) else 0
    n_tot = len(reg)
    # concentración medida en R-múltiplos (invariante a escala de precio)
    if len(reg) and reg["pnl_r"].sum() > 0:
        pos = reg[reg["pnl_r"] > 0]["pnl_r"]
        pct_top = float(pos.max() / pos.sum()) if len(pos) else float("nan")
    else:
        pct_top = float("nan")

    res = GauntletResult(
        name=name, n_trades=st.n_trades,
        pf_net=round(st.profit_factor, 3), pf_gross=round(st_gross.profit_factor, 3),
        expectancy_net=round(st.expectancy, 3), total_pnl_net=round(st.total_pnl, 1),
        sharpe=round(st.sharpe, 3), max_dd=round(st.max_drawdown, 3),
        perm_p=round(perm.get("p_value", float("nan")), 4),
        boot_ci_low=round(boot.get("ci_low", float("nan")), 3),
        boot_ci_high=round(boot.get("ci_high", float("nan")), 3),
        regimes=reg, n_regimes_profitable=n_prof, n_regimes_total=n_tot,
        pct_pnl_top_regime=round(pct_top, 3) if pct_top == pct_top else float("nan"),
    )
    _judge(res)
    return res


def _judge(r: GauntletResult) -> None:
    """Aplica los 3 filtros y emite veredicto + notas. Conservador: PASA solo si
    pasa LOS TRES."""
    notes = []
    pass_cost = r.pf_net > 1.0 and r.total_pnl_net > 0
    pass_sig = (r.perm_p == r.perm_p) and r.perm_p < 0.05 and r.n_trades >= 50
    pass_boot = (r.boot_ci_low == r.boot_ci_low) and r.boot_ci_low > 0
    pass_regime = (r.n_regimes_total >= 3 and
                   r.n_regimes_profitable >= max(3, r.n_regimes_total - 1) and
                   (r.pct_pnl_top_regime != r.pct_pnl_top_regime or r.pct_pnl_top_regime < 0.6))

    if not pass_cost:
        notes.append(f"FALLA COSTOS: PF_net={r.pf_net} (gross {r.pf_gross})")
    if not pass_sig:
        notes.append(f"FALLA/DÉBIL SIGNIFICANCIA: perm_p={r.perm_p} n={r.n_trades}")
    if not pass_boot:
        notes.append(f"BOOT CI cruza 0: [{r.boot_ci_low},{r.boot_ci_high}]")
    if not pass_regime:
        notes.append(f"CONCENTRACIÓN RÉGIMEN: {r.n_regimes_profitable}/{r.n_regimes_total} "
                     f"rentables, top={r.pct_pnl_top_regime}")

    if pass_cost and pass_sig and pass_boot and pass_regime:
        r.verdict = "CANDIDATO (pasa los 3 filtros — requiere verificación adversarial)"
    elif pass_cost and (pass_sig or pass_boot):
        r.verdict = "PROMETEDOR (pasa costos + 1 lente; revisar)"
    else:
        r.verdict = "DESCARTADO"
    r.notes = notes


def summarize(results: list[GauntletResult]) -> pd.DataFrame:
    return pd.DataFrame([{
        "hipotesis": r.name, "n": r.n_trades,
        "PF_net": r.pf_net, "PF_gross": r.pf_gross,
        "exp_net": r.expectancy_net, "perm_p": r.perm_p,
        "boot_lo": r.boot_ci_low, "reg_ok": f"{r.n_regimes_profitable}/{r.n_regimes_total}",
        "top_reg%": r.pct_pnl_top_regime, "veredicto": r.verdict,
    } for r in results]).sort_values("PF_net", ascending=False).reset_index(drop=True)
