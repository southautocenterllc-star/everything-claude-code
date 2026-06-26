"""Sistema de APRENDIZAJE CONSTANTE de Áureo.

Regla de Andrés (siempre): cada operación se registra con su CONTEXTO al entrar,
y luego se reconcilia su RESULTADO. Con eso se calculan estadísticas que mejoran
con cada trade — qué condiciones funcionan y cuáles no. Es la única forma honesta
de aprender de la operación real (aunque sea demo) y, eventualmente, encontrar un
edge condicional que el backtest a ciegas no ve.

Archivos (logs/, gitignored):
  - aureo_trades.jsonl   : una línea por trade ABIERTO, con contexto.
  - reconcilia contra MT5 (core.mt5_executor.report) para cerrar outcomes.

Uso:
    .venv/bin/python -m core.trade_journal reconcile   # actualiza resultados
    .venv/bin/python -m core.trade_journal stats       # win-rate, PF, por condición
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
JOURNAL = ROOT / "logs" / "aureo_trades.jsonl"


def capture_context() -> dict:
    """Foto del mercado al momento de entrar (multi-timeframe + macro)."""
    ctx = {}
    try:
        from core.data_feed import get_h1
        from strategies.aureo_pine import ema, rsi, atr
        h1, prov = get_h1(lookback=1000)
        c = h1["close"]
        ctx["price"] = round(float(c.iloc[-1]), 2)
        ctx["provider"] = prov
        ctx["rsi_h1"] = round(float(rsi(c, 14).iloc[-1]), 1)
        ctx["atr_h1"] = round(float(atr(h1, 14).iloc[-1]), 2)
        ctx["hour_utc"] = int(h1.index[-1].hour)
        h4 = h1.resample("4h").agg({"high": "max", "low": "min", "close": "last"}).dropna()
        e50, e200 = ema(h4["close"], 50).iloc[-1], ema(h4["close"], 200).iloc[-1]
        ctx["h4_bias"] = ("alcista" if h4["close"].iloc[-1] > e50 > e200
                          else "bajista" if h4["close"].iloc[-1] < e50 < e200 else "mixto")
    except Exception as e:  # noqa: BLE001
        ctx["context_error"] = str(e)
    return ctx


# Keys que el aprendizaje (stats / study_loop) espera SIEMPRE presentes en el
# contexto de cada trade. Si el caller (p.ej. demo_trader) arma un ctx parcial,
# se completan desde capture_context() — sin esto, h4_bias salía "?".
_CANON_CTX_KEYS = ("h4_bias", "rsi_h1", "atr_h1", "hour_utc", "price", "provider")


def _full_context(context: dict | None) -> dict:
    """Garantiza un contexto COMPLETO para el trade.

    - Sin ctx del caller → captura entera (capture_context()).
    - Con ctx parcial (caso demo_trader, que no trae h4_bias) → se rellenan SOLO
      las keys canónicas faltantes desde capture_context(), preservando lo que el
      caller ya puso (incl. extras como 'bar'). El ctx del caller manda en
      colisiones (precio/rsi del momento exacto de la señal).
    - Si la captura falla, NO se rompe la orden: se anota 'context_error' y se
      devuelve lo que haya (anti-BENDER: el flujo de trading nunca se cae por el
      journal).
    """
    if not context:
        return capture_context()
    ctx = dict(context)
    missing = [k for k in _CANON_CTX_KEYS if ctx.get(k) is None]
    if not missing:
        return ctx
    try:
        captured = capture_context()
    except Exception as e:  # noqa: BLE001 — defensa extra; capture_context ya atrapa adentro
        ctx.setdefault("context_error", str(e))
        return ctx
    if captured.get("context_error") and "h4_bias" in missing:
        # la captura falló adentro: no pudimos completar el sesgo; lo registramos
        ctx.setdefault("context_error", captured["context_error"])
    for k in missing:
        if captured.get(k) is not None:
            ctx[k] = captured[k]
    return ctx


def log_trade(order_result: dict, setup: str = "manual", context: dict | None = None) -> None:
    """Registra un trade recién abierto con su contexto. order_result viene de
    core.mt5_executor.order()."""
    if not order_result.get("ok"):
        return
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts_open": pd.Timestamp.now(tz="UTC").isoformat(),
        "position_id": order_result.get("order"),
        "side": order_result.get("side"),
        "symbol": order_result.get("symbol"),
        "entry_price": order_result.get("price"),
        "volume": order_result.get("volume"),
        "setup": setup,
        "context": _full_context(context),
        "outcome": None, "pnl": None, "ts_close": None,
    }
    with JOURNAL.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(f"[journal] trade abierto registrado: {entry['side']} {entry['symbol']} "
          f"@ {entry['entry_price']} (pos {entry['position_id']})")


def _read() -> list[dict]:
    if not JOURNAL.exists():
        return []
    rows = []
    for i, l in enumerate(JOURNAL.read_text().splitlines(), 1):
        if not l.strip():
            continue
        try:
            rows.append(json.loads(l))
        except json.JSONDecodeError:
            # Línea corrupta (append parcial / crash a mitad de escritura): la
            # saltamos avisando, en vez de tumbar todo el aprendizaje. Un daemon
            # escribe a este jsonl sin supervisión, así que esto pasa.
            print(f"[journal] línea {i} corrupta, ignorada", file=sys.stderr)
    return rows


def _write(rows: list[dict]) -> None:
    JOURNAL.write_text("\n".join(json.dumps(r, default=str) for r in rows) + "\n")


def _pid_key(v) -> str:
    """Clave canónica de un position_id/ticket para matchear sin que un int vs str
    (drift de serialización del jsonl) rompa el cierre. MT5 los da como int; el
    jsonl podría reñadir alguno como str — comparar por str normaliza ambos."""
    return str(v).strip() if v is not None else ""


def reconcile() -> int:
    """Marca outcome/pnl de los trades que ya cerraron en MT5 (por position_id).

    Cierra TODO trade del journal que MT5 ya cerró (tiene deal de salida entry=1)
    y NO sigue abierto. Idempotente: solo toca filas con outcome None, así correrlo
    N veces no duplica ni recalcula. Robusto al history sync: ahora el terminal es
    PERSISTENTE (aureo-mt5-terminal.service), pero si una corrida puntual trae el
    history vacío/parcial, NO inventa cierres — solo cierra lo que tenga su deal de
    salida real, y avisa si quedan colgados que MT5 no reporta ni abiertos ni con
    salida (para que un colgado se note en vez de pasar callado)."""
    from core.mt5_executor import report, ExecutorError
    rows = _read()
    if not rows:
        print("journal vacío."); return 0
    try:
        rep = report()
    except ExecutorError as e:
        print(f"no pude leer MT5: {e}", file=sys.stderr); return 1

    # Normalizamos a str para que el match no dependa del tipo (int del bridge vs
    # posible str del jsonl).
    open_ids = {_pid_key(p.get("ticket")) for p in rep.get("open", [])}
    # pnl por position_id desde deals de SALIDA (entry==1 = DEAL_ENTRY_OUT).
    pnl_by_pos: dict[str, float] = {}
    for d in rep.get("closed", []):
        if d.get("entry") == 1:
            k = _pid_key(d.get("position_id"))
            pnl_by_pos[k] = pnl_by_pos.get(k, 0.0) + float(d.get("profit") or 0.0)

    n = 0
    hung = []
    for r in rows:
        if r.get("outcome") is not None:
            continue  # ya cerrado → idempotencia
        pid = _pid_key(r.get("position_id"))
        if pid in open_ids:
            continue  # MT5 lo reporta ABIERTO → se deja abierto (genuino)
        if pid in pnl_by_pos:
            pnl = pnl_by_pos[pid]
            r["pnl"] = round(pnl, 2)
            r["outcome"] = "WIN" if pnl > 0 else "LOSS"
            r["ts_close"] = pd.Timestamp.now(tz="UTC").isoformat()
            n += 1
        else:
            # Ni abierto ni con deal de salida en la ventana de history: colgado.
            # No lo cerramos a ciegas (no sabemos su pnl); lo dejamos para la
            # próxima corrida con history fresco, pero lo señalamos.
            hung.append(r.get("position_id"))

    _write(rows)
    msg = f"reconciliados {n} trades. Abiertos: {len(open_ids)}."
    if hung:
        msg += (f" SIN cerrar (ni abiertos ni con salida en history): "
                f"{len(hung)} -> {hung}")
    print(msg)
    return 0


def stats() -> int:
    rows = [r for r in _read() if r.get("outcome")]
    if not rows:
        print("Sin trades cerrados todavía. (El aprendizaje arranca cuando cierren los primeros.)")
        return 0
    df = pd.DataFrame(rows)
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    wins = df[df["pnl"] > 0]["pnl"]; losses = df[df["pnl"] <= 0]["pnl"]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    print(f"=== APRENDIZAJE — {len(df)} trades cerrados ===")
    print(f"Win rate: {(df['pnl']>0).mean()*100:.0f}%  | PF: {pf:.2f}  | "
          f"PnL total: {df['pnl'].sum():+.2f}  | expectancy: {df['pnl'].mean():+.2f}/trade")
    # condicional: por sesgo H4 y por hora (cuando haya suficientes)
    df["h4_bias"] = df["context"].apply(lambda c: (c or {}).get("h4_bias", "?") if isinstance(c, dict) else "?")
    if len(df) >= 5:
        print("\nPor sesgo H4:")
        print(df.groupby("h4_bias")["pnl"].agg(["count", "mean", "sum"]).round(2).to_string())
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "reconcile":
        return reconcile()
    if cmd == "stats":
        return stats()
    print("uso: reconcile | stats"); return 1


if __name__ == "__main__":
    raise SystemExit(main())