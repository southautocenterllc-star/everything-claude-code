"""
build_events.py
Construye el calendario macro real para el event study de patterns.

- FOMC: fechas verificadas a mano desde la fuente oficial de la Fed
  (federalreserve.gov/monetarypolicy/fomccalendars.htm). Dia 2 de cada reunion
  = dia del anuncio (cuando el mercado reacciona).
- CPI y NFP: se jalan PROGRAMATICAMENTE de FRED (St. Louis Fed), la fuente
  autoritativa. No se transcriben a mano porque el shutdown de 2025 y el lapse
  de apropiaciones de 2026 metieron excepciones que romperían cualquier lista
  por regla ("primer viernes"). FRED ya trae esas excepciones.

Uso:
  export FRED_API_KEY="tu_key_gratis"   # https://fred.stlouisfed.org/docs/api/api_key.html
  python build_events.py                 # escribe events.csv

Si no hay FRED_API_KEY, escribe solo el FOMC verificado y avisa.
"""
from __future__ import annotations

import csv
import os
import urllib.request
import urllib.parse
import json

START = "2023-06-01"
END = "2026-12-31"

# FOMC verificado (dia del anuncio). Fuente: Federal Reserve, fomccalendars.htm.
FOMC = [
    ("2023-06-14", "confirmado"), ("2023-07-26", "confirmado"),
    ("2023-09-20", "confirmado"), ("2023-11-01", "confirmado"),
    ("2023-12-13", "confirmado"),
    ("2024-01-31", "confirmado"), ("2024-03-20", "confirmado"),
    ("2024-05-01", "confirmado"), ("2024-06-12", "confirmado"),
    ("2024-07-31", "confirmado"), ("2024-09-18", "confirmado"),
    ("2024-11-07", "confirmado"), ("2024-12-18", "confirmado"),
    ("2025-01-29", "confirmado"), ("2025-03-19", "confirmado"),
    ("2025-05-07", "confirmado"), ("2025-06-18", "confirmado"),
    ("2025-07-30", "confirmado"), ("2025-09-17", "confirmado"),
    ("2025-10-29", "confirmado"), ("2025-12-10", "confirmado"),
    ("2026-01-28", "confirmado"), ("2026-03-18", "confirmado"),
    ("2026-04-29", "confirmado"), ("2026-06-17", "tentativo"),
    ("2026-07-29", "tentativo"), ("2026-09-16", "tentativo"),
    ("2026-10-28", "tentativo"), ("2026-12-09", "tentativo"),
]

# IDs de release en FRED:
#  - 50  = Employment Situation (NFP). CONFIRMADO.
#  - 10  = Consumer Price Index (CPI). Verifica en fred.stlouisfed.org/releases
FRED_RELEASES = {50: "NFP", 10: "CPI"}


def _fetch_fred_release_dates(release_id: int, api_key: str) -> list[str]:
    """Devuelve las fechas de publicacion reales de un release de FRED."""
    base = "https://api.stlouisfed.org/fred/release/dates"
    params = urllib.parse.urlencode({
        "release_id": release_id,
        "api_key": api_key,
        "file_type": "json",
        "realtime_start": START,
        "realtime_end": END,
        "include_release_dates_with_no_data": "true",
    })
    with urllib.request.urlopen(f"{base}?{params}", timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [d["date"] for d in data.get("release_dates", [])
            if START <= d["date"] <= END]


def build(path: str = "events.csv") -> int:
    rows = [{"date": d, "event": "FOMC", "importance": "alta", "estado": estado}
            for d, estado in FOMC]

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("AVISO: sin FRED_API_KEY -> escribo solo FOMC verificado.")
    else:
        for rid, name in FRED_RELEASES.items():
            try:
                for d in _fetch_fred_release_dates(rid, api_key):
                    rows.append({"date": d, "event": name,
                                 "importance": "alta", "estado": "confirmado"})
                print(f"OK: {name} (release {rid}) cargado de FRED.")
            except Exception as e:  # noqa: BLE001
                print(f"AVISO: no pude cargar {name} de FRED: {e}")

    # Dedupe (misma fecha + mismo evento) y ordena
    seen, unique = set(), []
    for row in sorted(rows, key=lambda x: (x["date"], x["event"])):
        key = (row["date"], row["event"])
        if key not in seen:
            seen.add(key)
            unique.append(row)

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "event", "importance", "estado"])
        w.writeheader()
        w.writerows(unique)

    print(f"Escrito {path}: {len(unique)} eventos "
          f"({sum(r['event'] == 'FOMC' for r in unique)} FOMC).")
    return len(unique)


if __name__ == "__main__":
    build()
