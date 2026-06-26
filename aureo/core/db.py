"""Async SQLite layer para AUREO.
Schema mínimo viable post-reset 2026-05-21. Sin MT5 — los campos de ejecución
quedan nullable hasta que tengamos broker conectado.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "aureo_trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,            -- "tradingview", "manual", "signal_loop"
    symbol TEXT NOT NULL,            -- e.g. "XAUUSD"
    action TEXT NOT NULL,            -- "BUY", "SELL", "CLOSE"
    price REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    rsi REAL,
    atr REAL,
    macro_bias TEXT,
    score_technical INTEGER,
    score_macro INTEGER,
    score_news INTEGER,
    score_total INTEGER,
    raw_payload TEXT,                -- JSON original
    accepted INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,         -- "long", "short"
    entry_price REAL,
    exit_price REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    volume REAL,
    pnl REAL,
    pnl_pips REAL,
    outcome TEXT,                    -- "TP1", "TP2", "SL", "TP1_then_SL", "MANUAL"
    duration_min INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
"""


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def insert_signal(payload: dict, accepted: bool = True, rejection_reason: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO signals (
                ts, source, symbol, action, price, sl, tp1, tp2,
                rsi, atr, macro_bias, score_technical, score_macro, score_news, score_total,
                raw_payload, accepted, rejection_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                payload.get("source", "unknown"),
                payload.get("symbol", "XAUUSD"),
                payload.get("action", "?"),
                payload.get("price"),
                payload.get("sl"),
                payload.get("tp1"),
                payload.get("tp2"),
                payload.get("rsi"),
                payload.get("atr"),
                payload.get("macro_bias"),
                payload.get("score_technical"),
                payload.get("score_macro"),
                payload.get("score_news"),
                payload.get("score_total"),
                json.dumps(payload, default=str),
                1 if accepted else 0,
                rejection_reason,
            ),
        )
        await db.commit()
        return cur.lastrowid or 0


async def list_recent_signals(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, ts, source, symbol, action, price, score_total, accepted, rejection_reason "
            "FROM signals ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def journal_summary() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM signals")
        total_signals = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM signals WHERE accepted=1")
        accepted = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM trades")
        total_trades = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), SUM(pnl) FROM trades WHERE closed_at IS NOT NULL")
        row = await cur.fetchone()
        closed, wins, total_pnl = row[0] or 0, row[1] or 0, row[2] or 0.0
        win_rate = (wins / closed * 100) if closed else 0.0
    return {
        "signals_total": total_signals,
        "signals_accepted": accepted,
        "trades_total": total_trades,
        "trades_closed": closed,
        "wins": wins,
        "win_rate_pct": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
    }