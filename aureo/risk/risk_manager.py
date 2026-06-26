"""ÁUREO — gestor de riesgo (determinístico, anti-BENDER).

Calcula el tamaño de posición para XAU/USD basado en:
  - Riesgo fijo: 1 % del equity por trade
  - Stop distance: distancia al SL en USD/oz
  - Tamaño: risk_usd / (stop_distance × oz_per_lot), redondeado al lote mínimo

Protecciones adicionales:
  - Límite de pérdida diaria (default 3 % del equity)
  - Pyramiding=0: solo 1 posición abierta a la vez
  - El SL debe ser coherente con la dirección de la orden
  - Lote mínimo/máximo configurable

Sin LLM en el bucle (anti-BENDER): 100 % determinístico.

Uso:
    from risk.risk_manager import RiskManager, check_risk

    mgr = RiskManager(equity=1000.0)
    decision = mgr.size(entry=2350.0, sl=2335.0, side="BUY")
    if decision.approved:
        order("BUY", decision.lots, sl=2335.0, ...)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ──────────────────────────────────────────────────────────────────────────────
# Constantes por defecto (pueden sobrescribirse en el constructor)
# ──────────────────────────────────────────────────────────────────────────────
_RISK_PCT: float = 0.01          # 1 % del equity por trade
_MAX_DAILY_LOSS_PCT: float = 0.03  # 3 % del equity como límite diario
_LOT_STEP: float = 0.01          # Fracción mínima de lote (XAUUSD estándar)
_LOT_MIN: float = 0.01
_LOT_MAX: float = 0.10           # Tope prudente en demo y live con micro-lotes
_OZ_PER_LOT: int = 100           # 1 lote estándar = 100 onzas de oro


@dataclass
class RiskDecision:
    """Resultado del cálculo de riesgo. Siempre leer .approved antes de operar."""
    approved: bool
    lots: float
    risk_usd: float               # Riesgo en USD si el SL se activa
    stop_distance: float          # |entry - sl| en USD/oz
    reasons: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "APROBADO" if self.approved else "RECHAZADO"
        r = f"[RiskDecision] {status} | lotes={self.lots:.2f} | risk=${self.risk_usd:.2f} | stop={self.stop_distance:.2f}"
        if self.reasons:
            r += " | " + "; ".join(self.reasons)
        return r


class RiskManager:
    """Calcula y valida el tamaño de posición para cada orden de ÁUREO.

    Args:
        equity: Balance / equity de la cuenta en USD.
        risk_pct: Fracción del equity a arriesgar por trade (default 0.01 = 1 %).
        max_daily_loss_pct: Límite de pérdida acumulada diaria (default 0.03 = 3 %).
        lot_min / lot_max / lot_step: Configuración del broker.
        oz_per_lot: Onzas por lote estándar (100 en XAU/USD).
        daily_pnl: PnL realizado en el día actual (negativo = pérdida acumulada).
    """

    def __init__(
        self,
        equity: float,
        risk_pct: float = _RISK_PCT,
        max_daily_loss_pct: float = _MAX_DAILY_LOSS_PCT,
        lot_min: float = _LOT_MIN,
        lot_max: float = _LOT_MAX,
        lot_step: float = _LOT_STEP,
        oz_per_lot: int = _OZ_PER_LOT,
        daily_pnl: float = 0.0,
    ) -> None:
        if equity <= 0:
            raise ValueError(f"equity debe ser positivo, recibido: {equity}")
        self.equity = equity
        self.risk_pct = risk_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.lot_min = lot_min
        self.lot_max = lot_max
        self.lot_step = lot_step
        self.oz_per_lot = oz_per_lot
        self.daily_pnl = daily_pnl

    # ── API pública ──────────────────────────────────────────────────────────

    def size(
        self,
        entry: float,
        sl: float,
        side: str,
        open_positions: int = 0,
    ) -> RiskDecision:
        """Calcula el lote óptimo para una nueva orden.

        Args:
            entry: Precio de entrada (precio de mercado en el momento de la señal).
            sl: Nivel de stop-loss.
            side: 'BUY' o 'SELL'.
            open_positions: Número de posiciones de ÁUREO ya abiertas (pyramiding check).

        Returns:
            RiskDecision con .approved y .lots.
        """
        reasons: list[str] = []

        # ── 1. Pyramiding=0 ──────────────────────────────────────────────────
        if open_positions > 0:
            return RiskDecision(
                approved=False, lots=0.0, risk_usd=0.0, stop_distance=0.0,
                reasons=[f"pyramiding=0 — ya hay {open_positions} posición(es) abierta(s)"],
            )

        # ── 2. Validación de la dirección del SL ────────────────────────────
        side_upper = side.upper()
        if side_upper == "BUY" and sl >= entry:
            reasons.append(f"SL ({sl:.2f}) debe ser < entry ({entry:.2f}) en BUY")
            return RiskDecision(approved=False, lots=0.0, risk_usd=0.0,
                                stop_distance=0.0, reasons=reasons)
        if side_upper == "SELL" and sl <= entry:
            reasons.append(f"SL ({sl:.2f}) debe ser > entry ({entry:.2f}) en SELL")
            return RiskDecision(approved=False, lots=0.0, risk_usd=0.0,
                                stop_distance=0.0, reasons=reasons)

        # ── 3. Límite de pérdida diaria ──────────────────────────────────────
        max_daily_loss = self.equity * self.max_daily_loss_pct
        if self.daily_pnl <= -max_daily_loss:
            reasons.append(
                f"límite diario alcanzado: pnl_día={self.daily_pnl:.2f} "
                f"≤ -{max_daily_loss:.2f} ({self.max_daily_loss_pct*100:.0f}% equity)"
            )
            return RiskDecision(approved=False, lots=0.0, risk_usd=0.0,
                                stop_distance=0.0, reasons=reasons)

        # ── 4. Cálculo del lote ──────────────────────────────────────────────
        stop_dist = abs(entry - sl)          # USD/oz
        if stop_dist < 1e-6:
            return RiskDecision(
                approved=False, lots=0.0, risk_usd=0.0, stop_distance=stop_dist,
                reasons=["stop_distance demasiado pequeño (< 1e-6)"],
            )

        risk_usd = self.equity * self.risk_pct
        # 1 lot = oz_per_lot onzas → costo en USD de 1 lot al SL = stop_dist × oz_per_lot
        raw_lots = risk_usd / (stop_dist * self.oz_per_lot)

        # Redondear HACIA ABAJO al múltiplo de lot_step (nunca exceder el riesgo deseado)
        lots = self._floor_to_step(raw_lots, self.lot_step)
        lots = max(self.lot_min, min(self.lot_max, lots))

        # Riesgo efectivo con el lote redondeado
        effective_risk = lots * self.oz_per_lot * stop_dist
        reasons.append(
            f"lotes={lots:.2f} | stop_dist={stop_dist:.2f} | risk_ef=${effective_risk:.2f}"
        )

        # ── 5. Verificación post-redondeo ────────────────────────────────────
        if lots < self.lot_min:
            reasons.append(
                f"lotes calculados ({lots:.2f}) < lot_min ({self.lot_min:.2f}); "
                "orden demasiado pequeña para el riesgo definido"
            )
            return RiskDecision(approved=False, lots=lots, risk_usd=effective_risk,
                                stop_distance=stop_dist, reasons=reasons)

        return RiskDecision(
            approved=True,
            lots=lots,
            risk_usd=effective_risk,
            stop_distance=stop_dist,
            reasons=reasons,
        )

    # ── Utilidades ───────────────────────────────────────────────────────────

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        """Redondea value hacia abajo al múltiplo de step más cercano."""
        if step <= 0:
            return value
        return math.floor(value / step) * step

    def check_daily_limit(self) -> bool:
        """True si aún se puede operar (no se alcanzó el límite diario)."""
        return self.daily_pnl > -(self.equity * self.max_daily_loss_pct)


# ── Función de conveniencia ───────────────────────────────────────────────────

def check_risk(
    equity: float,
    entry: float,
    sl: float,
    side: str,
    open_positions: int = 0,
    daily_pnl: float = 0.0,
    risk_pct: float = _RISK_PCT,
    max_daily_loss_pct: float = _MAX_DAILY_LOSS_PCT,
) -> RiskDecision:
    """Atajo funcional para uso rápido sin instanciar RiskManager directamente.

    Ejemplo:
        d = check_risk(equity=1000.0, entry=2350.0, sl=2335.0, side="BUY")
        if d.approved:
            order("BUY", d.lots, sl=2335.0)
    """
    mgr = RiskManager(
        equity=equity,
        risk_pct=risk_pct,
        max_daily_loss_pct=max_daily_loss_pct,
        daily_pnl=daily_pnl,
    )
    return mgr.size(entry=entry, sl=sl, side=side, open_positions=open_positions)


# ── CLI de diagnóstico ────────────────────────────────────────────────────────

def _main() -> None:
    """Demo rápida: python -m risk.risk_manager"""
    print("=== ÁUREO RiskManager — demo ===")
    cases = [
        dict(equity=1000.0, entry=2350.0, sl=2335.0, side="BUY",    label="BUY normal"),
        dict(equity=1000.0, entry=2350.0, sl=2365.0, side="SELL",   label="SELL normal"),
        dict(equity=1000.0, entry=2350.0, sl=2335.0, side="BUY",    open_positions=1, label="BUY pyramiding bloqueado"),
        dict(equity=1000.0, entry=2350.0, sl=2355.0, side="BUY",    label="BUY SL en dirección equivocada"),
        dict(equity=1000.0, entry=2350.0, sl=2335.0, side="BUY",    daily_pnl=-35.0, label="BUY límite diario alcanzado"),
        dict(equity=1000.0, entry=2350.0, sl=2349.50, side="BUY",   label="BUY stop muy estrecho → lot_min"),
    ]
    for kw in cases:
        label = kw.pop("label")
        d = check_risk(**kw)
        print(f"\n[{label}]")
        print(f"  {d}")


if __name__ == "__main__":
    _main()
