"""Caricamento della configurazione (config.toml) e dei segreti (variabili d'ambiente)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .models import AssetClass, Instrument


@dataclass
class BrokersConfig:
    equity: str = "local"
    forex: str = "local"
    local_starting_cash: float = 100_000.0
    local_slippage_bps: float = 5.0


@dataclass
class NewsConfig:
    max_age_hours: float = 24
    max_news_per_cycle: int = 40
    finnhub_market_categories: list[str] = field(default_factory=lambda: ["general", "forex"])
    use_alphavantage_news: bool = False


@dataclass
class AnalystConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    batch_size: int = 8
    use_fallbacks: bool = True


@dataclass
class StrategyConfig:
    lookback_hours: float = 48
    min_confidence: float = 0.5
    entry_threshold: float = 0.6
    exit_threshold: float = 0.25


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05
    max_gross_exposure_pct: float = 1.0
    max_open_positions: int = 6
    daily_loss_limit_pct: float = 0.03
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.06
    max_holding_hours: float = 72
    cooldown_minutes: float = 120
    allow_short_equities: bool = False
    allow_short_forex: bool = True


@dataclass
class StorageConfig:
    db_path: str = "state/agent.db"
    local_broker_state: str = "state/local_broker.json"


@dataclass
class Secrets:
    anthropic_api_key: str | None
    finnhub_api_key: str | None
    alphavantage_api_key: str | None
    alpaca_key_id: str | None
    alpaca_secret_key: str | None
    oanda_token: str | None
    oanda_account_id: str | None

    @classmethod
    def from_env(cls) -> "Secrets":
        g = lambda k: os.environ.get(k) or None  # noqa: E731
        return cls(
            anthropic_api_key=g("ANTHROPIC_API_KEY"),
            finnhub_api_key=g("FINNHUB_API_KEY"),
            alphavantage_api_key=g("ALPHAVANTAGE_API_KEY"),
            alpaca_key_id=g("ALPACA_API_KEY_ID"),
            alpaca_secret_key=g("ALPACA_API_SECRET_KEY"),
            oanda_token=g("OANDA_API_TOKEN"),
            oanda_account_id=g("OANDA_ACCOUNT_ID"),
        )


@dataclass
class Config:
    watchlist: list[Instrument]
    brokers: BrokersConfig = field(default_factory=BrokersConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    analyst: AnalystConfig = field(default_factory=AnalystConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def instrument(self, symbol: str) -> Instrument:
        for inst in self.watchlist:
            if inst.symbol == symbol:
                return inst
        raise KeyError(symbol)

    @property
    def symbols(self) -> list[str]:
        return [i.symbol for i in self.watchlist]


def _section(cls, data: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Chiavi sconosciute in [{cls.__name__}]: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    wl = raw.get("watchlist", {})
    watchlist = (
        [Instrument(s.upper(), AssetClass.EQUITY) for s in wl.get("equities", [])]
        + [Instrument(s.upper(), AssetClass.ETF) for s in wl.get("etfs", [])]
        + [Instrument(s.upper(), AssetClass.FOREX) for s in wl.get("forex", [])]
    )
    if not watchlist:
        raise ValueError("La watchlist è vuota")
    for inst in watchlist:
        inst.validate()

    cfg = Config(
        watchlist=watchlist,
        brokers=_section(BrokersConfig, raw.get("brokers", {})),
        news=_section(NewsConfig, raw.get("news", {})),
        analyst=_section(AnalystConfig, raw.get("analyst", {})),
        strategy=_section(StrategyConfig, raw.get("strategy", {})),
        risk=_section(RiskConfig, raw.get("risk", {})),
        storage=_section(StorageConfig, raw.get("storage", {})),
    )
    if cfg.brokers.equity not in ("local", "alpaca"):
        raise ValueError("brokers.equity deve essere 'local' o 'alpaca'")
    if cfg.brokers.forex not in ("local", "oanda"):
        raise ValueError("brokers.forex deve essere 'local' o 'oanda'")
    return cfg
