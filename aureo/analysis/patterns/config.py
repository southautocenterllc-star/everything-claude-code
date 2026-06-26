"""
config.py
Configuracion central del Market Pattern Agent.
Ajusta aqui tickers, ventana historica y parametros de los indicadores.
"""
import os
from datetime import date, timedelta

# Carpeta de este modulo (para resolver rutas relativas de forma robusta)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

# ---------------------------------------------------------------------------
# ACTIVOS (simbolos de Yahoo Finance)
# ---------------------------------------------------------------------------
TICKERS = {
    "ORO":    "GC=F",    # Futuros de oro. Alternativa spot: "XAUUSD=X"
    "NASDAQ": "^IXIC",   # NASDAQ Composite. Alternativa: "^NDX" (Nasdaq 100)
    "SP500":  "^GSPC",   # S&P 500
}

# ---------------------------------------------------------------------------
# FUENTES LOCALES (tienen prioridad sobre Yahoo si el archivo existe)
# Parquet OHLCV intradiario que se resamplea a diario. Pensado para reusar
# la data Dukascopy que ya vive en el proyecto AUREO.
# ---------------------------------------------------------------------------
LOCAL_SOURCES = {
    "ORO": os.path.join(_PROJECT_ROOT, "data", "dukascopy", "XAUUSD-H1-all.parquet"),
}

# ---------------------------------------------------------------------------
# VENTANA HISTORICA (back-testing)
# ---------------------------------------------------------------------------
YEARS_BACK = 3                                    # >= 2 anios como pediste
START_DATE = (date.today() - timedelta(days=365 * YEARS_BACK)).isoformat()
END_DATE   = date.today().isoformat()

# ---------------------------------------------------------------------------
# PARAMETROS DE INDICADORES
# ---------------------------------------------------------------------------
SMA_FAST        = 50
SMA_SLOW        = 200
RSI_PERIOD      = 14
RSI_OVERSOLD    = 30
RSI_OVERBOUGHT  = 70
BREAKOUT_WINDOW = 20      # ruptura de maximo/minimo de N dias
ATR_PERIOD      = 14
DIVERGENCE_WINDOW = 10    # ventana para comparar precio vs RSI (divergencias)
GAP_THRESHOLD     = 0.005 # gap minimo (0.5%) respecto al cierre previo

# Horizontes (en dias de mercado) para medir el rendimiento futuro de cada senal
FORWARD_HORIZONS = [5, 10, 20]

# Ventana del estudio de eventos macro (+/- dias alrededor del evento)
EVENT_WINDOW = 5

# ---------------------------------------------------------------------------
# RUTAS
# ---------------------------------------------------------------------------
DATA_DIR   = "data"     # cache de precios descargados
OUTPUT_DIR = "output"   # reportes y graficos

# ---------------------------------------------------------------------------
# CAPA IA
# ---------------------------------------------------------------------------
AI_MODEL = "claude-sonnet-4-6"   # cambia el string si quieres otro modelo
AI_MAX_TOKENS = 2000

# ---------------------------------------------------------------------------
# TEST DE SIGNIFICANCIA (permutacion / bootstrap, NO t-stat)
# ---------------------------------------------------------------------------
PERMUTATION_ITERS = 5000   # remuestreos de la distribucion nula
PERMUTATION_SEED  = 42     # reproducibilidad
PERMUTATION_ALPHA = 0.05   # umbral de significancia
PERMUTATION_MIN_N = 30     # muestra minima para no celebrar ruido
