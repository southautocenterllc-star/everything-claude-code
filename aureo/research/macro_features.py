"""Panel de features macro/exógenas para el research de edge de oro.

El oro responde a tasas reales y al dólar, no a su propio precio (la batería de
patrones de precio H1 dio cero edge). Acá se arma un panel DIARIO de:
  - oro spot (resample H1→diario de nuestra data Dukascopy, desde 2008)
  - ^TNX  : yield nominal 10Y (%)
  - DXY   : índice dólar (DX-Y.NYB)
  - SI=F  : plata → ratio oro/plata
  - TIP   : ETF iShares TIPS (proxy INVERSO de yields reales; sube cuando el
            real yield baja). Upgrade futuro: DFII10/breakevens vía FRED (key).
  - ^VIX, CL=F : contexto risk/inflación

Diagnóstico (el GATE antes de construir estrategias): ¿la relación es
PREDICTIVA (lag → tradeable) o solo contemporánea (no tradeable)?

Fuente: Yahoo chart API (requests, sin yfinance). Todo cae si Yahoo falla.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLD_PARQUET = ROOT / "data" / "dukascopy" / "XAUUSD-H1-all.parquet"
_UA = {"User-Agent": "Mozilla/5.0 AUREO macro_features"}


NY = "America/New_York"


def _ny_date_index(idx_utc: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Clave de alineación = FECHA DE TRADING en NY (tz-naive). Harmoniza
    futuros (epoch ~medianoche UTC) y cash/ETF (epoch ~14:30 UTC) al mismo día."""
    return idx_utc.tz_convert(NY).normalize().tz_localize(None)


def fetch_yahoo_daily(symbol: str, range_: str = "10y") -> pd.Series:
    # OJO: range="max" devuelve datos MENSUALES en Yahoo (gap ~31d). "10y" da
    # diarios (~2500 puntos). Para más historia, encadenar ventanas con period1/2.
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval": "1d", "range": range_}, headers=_UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    idx = _ny_date_index(pd.to_datetime(res["timestamp"], unit="s", utc=True))
    close = pd.Series(res["indicators"]["quote"][0]["close"], index=idx, name=symbol)
    return close[~close.index.duplicated(keep="last")].dropna()


def gold_daily() -> pd.Series:
    """Oro spot Dukascopy H1 → cierre diario por FECHA DE TRADING NY."""
    df = pd.read_parquet(GOLD_PARQUET)
    df.index = pd.to_datetime(df.index, utc=True)
    key = _ny_date_index(df.index)
    g = df["close"].groupby(key).last()
    g.index = pd.DatetimeIndex(g.index)
    g.name = "gold_spot"
    return g.sort_index()


def build_macro_panel() -> pd.DataFrame:
    """Panel diario alineado por fecha NY. Gold de referencia = GC=F (misma
    convención que las macro); el spot Dukascopy se incluye para el gate de
    sanidad. Levels alineados por inner-join (sin ffill cross-fuente)."""
    series = {"gold": fetch_yahoo_daily("GC=F"), "gold_spot": gold_daily()}
    for sym, alias in [("^TNX", "y10"), ("DX-Y.NYB", "dxy"), ("SI=F", "silver"),
                       ("TIP", "tip"), ("^VIX", "vix"), ("CL=F", "oil")]:
        try:
            series[alias] = fetch_yahoo_daily(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  WARN {sym}: {str(e)[:60]}")
    panel = pd.concat(series, axis=1, sort=True).sort_index()
    # macro levels: ffill huecos cortos DENTRO de su propia historia (festivos);
    # luego exigir gold presente.
    panel = panel.ffill(limit=3).dropna(subset=["gold"])
    # Quitar fines de semana: el ffill arrastra el viernes a sáb/dom con ret=0 en
    # TODAS las series → filas-artefacto que sesgan baseline y correlaciones.
    panel = panel[panel.index.dayofweek < 5]
    panel["gold_silver"] = panel["gold"] / panel["silver"]
    return panel


def assert_alignment(panel: pd.DataFrame) -> float:
    """Gate duro: dos series del MISMO oro deben correlacionar >0.9. Si no, la
    alineación está rota y el diagnóstico no es interpretable."""
    R = pd.DataFrame({"a": panel["gold"].pct_change(),
                      "b": panel["gold_spot"].pct_change()}).dropna()
    c = float(R["a"].corr(R["b"]))
    # Umbral 0.65: es CROSS-INSTRUMENTO (futuro GC=F vs spot Dukascopy), con
    # cutoff diario distinto → no llega a 0.95 de same-source, pero >0.65 prueba
    # que las fechas casan. (<0.3 era el bug de alineación ya resuelto.)
    status = "OK" if c > 0.65 else "ROTA"
    print(f"[GATE alineación] corr(GC=F, spot) = {c:+.3f}  ({status}, n={len(R)})")
    if c <= 0.65:
        raise SystemExit("Alineación ROTA — abortando (corr oro-oro <= 0.65).")
    return c


def diagnostic(panel: pd.DataFrame) -> None:
    assert_alignment(panel)
    p = panel.drop(columns=["gold_spot"]).dropna().copy()
    print(f"Panel: {len(p)} días  {p.index.min().date()} → {p.index.max().date()}")
    print(f"Features: {[c for c in p.columns]}\n")

    gret = p["gold"].pct_change()
    chg = {
        "d_y10": p["y10"].diff(),                       # Δ yield nominal
        "d_dxy": p["dxy"].pct_change(),                 # %Δ dólar
        "d_tip": p["tip"].pct_change(),                 # %Δ TIP (real-yield inv.)
        "d_goldsilver": p["gold_silver"].pct_change(),
        "d_vix": p["vix"].pct_change(),
        "d_oil": p["oil"].pct_change(),
    }

    print("== Correlación CONTEMPORÁNEA (gold_ret[t] vs feature_change[t]) ==")
    print("   (signo esperado: y10 neg, dxy neg, tip POS, goldsilver pos)")
    for k, v in chg.items():
        c = gret.corr(v)
        print(f"   {k:14s} {c:+.3f}")

    print("\n== LEAD-LAG: corr(gold_ret[t], feature_change[t-k])  (k>0 = predictivo) ==")
    print("   un |corr| no-trivial en k=1..3 ⇒ hay señal TRADEABLE")
    header = "   feature        " + "".join(f"  k={k:+d}" for k in range(-2, 4))
    print(header)
    for k, v in chg.items():
        row = f"   {k:14s}"
        for lag in range(-2, 4):  # negativo = feature adelanta? no: feature[t-lag]
            c = gret.corr(v.shift(lag))
            row += f" {c:+.2f}"
        print(row)

    print("\n== Test predictivo simple: momentum macro (trailing 5d) → dirección oro mañana ==")
    base_up = (gret > 0).mean()
    fwd = p["gold"].pct_change().shift(-1)  # retorno de oro al día siguiente
    tests = {
        "tip_mom>0 (real yld baja) → LONG": np.sign(p["tip"].pct_change(5)),
        "dxy_mom<0 (dólar débil)   → LONG": -np.sign(p["dxy"].pct_change(5)),
        "y10_mom<0 (yield baja)    → LONG": -np.sign(p["y10"].diff(5)),
        "goldsilver_mom>0          → LONG": np.sign(p["gold_silver"].pct_change(5)),
    }
    print(f"   baseline: P(oro sube mañana) = {base_up:.3f}, ret medio = {fwd.mean()*1e4:+.1f} bps")
    for name, sig in tests.items():
        m = sig > 0
        n = int(m.sum())
        wr = float((fwd[m] > 0).mean()) if n else float("nan")
        mr = float(fwd[m].mean() * 1e4) if n else float("nan")
        # contraparte short
        ms = sig < 0
        wrs = float((fwd[ms] < 0).mean()) if ms.sum() else float("nan")
        print(f"   {name:40s} n={n:5d} long_WR={wr:.3f} ret={mr:+.1f}bps | short_WR={wrs:.3f}")


def _yh_h1(sym: str, range_: str = "730d") -> pd.Series:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"interval": "1h", "range": range_}, headers=_UA, timeout=25)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).floor("h")
    c = pd.Series(res["indicators"]["quote"][0]["close"], index=idx).dropna()
    return c[~c.index.duplicated(keep="last")]


def intraday_leadlag() -> None:
    """¿Intradía el DXY/yield ADELANTA al oro por horas? (lo que el diario no ve).
    Alinea oro H1 (Dukascopy) vs DXY/^TNX H1 (Yahoo) por hora UTC (.floor('h') en
    ambos) y mide lead-lag. k>0 = el feature adelanta al oro = TRADEABLE."""
    g = pd.read_parquet(GOLD_PARQUET)
    g.index = pd.to_datetime(g.index, utc=True)
    gold = g["close"].copy()
    gold.index = gold.index.floor("h")
    df = pd.concat({"gold": gold, "dxy": _yh_h1("DX-Y.NYB"), "y10": _yh_h1("^TNX")},
                   axis=1, sort=True).dropna()
    print(f"\n== Intradía H1 lead-lag (oro vs DXY/yield) — n={len(df)}, "
          f"{df.index.min()} → {df.index.max()} ==")
    gret = df.gold.pct_change()
    for name, feat in [("dxy", df.dxy.pct_change()), ("y10", df.y10.diff())]:
        row = f"   {name:5s}"
        for k in range(0, 5):
            row += f"  k={k}:{gret.corr(feat.shift(k)):+.3f}"
        print(row + "   (k=0 contemporáneo; k≥1 = feature adelanta = tradeable)")


if __name__ == "__main__":
    panel = build_macro_panel()
    diagnostic(panel)
    try:
        intraday_leadlag()
    except Exception as e:  # noqa: BLE001
        print(f"\n[intraday_leadlag no disponible: {e}]")
