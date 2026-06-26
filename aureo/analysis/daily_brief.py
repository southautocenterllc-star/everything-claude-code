"""ÁUREO — brief diario del oro al Telegram (APOYO DISCRECIONAL, no señales).

Andrés decidió (2026-06-03): Áureo le avisa y él opera a mano. Esto NO da órdenes
de compra/venta (el research mostró que no hay edge automático); da CONTEXTO con
PRECIOS REALES para que Andrés decida:
  - precio actual del oro (feed real: Twelve Data / Yahoo, mismo que TradingView)
  - tendencia (medias 50/200) y volatilidad/rango esperado (ATR)
  - soporte/resistencia recientes
  - contexto macro del momento: dólar (DXY) y tasas 10Y → viento a favor/en contra
    (relación CONTEMPORÁNEA real del oro, no predicción)
  - próximo evento fuerte (FOMC/CPI/NFP) que puede volar el precio

Determinístico, sin Claude en el bucle (lección anti-BENDER). Por defecto NO
envía (imprime); con --send postea al grupo TRADING.

Uso:
    .venv/bin/python -m analysis.daily_brief            # preview (no manda)
    .venv/bin/python -m analysis.daily_brief --send     # manda al Telegram
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from core.data_feed import get_h1
from strategies.aureo_pine import atr, ema
from research.macro_features import fetch_yahoo_daily

EVENTS_CSV = ROOT / "analysis" / "patterns" / "events.csv"


def _macro_context() -> str:
    """Dólar y tasas: dirección de los últimos ~3 días → viento a favor/contra."""
    try:
        dxy = fetch_yahoo_daily("DX-Y.NYB").tail(4)
        y10 = fetch_yahoo_daily("^TNX").tail(4)
        d_dxy = (dxy.iloc[-1] / dxy.iloc[-2] - 1) * 100
        d_y10 = y10.iloc[-1] - y10.iloc[-2]
        dolar = "débil 🟢" if d_dxy < -0.05 else "fuerte 🔴" if d_dxy > 0.05 else "plano ⚪"
        tasas = "bajando 🟢" if d_y10 < -0.02 else "subiendo 🔴" if d_y10 > 0.02 else "planas ⚪"
        score = (1 if d_dxy < -0.05 else -1 if d_dxy > 0.05 else 0) + \
                (1 if d_y10 < -0.02 else -1 if d_y10 > 0.02 else 0)
        viento = ("viento A FAVOR del oro 🟢" if score > 0 else
                  "viento EN CONTRA del oro 🔴" if score < 0 else "neutro ⚪")
        return (f"Dólar (DXY): {dolar}  |  Tasas 10Y: {tasas}\n"
                f"➡️ {viento}")
    except Exception as e:  # noqa: BLE001
        return f"(contexto macro no disponible: {e})"


def _next_event() -> str:
    try:
        ev = pd.read_csv(EVENTS_CSV, parse_dates=["date"])
        today = pd.Timestamp.now().normalize()
        fut = ev[ev["date"] >= today].sort_values("date")
        if fut.empty:
            return "Sin eventos cargados próximos."
        row = fut.iloc[0]
        dias = (row["date"].normalize() - today).days
        cuando = "HOY" if dias == 0 else "mañana" if dias == 1 else f"en {dias} días"
        return f"⚠️ Próximo evento fuerte: {row['event']} {cuando} ({row['date'].date()}) — suele mover el precio."
    except Exception as e:  # noqa: BLE001
        return f"(calendario no disponible: {e})"


def build_brief() -> str:
    df, provider = get_h1(lookback=1000)
    close = df["close"]
    price = float(close.iloc[-1])
    e50 = ema(close, 50).iloc[-1]
    e200 = ema(close, 200).iloc[-1]
    a = float(atr(df, 14).iloc[-1])

    if price > e50 > e200:
        trend = "alcista 📈 (sobre medias 50 y 200)"
    elif price < e50 < e200:
        trend = "bajista 📉 (bajo medias 50 y 200)"
    else:
        trend = "lateral/mixta ↔️"

    wk = df.tail(120)
    sop = float(wk["low"].min())
    res = float(wk["high"].max())
    rango_lo, rango_hi = price - a, price + a

    when = pd.Timestamp.now(tz="UTC").strftime("%d-%b %H:%M UTC")
    return (
        f"🥇 *ÁUREO — Oro (XAU/USD) hoy*  _{when}_\n"
        f"Precio real: *{price:,.1f}* USD/oz\n"
        f"Tendencia: {trend}\n"
        f"Rango esperado (ATR): {rango_lo:,.0f} – {rango_hi:,.0f}\n"
        f"Soporte semana: {sop:,.0f}  |  Resistencia: {res:,.0f}\n"
        f"\n{_macro_context()}\n"
        f"\n{_next_event()}\n"
        f"\n_Esto es CONTEXTO, no una orden de compra/venta. Vos decidís._ "
        f"(fuente precio: {provider})"
    )


def send_telegram(text: str) -> int:
    import requests
    token = os.environ.get("AUREO_TG_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("AUREO_TG_BROADCAST_CHATS", "").split(",") if c.strip()]
    if not token or not chats:
        print("ERROR: falta AUREO_TG_BOT_TOKEN o AUREO_TG_BROADCAST_CHATS en .env", file=sys.stderr)
        return 1
    ok = 0
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
                          timeout=15)
        if r.status_code == 200:
            ok += 1
            print(f"enviado a {chat} ✅")
        else:
            print(f"falló {chat}: {r.status_code} {r.text[:120]}", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="Postear al Telegram (si no, solo preview).")
    args = ap.parse_args()
    brief = build_brief()
    print("=" * 60)
    print(brief)
    print("=" * 60)
    if args.send:
        return send_telegram(brief)
    print("\n[PREVIEW — no enviado. Agregá --send para mandarlo al grupo.]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
