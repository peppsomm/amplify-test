"""OANDA practice (conto demo) per il forex.

L'URL è fisso sull'ambiente practice: questo modulo non può inviare ordini reali.
Documentazione: https://developer.oanda.com/rest-live-v20/introduction/
"""

from __future__ import annotations

import requests

from ..models import Account, Instrument, OrderResult, Position
from .base import BrokerError

PRACTICE_URL = "https://api-fxpractice.oanda.com"


class OandaPracticeBroker:
    name = "oanda-practice"

    def __init__(self, token: str, account_id: str, session: requests.Session | None = None, timeout: float = 15):
        self._s = session or requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._acct = f"{PRACTICE_URL}/v3/accounts/{account_id}"
        self._timeout = timeout

    def _req(self, method: str, url: str, **kw):
        r = self._s.request(method, url, timeout=self._timeout, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"OANDA {method} {url}: {r.status_code} {r.text[:300]}")
        return r.json()

    def account(self) -> Account:
        # NAV è espresso nella valuta del conto: usa un conto practice in USD
        return Account(equity=float(self._req("GET", f"{self._acct}/summary")["account"]["NAV"]))

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self._req("GET", f"{self._acct}/openPositions")["positions"]:
            long_u, short_u = float(p["long"]["units"]), float(p["short"]["units"])
            if long_u:
                out[p["instrument"]] = Position(p["instrument"], long_u, float(p["long"]["averagePrice"]))
            elif short_u:
                out[p["instrument"]] = Position(p["instrument"], short_u, float(p["short"]["averagePrice"]))
        return out

    def _pricing(self, inst: Instrument) -> dict:
        return self._req("GET", f"{self._acct}/pricing", params={"instruments": inst.symbol})["prices"][0]

    def price(self, inst: Instrument) -> float:
        p = self._pricing(inst)
        return (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2

    def is_market_open(self, inst: Instrument) -> bool:
        return bool(self._pricing(inst).get("tradeable", False))

    def submit_market_order(self, inst: Instrument, qty: float) -> OrderResult:
        body = {"order": {"type": "MARKET", "instrument": inst.symbol, "units": str(int(qty)), "timeInForce": "FOK", "positionFill": "DEFAULT"}}
        data = self._req("POST", f"{self._acct}/orders", json=body)
        fill = data.get("orderFillTransaction")
        if fill:
            return OrderResult(inst.symbol, qty, "filled", fill.get("id", ""), float(fill.get("price", 0)) or None)
        cancel = data.get("orderCancelTransaction", {})
        return OrderResult(inst.symbol, qty, "rejected", cancel.get("id", ""), None, cancel.get("reason", "sconosciuto"))

    def close_position(self, inst: Instrument) -> OrderResult:
        pos = self.positions().get(inst.symbol)
        if not pos:
            raise BrokerError(f"nessuna posizione aperta su {inst.symbol}")
        body = {"longUnits": "ALL"} if pos.qty > 0 else {"shortUnits": "ALL"}
        data = self._req("PUT", f"{self._acct}/positions/{inst.symbol}/close", json=body)
        fill = data.get("longOrderFillTransaction") or data.get("shortOrderFillTransaction") or {}
        return OrderResult(inst.symbol, -pos.qty, "filled" if fill else "submitted", fill.get("id", ""), float(fill["price"]) if fill.get("price") else None)
