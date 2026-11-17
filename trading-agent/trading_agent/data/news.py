"""Raccoglie le news da tutte le fonti configurate per la watchlist."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..models import AssetClass, Instrument, NewsItem
from .alphavantage import AlphaVantageClient
from .finnhub import FinnhubClient

log = logging.getLogger(__name__)

# Alpha Vantage usa questi prefissi per valute
_AV_FOREX = "FOREX:"


class NewsCollector:
    def __init__(
        self,
        watchlist: list[Instrument],
        finnhub: FinnhubClient | None,
        alphavantage: AlphaVantageClient | None = None,
        market_categories: list[str] | None = None,
        max_age_hours: float = 24,
    ):
        self.watchlist = watchlist
        self.finnhub = finnhub
        self.alphavantage = alphavantage
        self.market_categories = market_categories or ["general"]
        self.max_age = timedelta(hours=max_age_hours)

    def collect(self, now: datetime | None = None) -> list[NewsItem]:
        now = now or datetime.now(timezone.utc)
        items: list[NewsItem] = []

        if self.finnhub:
            today, since = now.date(), (now - self.max_age).date()
            for inst in self.watchlist:
                if inst.asset_class in (AssetClass.EQUITY, AssetClass.ETF):
                    items += self._safe(f"finnhub company-news {inst.symbol}", self.finnhub.company_news, inst.symbol, since, today)
            categories = list(self.market_categories)
            if not any(i.is_forex for i in self.watchlist) and "forex" in categories:
                categories.remove("forex")
            for cat in categories:
                items += self._safe(f"finnhub news/{cat}", self.finnhub.market_news, cat)

        if self.alphavantage:
            currencies = sorted({c for i in self.watchlist if i.is_forex for c in (i.base, i.quote)})
            topics = ["financial_markets", "economy_macro", "economy_monetary"]
            items += self._safe("alphavantage topics", self.alphavantage.news_sentiment, None, topics)
            if currencies:
                items += self._safe(
                    "alphavantage forex", self.alphavantage.news_sentiment, [_AV_FOREX + c for c in currencies], None
                )

        cutoff = now - self.max_age
        seen: set[str] = set()
        out = []
        for n in sorted(items, key=lambda n: n.published_at, reverse=True):
            key = n.url or n.id
            if n.published_at < cutoff or key in seen:
                continue
            seen.add(key)
            out.append(n)
        return out

    @staticmethod
    def _safe(label, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # una fonte che fallisce non deve fermare il ciclo
            log.warning("Fonte news non disponibile (%s): %s", label, e)
            return []
