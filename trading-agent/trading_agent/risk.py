"""Gestione del rischio: dimensionamento, limiti di esposizione, uscite e kill switch.

Questi controlli sono deterministici e hanno sempre l'ultima parola sugli ordini.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import RiskConfig
from .models import AssetClass, Instrument, Position


@dataclass
class SizingResult:
    qty: float  # con segno; 0 = nessun ordine
    reason: str


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def allow_short(self, inst: Instrument) -> bool:
        return self.cfg.allow_short_forex if inst.is_forex else self.cfg.allow_short_equities

    def daily_loss_breached(self, start_equity: float, equity: float) -> bool:
        if start_equity <= 0:
            return False
        return (start_equity - equity) / start_equity >= self.cfg.daily_loss_limit_pct

    def in_cooldown(self, last_trade_at: datetime | None, now: datetime) -> bool:
        return last_trade_at is not None and now - last_trade_at < timedelta(minutes=self.cfg.cooldown_minutes)

    def size_entry(
        self,
        inst: Instrument,
        direction: int,
        score: float,
        price: float,
        equity: float,
        gross_exposure: float,
        open_positions: int,
    ) -> SizingResult:
        if open_positions >= self.cfg.max_open_positions:
            return SizingResult(0, f"raggiunto il massimo di {self.cfg.max_open_positions} posizioni")
        if price <= 0 or equity <= 0:
            return SizingResult(0, "prezzo o equity non validi")

        # Più forte il segnale, più grande la posizione (fino al massimo consentito)
        scale = min(1.0, abs(score))
        target = equity * self.cfg.max_position_pct * scale
        room = equity * self.cfg.max_gross_exposure_pct - gross_exposure
        notional = min(target, room)
        if notional <= 0:
            return SizingResult(0, "esposizione lorda massima raggiunta")

        if inst.is_forex:
            per_unit = inst.usd_notional(1, price)
            qty = math.floor(notional / per_unit)
        else:
            qty = math.floor(notional / price)
        if qty <= 0:
            return SizingResult(0, f"importo {notional:.0f} USD insufficiente per 1 unità")
        return SizingResult(direction * qty, f"notional ~{inst.usd_notional(qty, price):.0f} USD")

    def exit_reason(self, inst: Instrument, pos: Position, price: float, opened_at: datetime | None, now: datetime) -> str | None:
        if pos.qty == 0 or pos.avg_price <= 0:
            return None
        ret = (price - pos.avg_price) / pos.avg_price * (1 if pos.qty > 0 else -1)
        if ret <= -self.cfg.stop_loss_pct:
            return f"stop loss ({ret:+.2%})"
        if ret >= self.cfg.take_profit_pct:
            return f"take profit ({ret:+.2%})"
        if opened_at and now - opened_at >= timedelta(hours=self.cfg.max_holding_hours):
            return f"durata massima superata ({self.cfg.max_holding_hours:.0f}h)"
        return None


def asset_bucket(inst: Instrument) -> str:
    return "forex" if inst.asset_class is AssetClass.FOREX else "equity"
