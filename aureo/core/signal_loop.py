"""Signal loop H1 de AUREO — modo PAPEL por defecto, sin TradingView.

Qué hace, en una línea: cada vez que cierra una vela H1, recalcula la señal de
la estrategia sobre data en vivo y, si hay entrada/salida nueva, la REGISTRA en
un journal local. NO postea al webhook de Vercel salvo que `AUREO_LIVE=1` esté
explícito en el entorno (y aún así exige URL+secret). Por defecto: papel puro.

Por qué papel: al 2026-06-02 NO hay estrategia con edge validado (la 'balanced'
murió en el backtest oficial; el prior de patterns salió sin señales
significativas para oro). El loop existe para tener la TUBERÍA lista y validada
con data en vivo —feed → evaluación bar-close → journal— de modo que cuando el
research entregue un edge solo haya que (1) cambiar el evaluador y (2) encender
`AUREO_LIVE=1`. Hasta entonces lo que loguea son señales de PLACEHOLDER, NO
operables.

Diseño anti-BENDER: el loop es 100% DETERMINÍSTICO. No hay ninguna llamada a
Claude/LLM dentro del bucle. Claude solo se usa on-demand fuera de este proceso
(research, reportes). Esta fue una lección dura del proyecto.

Pyramiding=0: una sola posición de papel a la vez (igual que el engine y el
webhook real). Las salidas (SL/TP1/TP2) se evalúan bar-by-bar igual que en
`backtest.engine`, regla conservadora: si una barra toca SL y TP, gana el SL.

Modos:
    --once     procesa las velas nuevas desde el último estado y termina
               (ideal para un systemd timer horario).
    --daemon   bucle infinito, duerme hasta poco después del próximo cierre H1.

Estado persistente en logs/paper_state.json (última barra procesada + posición
abierta) para que --once sea correcto entre invocaciones.

CLI:
    .venv/bin/python -m core.signal_loop --once
    .venv/bin/python -m core.signal_loop --daemon
    .venv/bin/python -m core.signal_loop --once --replay 50   # backfill papel
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.data_feed import FeedError, get_h1
from strategies.aureo_pine import PineParams, generate_signals

LOGS = ROOT / "logs"
JOURNAL = LOGS / "paper_journal.jsonl"
STATE = LOGS / "paper_state.json"

# Costos de referencia para el PnL de papel (mismos que el backtest oficial).
SPREAD = 0.35          # USD/oz, half a cada lado
SWAP_PER_NIGHT = 0.5   # USD/oz/noche

SYMBOL = "XAUUSD"
PIP = 0.01


# --------------------------------------------------------------------------- #
# Evaluador enchufable. Hoy: PLACEHOLDER sin edge. Cuando el research entregue
# un edge validado, registrar acá la nueva función y apuntar ACTIVE_STRATEGY.
# --------------------------------------------------------------------------- #
def _eval_balanced_placeholder(df: pd.DataFrame) -> pd.DataFrame:
    """Estrategia 'balanced' (EMA+RSI+ATR). SIN EDGE — solo para probar la
    tubería en papel. Devuelve el DF de señales del engine."""
    return generate_signals(df, PineParams())


STRATEGIES = {
    "balanced_placeholder": _eval_balanced_placeholder,
}
ACTIVE_STRATEGY = "balanced_placeholder"


@dataclass
class Position:
    direction: int      # +1 long, -1 short
    entry_time: str     # ISO
    entry_price: float
    sl: float
    tp1: float
    tp2: float
    tp1_hit: bool = False


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            pass
    return {"last_bar": None, "position": None}


def _save_state(state: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _journal(entry: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _maybe_post(payload: dict) -> dict:
    """Postea al webhook SOLO si AUREO_LIVE=1 y hay URL+secret. Si no, no-op.

    En papel esto nunca corre. Existe para que el día que haya edge se encienda
    con una sola variable de entorno, sin reescribir el loop."""
    if os.environ.get("AUREO_LIVE") != "1":
        return {"posted": False, "reason": "paper_mode"}
    url = os.environ.get("AUREO_WEBHOOK_URL", "").strip()
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not url or not secret:
        return {"posted": False, "reason": "missing_url_or_secret"}
    import requests
    body = dict(payload, secret=secret)
    try:
        r = requests.post(url, json=body, timeout=15)
        return {"posted": True, "status": r.status_code, "resp": r.text[:200]}
    except requests.RequestException as e:
        return {"posted": False, "reason": f"post_failed: {e}"}


def _emit(action: str, bar_time: pd.Timestamp, price: float,
          sl, tp1, tp2, *, strategy: str, provider: str,
          score_total: float = 0.0, extra: dict | None = None) -> None:
    """Construye el payload con el contrato del webhook y lo registra (y postea
    si está en vivo). `action` ∈ {BUY, SELL, CLOSE}."""
    payload = {
        "source": "aureo_signal_loop",
        "symbol": SYMBOL,
        "action": action,
        "price": round(float(price), 3),
        "sl": None if sl is None else round(float(sl), 3),
        "tp1": None if tp1 is None else round(float(tp1), 3),
        "tp2": None if tp2 is None else round(float(tp2), 3),
        "score_total": score_total,
    }
    post_result = _maybe_post(payload)
    entry = {
        "ts_emit": pd.Timestamp.now(tz="UTC").isoformat(),
        "bar_time": pd.Timestamp(bar_time).isoformat(),
        "mode": "live" if os.environ.get("AUREO_LIVE") == "1" else "paper",
        "strategy": strategy,
        "provider": provider,
        "payload": payload,
        "post": post_result,
    }
    if extra:
        entry.update(extra)
    _journal(entry)
    print(f"[{entry['mode']}] {action} @ {payload['price']} "
          f"bar={entry['bar_time']} strat={strategy} -> journal")


def _check_exit(pos: Position, bar: pd.Series) -> tuple[str | None, float, float]:
    """Replica la lógica de salida del engine para una barra. Devuelve
    (outcome|None, exit_price, pnl_por_unidad sin swap). Regla conservadora:
    si toca SL y TP en la misma barra, gana el SL."""
    d = pos.direction
    hi, lo = float(bar["high"]), float(bar["low"])
    sl_hit = (d == 1 and lo <= pos.sl) or (d == -1 and hi >= pos.sl)
    tp1_hit = (d == 1 and hi >= pos.tp1) or (d == -1 and lo <= pos.tp1)
    tp2_hit = (d == 1 and hi >= pos.tp2) or (d == -1 and lo <= pos.tp2)

    def _pnl(exit_px_pair):  # promedio de dos niveles (cierre parcial 50/50)
        a, b = exit_px_pair
        return ((a - pos.entry_price) * d + (b - pos.entry_price) * d) / 2 - SPREAD

    if sl_hit and (tp1_hit or tp2_hit):
        if pos.tp1_hit:
            return "TP1_then_SL", (pos.tp1 + pos.sl) / 2, _pnl((pos.tp1, pos.sl))
        return "SL", pos.sl, (pos.sl - pos.entry_price) * d - SPREAD
    if tp2_hit:
        return "TP2", (pos.tp1 + pos.tp2) / 2, _pnl((pos.tp1, pos.tp2))
    if sl_hit:
        if pos.tp1_hit:
            return "TP1_then_SL", (pos.tp1 + pos.sl) / 2, _pnl((pos.tp1, pos.sl))
        return "SL", pos.sl, (pos.sl - pos.entry_price) * d - SPREAD
    if tp1_hit and not pos.tp1_hit:
        pos.tp1_hit = True
        pos.sl = pos.entry_price  # break-even tras TP1 (igual que el engine)
    return None, 0.0, 0.0


def process_new_bars(state: dict, df: pd.DataFrame, signals: pd.DataFrame,
                     provider: str, strategy: str) -> dict:
    """Procesa, en orden, todas las barras de `df` posteriores a state['last_bar'].
    Muta y devuelve el estado. Determinístico."""
    last_bar = pd.Timestamp(state["last_bar"]) if state["last_bar"] else None
    pos = Position(**state["position"]) if state["position"] else None

    new_idx = df.index if last_bar is None else df.index[df.index > last_bar]
    for ts in new_idx:
        bar = df.loc[ts]
        closed_this_bar = False
        # 1) si hay posición abierta, ¿salió en esta barra?
        if pos is not None:
            outcome, exit_px, pnl = _check_exit(pos, bar)
            if outcome is not None:
                nights = max((ts.normalize() -
                              pd.Timestamp(pos.entry_time).normalize()).days, 0)
                pnl_net = pnl - SWAP_PER_NIGHT * nights
                _emit("CLOSE", ts, exit_px, None, None, None,
                      strategy=strategy, provider=provider,
                      extra={"outcome": outcome, "pnl_unit": round(pnl_net, 3),
                             "nights": nights, "entry_time": pos.entry_time})
                pos = None
                closed_this_bar = True
        # 2) entrada: SOLO si no hay posición Y no cerramos en esta barra. El
        #    engine hace `continue` tras cerrar (no abre en la barra de salida);
        #    replicarlo es lo que mantiene la PARIDAD con el backtest oficial.
        if pos is None and not closed_this_bar:
            sig = int(signals.loc[ts, "signal"]) if ts in signals.index else 0
            if sig != 0:
                row = signals.loc[ts]
                entry_px = float(df.loc[ts, "close"]) + (SPREAD / 2) * sig
                pos = Position(
                    direction=sig,
                    entry_time=ts.isoformat(),
                    entry_price=entry_px,
                    sl=float(row["sl"]), tp1=float(row["tp1"]), tp2=float(row["tp2"]),
                )
                _emit("BUY" if sig == 1 else "SELL", ts, entry_px,
                      row["sl"], row["tp1"], row["tp2"],
                      strategy=strategy, provider=provider)

    # Avanzar el estado de forma MONÓTONA: si el feed trajo menos data que la
    # corrida previa, NO rebobinar last_bar (evita re-emitir barras intermedias).
    new_last = df.index[-1]
    if last_bar is not None and new_last < last_bar:
        new_last = last_bar
    state["last_bar"] = str(new_last)
    state["position"] = asdict(pos) if pos else None
    return state


def run_once(replay: int = 0, lookback: int = 1000) -> int:
    strategy = ACTIVE_STRATEGY
    evaluator = STRATEGIES[strategy]
    try:
        df, provider = get_h1(lookback=lookback)
    except FeedError as e:
        print(f"ERROR feed: {e}", file=sys.stderr)
        return 1
    if len(df) < 220:  # EMA200 + colchón
        print(f"ADVERTENCIA: solo {len(df)} velas, insuficiente para EMA200; "
              "señales pueden ser NaN.", file=sys.stderr)

    signals = evaluator(df)
    state = _load_state()

    if replay and state["last_bar"] is None:
        # backfill: arrancar 'replay' barras atrás para poblar el journal
        if len(df) > replay:
            state["last_bar"] = str(df.index[-replay - 1])

    state = process_new_bars(state, df, signals, provider, strategy)
    _save_state(state)
    held = "sí" if state["position"] else "no"
    print(f"OK once: fuente={provider} velas={len(df)} última={state['last_bar']} "
          f"posición_abierta={held}")
    return 0


def run_daemon(poll_seconds: int = 60, lookback: int = 1000) -> int:
    print("Daemon papel arrancado. Ctrl-C para parar. "
          f"(AUREO_LIVE={os.environ.get('AUREO_LIVE', '0')})")
    last_processed_hour = None
    while True:
        now = pd.Timestamp.now(tz="UTC")
        # procesar una vez por hora, poco después del cierre de la vela
        if last_processed_hour != now.floor("h") and now.minute >= 1:
            try:
                run_once(lookback=lookback)
                last_processed_hour = now.floor("h")
            except Exception as e:  # noqa: BLE001 — el daemon no debe morirse
                print(f"ERROR ciclo (continúa): {e}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal loop H1 AUREO (papel).")
    ap.add_argument("--once", action="store_true", help="Procesa nuevas barras y sale.")
    ap.add_argument("--daemon", action="store_true", help="Bucle infinito horario.")
    ap.add_argument("--replay", type=int, default=0,
                    help="En la 1ª corrida, backfillea N barras al journal.")
    ap.add_argument("--lookback", type=int, default=1000)
    args = ap.parse_args()

    if args.daemon:
        return run_daemon(lookback=args.lookback)
    if args.once or True:  # default = once
        return run_once(replay=args.replay, lookback=args.lookback)


if __name__ == "__main__":
    raise SystemExit(main())