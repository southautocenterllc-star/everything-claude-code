"""Métricas estadísticas sobre lista de Trades."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from .engine import Trade


@dataclass
class BacktestStats:
    n_trades: int
    n_long: int
    n_short: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    expectancy: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    sharpe: float
    sortino: float
    cagr: float
    avg_duration_bars: float
    outcomes: dict[str, int]


def equity_curve(trades: Sequence[Trade], init_capital: float = 10_000.0,
                 size: float = 1.0) -> pd.Series:
    """Curva acumulada por trade (sin marcaje a mercado intra-trade)."""
    if not trades:
        return pd.Series(dtype=float)
    pnls = pd.Series(
        [t.pnl * size for t in trades],
        index=pd.DatetimeIndex([t.exit_time for t in trades]),
    ).sort_index()
    return init_capital + pnls.cumsum()


def compute_stats(trades: Sequence[Trade], init_capital: float = 10_000.0,
                  bars_per_year: int = 24 * 252) -> BacktestStats:
    if not trades:
        return BacktestStats(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, {})

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    n_long = sum(1 for t in trades if t.direction == 1)
    n_short = sum(1 for t in trades if t.direction == -1)

    win_rate = len(wins) / len(pnls) if len(pnls) else 0
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = -losses.sum() if len(losses) else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    expectancy = pnls.mean() if len(pnls) else 0.0

    eq = equity_curve(trades, init_capital)
    running_max = eq.cummax()
    dd = (eq - running_max) / running_max
    max_dd = abs(dd.min()) if len(dd) else 0.0

    if len(trades) >= 2:
        days = (trades[-1].exit_time - trades[0].entry_time).total_seconds() / 86400
        years = max(days / 365.25, 1 / 365.25)
    else:
        years = 1.0
    trades_per_year = len(pnls) / years

    returns = pnls / init_capital
    sharpe = (returns.mean() / returns.std(ddof=1)) * np.sqrt(trades_per_year) if returns.std(ddof=1) > 0 else 0.0
    downside = returns[returns < 0]
    sortino = (returns.mean() / downside.std(ddof=1)) * np.sqrt(trades_per_year) if len(downside) > 1 and downside.std(ddof=1) > 0 else 0.0

    if len(trades) >= 2:
        final_eq = eq.iloc[-1] if len(eq) else init_capital
        cagr = (final_eq / init_capital) ** (1 / years) - 1
    else:
        cagr = 0.0

    avg_duration = float(np.mean([t.duration_bars for t in trades]))

    outcomes: dict[str, int] = {}
    for t in trades:
        outcomes[t.outcome] = outcomes.get(t.outcome, 0) + 1

    return BacktestStats(
        n_trades=len(trades),
        n_long=n_long,
        n_short=n_short,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        profit_factor=pf,
        expectancy=expectancy,
        total_pnl=float(pnls.sum()),
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        max_drawdown=max_dd,
        sharpe=sharpe,
        sortino=sortino,
        cagr=cagr,
        avg_duration_bars=avg_duration,
        outcomes=outcomes,
    )


def print_stats(stats: BacktestStats) -> None:
    print(f"  Trades totales : {stats.n_trades}  (long {stats.n_long} / short {stats.n_short})")
    print(f"  Wins / Losses  : {stats.wins} / {stats.losses}")
    print(f"  Win Rate       : {stats.win_rate * 100:.2f}%")
    print(f"  Profit Factor  : {stats.profit_factor:.3f}")
    print(f"  Expectancy/trd : {stats.expectancy:+.2f} USD por unidad XAU")
    print(f"  PnL total      : {stats.total_pnl:+.2f}")
    print(f"  Avg Win        : {stats.avg_win:+.2f}")
    print(f"  Avg Loss       : {stats.avg_loss:+.2f}")
    print(f"  Max Drawdown   : {stats.max_drawdown * 100:.2f}%")
    print(f"  Sharpe         : {stats.sharpe:.3f}")
    print(f"  Sortino        : {stats.sortino:.3f}")
    print(f"  CAGR           : {stats.cagr * 100:+.2f}%")
    print(f"  Avg duration   : {stats.avg_duration_bars:.1f} barras")
    print(f"  Outcomes       : {stats.outcomes}")
