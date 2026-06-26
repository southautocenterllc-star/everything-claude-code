---
name: estado-aureo
description: >-
  Diagnóstico operativo completo de ÁUREO en una pasada: servicios systemd,
  terminal MT5 bajo Wine, botón AlgoTrading, feed del oro, cuenta demo, journal
  (WR/PF/PnL), posiciones abiertas y bitácora. Úsala cuando Andrés pregunte
  "¿cómo va Áureo?", "¿está operando?", "¿por qué está quieto / no mete
  operaciones?", "¿cómo va el demo / el PnL?", o ante el síntoma
  `SELL ... ok=False @ 0.0`. Read-only: reporta y diagnostica, no opera ni
  reinicia nada sin que Andrés lo pida (el classifier bloquea trading real).
---

# Estado de ÁUREO — diagnóstico operativo

Áureo es el agente de trading XAU/USD (oro, swing H1) de Andrés. Esta skill da el
estado real de su máquina viva y, sobre todo, **interpreta** cada señal con los
gotchas ya ganados a sangre. Contexto canónico completo en
`/home/andres-urquijo-personal/memoria-canonica/project_aureo.md` y la bitácora de
PnL en `reference_aureo_bitacora.md`.

Marco honesto innegociable: **ninguna estrategia pasó el gauntlet** (backtest 18
años + 11 hipótesis + macro + fair-value, todas mueren con costos). El demo es
para ver la máquina andar y dejar data limpia, NO porque gane. NADA en real hasta
edge validado. Cualquier PnL es ruido de pocos trades en un régimen.

Todo corre desde `/mnt/datos/holding/aureo/` con `.venv/bin/python` (nunca el
global). Si te invocan desde HOME, igual `cd` al repo primero.

## Procedimiento (corre en orden, reporta y diagnostica)

### 1. Servicios systemd `--user`

```bash
systemctl --user list-units 'aureo*' --all --no-pager
```

Lo que DEBE estar `active/running`: `aureo-bot` (comandos Telegram),
`aureo-mt5-terminal` (Wine), `aureo-demo-trader` (opera cada hora),
`aureo-price-watch` (vigía de niveles), `aureo-study` (aprendizaje cada 12h).
Timers `active/waiting`: `aureo-healthcheck` (15min), `aureo-market-study` (4h),
`aureo-brief` (07/15h), `aureo-bitacora` (domingo 20h).

DEPRECADOS — NO reactivar: `aureo-webhook` y `aureo-tunnel` (el receptor oficial
es Vercel `https://aureo-edge.vercel.app/api/webhook`; reactivarlos resucita los
falsos positivos del healthcheck del holding).

### 2. Terminal MT5 — UN solo proceso (regla de oro)

```bash
pgrep -xc terminal64.exe
```

Debe dar **exactamente 1**.
- `0` → el terminal no está vivo (aunque el service figure `active`): el bridge
  no engancha, las órdenes salen `ok=False @ 0.0`. Es causa raíz frecuente del
  "Áureo quieto". Lo arranca Andrés / `start_aureo.sh` o reiniciando
  `aureo-mt5-terminal.service` desde la sesión con manos.
- `2` (o más) → la API Python parió un segundo terminal que lee otro
  `common.ini` con algo-trading off → `trade_allowed=false`. El bridge debe
  correr con `portable=True` (commit `1cf8213`) para acoplarse SIEMPRE al del
  service y no duplicar. Regla de oro MT5: un solo terminal por instalación con
  la Python API.

### 3. Cuenta demo + botón AlgoTrading (lectura, no toca nada)

```bash
.venv/bin/python -m core.mt5_executor info
```

Read-only. Fíjate en:
- `terminal_trade_allowed` / `trade_allowed`: **debe ser `true`**. Si es `false`,
  el botón "Trading algorítmico" de la GUI está apagado → toda orden sale
  retcode 10027 (`ok=False @ 0.0`). El control real es el botón (atajo Ctrl+E),
  **NO** el `common.ini` (el terminal lo reescribe a `Enabled=0` en cada
  arranque; editar el archivo es inútil). No hay `xdotool`/`wmctrl` en la
  máquina → el click lo da Andrés a mano. `AllowLiveTrading=1` es flag DISTINTO
  y no basta.
- `login` 10011178843, server contiene "Demo", `balance`, `price` del oro vivo.
  Si `price` viene >0 hay feed; el `@ 0.0` del síntoma NO es falta de feed sino
  orden rechazada antes de llenarse.

La traba dura: el executor solo opera si server contiene "Demo" +
`AUREO_MT5_ALLOW_DEMO_TRADE=1`; real exigiría `AUREO_ALLOW_LIVE=1` (jamás sin
edge). NO metas órdenes de prueba: el classifier lo bloquea y `info` ya es prueba
suficiente.

### 4. Feed del oro (Linux side)

```bash
.venv/bin/python -m core.data_feed --bars 3
```

Velas XAU/USD H1 de Twelve Data con fallback Yahoo (`GC=F`). Si imprime velas con
precio coherente (oro ~4000s), el feed Linux está sano (independiente del feed
MT5 del paso 3).

### 5. Journal — WR / PF / PnL del demo

```bash
.venv/bin/python -m core.trade_journal stats
```

Da win-rate / profit-factor / expectancy + desglose por sesgo H4 y hora UTC sobre
`logs/aureo_trades.jsonl`. Para cerrar outcomes de deals MT5:
`.venv/bin/python -m core.trade_journal reconcile` (necesita el terminal MT5
persistente del paso 2; si está caído, reconcilia 0). Detalle por trade:
`logs/aureo_trades.jsonl` (Read directo).

### 6. Healthcheck determinista (vigía 15min)

```bash
.venv/bin/python -m analysis.healthcheck --once
```

Sin `--send` para no spamear Telegram. Chequea los 6 units, latido del
demo-trader leyendo `journalctl` del SERVICE (no el journal de trades: con
posición abierta el trader no escribe y daría falsa alarma), feed vivo, y detecta
el `ok=False @ 0.0`. Si el `aureo-healthcheck.service` figura `failed`, casi
siempre es porque está detectando una falla real (sale status 1) — leer
`journalctl --user -u aureo-healthcheck -n 30` para ver QUÉ falla, no asumir que
el healthcheck está roto.

### 7. Bitácora de cierre (opcional, si Andrés pide "el reporte")

```bash
.venv/bin/python -m analysis.bitacora        # escribe research/output/BITACORA.md
.venv/bin/python -m analysis.bitacora --send  # además postea al grupo TRADING
```

Balance vs $1000, WR/PF/expectancy, mejor/peor trade, desglose por sesgo H4 y
hora, posición abierta, nota honesta fija (demo sin edge). Cuadra con
`trade_journal stats`. Tras un cierre relevante, **actualizar también la bitácora
canónica** `reference_aureo_bitacora.md` (tabla de balance) en la memoria.

## Síntoma estrella: `SELL XAUUSD -> ok=False @ 0.0`

Áureo genera la señal y dispara la orden, pero MT5 la rechaza antes de llenarla.
El `@ 0.0` despista (parece falta de feed) pero NO lo es. Checklist en orden:

1. `pgrep -xc terminal64.exe` → ¿es 1? Si 0 o ≥2, ese es el problema (paso 2).
2. `core.mt5_executor info` → ¿`trade_allowed: true`? Si `false`, botón
   AlgoTrading apagado: Andrés lo prende a mano con Ctrl+E en la GUI (paso 3).
3. Bridge con `portable=True` (ya en commit `1cf8213`).

`AllowLiveTrading=1` en el ini NO arregla esto (es otro flag). Editar `common.ini`
tampoco (el terminal lo pisa al arrancar).

## Reglas de la skill

- **Read-only por defecto.** Diagnostica e informa. NO reinicies servicios, NO
  arranques/cierres el terminal, NO metas órdenes ni edités `.env` o `common.ini`
  sin que Andrés lo pida explícito — el classifier lo bloquea por leerse como
  armar trading real.
- Aislamiento (Principio #6): Áureo es independiente, no cruzar con wall-e /
  carlitos / flowtive / nexus-stack / Urpe.
- Tono costeño barranquillero con Andrés, directo y ejecutivo: dale el veredicto
  claro ("Áureo andando OK" / "está quieto por X, arréglalo con Y"), no un volcado
  crudo de comandos.
- Si tras el diagnóstico hay que TOCAR código (fix de un módulo), va con el arnés
  madre: `escritor-aureo` escribe → `validador-aureo` aprueba corriéndolo contra
  data real. Nunca uno sin el otro.
