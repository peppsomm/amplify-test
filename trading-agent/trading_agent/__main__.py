"""Riga di comando.

    python -m trading_agent run-once [--dry-run]
    python -m trading_agent loop [--interval 900] [--dry-run]
    python -m trading_agent status
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic
from dotenv import load_dotenv

from .agent import CycleReport, TradingAgent
from .analyst import NewsAnalyst
from .brokers import BrokerRouter
from .brokers.alpaca import AlpacaPaperBroker
from .brokers.local_paper import LocalPaperBroker
from .brokers.oanda import OandaPracticeBroker
from .config import Config, Secrets, load_config
from .data.alphavantage import AlphaVantageClient
from .data.finnhub import FinnhubClient
from .data.news import NewsCollector
from .models import Instrument
from .risk import RiskManager
from .store import Store
from .strategy import compute_signals

log = logging.getLogger("trading_agent")


class SetupError(RuntimeError):
    pass


def _require(value, name: str, why: str):
    if not value:
        raise SetupError(f"Manca {name} nel file .env ({why})")
    return value


def build(cfg: Config, secrets: Secrets, need_analyst: bool = True) -> tuple[TradingAgent, Store]:
    store = Store(cfg.storage.db_path)

    finnhub = FinnhubClient(_require(secrets.finnhub_api_key, "FINNHUB_API_KEY", "news e prezzi azioni/ETF"))
    av = AlphaVantageClient(secrets.alphavantage_api_key) if secrets.alphavantage_api_key else None

    has_eq = any(not i.is_forex for i in cfg.watchlist)
    has_fx = any(i.is_forex for i in cfg.watchlist)

    oanda = None
    if cfg.brokers.forex == "oanda":
        oanda = OandaPracticeBroker(
            _require(secrets.oanda_token, "OANDA_API_TOKEN", "brokers.forex = oanda"),
            _require(secrets.oanda_account_id, "OANDA_ACCOUNT_ID", "brokers.forex = oanda"),
        )
    if has_fx and cfg.brokers.forex == "local" and av is None:
        raise SetupError("Per il forex con broker locale serve ALPHAVANTAGE_API_KEY (prezzi) oppure brokers.forex = 'oanda'")

    def price_fn(inst: Instrument) -> float:
        if inst.is_forex:
            return oanda.price(inst) if oanda else av.price(inst.symbol)
        return finnhub.price(inst.symbol)

    local = None
    if "local" in (cfg.brokers.equity, cfg.brokers.forex):
        local = LocalPaperBroker(
            cfg.storage.local_broker_state,
            price_fn,
            {i.symbol: i for i in cfg.watchlist},
            cfg.brokers.local_starting_cash,
            cfg.brokers.local_slippage_bps,
        )
    if cfg.brokers.equity == "alpaca":
        equity_broker = AlpacaPaperBroker(
            _require(secrets.alpaca_key_id, "ALPACA_API_KEY_ID", "brokers.equity = alpaca"),
            _require(secrets.alpaca_secret_key, "ALPACA_API_SECRET_KEY", "brokers.equity = alpaca"),
        )
    else:
        equity_broker = local
    forex_broker = oanda or local
    if not has_eq:
        equity_broker = forex_broker
    if not has_fx:
        forex_broker = equity_broker

    collector = NewsCollector(
        cfg.watchlist,
        finnhub,
        av if cfg.news.use_alphavantage_news else None,
        cfg.news.finnhub_market_categories,
        cfg.news.max_age_hours,
    )

    analyst = None
    if need_analyst:
        # Anthropic() legge ANTHROPIC_API_KEY (o un profilo `ant auth login`)
        analyst = NewsAnalyst(
            anthropic.Anthropic(),
            cfg.watchlist,
            cfg.analyst.model,
            cfg.analyst.effort,
            cfg.analyst.batch_size,
            cfg.analyst.use_fallbacks,
        )

    agent = TradingAgent(cfg, store, collector, analyst, BrokerRouter(equity_broker, forex_broker), RiskManager(cfg.risk))
    return agent, store


def print_report(rep: CycleReport) -> None:
    print(f"\n=== Ciclo {rep.ts.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")
    print(f"News: {rep.news_fetched} lette, {rep.news_new} nuove, {rep.news_analyzed} analizzate da Claude")
    print("\nSegnali (score > 0 rialzista, < 0 ribassista):")
    for s in sorted(rep.signals.values(), key=lambda s: -abs(s.score)):
        if s.n_impacts == 0:
            continue
        print(f"  {s.symbol:8} {s.score:+.2f}  ({s.n_impacts} news)")
        for r in s.reasons[:2]:
            print(f"           {r[:160]}")
    print("\nAzioni:" if rep.actions else "\nAzioni: nessuna")
    for a in rep.actions:
        print(f"  - {a}")
    for w in rep.warnings:
        print(f"  ! {w}")


def cmd_status(agent: TradingAgent, store: Store) -> None:
    now = datetime.now(timezone.utc)
    for bucket, broker in agent.brokers.distinct():
        try:
            acct = broker.account()
            print(f"[{bucket}] {broker.name}: equity {acct.equity:,.2f} USD")
            for p in broker.positions().values():
                print(f"    {p.symbol:8} qty {p.qty:+g} @ {p.avg_price:.5g}")
        except Exception as e:  # noqa: BLE001
            print(f"[{bucket}] {broker.name}: non disponibile ({e})")
    impacts = store.recent_impacts(now - timedelta(hours=agent.cfg.strategy.lookback_hours))
    signals = compute_signals(impacts, agent.cfg.symbols, now, agent.cfg.strategy)
    print("\nSegnali correnti:")
    for s in sorted(signals.values(), key=lambda s: -abs(s.score)):
        print(f"  {s.symbol:8} {s.score:+.2f} ({s.n_impacts} news)")
    print("\nUltime decisioni:")
    for d in store.recent_decisions(10):
        print(f"  {d[0][:19]} {d[1]:8} {d[2]:12} qty={d[4]:+g} {d[6]:9} {(d[7] or '')[:100]}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="trading_agent", description="Agente di trading guidato dalle news (paper trading)")
    p.add_argument("--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r1 = sub.add_parser("run-once", help="esegue un singolo ciclo")
    r1.add_argument("--dry-run", action="store_true", help="decide ma non invia ordini")
    lp = sub.add_parser("loop", help="esegue cicli a intervalli regolari")
    lp.add_argument("--interval", type=int, default=900, help="secondi tra i cicli (default 900)")
    lp.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="mostra conto, posizioni, segnali e ultime decisioni")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config)
        agent, store = build(cfg, Secrets.from_env(), need_analyst=args.cmd != "status")
    except (SetupError, ValueError, FileNotFoundError) as e:
        print(f"Errore di configurazione: {e}", file=sys.stderr)
        return 2

    if args.cmd == "status":
        cmd_status(agent, store)
        return 0
    if args.cmd == "run-once":
        print_report(agent.run_cycle(dry_run=args.dry_run))
        return 0

    log.info("Avvio loop ogni %ds (Ctrl+C per fermare)%s", args.interval, " [DRY-RUN]" if args.dry_run else "")
    try:
        while True:
            try:
                print_report(agent.run_cycle(dry_run=args.dry_run))
            except Exception:  # noqa: BLE001 - il loop deve sopravvivere a errori transitori
                log.exception("Ciclo fallito")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Interrotto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
