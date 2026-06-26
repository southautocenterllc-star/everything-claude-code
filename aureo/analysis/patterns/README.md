# Market Pattern Agent

Agente **híbrido** (motor cuantitativo + capa de IA) para analizar **oro (GC=F)**,
**NASDAQ (^IXIC)** y **S&P 500 (^GSPC)** sobre 3 años de historia, detectar patrones
que se repiten y generar un reporte interpretado.

## Qué hace

1. **Carga** precios históricos. Para el **oro** usa por defecto la data local
   **Dukascopy XAUUSD H1** del proyecto (`data/dukascopy/`), resampleada a diario;
   para **NASDAQ** y **S&P 500** descarga de Yahoo Finance y cachea localmente.
2. **Calcula** indicadores: SMA 50/200, RSI(14), ATR(14), máximos/mínimos previos.
3. **Detecta patrones** en las 3 dimensiones que priorizaste:
   - **Estacionalidad / ciclos**: retorno medio por mes y por día de la semana, mejor/peor mes.
   - **Setups técnicos**: golden/death cross, reversiones por RSI, breakouts,
     **divergencias RSI**, **velas envolventes** (engulfing) y **gaps**. Cada señal se
     **backtestea** midiendo retorno futuro a 5/10/20 días, *win rate*, tamaño de muestra
     y **significancia estadística** (t-stat, p-value): así separas la **ventaja real**
     del ruido, y una **validación walk-forward** (in-sample vs out-of-sample)
     muestra si el edge **persiste fuera de muestra** (anti-overfitting).
   - **Reacción a eventos macro**: estudio de eventos ±5 días, **por tipo** de evento
     (FOMC, NFP...), con un calendario **real verificado** en `events.csv`.
4. **Interpreta** los hallazgos con Claude (capa IA) en español, tono profesional.
5. **Genera** un reporte en Markdown + gráficos PNG en `output/`.

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
python main.py              # corrida normal (usa cache si existe)
python main.py --refresh    # vuelve a descargar precios
python main.py --no-ai      # sin la capa de interpretación IA
```

Para activar la **capa IA**:

```bash
export ANTHROPIC_API_KEY="tu_api_key"
```

Si no defines la key, el sistema corre igual y omite solo la interpretación.

## Configuración

Todo se ajusta en `config.py`: activos, años de historia, períodos de los
indicadores, umbrales de RSI, horizontes de backtest y modelo de IA.

## Eventos macro

`events.csv` trae un calendario **real**: fechas **FOMC** verificadas (2023-2026) y
**NFP** (primer viernes de cada mes). Se genera de forma reproducible con
`python build_events.py`. Formato: `date,event,importance`. Para añadir CPI u otros
eventos, agrega filas con su tipo en la columna `event`; el estudio de eventos se
calcula **por tipo** automáticamente.

## Fuente de datos del oro

Por defecto el oro sale de `data/dukascopy/XAUUSD-H1-all.parquet` (definido en
`config.LOCAL_SOURCES`), resampleado de H1 a diario. Si borras esa entrada o el
archivo no existe, cae automáticamente a Yahoo Finance (`GC=F`).

## Estructura

```
config.py        parámetros centrales (incl. LOCAL_SOURCES)
data_loader.py   fuente local (parquet) + descarga/cache Yahoo
indicators.py    indicadores técnicos
patterns.py      estacionalidad + señales (cruces, RSI, breakouts, divergencias, engulfing, gaps)
backtest.py      backtest + significancia (t/p) + walk-forward + event study por tipo + correlación
ai_layer.py      interpretación con Claude
report.py        reporte Markdown + gráficos
main.py          orquestador (CLI)
build_events.py  generador del calendario macro real (events.csv)
events.csv       eventos macro (FOMC + NFP)
_test_synthetic.py   prueba end-to-end con datos sintéticos (sin red ni API)
```

## Validar sin red ni API

```bash
python _test_synthetic.py
```

Corre toda la cadena con datos simulados y confirma que genera el reporte.

## Limitaciones y siguientes pasos

- Las señales son clásicas y deliberadamente simples para que el edge sea
  interpretable. No optimiza parámetros (evita *overfitting*).
- El **p-value** usa una aproximación normal (no t de Student exacta); con n grande
  son casi idénticos. Trátalo como guía, no como verdad absoluta.
- No incluye costos de transacción ni *slippage* en el backtest.
- Próximas iteraciones sugeridas: exportar el reporte a **PDF**, ampliar el calendario
  macro (CPI/PCE), añadir índices con data local intradía, y un *scheduler* para
  correrlo automático (cron/Antigravity).

> Aviso: herramienta analítica/educativa. **No** es asesoría de inversión.
