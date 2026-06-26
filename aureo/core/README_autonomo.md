# AUREO autónomo (sin TradingView) — feed + signal loop en MODO PAPEL

Estado al 2026-06-02. Reemplaza la dependencia de TradingView por un feed propio.
**No dispara señales en vivo**: el backtest oficial + la batería de research
(ver `research/`) confirmaron que NO hay edge desplegable todavía. La tubería
está lista y validada con data en vivo; cuando haya un edge que pase el gauntlet
solo hay que (1) registrar el evaluador en `core/signal_loop.py::STRATEGIES` y
(2) encender `AUREO_LIVE=1`.

## Componentes

### `core/data_feed.py` — velas XAU/USD H1
Dos fuentes, en orden de preferencia:
1. **Twelve Data** (`XAU/USD` spot) — requiere `TWELVEDATA_API_KEY` en el entorno.
2. **Yahoo Finance** (`GC=F`, futuro COMEX) — fallback sin API key, vía requests.

Ambas normalizan a OHLC UTC ascendente y **descartan la vela en formación**
(`drop_forming`) para no evaluar señales sobre data incompleta.

```bash
.venv/bin/python -m core.data_feed --bars 6     # smoke test (usa Yahoo si no hay key)
```

Para spot real (recomendado), sacar key gratis en twelvedata.com y:
```bash
export TWELVEDATA_API_KEY=...   # o ponerla en .env y cargarla en el service
```

### `core/signal_loop.py` — evaluación bar-close, modo papel
Cada cierre H1 recalcula la señal y, si hay entrada/salida nueva, la **registra**
en `logs/paper_journal.jsonl` con el contrato exacto del webhook de Vercel
(`{source,symbol,action,price,sl,tp1,tp2,score_total}`). Pyramiding=0. Salidas
SL/TP1/TP2 evaluadas bar-by-bar igual que `backtest.engine` (regla SL-first).

- **Determinístico**: NO hay Claude/LLM dentro del bucle (lección anti-BENDER).
- **Papel por defecto**: nunca postea salvo `AUREO_LIVE=1` + `AUREO_WEBHOOK_URL` + `WEBHOOK_SECRET`.
- Estrategia activa hoy: `balanced_placeholder` (SIN edge — solo prueba la tubería;
  `score_total=0` y tag explícito en cada entrada del journal).

```bash
.venv/bin/python -m core.signal_loop --once              # procesa nuevas barras y sale
.venv/bin/python -m core.signal_loop --once --replay 120 # backfill inicial al journal
.venv/bin/python -m core.signal_loop --daemon            # bucle horario residente
```

Estado persistente en `logs/paper_state.json` (última barra + posición abierta),
así `--once` es correcto entre invocaciones.

## Despliegue (systemd --user) — PREPARADO, no instalado

Archivos en `deploy/`. El patrón recomendado es **timer horario** corriendo
`--once` (determinístico, no residente):

```bash
cp deploy/aureo-paper-loop.{service,timer} ~/.config/systemd/user/
# revisar WorkingDirectory/ExecStart si el repo no está en ~/holding/aureo
systemctl --user daemon-reload
systemctl --user enable --now aureo-paper-loop.timer
systemctl --user list-timers | grep aureo
journalctl --user -u aureo-paper-loop.service -n 30
```

Para ir EN VIVO el día que haya edge: descomentar `AUREO_LIVE=1` +
`AUREO_WEBHOOK_URL` + secret en el `.service` (mejor vía `EnvironmentFile=.env`),
cambiar `ACTIVE_STRATEGY` al evaluador validado, `daemon-reload` + restart.

## Por qué papel y no en vivo
Ver `research/output/` y la memoria canónica: ni la 'balanced' ni ninguna de las
11 hipótesis de la batería tienen edge que sobreviva costos (spread+swap),
significancia y robustez por régimen. Encender en vivo sería operar ruido.