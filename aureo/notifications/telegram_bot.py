"""AUREO Telegram bot — polling con allowlist + comandos básicos."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ["AUREO_TG_BOT_TOKEN"]
ALLOWED_USERS = {x.strip().lower() for x in os.environ.get("AUREO_TG_ALLOWED_USERS", "").split(",") if x.strip()}
WEBHOOK_BASE = os.environ.get("AUREO_WEBHOOK_BASE", "http://127.0.0.1:8080")
AUTHORIZED_CHATS_FILE = BASE_DIR / "logs" / "authorized_chats.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] aureo.bot: %(message)s")
log = logging.getLogger("aureo.bot")


def _load_authorized_chats() -> set[int]:
    if not AUTHORIZED_CHATS_FILE.exists():
        return set()
    try:
        return set(json.loads(AUTHORIZED_CHATS_FILE.read_text()))
    except Exception:
        return set()


def _save_authorized_chats(chats: set[int]) -> None:
    AUTHORIZED_CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTHORIZED_CHATS_FILE.write_text(json.dumps(sorted(chats)))


def _user_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return False
    u = update.effective_user
    return bool(u and u.username and u.username.lower() in ALLOWED_USERS)


async def _register_chat(chat_id: int) -> None:
    chats = _load_authorized_chats()
    if chat_id not in chats:
        chats.add(chat_id)
        _save_authorized_chats(chats)
        log.info("auto-registrado chat_id=%s para broadcast", chat_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_allowed(update):
        await update.message.reply_text("Acceso no autorizado.")
        return
    await _register_chat(update.effective_chat.id)
    await update.message.reply_text(
        "🟡 *AUREO online*\n"
        "Comandos: `/status` `/journal` `/help`\n"
        "Chat registrado para broadcast.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_allowed(update):
        return
    await update.message.reply_text(
        "*AUREO — comandos*\n"
        "`/status` — resumen de señales y trades\n"
        "`/journal` — últimas 10 señales\n"
        "`/help` — esto",
        parse_mode="Markdown",
    )


async def _http_get(path: str) -> dict | None:
    url = f"{WEBHOOK_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning("GET %s falló: %s", url, e)
        return None


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_allowed(update):
        return
    await _register_chat(update.effective_chat.id)
    data = await _http_get("/status")
    if not data:
        await update.message.reply_text("⚠️ No pude contactar el webhook receiver. ¿Servicio arriba?")
        return
    msg = (
        f"📊 *AUREO Status*\n"
        f"Señales totales: `{data['signals_total']}` (aceptadas: `{data['signals_accepted']}`\n"
        f"Trades totales: `{data['trades_total']}` | cerrados: `{data['trades_closed']}`\n"
        f"Wins: `{data['wins']}` | Win rate: `{data['win_rate_pct']}%`\n"
        f"PnL total: `{data['total_pnl']}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _user_allowed(update):
        return
    await _register_chat(update.effective_chat.id)
    data = await _http_get("/journal?limit=10")
    if not data:
        await update.message.reply_text("⚠️ Webhook receiver no responde.")
        return
    signals = data.get("recent_signals", [])
    if not signals:
        await update.message.reply_text("Sin señales todavía.")
        return
    lines = ["📒 *Últimas señales*"]
    for s in signals:
        flag = "✅" if s["accepted"] else "❌"
        lines.append(f"{flag} #{s['id']} `{s['action']}` {s['symbol']} @ `{s.get('price','?')}` score=`{s.get('score_total','?')}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _user_allowed(update):
        return
    log.warning("rechazado user=%s chat=%s", update.effective_user.username if update.effective_user else "?", update.effective_chat.id)


def main() -> None:
    if not ALLOWED_USERS:
        log.error("AUREO_TG_ALLOWED_USERS vacío — el bot no respondería a nadie")
        sys.exit(1)
    log.info("AUREO bot arrancando. allowed_users=%s webhook=%s", ALLOWED_USERS, WEBHOOK_BASE)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(MessageHandler(filters.ALL, reject_unauthorized))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
