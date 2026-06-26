"""Corre la batería de hipótesis por el gauntlet, en dos fases.

Fase 1 (barata, todas): costos + régimen + block-bootstrap, SIN permutación.
        Elimina lo que ni siquiera gana neto o vive de un solo régimen.
Fase 2 (cara, solo supervivientes de costos): null por entradas aleatorias con
        muchas iteraciones para significancia.

Usage:
    .venv/bin/python -m research.run_battery
    .venv/bin/python -m research.run_battery --perm 3000 --from 2008-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research.harness import load_h1, run_gauntlet, summarize
from research.hypotheses import BATTERY

OUT = ROOT / "research" / "output"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--perm", type=int, default=2000, help="iters permutación fase 2")
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    args = ap.parse_args()

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)

    df = load_h1(args.start, args.end)
    print(f"Data: {len(df)} velas H1  {df.index.min().date()} → {df.index.max().date()}")
    print(f"Costos base: spread 0.35 + swap 0.5 USD/oz/noche\n")

    # ---------- Fase 1: screening barato ----------
    print("=" * 90)
    print("FASE 1 — screening (costos + régimen + bootstrap, sin permutación)")
    print("=" * 90)
    phase1 = []
    for name, gen in BATTERY.items():
        r = run_gauntlet(name, gen, df, perm_iters=0)
        phase1.append(r)
        print(f"{name:26s} PF_net={r.pf_net:6.3f} gross={r.pf_gross:6.3f} "
              f"n={r.n_trades:5d} boot_lo={r.boot_ci_low:+7.3f} "
              f"reg={r.n_regimes_profitable}/{r.n_regimes_total} top={r.pct_pnl_top_regime} "
              f"| {r.verdict}")

    # supervivientes de costos (PF_net>1) → fase 2
    survivors = [r for r in phase1 if r.pf_net > 1.0 and r.total_pnl_net > 0]
    print(f"\nSupervivientes de COSTOS (PF_net>1): "
          f"{[r.name for r in survivors] or 'NINGUNO'}")

    # ---------- Fase 2: permutación cara ----------
    final = {r.name: r for r in phase1}
    if survivors:
        print("\n" + "=" * 90)
        print(f"FASE 2 — null por entradas aleatorias ({args.perm} iters) sobre supervivientes")
        print("=" * 90)
        for r0 in survivors:
            r = run_gauntlet(r0.name, BATTERY[r0.name], df, perm_iters=args.perm)
            final[r.name] = r
            print(f"{r.name:26s} perm_p={r.perm_p}  | {r.verdict}")
            for n in r.notes:
                print(f"    - {n}")

    # ---------- Resumen ----------
    results = list(final.values())
    print("\n" + "=" * 90)
    print("RESUMEN")
    print("=" * 90)
    summ = summarize(results)
    print(summ.to_string(index=False))

    OUT.mkdir(parents=True, exist_ok=True)
    summ.to_csv(OUT / "battery_summary.csv", index=False)
    # regímenes de los supervivientes
    with (OUT / "regimes.txt").open("w") as f:
        for r in results:
            if r.pf_net > 1.0:
                f.write(f"\n## {r.name}  (PF_net={r.pf_net})\n")
                f.write(r.regimes.to_string(index=False) + "\n")
    print(f"\nGuardado: {OUT/'battery_summary.csv'}  y  {OUT/'regimes.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
