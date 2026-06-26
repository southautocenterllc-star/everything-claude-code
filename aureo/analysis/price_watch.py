"""ÁUREO — vigía de precios del oro. Alerta al Telegram al tocar cada nivel.

Vigila el precio en vivo (XAU/USD) y dispara una alerta cuando toca/cruza un
nivel importante definido en config/price_levels.json (zona de venta, quiebre,
objetivos, invalidación). Determinístico, sin Claude en el bucle (anti-BENDER).

Anti-spam: cada nivel se "dispara" una sola vez; se RE-ARMA cuando el precio se
aleja del nivel más allá de `buffer` (así no repite la misma alerta en cada
poll, pero sí vuelve a avisar si el precio sale y regresa).

Tipos de nivel:
  - zone  {lo, hi}  → dispara cuando el precio entra en [lo, hi]
  - below {price}   → dispara cuando el precio cae por debajo de price
  - above {price}   → dispara cuando el precio sube por encima de price

Uso:
    .venv/bin/python -m analysis.price_watch --once            # un chequeo (timer)
    .venv/bin/python -m analysis.price_watch --daemon          # vigila cada 3 min
    .venv/bin/python -m analysis.price_watch --once --dry-run  # no manda, imprime
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

LEVELS_FILE = ROOT / "config" / "price_levels.json"
STATE_FILE = ROOT / "logs" / "price_watch_state.json"
_UA = {"User-Agent": "Mozilla/5.0 AUREO price_watch"}


def get_spot_price() -> tuple[float, str]:
    """Precio más fresco posible (último 1m de Yahoo GC=F). Fallback: feed H1."""
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
                         params={"interval": "1m", "range": "1d"}, headers=_UA, timeout=15)
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        ts = res["timestamp"]
        for px, t in zip(reversed(closes), reversed(ts)):
            if px is not None:
                return float(px), pd.to_datetime(t, unit="s", utc=True).strftime("%H:%M UTC")
    except Exception:  # noqa: BLE001
        pass
    from core.data_feed import get_h1
    df, _ = get_h1(lookback=5)
    return float(df["close"].iloc[-1]), df.index[-1].strftime("%H:%M UTC")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            pass
    return {}


def _save_state(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2))


def _triggered(level: dict, price: float) -> bool:
    t = level["type"]
    if t == "zone":
        return level["lo"] <= price <= level["hi"]
    if t == "below":
        return price < level["price"]
    if t == "above":
        return price > level["price"]
    return False


def _clear_of(level: dict, price: float, buf: float) -> bool:
    """True si el precio está claramente FUERA del nivel (para re-armar)."""
    t = level["type"]
    if t == "zone":
        return price < level["lo"] - buf or price > level["hi"] + buf
    if t == "below":
        return price > level["price"] + buf
    if t == "above":
        return price < level["price"] - buf
    return True


def check(price: float, when: str, cfg: dict, state: dict) -> list[str]:
    buf = float(cfg.get("buffer", 3.0))
    alerts = []
    for lvl in cfg["levels"]:
        name = lvl["name"]
        armed = state.get(name, {}).get("armed", True)
        if _triggered(lvl, price) and armed:
            alerts.append(f"{lvl.get('emoji', '🔔')} *ÁUREO alerta* — {price:,.1f} ({when})\n{lvl['msg']}")
            state[name] = {"armed": False}
        elif _clear_of(lvl, price, buf):
            state[name] = {"armed": True}
    return alerts


def send_telegram(text: str) -> bool:
    token = os.environ.get("AUREO_TG_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("AUREO_TG_BROADCAST_CHATS", "").split(",") if c.strip()]
    if not token or not chats:
        print("ERROR: falta AUREO_TG_BOT_TOKEN/AUREO_TG_BROADCAST_CHATS", file=sys.stderr)
        return False
    ok = False
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "Markdown"}, timeout=15)
        ok = ok or r.status_code == 200
    return ok


def run_once(dry: bool = False) -> int:
    cfg = json.loads(LEVELS_FILE.read_text())
    price, when = get_spot_price()
    state = _load_state()
    alerts = check(price, when, cfg, state)
    _save_state(state)
    print(f"precio {price:,.1f} ({when}) | niveles armados: "
          f"{[l['name'] for l in cfg['levels'] if state.get(l['name'],{}).get('armed',True)]}")
    for a in alerts:
        print("ALERTA:\n" + a)
        if not dry:
            send_telegram(a)
    if not alerts:
        print("(sin nivel tocado)")
    return 0


def run_daemon(poll_seconds: int = 180) -> int:
    print(f"Vigía de precios ON (cada {poll_seconds//60} min). Ctrl-C para parar.")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"err ciclo (sigue): {e}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--poll", type=int, default=180)
    args = ap.parse_args()
    if args.daemon:
        return run_daemon(args.poll)
    return run_once(dry=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
