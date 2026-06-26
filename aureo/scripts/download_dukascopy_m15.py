"""Download XAU/USD 15-min (M15) history from Dukascopy.

Para el research de scalping multi-timeframe (sesgo H4 → entrada 15M). El H4 se
resamplea del M15 (o del H1 existente); el M15 es la base de ejecución.

Output: parquet por año en data/dukascopy/XAUUSD-M15-<YYYY>.parquet + combinado
XAUUSD-M15-all.parquet. Idempotente (si el año existe, lo salta salvo --force).

Uso:
    .venv/bin/python -m scripts.download_dukascopy_m15 --years 11
    .venv/bin/python -m scripts.download_dukascopy_m15 --year 2024 --force
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dukascopy_python import INTERVAL_MIN_15, OFFER_SIDE_BID, fetch

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "dukascopy"
INSTRUMENT = "XAU/USD"
SLUG = "XAUUSD"
TF = "M15"


def _year_path(year: int) -> Path:
    return OUT_DIR / f"{SLUG}-{TF}-{year}.parquet"


def _download_year(year: int, force: bool) -> pd.DataFrame:
    path = _year_path(year)
    if path.exists() and not force:
        print(f"[skip] {path.name} ya existe ({path.stat().st_size // 1024} KB)")
        return pd.read_parquet(path)
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    print(f"[fetch] {INSTRUMENT} {TF} {start.date()} → {end.date()} ...", flush=True)
    df = fetch(instrument=INSTRUMENT, interval=INTERVAL_MIN_15,
               offer_side=OFFER_SIDE_BID, start=start, end=end)
    if df is None or len(df) == 0:
        print(f"[warn] {year}: respuesta vacía, saltando")
        return pd.DataFrame()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    print(f"[ok] {path.name}: {len(df)} velas {TF}, {path.stat().st_size // 1024} KB", flush=True)
    return df


def _combine_all() -> Path:
    parts = sorted(OUT_DIR.glob(f"{SLUG}-{TF}-*.parquet"))
    parts = [p for p in parts if "all" not in p.name]
    if not parts:
        return Path()
    full = pd.concat([pd.read_parquet(p) for p in parts]).sort_index()
    full = full[~full.index.duplicated(keep="last")]
    combined = OUT_DIR / f"{SLUG}-{TF}-all.parquet"
    full.to_parquet(combined)
    print(f"[ok] combinado {combined.name}: {len(full)} velas, "
          f"{full.index.min()} → {full.index.max()}", flush=True)
    return combined


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=11, help="años atrás (default 11)")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.year is not None:
        years = [args.year]
    else:
        now_year = datetime.now(timezone.utc).year
        years = list(range(now_year - args.years + 1, now_year + 1))
    print(f"target years: {years}", flush=True)
    for y in years:
        try:
            _download_year(y, args.force)
        except Exception as e:  # noqa: BLE001
            print(f"[err] {y}: {e}", file=sys.stderr)
    _combine_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
