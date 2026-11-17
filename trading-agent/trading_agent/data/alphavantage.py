"""Client Alpha Vantage: news con sentiment (NEWS_SENTIMENT) e cambi forex.

Il piano gratuito consente ~25 richieste al giorno: i cambi sono messi in cache.
Documentazione: https://www.alphavantage.co/documentation/
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from ..models import NewsItem
from .finnhub import DataSourceError

BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageClient:
    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        timeout: float = 20,
        fx_cache_seconds: float = 1800,
    ):
        self._key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout
        self._fx_cache_seconds = fx_cache_seconds
        self._fx_cache: dict[str, tuple[float, float]] = {}

    def _get(self, **params) -> dict:
        r = self._session.get(BASE_URL, params={**params, "apikey": self._key}, timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        # Alpha Vantage segnala errori e limiti con HTTP 200 e un campo testuale
        for k in ("Note", "Information", "Error Message"):
            if k in data:
                raise DataSourceError(f"Alpha Vantage: {data[k]}")
        return data

    def news_sentiment(self, tickers: list[str] | None = None, topics: list[str] | None = None, limit: int = 50) -> list[NewsItem]:
        params = {"function": "NEWS_SENTIMENT", "limit": str(limit), "sort": "LATEST"}
        if tickers:
            params["tickers"] = ",".join(tickers)
        if topics:
            params["topics"] = ",".join(topics)
        out: list[NewsItem] = []
        for f in self._get(**params).get("feed", []):
            try:
                published = datetime.strptime(f["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            out.append(
                NewsItem(
                    id=f"alphavantage:{f.get('url')}",
                    source=f"alphavantage/{f.get('source', '')}",
                    headline=f.get("title", ""),
                    summary=f.get("summary", ""),
                    url=f.get("url", ""),
                    published_at=published,
                    related=[t.get("ticker", "") for t in f.get("ticker_sentiment", [])],
                    provider_sentiment=_float_or_none(f.get("overall_sentiment_score")),
                )
            )
        return out

    def price(self, symbol: str) -> float:
        """Cambio per una coppia in formato BASE_QUOTE (es. EUR_USD)."""
        cached = self._fx_cache.get(symbol)
        if cached and time.monotonic() - cached[1] < self._fx_cache_seconds:
            return cached[0]
        base, quote = symbol.split("_")
        data = self._get(function="CURRENCY_EXCHANGE_RATE", from_currency=base, to_currency=quote)
        try:
            rate = float(data["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
        except (KeyError, ValueError) as e:
            raise DataSourceError(f"Alpha Vantage: cambio non disponibile per {symbol}") from e
        self._fx_cache[symbol] = (rate, time.monotonic())
        return rate


def _float_or_none(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
