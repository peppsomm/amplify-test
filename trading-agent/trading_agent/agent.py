"""Orchestrazione di un ciclo: news -> analisi Claude -> segnali -> uscite -> entrate."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .analyst import NewsAnalyst
from .brokers import Broker, BrokerError, BrokerRouter
from .config import Config
from .data.news import NewsCollector
from .models import Instrument, Position, Signal
from .risk import RiskManager
from .store import Store
from .strategy import Action, compute_signals, decide

log = logging.getLogger(__name__)


@dataclass
class BucketState:
    broker: Broker
    equity: float
    start_equity: float
    positions: dict[str, Position]
    gross_exposure: float
    kill_switch: bool


@dataclass
class CycleReport:
    ts: datetime
    news_fetched: int = 0
    news_new: int = 0
    news_analyzed: int = 0
    signals: dict[str, Signal] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TradingAgent:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        collector: NewsCollector | None,
        analyst: NewsAnalyst | None,
        brokers: BrokerRouter,
        risk: RiskManager,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.cfg = cfg
        self.store = store
        self.collector = collector
        self.analyst = analyst
        self.brokers = brokers
        self.risk = risk
        self.clock = clock
        self._inst = {i.symbol: i for i in cfg.watchlist}

    # ------------------------------------------------------------------
    def run_cycle(self, dry_run: bool = False) -> CycleReport:
        now = self.clock()
        rep = CycleReport(ts=now)

        self._ingest_news(now, rep)

        impacts = self.store.recent_impacts(now - timedelta(hours=self.cfg.strategy.lookback_hours))
        rep.signals = compute_signals(impacts, self.cfg.symbols, now, self.cfg.strategy)

        buckets = self._load_buckets(now, rep)
        self._manage_exits(buckets, rep, now, dry_run)
        self._open_entries(buckets, rep, now, dry_run)
        return rep

    # ------------------------------------------------------------------
    def _ingest_news(self, now: datetime, rep: CycleReport) -> None:
        if self.collector:
            fetched = self.collector.collect(now)
            rep.news_fetched = len(fetched)
            rep.news_new = self.store.add_news(fetched, now)
        self.store.skip_stale_news(now - timedelta(hours=self.cfg.news.max_age_hours))
        pending = self.store.pending_news(self.cfg.news.max_news_per_cycle)
        if pending and self.analyst:
            analyses, done = self.analyst.analyze(pending, now)
            self.store.save_analyses(analyses, done, now)
            rep.news_analyzed = len(done)
            if len(done) < len(pending):
                rep.warnings.append(f"{len(pending) - len(done)} news non analizzate: verranno ritentate")

    def _load_buckets(self, now: datetime, rep: CycleReport) -> dict[str, BucketState]:
        out = {}
        day = now.date().isoformat()
        for bucket, broker in self.brokers.distinct():
            try:
                equity = broker.account().equity
                positions = {s: p for s, p in broker.positions().items() if s in self._inst}
                gross = sum(self._inst[s].usd_notional(p.qty, broker.price(self._inst[s])) for s, p in positions.items())
            except Exception as e:  # noqa: BLE001 - un broker giù non deve fermare l'altro
                rep.warnings.append(f"broker {broker.name} non disponibile: {e}")
                continue
            start = self.store.start_equity(day, bucket, equity)
            kill = self.risk.daily_loss_breached(start, equity)
            if kill:
                rep.warnings.append(
                    f"[{bucket}] limite di perdita giornaliera raggiunto ({equity:.0f} vs {start:.0f}): nessuna nuova entrata"
                )
            out[bucket] = BucketState(broker, equity, start, positions, gross, kill)
        return out

    def _bucket_for(self, inst: Instrument, buckets: dict[str, BucketState]) -> BucketState | None:
        if "all" in buckets:
            return buckets["all"]
        return buckets.get("forex" if inst.is_forex else "equity")

    # ------------------------------------------------------------------
    def _manage_exits(self, buckets, rep: CycleReport, now: datetime, dry_run: bool) -> None:
        for b in buckets.values():
            for sym, pos in list(b.positions.items()):
                inst = self._inst[sym]
                sig = rep.signals[sym]
                try:
                    price = b.broker.price(inst)
                    market_open = b.broker.is_market_open(inst)
                except Exception as e:  # noqa: BLE001
                    rep.warnings.append(f"{sym}: prezzo/mercato non disponibile ({e}), uscite non verificate")
                    continue
                reason = self.risk.exit_reason(inst, pos, price, self.store.opened_at(sym), now)
                if not reason and decide(sig, pos.qty, self.cfg.strategy, self.risk.allow_short(inst)) is Action.EXIT:
                    reason = f"segnale invertito (score {sig.score:+.2f})"
                if not reason:
                    continue
                if not market_open:
                    rep.actions.append(f"{sym}: uscita necessaria ({reason}) ma mercato chiuso")
                    continue
                self._execute(b, inst, Action.EXIT, -pos.qty, price, sig.score, reason, rep, now, dry_run, close=True)

    def _open_entries(self, buckets, rep: CycleReport, now: datetime, dry_run: bool) -> None:
        # Segnali più forti per primi: ricevono per primi lo spazio di esposizione
        for sig in sorted(rep.signals.values(), key=lambda s: -abs(s.score)):
            inst = self._inst[sig.symbol]
            b = self._bucket_for(inst, buckets)
            if b is None or sig.symbol in b.positions:
                continue
            action = decide(sig, 0, self.cfg.strategy, self.risk.allow_short(inst))
            if action is Action.HOLD:
                continue
            label = f"{sig.symbol} {action.value} (score {sig.score:+.2f})"
            if b.kill_switch:
                rep.actions.append(f"{label}: bloccato dal limite di perdita giornaliera")
                continue
            if self.risk.in_cooldown(self.store.last_trade_at(sig.symbol), now):
                rep.actions.append(f"{label}: in cooldown")
                continue
            try:
                if not b.broker.is_market_open(inst):
                    rep.actions.append(f"{label}: mercato chiuso")
                    continue
                price = b.broker.price(inst)
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"{sig.symbol}: impossibile verificare mercato/prezzo ({e})")
                continue
            direction = 1 if action is Action.ENTER_LONG else -1
            sizing = self.risk.size_entry(inst, direction, sig.score, price, b.equity, b.gross_exposure, len(b.positions))
            if sizing.qty == 0:
                rep.actions.append(f"{label}: nessun ordine ({sizing.reason})")
                continue
            reason = f"{sizing.reason}; " + " | ".join(sig.reasons[:2])
            if self._execute(b, inst, action, sizing.qty, price, sig.score, reason, rep, now, dry_run):
                b.positions[sig.symbol] = Position(sig.symbol, sizing.qty, price)
                b.gross_exposure += inst.usd_notional(sizing.qty, price)

    def _execute(self, b, inst, action, qty, price, score, reason, rep, now, dry_run, close=False) -> bool:
        tag = "[DRY-RUN] " if dry_run else ""
        if dry_run:
            self.store.log_decision(now, inst.symbol, action.value, score, qty, price, "dry_run", "", reason)
            rep.actions.append(f"{tag}{inst.symbol} {action.value} qty={qty:+g} @ ~{price:.5g}: {reason}")
            return True
        try:
            res = b.broker.close_position(inst) if close else b.broker.submit_market_order(inst, qty)
        except BrokerError as e:
            self.store.log_decision(now, inst.symbol, action.value, score, qty, price, "error", "", f"{reason} | {e}")
            rep.warnings.append(f"{inst.symbol} {action.value} fallito: {e}")
            return False
        self.store.log_decision(now, inst.symbol, action.value, score, qty, res.fill_price or price, res.status, res.order_id, reason)
        if res.status == "rejected":
            rep.warnings.append(f"{inst.symbol} {action.value} rifiutato: {res.message}")
            return False
        if close:
            self.store.clear_opened(inst.symbol)
            b.positions.pop(inst.symbol, None)
        else:
            self.store.set_opened(inst.symbol, now)
        rep.actions.append(f"{inst.symbol} {action.value} qty={qty:+g} {res.status} @ {res.fill_price or price:.5g}: {reason}")
        return True
