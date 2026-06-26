"""ÁUREO — operador automático en DEMO (deterministico, anti-BENDER).

Andrés pidió que Áureo "siga operando" en la demo para verlo andar y JUNTAR
DATOS DE APRENDIZAJE. Cada hora: evalúa la estrategia sobre el último cierre H1,
y si hay entrada nueva y NO hay posición abierta (pyramiding=0), mete la orden en
la demo vía core.mt5_executor (que la registra sola en el journal). El stop/TP los
maneja MT5. Luego reconcilia resultados → el aprendizaje se cierra.

HONESTO: la estrategia activa (balanced) NO tiene edge — esto es para ver la
máquina operar y acumular datos reales, NO para ganar. Solo DEMO (traba dura en
el executor). Sin Claude en el bucle: 100% determinístico.

Uso:
    .venv/bin/python -m core.demo_trader --once     # una evaluación (timer)
    .venv/bin/python -m core.demo_trader --daemon   # cada hora
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.data_feed import FeedError, get_h1
from strategies.aureo_pine import PineParams, generate_signals

SETUP = "demo_auto_balanced"   # estrategia activa (placeholder sin edge)
VOLUME = 0.01
SYMBOL = "XAUUSD"

# Parámetros del DEMO: TP cortos a escala H1 (el default global 3.0/6.0 ATR son
# objetivos de swing que tardan días en H1 — se dejan intactos porque son el
# baseline de todo el research). Acá: SL 1.0 / TP1 1.5 / TP2 3.0 ATR → R:R 1.5,
# objetivos de ~1.5*ATR (≈30-40 USD) que cierran en horas, no días, y dan más
# trades cerrados = más datos de aprendizaje, que es el propósito del demo.
DEMO_PARAMS = PineParams(atr_sl_mult=1.0, atr_tp1_mult=1.5, atr_tp2_mult=3.0)

# Breakeven en vivo: cuando un trade abierto va a favor ≥ este múltiplo de ATR,
# mover el SL a la entrada para que no se devuelva a pérdida (el engine de
# backtest ya lo hace tras TP1; el camino vivo no lo tenía).
BE_TRIGGER_MULT = 1.0


def evaluate_and_trade() -> None:
    from core.mt5_executor import report, order, ExecutorError
    from core.trade_journal import reconcile

    try:
        df, prov = get_h1(lookback=1000)
    except FeedError as e:
        print(f"[demo_trader] feed falló: {e}", file=sys.stderr); return

    sig = generate_signals(df, DEMO_PARAMS)
    last = sig.iloc[-1]
    s = int(last["signal"])
    bar = df.index[-1]

    # pyramiding=0: no abrir si ya hay posición de Áureo
    try:
        rep = report()
    except ExecutorError as e:
        print(f"[demo_trader] no pude leer MT5: {e}", file=sys.stderr); return
    open_aureo = rep.get("open", [])

    if s != 0 and not open_aureo:
        side = "BUY" if s == 1 else "SELL"
        ctx = {"price": round(float(last["close"]), 2),
               "rsi_h1": round(float(last["rsi"]), 1),
               "atr_h1": round(float(last["atr"]), 2),
               "hour_utc": int(bar.hour), "provider": prov, "bar": str(bar)}
        try:
            r = order(side, VOLUME, sl=round(float(last["sl"]), 2),
                      tp=round(float(last["tp1"]), 2), symbol=SYMBOL,
                      setup=SETUP, context=ctx)
            print(f"[demo_trader] {side} {SYMBOL} -> ok={r.get('ok')} @ {r.get('price')}")
        except ExecutorError as e:
            print(f"[demo_trader] orden rechazada: {e}", file=sys.stderr)
    else:
        if open_aureo:
            _manage_breakeven(open_aureo, float(last["atr"]))
        motivo = "ya hay posición abierta" if open_aureo else "sin señal en esta barra"
        print(f"[demo_trader] no opera ({motivo}). bar={bar} signal={s}")

    # APRENDIZAJE: reconciliar resultados de trades que cerraron
    try:
        reconcile()
    except Exception as e:  # noqa: BLE001
        print(f"[demo_trader] reconcile aviso: {e}", file=sys.stderr)


def _manage_breakeven(open_aureo: list, atr: float, trigger_mult: float = BE_TRIGGER_MULT) -> None:
    """Mueve el SL a la entrada en posiciones que ya van a favor ≥ trigger_mult*ATR.
    Solo aprieta el stop (nunca lo afloja) y solo una vez (idempotente: tras
    quedar en entrada, la condición already_be lo frena)."""
    from core.mt5_executor import ExecutorError, info, modify

    if not open_aureo or atr <= 0:
        return
    try:
        tick = info()
    except ExecutorError as e:
        print(f"[demo_trader] BE: no pude leer precio: {e}", file=sys.stderr); return
    bid = tick.get("bid"); ask = tick.get("ask")
    if not bid or not ask:
        print("[demo_trader] BE: sin tick válido", file=sys.stderr); return

    for p in open_aureo:
        side = p.get("type")
        entry = float(p.get("price_open") or 0)
        cur_sl = float(p.get("sl") or 0)
        tp = float(p.get("tp") or 0)
        ticket = p.get("ticket")
        sym = p.get("symbol", SYMBOL)
        if not entry or not ticket:
            continue
        if side == "BUY":
            in_profit = (bid - entry) >= trigger_mult * atr
            already_be = cur_sl >= entry            # SL ya en/sobre la entrada
        elif side == "SELL":
            in_profit = (entry - ask) >= trigger_mult * atr
            already_be = 0 < cur_sl <= entry        # SL ya en/bajo la entrada
        else:
            continue
        if in_profit and not already_be:
            try:
                modify(ticket, sl=round(entry, 2), tp=tp, symbol=sym)
                print(f"[demo_trader] BE: {side} {ticket} SL->entrada {entry:.2f}")
            except ExecutorError as e:
                print(f"[demo_trader] BE modify falló ({ticket}): {e}", file=sys.stderr)


def run_daemon(poll_seconds: int = 300) -> int:
    print("[demo_trader] operador demo ON (evalúa al cambiar de hora). Ctrl-C para parar.")
    last_hour = None
    while True:
        now = pd.Timestamp.now(tz="UTC")
        if last_hour != now.floor("h") and now.minute >= 1:
            try:
                evaluate_and_trade()
                last_hour = now.floor("h")
            except Exception as e:  # noqa: BLE001
                print(f"[demo_trader] err ciclo (sigue): {e}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--poll", type=int, default=300)
    args = ap.parse_args()
    if args.daemon:
        return run_daemon(args.poll)
    evaluate_and_trade()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())