"""ÁUREO — estudio CONSTANTE (determinístico, anti-BENDER).

Andrés: "quiero que estés estudiando constantemente". Este bucle, cada N horas:
  1. Reconcilia los trades demo cerrados (cierra el ciclo de aprendizaje).
  2. Calcula estadísticas de aprendizaje (win-rate, PF, por sesgo H4 / hora) que
     CRECEN con cada operación real → acá está el estudio que evoluciona.
  3. Escribe un registro fechado en research/output/STUDY_LOG.md.
  4. Si el aprendizaje empieza a mostrar una condición con ventaja (≥20 trades y
     PF>1.3 en algún bucket), AVISA al Telegram (candidato a edge para verificar).

Sin Claude en el bucle (regla anti-BENDER). El re-test pesado de hipótesis
(research.run_battery) queda manual/semanal; acá el estudio que evoluciona es el
aprendizaje sobre la operación real, que es lo que de verdad cambia día a día.

Uso:
    .venv/bin/python -m research.study_loop --once
    .venv/bin/python -m research.study_loop --daemon   # cada 12h
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STUDY_LOG = ROOT / "research" / "output" / "STUDY_LOG.md"


def study_once() -> None:
    from core.trade_journal import reconcile, _read
    import io
    import contextlib

    # 1) reconciliar (cierra outcomes)
    try:
        reconcile()
    except Exception as e:  # noqa: BLE001
        print(f"[study] reconcile aviso: {e}", file=sys.stderr)

    # 2) stats de aprendizaje (capturar la salida del módulo)
    rows = [r for r in _read() if r.get("outcome")]
    open_n = len([r for r in _read() if not r.get("outcome")])
    when = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC")

    line = f"\n## {when}\n- trades cerrados: {len(rows)} | abiertos: {open_n}\n"
    alert = None
    if rows:
        df = pd.DataFrame(rows)
        df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
        wr = (df["pnl"] > 0).mean()
        wins, losses = df[df["pnl"] > 0]["pnl"], df[df["pnl"] <= 0]["pnl"]
        pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
        line += (f"- win-rate {wr*100:.0f}% | PF {pf:.2f} | "
                 f"PnL {df['pnl'].sum():+.2f} | exp {df['pnl'].mean():+.2f}/trade\n")
        # buscar condición con ventaja (≥20 trades, PF bucket > 1.3)
        df["h4"] = df["context"].apply(lambda c: (c or {}).get("h4_bias", "?") if isinstance(c, dict) else "?")
        if len(df) >= 20:
            for bias, g in df.groupby("h4"):
                gw, gl = g[g.pnl > 0]["pnl"], g[g.pnl <= 0]["pnl"]
                gpf = gw.sum() / abs(gl.sum()) if len(gl) and gl.sum() != 0 else float("inf")
                line += f"  - sesgo H4 {bias}: n={len(g)} PF={gpf:.2f}\n"
                if len(g) >= 20 and gpf > 1.3:
                    alert = (f"🧠 ÁUREO estudio: condición con ventaja detectada — "
                             f"sesgo H4 {bias}, n={len(g)}, PF={gpf:.2f}. Candidato a verificar.")
    else:
        line += "- (aún sin trades cerrados; el estudio arranca cuando cierren los primeros)\n"

    STUDY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with STUDY_LOG.open("a") as f:
        f.write(line)
    print(line.strip())

    if alert:
        try:
            from analysis.daily_brief import send_telegram
            send_telegram(alert)
            print("[study] ALERTA enviada al Telegram:", alert)
        except Exception as e:  # noqa: BLE001
            print(f"[study] no pude alertar: {e}", file=sys.stderr)


def run_daemon(poll_seconds: int = 43200) -> int:
    print(f"[study] estudio constante ON (cada {poll_seconds//3600}h). Ctrl-C para parar.")
    while True:
        try:
            study_once()
        except Exception as e:  # noqa: BLE001
            print(f"[study] err ciclo (sigue): {e}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--poll", type=int, default=43200)
    args = ap.parse_args()
    if args.daemon:
        return run_daemon(args.poll)
    study_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
