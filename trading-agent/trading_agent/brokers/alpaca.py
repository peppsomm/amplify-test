"""Alpaca paper trading (azioni ed ETF USA).

L'URL è fisso sull'ambiente paper: questo modulo non può inviare ordini reali.
Documentazione: https://docs.alpaca.markets/reference
"""

from __future__ import annotations

import requests

from ..models import Account, Instrument, OrderResult, Position
from .base import BrokerError

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class AlpacaPaperBroker:
    name = "alpaca-paper"

    def __init__(self, key_id: str, secret_key: str, session: requests.Session | None = None, timeout: float = 15):
        self._s = session or requests.Session()
        self._s.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key})
        self._timeout = timeout

    def _req(self, method: str, url: str, **kw):
        r = self._s.request(method, url, timeout=self._timeout, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"Alpaca {method} {url}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def account(self) -> Account:
        return Account(equity=float(self._req("GET", f"{PAPER_URL}/v2/account")["equity"]))

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self._req("GET", f"{PAPER_URL}/v2/positions"):
            qty = abs(float(p["qty"])) * (-1 if p.get("side") == "short" else 1)
            out[p["symbol"]] = Position(p["symbol"], qty, float(p["avg_entry_price"]))
        return out

    def price(self, inst: Instrument) -> float:
        data = self._req("GET", f"{DATA_URL}/v2/stocks/{inst.symbol}/trades/latest")
        return float(data["trade"]["p"])

    def is_market_open(self, inst: Instrument) -> bool:
        return bool(self._req("GET", f"{PAPER_URL}/v2/clock")["is_open"])

    def submit_market_order(self, inst: Instrument, qty: float) -> OrderResult:
        body = {
            "symbol": inst.symbol,
            "qty": str(int(abs(qty))),
            "side": "buy" if qty > 0 else "sell",
            "type": "market",
            "time_in_force": "day",
        }
        o = self._req("POST", f"{PAPER_URL}/v2/orders", json=body)
        price = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        return OrderResult(inst.symbol, qty, "filled" if o.get("status") == "filled" else "submitted", o.get("id", ""), price, o.get("status", ""))

    def close_position(self, inst: Instrument) -> OrderResult:
        o = self._req("DELETE", f"{PAPER_URL}/v2/positions/{inst.symbol}")
        qty = float(o.get("qty") or 0) * (-1 if o.get("side") == "sell" else 1)
        return OrderResult(inst.symbol, qty, "submitted", o.get("id", ""), None, o.get("status", ""))
