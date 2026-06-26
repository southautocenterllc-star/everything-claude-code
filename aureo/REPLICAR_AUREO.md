# Replicar ÁUREO — guía de transferencia

Guía autocontenida para montar tu propio ÁUREO de cero en otra máquina. No
necesitas nada del entorno original del autor: todo lo que hace falta está acá.

---

## 0. Lo primero — la verdad honesta (léelo antes de ilusionarte)

ÁUREO es un agente de trading de oro (XAU/USD, swing en temporalidad H1) que vive
solo: genera/recibe señales, opera una cuenta **demo** de MetaTrader 5, avisa por
Telegram y aprende de cada trade. Es un proyecto **personal y educativo**.

**Ninguna estrategia de ÁUREO tiene ventaja (edge) demostrada.** Se probó en serio:
backtest de 18 años sobre datos reales de Dukascopy (112,879 velas H1), una batería
de 11 hipótesis, features macro, y un modelo de fair-value. **Todas mueren cuando
metes los costos reales** (spread + swap overnight). El detalle está en
`research/output/EDGE_RESEARCH_NOTE.md`. El swap/carry overnight es el costo
DOMINANTE en swing de oro, no el spread — esa es la lección más cara del proyecto.

Por eso:
- Esto corre en **demo** (plata de mentira). NADA en real hasta tener un edge
  validado contra costos + significancia + robustez por régimen.
- Cualquier PnL que veas es ruido de pocos trades en un régimen, no habilidad.
- ÁUREO **no promete rentabilidad ni da consejo financiero a terceros**. Si lo
  compartes, deja esto clarísimo.

El valor real de replicarlo no es ganar plata: es tener una **máquina de trading
autónoma, determinista y bien instrumentada** para aprender, medir, y algún día
probar tus propias hipótesis con disciplina (ver sección 8, el gauntlet).

---

## 1. Arquitectura real (la de hoy, no la del viejo README.md)

```
                    ┌─────────────────────────────────────────┐
                    │  Linux + systemd --user (todo local)      │
                    │                                           │
  Yahoo / Twelve ──►│  core/data_feed.py   (velas XAU/USD H1)   │
  Data (oro)        │         │                                 │
                    │         ▼                                 │
                    │  core/demo_trader.py ──► core/mt5_executor│──► MT5 (Wine)
                    │   (evalúa cada hora,     core/mt5_bridge      cuenta DEMO
                    │    pyramiding=0,         (wine-python)        login propio
                    │    SL/TP + breakeven)         │                  │
                    │         │                     ▼                  ▼
                    │         ▼                  órdenes 0.01      ejecuta orden
                    │  core/trade_journal.py ◄──── registra + reconcilia
                    │   (logs/aureo_trades.jsonl: WR/PF/expectancy) │
                    │         │                                       │
                    │         ▼                                       │
                    │  analysis/*  ──► Telegram (@tu_bot, grupo)      │
                    │   brief diario · vigía de niveles ·             │
                    │   estudio de mercado (macro+noticias+geo) ·     │
                    │   healthcheck · bitácora                        │
                    └─────────────────────────────────────────────────┘
```

Todo es **determinista** (no hay un LLM dentro del bucle de trading — regla de
diseño dura, por seguridad). La capa de "estudio de mercado" es solo APOYO
discrecional: macro cuantitativo + RSS de noticias + eventos, nunca da órdenes.

Hay también un camino histórico vía **webhook** (TradingView → función serverless
en Vercel → Telegram). Es opcional; el corazón hoy es el operador demo local.

---

## 2. Requisitos

Cuentas y software que el amigo debe conseguir (gratis todo):

- **Linux** (probado en Ubuntu 26.04) con `systemd --user`. Un PC que pueda quedar
  encendido si quieres operación continua.
- **Python 3.12** para el lado Linux (el venv del repo).
- **Wine 10** (sin sudo sirve) + **Python 3.11 para Windows** dentro del prefijo
  Wine — esto es lo que habla con la API de MetaTrader5 (la lib `MetaTrader5` solo
  existe para Windows). `numpy==1.26.4` **obligatorio** bajo Wine (numpy 2.x
  CUELGA).
- **MetaTrader 5** (terminal Windows) instalado dentro del prefijo Wine.
- Una **cuenta DEMO de MT5** (cualquier broker; el original usa MetaQuotes-Demo,
  $1000 ficticios). El server DEBE contener la palabra "Demo" o la traba de
  seguridad no deja operar.
- Un **bot de Telegram propio** (lo creas con @BotFather en 2 minutos) y tu
  `chat_id` / grupo destino.
- Opcionales: `TWELVEDATA_API_KEY` (feed de oro premium; sin ella cae a Yahoo),
  `FRED_API_KEY` (calendario macro NFP/CPI real), `ANTHROPIC_API_KEY` (solo si
  reactivas alguna capa de interpretación con IA; el core no la necesita).

---

## 3. Montaje de cero

```bash
# 1) Clona el repo (sin secretos — ver sección 9)
git clone <tu-fork-o-copia-de-aureo> ~/aureo && cd ~/aureo

# 2) venv Linux + deps
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
#   OJO: numpy se fija aparte para el lado Wine; en Linux requirements pide >=2,
#   pero la API MT5 corre en el python de Wine (paso 4) con numpy==1.26.4.

# 3) Config: copia el ejemplo y rellena (ver .env.example, está completo)
cp .env.example .env
$EDITOR .env

# 4) MT5 bajo Wine (lo más laborioso, una sola vez)
#    a. Instala Wine 10 (sin sudo: WoW64 portable o tu gestor).
#    b. Crea el prefijo y mete Python 3.11 Windows + la lib MetaTrader5:
export WINEPREFIX="$PWD/data/mt5/wineprefix"
#       descarga python-3.11.x-amd64.exe y MT5 setup, instálalos con `wine`.
#       dentro de wine-python:  pip install MetaTrader5 numpy==1.26.4
#    c. Abre el terminal MT5 una vez a mano, loguéate en tu cuenta DEMO,
#       y DEJA PRENDIDO el botón "Trading algorítmico" (Ctrl+E, cuadrito verde).
#       Esto NO se puede automatizar por archivo (ver gotcha #3).

# 5) Smoke test (read-only, no mete órdenes):
.venv/bin/python -m core.data_feed --bars 3      # feed del oro vivo
.venv/bin/python -m core.mt5_executor info       # cuenta + trade_allowed:true
```

**Paths absolutos:** el repo original vive en `/mnt/datos/holding/aureo`. Los
units de `deploy/*.service` y `start_aureo.sh` tienen esa ruta hardcodeada. Antes
de instalar servicios, reemplaza la ruta por la tuya:

```bash
grep -rl "/mnt/datos/holding/aureo" deploy/ start_aureo.sh \
  | xargs sed -i "s#/mnt/datos/holding/aureo#$HOME/aureo#g"
```

---

## 4. Cómo levantarlo

**Modo simple (nohup, no sobrevive reboot)** — para probar:

```bash
./start_aureo.sh      # idempotente: terminal MT5 + operador demo + vigía + estudio
# parar todo:
pkill -f 'demo_trader|study_loop|price_watch|terminal64.exe'
```

**Modo permanente (systemd --user, sobrevive reboot)** — para operación continua:

```bash
cp deploy/aureo-*.service deploy/aureo-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  aureo-mt5-terminal aureo-demo-trader aureo-bot aureo-price-watch aureo-study \
  aureo-healthcheck.timer aureo-market-study.timer aureo-brief.timer aureo-bitacora.timer
# habilita el lingering para que arranquen sin sesión iniciada:
loginctl enable-linger "$USER"
```

---

## 5. Qué hace cada servicio

- `aureo-mt5-terminal` — terminal MT5 bajo Wine, persistente (que la API enganche
  rápido y sincronice el history). **Uno solo, siempre** (gotcha #1).
- `aureo-demo-trader` — el operador: cada hora evalúa la estrategia sobre el último
  cierre H1, pyramiding=0, mete orden 0.01 con SL/TP, mueve a breakeven en vivo,
  registra y reconcilia. Estrategia SIN edge (es para ver la máquina andar).
- `aureo-bot` — bot de Telegram (polling), comandos `/status /market /journal
  /positions /help`, allowlist por username.
- `aureo-price-watch` — vigía de niveles de precio, alerta al Telegram al tocar
  zonas (configurable en `config/price_levels.json`).
- `aureo-study` — cada 12h reconcilia + saca stats de aprendizaje por sesgo H4 y
  hora; avisa si algún bucket llega a ≥20 trades con PF>1.3 (candidato a edge **que
  hay que verificar con el gauntlet** antes de creerle).
- Timers: `aureo-healthcheck` (15min, vigía determinista), `aureo-market-study`
  (4h, macro+noticias+geopolítica), `aureo-brief` (mañana/tarde, resumen del oro),
  `aureo-bitacora` (domingo, reporte de cierre del periodo).

---

## 6. Operación día a día

```bash
.venv/bin/python -m core.trade_journal stats     # WR / PF / expectancy + desglose
.venv/bin/python -m core.trade_journal reconcile # cierra outcomes de deals MT5
.venv/bin/python -m analysis.healthcheck --once  # estado de todo en una pasada
.venv/bin/python -m analysis.bitacora --send     # reporte de cierre al Telegram
```

Hay una **skill de diagnóstico** (`.claude/skills/estado-aureo/`) que en una pasada
te da el estado completo e interpreta cada síntoma — úsala si trabajas el repo con
Claude Code (`/estado-aureo`).

---

## 7. GOTCHAS críticos (ganados a sangre — no los aprendas otra vez)

1. **UN solo `terminal64.exe`.** `pgrep -xc terminal64.exe` debe dar 1. Si hay 2,
   la API Python parió un segundo terminal que lee otro `common.ini` con
   algo-trading apagado → `trade_allowed=false`. El bridge debe correr con
   `portable=True` (ya está, commit `1cf8213`) para acoplarse al del service y no
   duplicar.
2. **`numpy==1.26.4` bajo Wine.** numpy 2.x CUELGA el python de Wine al importar
   la API MT5. Pin obligatorio en el wine-python.
3. **El botón "Trading algorítmico" manda, no el archivo.** Si las órdenes salen
   `ok=False @ 0.0` (retcode 10027 AutoTrading disabled), es que el botón está
   apagado. Se prende a mano en la GUI con **Ctrl+E** (cuadrito verde). Editar
   `Config/common.ini` (`[Experts] Enabled=1`) es **inútil**: el terminal lo
   reescribe a 0 en cada arranque. `AllowLiveTrading=1` es un flag DISTINTO y no
   basta. Sin `xdotool`/`wmctrl` no se automatiza el click.
4. **El `@ 0.0` despista.** Parece falta de feed pero NO lo es: el feed está vivo
   (lo confirmas con `core.mt5_executor info` → `price>0`). El 0.0 es el precio de
   una orden rechazada antes de llenarse. Checklist: terminal=1 → botón verde →
   `portable=True`.
5. **Traba de seguridad demo-only.** El executor SOLO opera si el server contiene
   "Demo" y `AUREO_MT5_ALLOW_DEMO_TRADE=1`. Operar real exigiría `AUREO_ALLOW_LIVE=1`
   — **no lo pongas sin un edge validado.** Es la baranda que evita perder plata de
   verdad por accidente.
6. **Daemons con `nohup` mueren en el reboot.** Para permanencia real usa los
   systemd `--user` + `loginctl enable-linger`.
7. **No metas un LLM en el bucle de trading.** Regla de diseño: el operador es
   100% determinista. La IA, si acaso, solo en capas de análisis on-demand, nunca
   decidiendo órdenes sola.

---

## 8. El conocimiento más valioso — el gauntlet de research

Antes de creerle a CUALQUIER estrategia, pásala por el gauntlet. Es lo que mató
todas las hipótesis del proyecto y lo que te ahorra meses persiguiendo espejismos.

`research/harness.py` corre 3 filtros sobre las 112,879 velas H1 reales:

1. **Costos base realistas:** spread **35 pips** (= 0.35 USD/oz; ojo: el engine usa
   `spread_pips * pip_value`, con `pip_value=0.01` hay que pasar `spread_pips=35`,
   NO 0.35 — ese footgun de 100x ya está corregido) + swap **0.5 USD/oz/noche**. En
   swing, el swap es el costo dominante.
2. **Significancia:** no t-stat (miente por solapamiento de ventanas). Se usa
   **rotación circular** (circular-shift) de la serie de retornos preservando el
   clustering y la N exacta de las entradas. Validado contra random walk ANTES de
   creerle: un test que se valida solo no sirve.
3. **Robustez por régimen:** 5 bloques macro 2008→2026. Si el 80% del PnL viene de
   un solo régimen (p.ej. el toro 2023-26), eso es beta direccional, no edge.

Veredicto acumulado: patrones de precio H1, prior de patrones diario, macro como
predictor, y fair-value mean-reversion — **todos descartados con rigor**. La
relación del oro con yields/DXY existe pero es CONTEMPORÁNEA, no predictiva (cuando
el macro se mueve, el oro ya se movió en la misma barra). Lo único sin explorar:
sorpresa de evento FOMC/CPI (consenso vs actual, requiere feed nuevo).

Si encuentras un bucket candidato (`aureo-study` te avisa), **no lo cablees al
operador** hasta que pase el gauntlet con costos + significancia + robustez.

---

## 9. Método de trabajo — el arnés madre (recomendado)

Todo código o análisis se escribe con un par de agentes y NO se da por bueno hasta
que el verificador lo apruebe **corriéndolo contra datos reales** (no leyéndolo):

- `escritor-*` — coder de mínima superficie, escribe limpio y ejecutable.
- `validador-*` — escéptico, CORRE el código, caza look-ahead, el footgun del
  spread 100x, rupturas de paridad engine↔loop, secretos. Veredicto JSON
  `{aprobado, razones, correcciones, severidad}`. Ante duda, rechaza.

Los arneses están en `.claude/agents/`. Este lazo (escritor → validador → corrige)
es lo que cazó los bugs HIGH del proyecto. Úsalo para todo: bots, infra, research.

---

## 10. Qué NO viene en el paquete (lo pones tú)

Gitignored a propósito — son tuyos, nunca se comparten:

- `.env` — TODOS los secretos (token del bot, login/clave MT5, API keys). Parte de
  `.env.example` y rellena con LO TUYO. Crea tu propio bot, tu propia cuenta demo,
  tu propio `WEBHOOK_SECRET` aleatorio.
- `data/` — la data pesada (parquets de Dukascopy ~2.5G, el prefijo Wine con MT5).
  Bájala con `scripts/download_dukascopy.py` y monta tu Wine de cero.
- `logs/`, `*.db`, `reports/`, `research/output/` — outputs generados, regenerables.
- `.venv/` — recréalo con `pip install -r requirements.txt`.

Con eso tienes Áureo entero y reproducible. Cualquier número de cuenta, token o URL
que veas en el historial o en docs es del entorno original — reemplázalo por el
tuyo. Suerte, y recuerda: **demo hasta que haya edge.**
