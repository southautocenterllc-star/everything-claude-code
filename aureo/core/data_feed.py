"""Data feed XAU/USD H1 para AUREO — sin TradingView.

Provee velas H1 de oro desde dos fuentes, en este orden de preferencia:

  1. Twelve Data  — símbolo `XAU/USD` (spot). Requiere `TWELVEDATA_API_KEY`
     en el entorno. Plan free: 8 req/min, 800/día (suficiente para 1 req/hora).
  2. Yahoo Finance — símbolo `GC=F` (futuro de oro COMEX, proxy del spot).
     Sin API key, vía el endpoint chart público (requests, sin yfinance).

Ambas se normalizan al MISMO contrato:
  - DataFrame con índice DatetimeIndex tz-aware UTC, ascendente.
  - Columnas: open, high, low, close, volume (float).
  - SOLO velas YA CERRADAS — la barra en formación se descarta (`drop_forming`).

Notas honestas:
  - GC=F (futuro) NO es idéntico a XAU/USD spot (hay basis y horario COMEX con
    huecos). Es un FALLBACK aceptable para no quedarnos ciegos, no la fuente
    primaria. Con la API key de Twelve Data usamos spot real.
  - Este módulo solo TRAE datos. No decide nada ni dispara señales.

CLI:
    .venv/bin/python -m core.data_feed            # imprime últimas velas + fuente
    .venv/bin/python -m core.data_feed --bars 10
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Literal

import pandas as pd
import requests

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AUREO/1.0 data_feed"

Provider = Literal["twelvedata", "yahoo"]

COLS = ["open", "high", "low", "close", "volume"]


class FeedError(RuntimeError):
    """Falla recuperable al traer datos de una fuente."""


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Asegura índice UTC ascendente, columnas float ordenadas, sin NaN en OHLC."""
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in COLS:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df = df[COLS]
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def fetch_twelvedata(api_key: str, symbol: str = "XAU/USD",
                     interval: str = "1h", outputsize: int = 500,
                     timeout: float = 15.0) -> pd.DataFrame:
    """Trae velas de Twelve Data. Lanza FeedError ante cualquier problema."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(min(outputsize, 5000)),
        "apikey": api_key,
        "timezone": "UTC",
        "order": "ASC",
    }
    try:
        r = requests.get(TWELVEDATA_URL, params=params, timeout=timeout,
                         headers={"User-Agent": _UA})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise FeedError(f"twelvedata request falló: {e}") from e

    if isinstance(data, dict) and data.get("status") == "error":
        raise FeedError(f"twelvedata error: {data.get('message')}")
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, list) or not values:
        raise FeedError(f"twelvedata 'values' no es lista no-vacía (resp: {str(data)[:200]})")

    try:  # respuesta HTTP 200 + JSON válido pero con shape inesperada → FeedError
        df = pd.DataFrame(values)
        df = df.rename(columns={"datetime": "ts"}).set_index("ts")
        return _normalize(df)
    except (ValueError, KeyError, TypeError) as e:
        raise FeedError(f"twelvedata shape inesperada: {e}") from e


def fetch_yahoo(symbol: str = "GC=F", interval: str = "1h",
                range_: str = "730d", timeout: float = 15.0) -> pd.DataFrame:
    """Trae velas del endpoint chart de Yahoo (sin yfinance). Lanza FeedError."""
    params = {"interval": interval, "range": range_, "includePrePost": "false"}
    try:
        r = requests.get(YAHOO_URL.format(symbol=symbol), params=params,
                         timeout=timeout, headers={"User-Agent": _UA})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise FeedError(f"yahoo request falló: {e}") from e

    try:
        result = data["chart"]["result"][0]
        ts = result["timestamp"]
        q = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "close": q.get("close"),
            "volume": q.get("volume"),
        }, index=pd.to_datetime(ts, unit="s", utc=True))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        err = (data or {}).get("chart", {}).get("error")
        raise FeedError(f"yahoo estructura inesperada ({err}): {e}") from e
    return _normalize(df)


def drop_forming(df: pd.DataFrame, now: pd.Timestamp | None = None,
                 bar_minutes: int = 60) -> pd.DataFrame:
    """Descarta la última barra si aún está EN FORMACIÓN.

    Una barra H1 con timestamp T cierra en T + bar_minutes. Si el instante
    actual cae dentro de [T, T+bar) la barra no terminó: se quita para no
    evaluar señales sobre data incompleta (look-ahead barato pero letal).
    """
    if df.empty:
        return df
    # 1) Descartar barras finales OFF-GRID: Yahoo a veces emite la vela en
    #    formación con timestamp intra-hora (p.ej. 04:21:20) que NO está alineado
    #    a la grilla H1. Esas son parciales por definición → fuera.
    grid = bar_minutes * 60
    while len(df) and ((df.index[-1].minute * 60 + df.index[-1].second) % grid) != 0:
        df = df.iloc[:-1]
    if df.empty:
        return df
    # 2) Descartar la última barra si aún no cerró según el reloj.
    now = now or pd.Timestamp.now(tz="UTC")
    last_open = df.index[-1]
    last_close = last_open + pd.Timedelta(minutes=bar_minutes)
    if now < last_close:
        return df.iloc[:-1]
    return df


def get_h1(lookback: int = 500, drop_unclosed: bool = True,
           now: pd.Timestamp | None = None) -> tuple[pd.DataFrame, Provider]:
    """Devuelve (velas_H1, fuente). Twelve Data si hay API key; si no o si
    falla, cae a Yahoo GC=F. Lanza FeedError solo si AMBAS fallan."""
    api_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    errors = []

    if api_key:
        try:
            df = fetch_twelvedata(api_key, outputsize=lookback)
            if drop_unclosed:
                df = drop_forming(df, now)
            if not df.empty:
                return df, "twelvedata"
            errors.append("twelvedata: vacío tras normalizar")
        except Exception as e:  # noqa: BLE001 — cualquier fallo NO debe matar el
            errors.append(f"twelvedata: {e}")  # fallback a Yahoo (live-safety)

    try:
        # range Yahoo: ~60d de H1 ≈ 1000 velas de mercado; recorta a lookback.
        df = fetch_yahoo()
        if drop_unclosed:
            df = drop_forming(df, now)
        if not df.empty:
            return df.tail(lookback), "yahoo"
        errors.append("yahoo: vacío tras normalizar")
    except Exception as e:  # noqa: BLE001
        errors.append(f"yahoo: {e}")

    raise FeedError("todas las fuentes fallaron -> " + " | ".join(errors))


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke test del data feed XAU/USD H1.")
    ap.add_argument("--bars", type=int, default=5, help="Cuántas velas mostrar.")
    ap.add_argument("--lookback", type=int, default=200)
    args = ap.parse_args()

    try:
        df, provider = get_h1(lookback=args.lookback)
    except FeedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Fuente: {provider}  |  velas cerradas: {len(df)}  "
          f"|  rango: {df.index.min()} → {df.index.max()}")
    print(df.tail(args.bars).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())