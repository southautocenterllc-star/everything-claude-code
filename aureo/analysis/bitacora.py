"""ÁUREO — bitácora de período del demo (reporte de desempeño, determinístico).

Andrés pidió "vayamos marcando una bitácora": un resumen honesto del desempeño de
la cuenta demo a partir del journal de aprendizaje (logs/aureo_trades.jsonl). Es
el reporte de CIERRE de período (pensado para corte SEMANAL los domingos, y
on-demand). Sin Claude en el bucle (anti-BENDER), 100% reglas sobre el journal.

Qué muestra:
  - Balance: inicial $1000 → realizado actual (suma de pnl de cerrados), % retorno.
  - Métricas: nº cerrados, win-rate, profit factor, expectancy, mejor/peor trade.
  - Desglose por sesgo H4 (alcista/bajista/mixto) y por hora UTC.
  - Posición abierta actual si la hay.
  - Nota honesta fija: es demo de aprendizaje, sin edge validado, el resultado NO
    es señal de edge (pocos trades / régimen).

Salida: research/output/BITACORA.md (gitignored) + stdout. Con --send postea al
grupo TRADING de Telegram (formato Markdown). Por defecto NO envía (preview).

Uso:
    .venv/bin/python -m analysis.bitacora --once          # preview (no manda)
    .venv/bin/python -m analysis.bitacora --once --send    # postea al Telegram
"""

from __future__ import annotations

import argparse
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

from core.trade_journal import _read
from analysis.daily_brief import send_telegram

MD_OUT = ROOT / "research" / "output" / "BITACORA.md"
INITIAL_BALANCE = 1000.0

HONEST_NOTE = (
    "Esto es una cuenta DEMO de aprendizaje. La estrategia NO tiene un edge "
    "validado estadísticamente. Con tan pocos trades y en un solo régimen de "
    "mercado, el resultado realizado NO es señal de edge: puede ser suerte o el "
    "régimen del momento. La bitácora sirve para registrar y aprender, no para "
    "concluir que el sistema gana."
)


def _bias_of(ctx: object) -> str:
    if isinstance(ctx, dict):
        b = ctx.get("h4_bias")
        if b:
            return str(b)
    return "?"


def _hour_of(ctx: object) -> object:
    if isinstance(ctx, dict):
        h = ctx.get("hour_utc")
        if h is not None:
            try:
                return int(h)
            except (ValueError, TypeError):
                return "?"
    return "?"


def compute() -> dict:
    rows = _read()
    closed = [r for r in rows if r.get("outcome")]
    open_trades = [r for r in rows if r.get("outcome") is None]

    out: dict = {
        "n_total": len(rows),
        "n_closed": len(closed),
        "n_open": len(open_trades),
        "open": open_trades[-1] if open_trades else None,
        "initial": INITIAL_BALANCE,
    }

    if not closed:
        out["empty"] = True
        out["balance"] = INITIAL_BALANCE
        out["realized_pnl"] = 0.0
        out["return_pct"] = 0.0
        return out

    df = pd.DataFrame(closed)
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    df["h4_bias"] = df["context"].apply(_bias_of)
    df["hour_utc"] = df["context"].apply(_hour_of)

    pnl = df["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_win = float(wins.sum())
    gross_loss = float(losses.sum())
    pf = (gross_win / abs(gross_loss)) if gross_loss != 0 else float("inf")
    realized = float(pnl.sum())

    best_i = pnl.idxmax()
    worst_i = pnl.idxmin()

    def _trade_brief(i) -> dict:
        r = df.loc[i]
        return {
            "pnl": float(r["pnl"]),
            "side": r.get("side"),
            "entry_price": r.get("entry_price"),
            "h4_bias": r.get("h4_bias"),
            "hour_utc": r.get("hour_utc"),
            "ts_open": r.get("ts_open"),
        }

    by_bias = []
    for bias, g in df.groupby("h4_bias"):
        gp = g["pnl"]
        by_bias.append({
            "key": str(bias),
            "count": int(len(g)),
            "wr": float((gp > 0).mean() * 100),
            "pnl": float(gp.sum()),
        })
    by_bias.sort(key=lambda d: d["count"], reverse=True)

    by_hour = []
    for hour, g in df.groupby("hour_utc"):
        gp = g["pnl"]
        by_hour.append({
            "key": hour if hour == "?" else int(hour),
            "count": int(len(g)),
            "wr": float((gp > 0).mean() * 100),
            "pnl": float(gp.sum()),
        })
    by_hour.sort(key=lambda d: (d["key"] == "?", d["key"]))

    out.update({
        "empty": False,
        "realized_pnl": realized,
        "balance": INITIAL_BALANCE + realized,
        "return_pct": realized / INITIAL_BALANCE * 100,
        "win_rate": float((pnl > 0).mean() * 100),
        "n_wins": int((pnl > 0).sum()),
        "n_losses": int((pnl <= 0).sum()),
        "profit_factor": pf,
        "expectancy": float(pnl.mean()),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "best": _trade_brief(best_i),
        "worst": _trade_brief(worst_i),
        "by_bias": by_bias,
        "by_hour": by_hour,
        "first_open": df["ts_open"].min() if "ts_open" in df else None,
        "last_close": df["ts_close"].max() if "ts_close" in df else None,
    })
    return out


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def render_md(o: dict) -> str:
    when = pd.Timestamp.now(tz="UTC").strftime("%d-%b-%Y %H:%M UTC")
    lines = [
        "# ÁUREO — Bitácora del período (cuenta demo)",
        f"_Generada {when}_",
        "",
    ]

    if o.get("empty"):
        lines += [
            "## Sin trades cerrados todavía",
            f"Balance: ${o['initial']:,.2f} (inicial, sin movimiento realizado).",
            "",
        ]
        if o.get("open"):
            lines += _open_lines_md(o["open"])
        lines += ["", "## Nota honesta", HONEST_NOTE]
        return "\n".join(lines)

    sign = "+" if o["realized_pnl"] >= 0 else ""
    lines += [
        "## Balance",
        f"- Inicial: ${o['initial']:,.2f}",
        f"- Realizado actual: ${o['balance']:,.2f}  "
        f"({sign}{o['realized_pnl']:,.2f}, {sign}{o['return_pct']:.2f}%)",
        "",
        "## Métricas (trades cerrados)",
        f"- Trades cerrados: {o['n_closed']}  "
        f"({o['n_wins']} ganadores / {o['n_losses']} perdedores)",
        f"- Win-rate: {o['win_rate']:.0f}%",
        f"- Profit factor: {_pf_str(o['profit_factor'])}",
        f"- Expectancy: {o['expectancy']:+.2f} por trade",
        f"- Bruto: ganado ${o['gross_win']:,.2f} / perdido ${o['gross_loss']:,.2f}",
        f"- Mejor trade: {o['best']['pnl']:+.2f} "
        f"({o['best']['side']} @ {o['best']['entry_price']}, "
        f"H4 {o['best']['h4_bias']}, {o['best']['hour_utc']}h UTC)",
        f"- Peor trade: {o['worst']['pnl']:+.2f} "
        f"({o['worst']['side']} @ {o['worst']['entry_price']}, "
        f"H4 {o['worst']['h4_bias']}, {o['worst']['hour_utc']}h UTC)",
        "",
        "## Desglose por sesgo H4",
    ]
    for b in o["by_bias"]:
        lines.append(f"- {b['key']}: {b['count']} trades · WR {b['wr']:.0f}% · "
                     f"pnl {b['pnl']:+.2f}")
    lines += ["", "## Desglose por hora UTC"]
    for h in o["by_hour"]:
        hk = f"{h['key']:02d}h" if isinstance(h["key"], int) else f"{h['key']}"
        lines.append(f"- {hk}: {h['count']} trades · WR {h['wr']:.0f}% · "
                     f"pnl {h['pnl']:+.2f}")

    lines += [""]
    if o.get("open"):
        lines += _open_lines_md(o["open"])
        lines += [""]

    lines += ["## Nota honesta", HONEST_NOTE]
    return "\n".join(lines)


def _open_lines_md(t: dict) -> list[str]:
    ctx = t.get("context") if isinstance(t.get("context"), dict) else {}
    return [
        "## Posición abierta actual",
        f"- {t.get('side')} {t.get('symbol')} @ {t.get('entry_price')} "
        f"(vol {t.get('volume')}) abierto {t.get('ts_open')}",
        f"  contexto: H4 {ctx.get('h4_bias', '?')}, "
        f"RSI {ctx.get('rsi_h1', '?')}, hora {ctx.get('hour_utc', '?')}h UTC",
    ]


def render_telegram(o: dict) -> str:
    when = pd.Timestamp.now(tz="UTC").strftime("%d-%b %H:%M UTC")
    head = f"📓 *ÁUREO — Bitácora del período* _{when}_\n\n"

    if o.get("empty"):
        head += f"Balance: *${o['initial']:,.2f}* (sin trades cerrados aún).\n"
        if o.get("open"):
            head += "\n" + _open_line_tg(o["open"])
        head += f"\n\n_⚠️ {HONEST_NOTE}_"
        return head

    sign = "+" if o["realized_pnl"] >= 0 else ""
    emoji = "🟢" if o["realized_pnl"] >= 0 else "🔴"
    head += (
        f"{emoji} Balance: *${o['balance']:,.2f}*  "
        f"({sign}{o['realized_pnl']:,.2f} / {sign}{o['return_pct']:.2f}%)\n"
        f"Trades cerrados: *{o['n_closed']}*  "
        f"({o['n_wins']}W / {o['n_losses']}L)\n"
        f"Win-rate: *{o['win_rate']:.0f}%*  |  PF: *{_pf_str(o['profit_factor'])}*  |  "
        f"Exp: *{o['expectancy']:+.2f}*/trade\n"
        f"Mejor: {o['best']['pnl']:+.2f}  |  Peor: {o['worst']['pnl']:+.2f}\n"
    )

    head += "\n*Por sesgo H4:*\n"
    for b in o["by_bias"]:
        head += f"• {b['key']}: {b['count']}t · WR {b['wr']:.0f}% · {b['pnl']:+.2f}\n"

    head += "\n*Por hora UTC:*\n"
    for h in o["by_hour"]:
        hk = f"{h['key']:02d}h" if isinstance(h["key"], int) else f"{h['key']}"
        head += f"• {hk}: {h['count']}t · WR {h['wr']:.0f}% · {h['pnl']:+.2f}\n"

    if o.get("open"):
        head += "\n" + _open_line_tg(o["open"])

    head += f"\n\n_⚠️ {HONEST_NOTE}_"
    return head


def _open_line_tg(t: dict) -> str:
    ctx = t.get("context") if isinstance(t.get("context"), dict) else {}
    return (f"📌 *Abierta ahora:* {t.get('side')} {t.get('symbol')} @ "
            f"{t.get('entry_price')} (H4 {ctx.get('h4_bias', '?')})")


def run_once(send: bool = False) -> int:
    o = compute()
    md = render_md(o)
    print("=" * 64)
    print(md)
    print("=" * 64)

    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text(md)
    print(f"[bitacora] escrita en {MD_OUT}")

    if send:
        return send_telegram(render_telegram(o))
    print("[bitacora] (PREVIEW — agregá --send para postear al grupo TRADING.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ÁUREO — bitácora del período (demo).")
    ap.add_argument("--once", action="store_true", help="Genera la bitácora (default).")
    ap.add_argument("--send", action="store_true",
                    help="Postear al Telegram. Sin él = preview (no manda).")
    args = ap.parse_args()
    return run_once(send=args.send)


if __name__ == "__main__":
    raise SystemExit(main())
