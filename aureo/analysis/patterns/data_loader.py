"""
data_loader.py
Descarga precios historicos de Yahoo Finance y los cachea en disco (CSV)
para no volver a pegarle a la red en cada corrida.
"""
import os
import pandas as pd

import config


def _download(ticker: str) -> pd.DataFrame:
    """Descarga OHLCV de un ticker via yfinance."""
    import yfinance as yf
    df = yf.download(
        ticker,
        start=config.START_DATE,
        end=config.END_DATE,
        auto_adjust=True,
        progress=False,
    )
    # yfinance reciente puede devolver columnas MultiIndex (Price, Ticker)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index.name = "Date"
    return df


def _load_local_parquet(path: str) -> pd.DataFrame:
    """
    Lee un parquet OHLCV intradiario (p.ej. Dukascopy XAUUSD H1) y lo resamplea
    a barras DIARIAS, que es la granularidad que usa el resto del modulo
    (SMA 50/200, estacionalidad, etc.). Normaliza columnas a Open/High/Low/
    Close/Volume y deja el indice como fecha sin zona horaria.
    """
    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]

    # Indice -> datetime sin tz, normalizado a dia de mercado (UTC)
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx

    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    daily = df.resample("1D").agg(agg).dropna(subset=["close"])

    daily = daily.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    daily.index.name = "Date"

    # Recortar a la ventana historica configurada
    daily = daily.loc[config.START_DATE:config.END_DATE]
    return daily


def load_prices(refresh: bool = False) -> dict[str, pd.DataFrame]:
    """
    Devuelve {nombre: DataFrame OHLCV diario} para todos los activos en
    config.TICKERS. Si el activo tiene una fuente local en config.LOCAL_SOURCES
    y el archivo existe, esa fuente tiene prioridad sobre Yahoo Finance.
    Para Yahoo se usa cache en disco salvo que refresh=True.
    """
    os.makedirs(config.DATA_DIR, exist_ok=True)
    data: dict[str, pd.DataFrame] = {}

    for name, ticker in config.TICKERS.items():
        local = config.LOCAL_SOURCES.get(name)
        if local and os.path.exists(local):
            print(f"  Usando fuente local {name}: {os.path.basename(local)}")
            df = _load_local_parquet(local)
            data[name] = df.dropna(how="all")
            continue

        path = os.path.join(config.DATA_DIR, f"{name}.csv")
        if os.path.exists(path) and not refresh:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        else:
            print(f"  Descargando {name} ({ticker})...")
            df = _download(ticker)
            df.to_csv(path)
        data[name] = df.dropna(how="all")

    return data
