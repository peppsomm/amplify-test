"""Client Finnhub: news per titolo, news di mercato e quotazioni azioni/ETF.

Documentazione: https://finnhub.io/docs/api
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import requests

from ..models import NewsItem

BASE_URL = "https://finnhub.io/api/v1"


class DataSourceError(RuntimeError):
    pass


class FinnhubClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, timeout: float = 15):
        self._session = session or requests.Session()
        self._session.headers["X-Finnhub-Token"] = api_key
        self._timeout = timeout

    def _get(self, path: str, **params):
        r = self._session.get(f"{BASE_URL}{path}", params=params, timeout=self._timeout)
        if r.status_code == 429:
            raise DataSourceError("Finnhub: limite di richieste superato")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _to_news(raw: dict, fallback_related: str = "") -> NewsItem | None:
        if not raw.get("headline") or not raw.get("datetime"):
            return None
        related = raw.get("related") or fallback_related
        return NewsItem(
            id=f"finnhub:{raw.get('id') or raw.get('url')}",
            source=f"finnhub/{raw.get('source', '')}",
            headline=raw["headline"],
            summary=raw.get("summary", "") or "",
            url=raw.get("url", "") or "",
            published_at=datetime.fromtimestamp(int(raw["datetime"]), tz=timezone.utc),
            related=[s.strip() for s in related.split(",") if s.strip()],
        )

    def company_news(self, symbol: str, since: date, until: date) -> list[NewsItem]:
        data = self._get("/company-news", symbol=symbol, **{"from": since.isoformat(), "to": until.isoformat()})
        return [n for n in (self._to_news(d, symbol) for d in data or []) if n]

    def market_news(self, category: str) -> list[NewsItem]:
        data = self._get("/news", category=category)
        return [n for n in (self._to_news(d) for d in data or []) if n]

    def price(self, symbol: str) -> float:
        q = self._get("/quote", symbol=symbol)
        price = float(q.get("c") or 0)
        if price <= 0:
            raise DataSourceError(f"Finnhub: prezzo non disponibile per {symbol}")
        return price
