"""Download XAU/USD H1 history from Dukascopy.

Usage:
    python -m scripts.download_dukascopy --years 3
    python -m scripts.download_dukascopy --from 2023-01-01 --to 2024-01-01

Output: parquet por año en data/dukascopy/XAUUSD-H1-<YYYY>.parquet + un
combinado XAUUSD-H1-all.parquet. Idempotente: si el año ya existe en disco
y --force no se pasa, lo saltea.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dukascopy_python import (
    INTERVAL_HOUR_1,
    OFFER_SIDE_BID,
    fetch,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "dukascopy"
INSTRUMENT = "XAU/USD"
SLUG = "XAUUSD"


def _year_path(year: int) -> Path:
    return OUT_DIR / f"{SLUG}-H1-{year}.parquet"


def _download_year(year: int, force: bool) -> pd.DataFrame:
    path = _year_path(year)
    if path.exists() and not force:
        print(f"[skip] {path.name} ya existe ({path.stat().st_size // 1024} KB)")
        return pd.read_parquet(path)

    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    print(f"[fetch] {INSTRUMENT} H1 {start.date()} → {end.date()} ...")
    df = fetch(
        instrument=INSTRUMENT,
        interval=INTERVAL_HOUR_1,
        offer_side=OFFER_SIDE_BID,
        start=start,
        end=end,
    )
    if df is None or len(df) == 0:
        print(f"[warn] {year}: respuesta vacía, saltando")
        return pd.DataFrame()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    print(f"[ok] {path.name}: {len(df)} velas H1, {path.stat().st_size // 1024} KB")
    return df


def _combine_all() -> Path:
    parts = sorted(OUT_DIR.glob(f"{SLUG}-H1-*.parquet"))
    parts = [p for p in parts if "all" not in p.name]
    if not parts:
        return Path()
    frames = [pd.read_parquet(p) for p in parts]
    full = pd.concat(frames).sort_index()
    full = full[~full.index.duplicated(keep="last")]
    combined = OUT_DIR / f"{SLUG}-H1-all.parquet"
    full.to_parquet(combined)
    print(f"[ok] combinado {combined.name}: {len(full)} velas, "
          f"{full.index.min()} → {full.index.max()}")
    return combined


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=3,
                        help="Cuántos años atrás bajar (default 3)")
    parser.add_argument("--year", type=int, default=None,
                        help="Bajar solo un año específico")
    parser.add_argument("--force", action="store_true",
                        help="Re-descargar aunque el archivo exista")
    args = parser.parse_args()

    if args.year is not None:
        years = [args.year]
    else:
        now_year = datetime.now(timezone.utc).year
        years = list(range(now_year - args.years + 1, now_year + 1))

    print(f"target years: {years}")
    for y in years:
        try:
            _download_year(y, args.force)
        except Exception as e:
            print(f"[err] {y}: {e}", file=sys.stderr)

    _combine_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
