"""Tipi di dominio condivisi."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    FOREX = "forex"


class UnsupportedInstrument(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: AssetClass

    @property
    def is_forex(self) -> bool:
        return self.asset_class is AssetClass.FOREX

    @property
    def base(self) -> str:
        return self.symbol.split("_")[0]

    @property
    def quote(self) -> str:
        return self.symbol.split("_")[1]

    def validate(self) -> None:
        if self.is_forex:
            parts = self.symbol.split("_")
            if len(parts) != 2 or not all(len(p) == 3 for p in parts):
                raise UnsupportedInstrument(f"Coppia forex non valida: {self.symbol} (usa BASE_QUOTE, es. EUR_USD)")
            if "USD" not in parts:
                raise UnsupportedInstrument(f"Solo coppie con USD supportate: {self.symbol}")

    def usd_notional(self, qty: float, price: float) -> float:
        """Valore assoluto in USD di una posizione di `qty` unità al prezzo `price`."""
        if not self.is_forex:
            return abs(qty) * price
        if self.quote == "USD":
            return abs(qty) * price
        return abs(qty)  # base USD: le unità sono già dollari

    def usd_pnl(self, qty: float, entry: float, price: float) -> float:
        """P&L non realizzato in USD (qty con segno: >0 long, <0 short)."""
        pnl_quote = qty * (price - entry)
        if self.is_forex and self.base == "USD":
            return pnl_quote / price
        return pnl_quote


class NewsItem(BaseModel):
    id: str  # "<fonte>:<id nativo>"
    source: str
    headline: str
    summary: str = ""
    url: str = ""
    published_at: datetime
    related: list[str] = Field(default_factory=list)
    provider_sentiment: float | None = None


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

    @property
    def sign(self) -> int:
        return {"bullish": 1, "bearish": -1, "neutral": 0}[self.value]


class Horizon(str, Enum):
    INTRADAY = "intraday"
    DAYS = "days"
    WEEKS = "weeks"


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


class Impact(BaseModel):
    symbol: str
    direction: Direction
    magnitude: float
    confidence: float
    horizon: Horizon
    rationale: str

    @field_validator("magnitude", "confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return _clamp01(v)


class NewsAnalysis(BaseModel):
    news_id: str
    is_material: bool
    impacts: list[Impact] = Field(default_factory=list)


class AnalysisBatch(BaseModel):
    analyses: list[NewsAnalysis]


@dataclass
class StoredImpact:
    news_id: str
    published_at: datetime
    headline: str
    impact: Impact


@dataclass
class Signal:
    symbol: str
    score: float = 0.0
    n_impacts: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Position:
    symbol: str
    qty: float  # con segno
    avg_price: float


@dataclass
class Account:
    equity: float


@dataclass
class OrderResult:
    symbol: str
    qty: float  # con segno
    status: str  # "filled", "submitted", "rejected"
    order_id: str = ""
    fill_price: float | None = None
    message: str = ""
