# AUREO — Linux Phase 1 (sin MT5)

> ⚠️ Este README describe la **Fase 1 histórica** (scaffolding webhook, sin MT5).
> Áureo ya evolucionó: operador demo MT5 bajo Wine, daemons autónomos, capas de
> estudio de mercado. **Para montarlo de cero o entender la arquitectura actual,
> lee [`REPLICAR_AUREO.md`](REPLICAR_AUREO.md)** — esa es la guía vigente y
> autocontenida.

Trading XAU/USD agent — versión Linux post-reset 2026-05-21.

**Estado:** scaffolding mínimo viable. Acepta señales por webhook,
las persiste en SQLite, dispara notificación al bot Telegram.
**Sin MT5, sin signal generation autónoma todavía** — esos vienen en
sesiones futuras.

## Arquitectura actual

```
TradingView Pine  ──HTTPS──► (Cloudflare Tunnel) ──► FastAPI /webhook
                                                            │
                                                            ▼
                                                       SQLite (signals + trades)
                                                            │
                                                            ▼
                                                    Bot Telegram broadcast
```

## Servicios

- **`aureo-webhook.service`** — FastAPI en `127.0.0.1:8080`
- **`aureo-bot.service`** — bot Telegram polling, comandos `/start /status /journal /help`

## Endpoints

- `GET /health` → status básico
- `GET /status` → contadores agregados
- `GET /journal?limit=10` → últimas N señales
- `POST /webhook` → payload JSON con `secret` + `action` + datos opcionales

## Payload esperado

```json
{
  "secret": "WEBHOOK_SECRET del .env",
  "source": "tradingview",
  "symbol": "XAUUSD",
  "action": "BUY",
  "price": 2340.5,
  "sl": 2335.0,
  "tp1": 2350.0,
  "tp2": 2360.0,
  "rsi": 58,
  "atr": 4.2,
  "macro_bias": "long",
  "score_technical": 40,
  "score_macro": 25,
  "score_news": 15,
  "score_total": 80
}
```

## Smoke test local

```bash
curl -X POST http://127.0.0.1:8080/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"<WEBHOOK_SECRET>","source":"manual","symbol":"XAUUSD","action":"BUY","price":2340}'
```

## Pendiente para sesiones futuras

- Cloudflare Tunnel pa' exponer webhook a TradingView por HTTPS público
- Data feed alternativo a MT5 (yfinance / Twelve Data / Alpha Vantage)
- `signal_loop.py` reactivado con data feed nuevo
- `market_watcher.py` con Claude API (vía claude --print bundled)
- `trade_reconciler.py` cuando haya ejecución real
- MT5 connector opcional si Andrés decide volver a Windows o usar wine
