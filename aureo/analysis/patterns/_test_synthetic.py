"""Prueba end-to-end con datos sinteticos: valida toda la cadena sin red ni IA."""
import numpy as np
import pandas as pd

import data_loader
import main

# --- Generar OHLCV sintetico realista ---
def synth(seed):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=750)
    n = len(idx)  # ~3 anios de dias de mercado
    ret = rng.normal(0.0004, 0.011, n)
    close = 100 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    vol = rng.integers(1e6, 5e6, n)
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )
    df.index.name = "Date"
    return df

# Monkeypatch para evitar yfinance/red
data_loader.load_prices = lambda refresh=False: {
    "ORO": synth(1), "NASDAQ": synth(2), "SP500": synth(3)
}

results = main.run(refresh=False, use_ai=False)

# Verificaciones
print("\n=== VERIFICACION ===")
for name, r in results["assets"].items():
    se = r["signals_eval"]
    print(f"{name}: senales evaluadas={len(se)}, "
          f"event_study_filas={0 if r['event_study'] is None else len(r['event_study'])}")
print("Correlacion shape:", results["correlation"].shape)
import os
print("reporte.md existe:", os.path.exists("output/reporte.md"))
print("graficos:", [f for f in os.listdir("output") if f.endswith(".png")])
print("OK")
