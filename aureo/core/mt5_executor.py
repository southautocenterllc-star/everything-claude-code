"""Conector MT5 del lado Áureo (Linux) → invoca el puente en wine-python.

Interfaz limpia para que Áureo lea la cuenta y mande/cierre órdenes en MT5
(que corre bajo Wine). Lee credenciales del .env. NO contiene secretos.

TRABA DURA DE SEGURIDAD: solo opera si el servidor es DEMO (contiene "Demo")
y AUREO_MT5_ALLOW_DEMO_TRADE=1. Para real haría falta AUREO_ALLOW_LIVE=1
explícito — y NO se enciende hasta tener una estrategia con edge validado
(hoy NO la hay; ver research/). Esto es para ver el robot operar en demo.

Uso:
    .venv/bin/python -m core.mt5_executor info
    .venv/bin/python -m core.mt5_executor order SELL 0.01 4490 4430
    .venv/bin/python -m core.mt5_executor close
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

WINEPREFIX = str(ROOT / "data" / "mt5" / "wineprefix")
PYWIN = f"{WINEPREFIX}/drive_c/Program Files/Python311/python.exe"
BRIDGE = "core/mt5_bridge.py"
SYMBOL_DEFAULT = "XAUUSD"


class ExecutorError(RuntimeError):
    pass


def _guard_can_trade() -> None:
    server = os.environ.get("MT5_SERVER", "")
    allow_live = os.environ.get("AUREO_ALLOW_LIVE") == "1"
    allow_demo = os.environ.get("AUREO_MT5_ALLOW_DEMO_TRADE") == "1"
    is_demo = "demo" in server.lower()
    if allow_live:
        return  # real explícitamente habilitado (no usar sin edge validado)
    if not (is_demo and allow_demo):
        raise ExecutorError(
            f"TRABA: solo demo. server={server!r} is_demo={is_demo} "
            f"allow_demo={allow_demo}. Para real: AUREO_ALLOW_LIVE=1 (NO recomendado sin edge).")


def _run_bridge(args: list[str], timeout: float = 180) -> dict:
    env = dict(os.environ)
    env["WINEPREFIX"] = WINEPREFIX
    env["WINEDEBUG"] = "-all"
    env.setdefault("DISPLAY", ":0")
    for k in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        if not env.get(k):
            raise ExecutorError(f"falta {k} en el entorno/.env")
    cmd = ["wine", PYWIN, BRIDGE, *args]
    try:
        out = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                             text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ExecutorError(f"timeout corriendo el puente: {e}") from e
    for line in out.stdout.splitlines():
        if line.startswith("AUREO_MT5 "):
            return json.loads(line[len("AUREO_MT5 "):])
    raise ExecutorError(f"el puente no devolvió resultado. stderr/cola: "
                        f"{out.stdout[-300:]}")


def info() -> dict:
    return _run_bridge(["info"])


def report() -> dict:
    """Posiciones abiertas + deals cerrados de Áureo (lectura, sin traba)."""
    return _run_bridge(["report"])


def order(side: str, volume: float, sl: float = 0, tp: float = 0,
          symbol: str = SYMBOL_DEFAULT, setup: str = "manual",
          context: dict | None = None) -> dict:
    _guard_can_trade()
    r = _run_bridge(["order", side.upper(), str(volume), str(sl), str(tp), symbol])
    # APRENDIZAJE CONSTANTE: toda orden exitosa se registra con su contexto.
    try:
        from core.trade_journal import log_trade
        log_trade(r, setup=setup, context=context)
    except Exception as e:  # noqa: BLE001 — el log nunca debe tumbar la operación
        print(f"[journal] aviso: no se pudo registrar el trade: {e}", file=sys.stderr)
    return r


def modify(ticket, sl: float = 0, tp: float = 0, symbol: str = SYMBOL_DEFAULT) -> dict:
    """Cambia SL/TP de una posición abierta (breakeven). Misma traba demo."""
    _guard_can_trade()
    return _run_bridge(["modify", str(ticket), str(sl), str(tp), symbol])


def close(symbol: str = SYMBOL_DEFAULT) -> dict:
    _guard_can_trade()
    return _run_bridge(["close", symbol])


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("uso: info | order SIDE vol sl tp [symbol] | modify ticket sl tp [symbol] | close [symbol]"); return 1
    try:
        cmd = args[0]
        if cmd == "info":
            r = info()
        elif cmd == "report":
            r = report()
        elif cmd == "order":
            r = order(args[1], float(args[2]),
                      float(args[3]) if len(args) > 3 else 0,
                      float(args[4]) if len(args) > 4 else 0,
                      args[5] if len(args) > 5 else SYMBOL_DEFAULT)
        elif cmd == "modify":
            r = modify(args[1], float(args[2]) if len(args) > 2 else 0,
                       float(args[3]) if len(args) > 3 else 0,
                       args[4] if len(args) > 4 else SYMBOL_DEFAULT)
        elif cmd == "close":
            r = close(args[1] if len(args) > 1 else SYMBOL_DEFAULT)
        else:
            print(f"comando desconocido: {cmd}"); return 1
    except ExecutorError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2
    print(json.dumps(r, indent=2, default=str))
    return 0 if r.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())