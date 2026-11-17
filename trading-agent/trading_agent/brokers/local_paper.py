"""Broker simulato locale: nessun conto richiesto, stato salvato su file JSON.

Modello semplificato: ogni posizione è a margine, l'equity è
cassa + P&L non realizzato in USD. Utile per provare la pipeline end-to-end.
Gli orari di mercato ignorano le festività.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..models import Account, Instrument, OrderResult, Position
from .base import BrokerError

NY = ZoneInfo("America/New_York")


def us_equity_market_open(now: datetime) -> bool:
    t = now.astimezone(NY)
    return t.weekday() < 5 and time(9, 30) <= t.time() < time(16, 0)


def forex_market_open(now: datetime) -> bool:
    # Forex: da domenica 17:00 a venerdì 17:00 (ora di New York)
    t = now.astimezone(NY)
    wd = t.weekday()  # 0 = lunedì
    if wd == 5:
        return False
    if wd == 6:
        return t.time() >= time(17, 0)
    if wd == 4:
        return t.time() < time(17, 0)
    return True


class LocalPaperBroker:
    name = "local"

    def __init__(
        self,
        state_path: str | Path | None,
        price_fn: Callable[[Instrument], float],
        instruments: dict[str, Instrument],
        starting_cash: float = 100_000.0,
        slippage_bps: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.state_path = Path(state_path) if state_path else None
        self.price_fn = price_fn
        self.instruments = instruments
        self.slippage = slippage_bps / 10_000
        self.clock = clock
        self.state = {"cash": starting_cash, "realized_pnl": 0.0, "positions": {}, "orders": []}
        if self.state_path and self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=2))

    def price(self, inst: Instrument) -> float:
        return self.price_fn(inst)

    def is_market_open(self, inst: Instrument) -> bool:
        now = self.clock()
        return forex_market_open(now) if inst.is_forex else us_equity_market_open(now)

    def positions(self) -> dict[str, Position]:
        return {s: Position(s, p["qty"], p["avg_price"]) for s, p in self.state["positions"].items() if p["qty"] != 0}

    def account(self) -> Account:
        equity = self.state["cash"]
        for sym, pos in self.positions().items():
            inst = self.instruments[sym]
            equity += inst.usd_pnl(pos.qty, pos.avg_price, self.price(inst))
        return Account(equity=equity)

    def submit_market_order(self, inst: Instrument, qty: float) -> OrderResult:
        if qty == 0:
            raise BrokerError("quantità zero")
        mid = self.price(inst)
        fill = mid * (1 + self.slippage) if qty > 0 else mid * (1 - self.slippage)
        self._apply_fill(inst, qty, fill)
        oid = uuid.uuid4().hex[:12]
        self.state["orders"].append({"id": oid, "symbol": inst.symbol, "qty": qty, "price": fill, "ts": self.clock().isoformat()})
        self._save()
        return OrderResult(inst.symbol, qty, "filled", oid, fill)

    def close_position(self, inst: Instrument) -> OrderResult:
        pos = self.positions().get(inst.symbol)
        if not pos:
            raise BrokerError(f"nessuna posizione aperta su {inst.symbol}")
        return self.submit_market_order(inst, -pos.qty)

    def _apply_fill(self, inst: Instrument, qty: float, price: float) -> None:
        p = self.state["positions"].get(inst.symbol, {"qty": 0.0, "avg_price": 0.0})
        old_qty, avg = p["qty"], p["avg_price"]
        new_qty = old_qty + qty
        if old_qty == 0 or (old_qty > 0) == (qty > 0):
            # apertura o incremento: prezzo medio ponderato
            avg = (abs(old_qty) * avg + abs(qty) * price) / abs(new_qty)
        else:
            # riduzione, chiusura o inversione: realizza il P&L sulla parte chiusa
            closed = min(abs(qty), abs(old_qty)) * (1 if old_qty > 0 else -1)
            pnl = inst.usd_pnl(closed, avg, price)
            self.state["cash"] += pnl
            self.state["realized_pnl"] += pnl
            if new_qty != 0 and (new_qty > 0) != (old_qty > 0):
                avg = price  # inversione: la parte residua nasce al prezzo corrente
        if new_qty == 0:
            self.state["positions"].pop(inst.symbol, None)
        else:
            self.state["positions"][inst.symbol] = {"qty": new_qty, "avg_price": avg}
