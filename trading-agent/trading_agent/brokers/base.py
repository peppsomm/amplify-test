"""Interfaccia comune dei broker e instradamento per classe di attivo."""

from __future__ import annotations

from typing import Protocol

from ..models import Account, Instrument, OrderResult, Position


class BrokerError(RuntimeError):
    pass


class Broker(Protocol):
    name: str

    def account(self) -> Account: ...

    def positions(self) -> dict[str, Position]: ...

    def price(self, inst: Instrument) -> float: ...

    def is_market_open(self, inst: Instrument) -> bool: ...

    def submit_market_order(self, inst: Instrument, qty: float) -> OrderResult: ...

    def close_position(self, inst: Instrument) -> OrderResult: ...


class BrokerRouter:
    """Instrada azioni/ETF verso un broker e forex verso un altro (possono coincidere)."""

    def __init__(self, equity: Broker, forex: Broker):
        self.equity = equity
        self.forex = forex

    def for_instrument(self, inst: Instrument) -> Broker:
        return self.forex if inst.is_forex else self.equity

    def distinct(self) -> list[tuple[str, Broker]]:
        """Coppie (bucket, broker) senza duplicati, per letture di conto e posizioni."""
        if self.equity is self.forex:
            return [("all", self.equity)]
        return [("equity", self.equity), ("forex", self.forex)]
