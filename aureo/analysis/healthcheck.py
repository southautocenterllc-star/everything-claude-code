"""ÁUREO — healthcheck propio (vigía determinístico de los 2 semanas de demo).

Detecta si Áureo se rompe durante el período de demo y AVISA al grupo TRADING.
Sin Claude en el bucle (anti-BENDER), 100% lectura de estado del sistema. NO
opera, NO da órdenes: solo mira systemd, el latido del demo-trader, el feed del
oro, y la última corrida del demo-trader.

Chequeos:
  1. Servicios systemd --user activos (systemctl is-active por subprocess — esto
     es LECTURA de estado, no trading): aureo-mt5-terminal, aureo-demo-trader,
     aureo-bot, aureo-price-watch, aureo-study, y el timer aureo-market-study.timer.
  2. Latido del demo-trader: la última actividad del journal (último ts) debe ser
     de hace <= 90 min (el demo-trader corre cada hora). Si no se puede leer, NO
     se falsea OK: se marca desconocido (warn).
  3. Feed del oro vivo: core.data_feed.get_h1 responde y el último precio > 0.
  4. Última corrida del demo-trader (journalctl): si la última línea relevante es
     un error tipo "ok=False @ 0.0" (AlgoTrading apagado), se marca. Opcional y
     robusto: si journalctl no se puede leer, no rompe ni falsea.

Anti-spam (logs/healthcheck_state.json): no repite la MISMA alerta; cuando algo se
RECUPERA avisa "ya volvió". Si todo OK → no manda nada, solo loguea verde.

Uso:
    .venv/bin/python -m analysis.healthcheck --once           # chequea + imprime (no manda)
    .venv/bin/python -m analysis.healthcheck --once --send     # permite alertar
    .venv/bin/python -m analysis.healthcheck --daemon --send   # loop cada 15 min
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from analysis.daily_brief import send_telegram

JOURNAL = ROOT / "logs" / "aureo_trades.jsonl"
STATE_FILE = ROOT / "logs" / "healthcheck_state.json"

SERVICES = [
    "aureo-mt5-terminal",
    "aureo-demo-trader",
    "aureo-bot",
    "aureo-price-watch",
    "aureo-study",
]
TIMERS = ["aureo-market-study.timer"]

HEARTBEAT_MAX_MIN = 90
_SYSTEMCTL_TIMEOUT = 10
_JOURNALCTL_TIMEOUT = 10


def _is_active(unit: str) -> tuple[bool | None, str]:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=_SYSTEMCTL_TIMEOUT,
        )
        state = (r.stdout or r.stderr).strip() or "desconocido"
        return (state == "active", state)
    except Exception as e:  # noqa: BLE001
        return (None, f"error systemctl: {type(e).__name__}")


def check_services() -> list[tuple[str, bool | None, str]]:
    results = []
    for unit in SERVICES + TIMERS:
        ok, state = _is_active(unit)
        results.append((f"svc:{unit}", ok, state))
    return results


def _journal_last_ts() -> "pd.Timestamp | None":
    try:
        from core.trade_journal import _read
        rows = _read()
    except Exception:  # noqa: BLE001
        return None
    last_ts = None
    for r in rows:
        for field in ("ts_close", "ts_open"):
            v = r.get(field)
            if not v:
                continue
            try:
                t = pd.Timestamp(v)
                if t.tzinfo is None:
                    t = t.tz_localize("UTC")
                if last_ts is None or t > last_ts:
                    last_ts = t
            except Exception:  # noqa: BLE001
                continue
    return last_ts


def _service_last_activity() -> "pd.Timestamp | None":
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "aureo-demo-trader",
             "-n", "1", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=_JOURNALCTL_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return None
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return None
    stamp = line[-1].split(" ", 1)[0]
    try:
        t = pd.Timestamp(stamp)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    except Exception:  # noqa: BLE001
        return None


def check_heartbeat() -> tuple[str, bool | None, str]:
    key = "heartbeat:demo-trader"
    now = pd.Timestamp.now(tz="UTC")

    svc_ts = _service_last_activity()
    if svc_ts is not None:
        age = (now - svc_ts).total_seconds() / 60.0
        detail = f"último log del service hace {age:.0f} min ({svc_ts.strftime('%d-%b %H:%M UTC')})"
        return (key, age <= HEARTBEAT_MAX_MIN, detail)

    j_ts = _journal_last_ts()
    if j_ts is not None:
        age = (now - j_ts).total_seconds() / 60.0
        detail = (f"sin log del service; última escritura al journal hace {age:.0f} min "
                  f"({j_ts.strftime('%d-%b %H:%M UTC')}) — nota: el journal no se escribe "
                  f"con posición abierta, puede ser viejo sin que el trader esté caído")
        return (key, True if age <= HEARTBEAT_MAX_MIN else None, detail)

    return (key, None, "no pude leer ni el log del service ni el journal")


def check_feed() -> tuple[str, bool | None, str]:
    key = "feed:gold"
    try:
        from core.data_feed import get_h1
        df, provider = get_h1(lookback=5)
        price = float(df["close"].iloc[-1])
        if price > 0:
            return (key, True, f"precio {price:,.1f} (fuente {provider})")
        return (key, False, f"precio no positivo: {price} (fuente {provider})")
    except Exception as e:  # noqa: BLE001
        return (key, False, f"feed caído: {str(e)[:80]}")


def check_demo_last_run() -> tuple[str, bool | None, str]:
    key = "demo-trader:last-run"
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "aureo-demo-trader",
             "-n", "60", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=_JOURNALCTL_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return (key, None, f"journalctl no disponible: {type(e).__name__}")
    if r.returncode != 0 and not r.stdout:
        return (key, None, f"journalctl rc={r.returncode}")

    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return (key, None, "sin logs recientes del demo-trader")

    bad_markers = ["ok=False @ 0.0", "ok=false @ 0.0", "algotrading", "autotrading"]
    for ln in reversed(lines):
        low = ln.lower()
        if "ok=false @ 0.0" in low:
            return (key, False, f"última corrida con error: {ln.strip()[:90]}")
        if ("algotrading" in low or "autotrading" in low) and ("off" in low or "disabled" in low or "apagad" in low):
            return (key, False, f"AlgoTrading apagado: {ln.strip()[:90]}")
    return (key, True, "sin error 'ok=False @ 0.0' en las últimas líneas")


def run_checks() -> list[tuple[str, bool | None, str]]:
    results: list[tuple[str, bool | None, str]] = []
    results += check_services()
    results.append(check_heartbeat())
    results.append(check_feed())
    results.append(check_demo_last_run())
    return results


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


def _status_str(ok: bool | None) -> str:
    return "OK" if ok is True else "FALLA" if ok is False else "DESCONOCIDO"


def _build_alert(failing: list[tuple[str, bool | None, str]]) -> str:
    when = pd.Timestamp.now(tz="UTC").strftime("%d-%b %H:%M UTC")
    head = f"🚨 *ÁUREO — healthcheck: algo se rompió* _{when}_\n\n"
    for key, ok, detail in failing:
        head += f"🔴 *{key}* — {detail}\n"
    head += "\n_Vigía determinístico. Revisá el servicio/feed afectado._"
    return head


def _build_recovery(recovered_keys: list[str]) -> str:
    when = pd.Timestamp.now(tz="UTC").strftime("%d-%b %H:%M UTC")
    head = f"✅ *ÁUREO — ya volvió* _{when}_\n\n"
    head += "Se recuperó:\n"
    for k in recovered_keys:
        head += f"🟢 {k}\n"
    return head


def run_once(send: bool = False) -> int:
    results = run_checks()
    state = _load_state()
    prev_failing = set(state.get("failing", []))

    failing = [(k, ok, d) for (k, ok, d) in results if ok is False]
    unknown = [(k, ok, d) for (k, ok, d) in results if ok is None]
    failing_keys = {k for (k, _, _) in failing}

    when = pd.Timestamp.now(tz="UTC").strftime("%d-%b %H:%M UTC")
    print(f"=== ÁUREO healthcheck — {when} ===")
    for key, ok, detail in results:
        mark = "🟢" if ok is True else "🔴" if ok is False else "⚪"
        print(f"{mark} {key:32s} [{_status_str(ok)}] {detail}")

    recovered = sorted(prev_failing - failing_keys)
    new_failing = [(k, ok, d) for (k, ok, d) in failing if k not in prev_failing]

    rc = 0
    if not failing:
        if recovered:
            print(f"[healthcheck] RECUPERADO: {recovered}")
            if send:
                send_telegram(_build_recovery(recovered))
            else:
                print("[healthcheck] (recuperación detectada — PREVIEW, agregá --send)")
        else:
            print("[healthcheck] VERDE — todo OK, no se manda nada.")
    else:
        print(f"[healthcheck] FALLAS: {sorted(failing_keys)}"
              f"{' (nuevas: ' + str([k for k,_,_ in new_failing]) + ')' if new_failing else ' (ya avisadas)'}")
        if unknown:
            print(f"[healthcheck] desconocidos (no alertan): {[k for k,_,_ in unknown]}")
        rc = 1
        if new_failing:
            if send:
                send_telegram(_build_alert(failing))
            else:
                print("[healthcheck] (alerta ameritada — PREVIEW, agregá --send para avisar)")
        else:
            print("[healthcheck] (mismas fallas ya avisadas — anti-spam, no re-manda)")
        if recovered and send:
            send_telegram(_build_recovery(recovered))

    state["failing"] = sorted(failing_keys)
    state["last_run"] = pd.Timestamp.now(tz="UTC").isoformat()
    _save_state(state)
    return rc


def run_daemon(poll_seconds: int = 900, send: bool = False) -> int:
    print(f"[healthcheck] vigía ON (cada {poll_seconds//60} min). Ctrl-C para parar.")
    while True:
        try:
            run_once(send=send)
        except Exception as e:  # noqa: BLE001
            print(f"[healthcheck] err ciclo (sigue): {e}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    ap = argparse.ArgumentParser(description="ÁUREO — healthcheck determinístico.")
    ap.add_argument("--once", action="store_true", help="Un chequeo (default).")
    ap.add_argument("--daemon", action="store_true", help="Loop cada 15 min.")
    ap.add_argument("--send", action="store_true",
                    help="Permitir alertar al Telegram. Sin él = preview.")
    ap.add_argument("--poll", type=int, default=900, help="Segundos entre chequeos en daemon.")
    args = ap.parse_args()
    if args.daemon:
        return run_daemon(poll_seconds=args.poll, send=args.send)
    return run_once(send=args.send)


if __name__ == "__main__":
    raise SystemExit(main())
