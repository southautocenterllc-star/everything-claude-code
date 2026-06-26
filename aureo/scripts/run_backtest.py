"""Runner del backtest AUREO sobre data Dukascopy.

Usage:
    python -m scripts.run_backtest
    python -m scripts.run_backtest --from 2024-01-01 --to 2025-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import compute_stats, equity_curve, print_stats
from strategies.aureo_pine import PineParams, generate_signals

DATA_PATH = ROOT / "data" / "dukascopy" / "XAUUSD-H1-all.parquet"
REPORTS_DIR = ROOT / "reports"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", default=None,
                        help="Fecha inicio YYYY-MM-DD")
    parser.add_argument("--to", dest="end", default=None,
                        help="Fecha fin YYYY-MM-DD")
    parser.add_argument("--spread", type=float, default=35.0,
                        help="Spread en 'pips' del engine (pip=0.01 USD/oz). "
                             "35 = 0.35 USD/oz reales (típico XAU). OJO: pasar "
                             "0.35 acá da 0.0035 USD/oz (100x muy chico) — bug "
                             "histórico; el default ahora es 35.")
    parser.add_argument("--swap", type=float, default=0.0,
                        help="Swap overnight en USD/onza/noche (costo de financiación; "
                             "en swing de XAU suele pesar más que el spread)")
    args = parser.parse_args()

    if not DATA_PATH.exists():
        print(f"ERROR: falta {DATA_PATH}. Corré scripts/download_dukascopy.py primero.")
        return 1

    df = pd.read_parquet(DATA_PATH)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    if args.start:
        df = df[df.index >= pd.Timestamp(args.start, tz="UTC")]
    if args.end:
        df = df[df.index <= pd.Timestamp(args.end, tz="UTC")]

    print(f"Periodo backtest: {df.index.min()} → {df.index.max()}  ({len(df)} velas H1)")

    params = PineParams()
    signals = generate_signals(df, params)
    n_long = int((signals["signal"] == 1).sum())
    n_short = int((signals["signal"] == -1).sum())
    print(f"Señales generadas: {n_long} long + {n_short} short (pre-filtro pyramiding)")

    cfg = BacktestConfig(spread_pips=args.spread, swap_per_night=args.swap)
    trades = run_backtest(signals, df, cfg)
    print(f"Trades ejecutados (post pyramiding=0): {len(trades)}")

    stats = compute_stats(trades, init_capital=cfg.init_capital)
    print()
    print("Resultado AUREO Pine (parámetros 'balanced'):")
    print_stats(stats)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    eq = equity_curve(trades, cfg.init_capital)

    fig, ax = plt.subplots(figsize=(12, 5))
    eq.plot(ax=ax, lw=1.2)
    ax.set_title(f"AUREO Pine — equity curve   XAUUSD H1   {df.index.min().date()} → {df.index.max().date()}")
    ax.set_ylabel("Equity (USD)")
    ax.grid(alpha=0.3)
    eq_png = REPORTS_DIR / "equity.png"
    fig.tight_layout()
    fig.savefig(eq_png, dpi=130)
    print(f"\nGuardado: {eq_png}")

    trades_csv = REPORTS_DIR / "trades.csv"
    pd.DataFrame([{
        "entry_time": t.entry_time,
        "exit_time": t.exit_time,
        "direction": "LONG" if t.direction == 1 else "SHORT",
        "entry": t.entry_price,
        "exit": t.exit_price,
        "sl": t.sl,
        "tp1": t.tp1,
        "tp2": t.tp2,
        "duration_bars": t.duration_bars,
        "outcome": t.outcome,
        "pnl": t.pnl,
    } for t in trades]).to_csv(trades_csv, index=False)
    print(f"Guardado: {trades_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
