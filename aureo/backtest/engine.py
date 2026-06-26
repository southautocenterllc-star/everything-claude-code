"""Backtest engine para AUREO.

Simula trade-by-trade sobre velas OHLC con señales del strategies/aureo_pine.

Reglas:
- Pyramiding=0: una sola posición a la vez.
- Entrada al close de la barra que disparó la señal (asunción Pine bar-close).
- Salidas dentro de las barras siguientes evaluando high/low contra SL/TP1/TP2.
  * Si la misma barra toca SL y TP, asumimos peor caso (SL primero) — conservador.
- TP1 cierra 50% del tamaño; TP2 cierra el resto.
- Sin slippage ni comisión por default (configurable).
- Risk dimensional: cada trade asume capital fijo, position size 1.0 unidad
  (la métrica es PnL por unidad XAU, no por % de equity).

Output: lista de Trade dicts con entry/exit/pnl/duration/outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

Outcome = Literal["TP2", "TP1_then_SL", "TP1_only", "SL", "OPEN"]


@dataclass
class Trade:
    entry_time: pd.Timestamp
    direction: int  # +1 long, -1 short
    entry_price: float
    sl: float
    tp1: float
    tp2: float
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None  # weighted avg si hubo partial
    outcome: Outcome = "OPEN"
    duration_bars: int = 0
    pnl: float = 0.0  # en moneda cotización por unidad


@dataclass
class BacktestConfig:
    spread_pips: float = 0.0  # XAU/USD: 1 pip = 0.01. Default 0 = sin spread.
    pip_value: float = 0.01
    init_capital: float = 10_000.0
    swap_per_night: float = 0.0  # financiación overnight, USD/onza/noche (costo).
    # OJO: en swing de XAU el swap suele ser el costo DOMINANTE (mayor que el
    # spread) porque los trades cruzan varias noches. Default 0 = no se modela.


def run_backtest(signals: pd.DataFrame, ohlc: pd.DataFrame,
                cfg: BacktestConfig | None = None) -> list[Trade]:
    cfg = cfg or BacktestConfig()
    spread = cfg.spread_pips * cfg.pip_value
    trades: list[Trade] = []

    # signals.index == ohlc.index suposicion (mismo timeframe)
    idx = signals.index
    sig = signals["signal"].to_numpy()
    sl_arr = signals["sl"].to_numpy()
    tp1_arr = signals["tp1"].to_numpy()
    tp2_arr = signals["tp2"].to_numpy()
    close = ohlc["close"].to_numpy()
    high = ohlc["high"].to_numpy()
    low = ohlc["low"].to_numpy()

    open_trade: Trade | None = None
    tp1_hit = False
    n = len(idx)

    for i in range(n):
        # 1) Si hay trade abierto, evaluar salida ESTA barra
        if open_trade is not None:
            d = open_trade.direction
            bar_high = high[i]
            bar_low = low[i]

            sl_hit = (d == 1 and bar_low <= open_trade.sl) or (d == -1 and bar_high >= open_trade.sl)
            tp1_hit_bar = (d == 1 and bar_high >= open_trade.tp1) or (d == -1 and bar_low <= open_trade.tp1)
            tp2_hit_bar = (d == 1 and bar_high >= open_trade.tp2) or (d == -1 and bar_low <= open_trade.tp2)

            # Caso: misma barra toca SL y algún TP → conservador, SL primero
            if sl_hit and (tp1_hit_bar or tp2_hit_bar):
                if tp1_hit:
                    half_tp1 = (open_trade.tp1 - open_trade.entry_price) * d
                    half_sl = (open_trade.sl - open_trade.entry_price) * d
                    open_trade.pnl = (half_tp1 + half_sl) / 2 - spread
                    open_trade.exit_price = (open_trade.tp1 + open_trade.sl) / 2
                    open_trade.outcome = "TP1_then_SL"
                else:
                    open_trade.pnl = (open_trade.sl - open_trade.entry_price) * d - spread
                    open_trade.exit_price = open_trade.sl
                    open_trade.outcome = "SL"
                open_trade.exit_time = idx[i]
                open_trade.duration_bars = i - signals.index.get_loc(open_trade.entry_time)
                trades.append(open_trade)
                open_trade = None
                tp1_hit = False
                continue

            # TP2 → cierre total
            if tp2_hit_bar:
                half_tp1 = (open_trade.tp1 - open_trade.entry_price) * d
                half_tp2 = (open_trade.tp2 - open_trade.entry_price) * d
                open_trade.pnl = (half_tp1 + half_tp2) / 2 - spread
                open_trade.exit_price = (open_trade.tp1 + open_trade.tp2) / 2
                open_trade.outcome = "TP2"
                open_trade.exit_time = idx[i]
                open_trade.duration_bars = i - signals.index.get_loc(open_trade.entry_time)
                trades.append(open_trade)
                open_trade = None
                tp1_hit = False
                continue

            # SL puro
            if sl_hit:
                if tp1_hit:
                    half_tp1 = (open_trade.tp1 - open_trade.entry_price) * d
                    half_sl = (open_trade.sl - open_trade.entry_price) * d
                    open_trade.pnl = (half_tp1 + half_sl) / 2 - spread
                    open_trade.exit_price = (open_trade.tp1 + open_trade.sl) / 2
                    open_trade.outcome = "TP1_then_SL"
                else:
                    open_trade.pnl = (open_trade.sl - open_trade.entry_price) * d - spread
                    open_trade.exit_price = open_trade.sl
                    open_trade.outcome = "SL"
                open_trade.exit_time = idx[i]
                open_trade.duration_bars = i - signals.index.get_loc(open_trade.entry_time)
                trades.append(open_trade)
                open_trade = None
                tp1_hit = False
                continue

            # TP1 marcador (mueve SL a entry — break-even)
            if tp1_hit_bar and not tp1_hit:
                tp1_hit = True
                open_trade.sl = open_trade.entry_price  # break-even después de TP1

        # 2) Si no hay trade, evaluar entrada en esta barra
        if open_trade is None and sig[i] != 0:
            d = int(sig[i])
            entry = close[i] + (spread / 2) * d  # paga half-spread al entrar
            open_trade = Trade(
                entry_time=idx[i],
                direction=d,
                entry_price=entry,
                sl=float(sl_arr[i]),
                tp1=float(tp1_arr[i]),
                tp2=float(tp2_arr[i]),
            )
            tp1_hit = False

    # Trade colgado al final
    if open_trade is not None:
        open_trade.exit_time = idx[-1]
        open_trade.exit_price = float(close[-1])
        open_trade.pnl = (open_trade.exit_price - open_trade.entry_price) * open_trade.direction - spread
        open_trade.outcome = "OPEN"
        open_trade.duration_bars = n - 1 - signals.index.get_loc(open_trade.entry_time)
        trades.append(open_trade)

    # Costo de financiación overnight (swap)
    if cfg.swap_per_night:
        for t in trades:
            if t.entry_time is not None and t.exit_time is not None:
                nights = max((t.exit_time.normalize() - t.entry_time.normalize()).days, 0)
                t.pnl -= cfg.swap_per_night * nights

    return trades
