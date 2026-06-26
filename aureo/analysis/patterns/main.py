"""
main.py
Orquestador del Market Pattern Agent.

Uso:
  python main.py                 # corrida normal (usa cache si existe)
  python main.py --refresh       # vuelve a descargar los precios
  python main.py --no-ai         # omite la capa de interpretacion IA
"""
import argparse
import os

import pandas as pd

import config
import data_loader
import indicators
import patterns
import backtest
import ai_layer
import report


def _load_events(path: str = "events.csv"):
    """Devuelve un DataFrame con columnas 'date' (Timestamp) y 'event' (tipo)."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=["date", "event"])
    ev = pd.read_csv(path)
    ev["date"] = pd.to_datetime(ev["date"])
    if "event" not in ev.columns:
        ev["event"] = "evento"
    return ev


def run(refresh: bool = False, use_ai: bool = True) -> dict:
    print("1/5  Cargando precios...")
    raw = data_loader.load_prices(refresh=refresh)

    print("2/5  Calculando indicadores y patrones...")
    data_ind = {}
    assets = {}
    events = _load_events()

    has_events = events is not None and len(events) > 0
    for name, df in raw.items():
        d = indicators.add_indicators(df)
        d = patterns.technical_signals(d)
        data_ind[name] = d
        assets[name] = {
            "df": d,
            "seasonality": patterns.seasonality(d),
            "signals_eval": backtest.evaluate_signals(d),
            "signals_wf": backtest.evaluate_signals_walkforward(d),
            "event_study": backtest.event_study(d, events["date"]) if has_events else None,
            "event_study_by_type": backtest.event_study_by_type(d, events) if has_events else {},
        }

    print("3/5  Calculando correlacion...")
    corr = backtest.correlation_matrix(data_ind)

    results = {"assets": assets, "correlation": corr}

    print("4/5  Interpretacion IA...")
    findings = _summarize_for_ai(results)
    ai_text = ai_layer.interpret(findings) if use_ai else "[IA omitida con --no-ai]"

    print("5/5  Generando reporte...")
    path = report.build_report(results, ai_text)
    print(f"\nListo. Reporte en: {path}")
    return results


def _summarize_for_ai(results: dict) -> dict:
    """Compacta los hallazgos a JSON liviano para la capa IA."""
    out = {"correlacion": results["correlation"].round(3).to_dict(), "activos": {}}
    for name, r in results["assets"].items():
        seas = r["seasonality"]
        wf = r.get("signals_wf")
        by_type = r.get("event_study_by_type") or {}
        out["activos"][name] = {
            "mejor_mes": seas["best_month"],
            "peor_mes": seas["worst_month"],
            "retorno_medio_por_mes": (seas["by_month"]["mean"] * 100).round(3).to_dict(),
            "backtest_senales": r["signals_eval"].round(4).to_dict(orient="records"),
            "walk_forward": (wf.round(4).to_dict(orient="records")
                             if wf is not None and len(wf) > 0 else None),
            "eventos_por_tipo": {
                t: s.round(4).to_dict(orient="records") for t, s in by_type.items()
            } or None,
        }
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Market Pattern Agent")
    ap.add_argument("--refresh", action="store_true", help="redescargar precios")
    ap.add_argument("--no-ai", action="store_true", help="omitir capa IA")
    args = ap.parse_args()
    run(refresh=args.refresh, use_ai=not args.no_ai)
