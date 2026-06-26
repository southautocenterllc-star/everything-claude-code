"""AUREO webhook receiver — FastAPI.

Acepta señales desde TradingView Pine Script (vía Cloudflare Tunnel)
o curl manual. Valida WEBHOOK_SECRET, persiste en SQLite, dispara
notificación al bot Telegram.

Sin MT5 — solo intake + persistencia + notificación. Ejecución real
queda para cuando se cablee broker.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from . import db

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BOT_TOKEN = os.environ.get("AUREO_TG_BOT_TOKEN", "")
BROADCAST_CHATS = [x.strip() for x in os.environ.get("AUREO_TG_BROADCAST_CHATS", "").split(",") if x.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("aureo.webhook")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    log.info("DB ready at %s", db.DB_PATH)
    yield


app = FastAPI(title="AUREO Webhook Receiver", version="1.0.0-linux", lifespan=lifespan)


async def _broadcast(text: str) -> None:
    if not BOT_TOKEN or not BROADCAST_CHATS:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        for chat_id in BROADCAST_CHATS:
            try:
                await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
            except Exception as e:
                log.warning("broadcast fallo chat=%s: %s", chat_id, e)


def _format_signal(payload: dict, signal_id: int, accepted: bool, reason: str | None) -> str:
    status = "✅ ACEPTADA" if accepted else f"❌ RECHAZADA ({reason})"
    return (
        f"🟡 *Señal AUREO #{signal_id}* — {status}\n"
        f"Source: `{payload.get('source','?')}`\n"
        f"Symbol: `{payload.get('symbol','?')}`\n"
        f"Action: *{payload.get('action','?')}*\n"
        f"Price: `{payload.get('price','?')}` | SL: `{payload.get('sl','?')}` | "
        f"TP1: `{payload.get('tp1','?')}` | TP2: `{payload.get('tp2','?')}`\n"
        f"Score total: `{payload.get('score_total','?')}`"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "mt5": "not_connected", "db": str(db.DB_PATH)}


@app.get("/status")
async def status():
    return await db.journal_summary()


@app.get("/journal")
async def journal(limit: int = 20):
    return {
        "summary": await db.journal_summary(),
        "recent_signals": await db.list_recent_signals(limit),
    }


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid json: {body[:200]!r}")

    secret = payload.get("secret") or request.headers.get("X-Webhook-Secret", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        log.warning("rechazo secret mismatch source=%s", payload.get("source"))
        raise HTTPException(status_code=401, detail="invalid secret")

    payload.pop("secret", None)
    accepted = True
    reason: str | None = None

    action = (payload.get("action") or "").upper()
    if action not in {"BUY", "SELL", "CLOSE"}:
        accepted = False
        reason = f"action_invalida: {action}"

    signal_id = await db.insert_signal(payload, accepted=accepted, rejection_reason=reason)
    log.info("signal id=%s accepted=%s action=%s symbol=%s", signal_id, accepted, action, payload.get("symbol"))

    await _broadcast(_format_signal(payload, signal_id, accepted, reason))

    return {"id": signal_id, "accepted": accepted, "rejection_reason": reason}