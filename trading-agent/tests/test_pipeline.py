"""Test end-to-end con un client Claude finto, una fonte news finta e il broker locale."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_agent.agent import TradingAgent
from trading_agent.analyst import NewsAnalyst, build_output_schema
from trading_agent.brokers import BrokerRouter
from trading_agent.brokers.local_paper import LocalPaperBroker
from trading_agent.config import Config
from trading_agent.models import AssetClass, Instrument, NewsItem
from trading_agent.risk import RiskManager
from trading_agent.store import Store

NOW = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
WATCHLIST = [Instrument("AAPL", AssetClass.EQUITY), Instrument("SPY", AssetClass.ETF), Instrument("EUR_USD", AssetClass.FOREX)]


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responder(kwargs)


def fake_client(responder):
    msgs = FakeMessages(responder)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def response(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0),
    )


def bullish_aapl(kwargs):
    text = kwargs["messages"][0]["content"]
    ids = [json.loads(chunk.split("\n")[1])["news_id"] for chunk in text.split("<news_item>")[1:]]
    analyses = []
    for nid in ids:
        if "apple" in nid:
            analyses.append(
                {
                    "news_id": nid,
                    "is_material": True,
                    "impacts": [
                        {"symbol": "AAPL", "direction": "bullish", "magnitude": 0.9, "confidence": 0.9, "horizon": "days", "rationale": "utili sopra le attese"},
                        {"symbol": "TSLA", "direction": "bullish", "magnitude": 0.9, "confidence": 0.9, "horizon": "days", "rationale": "fuori watchlist"},
                    ],
                }
            )
        else:
            analyses.append({"news_id": nid, "is_material": False, "impacts": []})
    analyses.append({"news_id": "inventato", "is_material": True, "impacts": []})
    return response({"analyses": analyses})


class FakeCollector:
    def collect(self, now):
        return [
            NewsItem(id="t:apple-earnings", source="t", headline="Apple beats estimates, raises guidance", published_at=now - timedelta(hours=1)),
            NewsItem(id="t:noise", source="t", headline="Company X opens new office", published_at=now - timedelta(hours=2)),
        ]


def make_agent(tmp_path, responder=bullish_aapl):
    cfg = Config(watchlist=WATCHLIST)
    client, msgs = fake_client(responder)
    analyst = NewsAnalyst(client, WATCHLIST, batch_size=8)
    prices = {"AAPL": 200.0, "SPY": 600.0, "EUR_USD": 1.10}
    broker = LocalPaperBroker(
        tmp_path / "broker.json", lambda i: prices[i.symbol], {i.symbol: i for i in WATCHLIST}, 100_000, 0, clock=lambda: NOW
    )
    store = Store(":memory:")
    agent = TradingAgent(cfg, store, FakeCollector(), analyst, BrokerRouter(broker, broker), RiskManager(cfg.risk), clock=lambda: NOW)
    return agent, store, broker, msgs, prices


def test_schema_restricts_symbols():
    schema = build_output_schema(["AAPL", "EUR_USD"])
    impact = schema["properties"]["analyses"]["items"]["properties"]["impacts"]["items"]
    assert impact["properties"]["symbol"]["enum"] == ["AAPL", "EUR_USD"]
    assert impact["additionalProperties"] is False


def test_request_shape(tmp_path):
    agent, _, _, msgs, _ = make_agent(tmp_path)
    agent.run_cycle()
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in call and "temperature" not in call
    assert "<news_item>" in call["messages"][0]["content"]


def test_end_to_end_opens_long_once(tmp_path):
    agent, store, broker, msgs, _ = make_agent(tmp_path)
    rep = agent.run_cycle()

    assert rep.news_new == 2 and rep.news_analyzed == 2
    assert rep.signals["AAPL"].score > 0.6
    assert rep.signals["SPY"].n_impacts == 0
    pos = broker.positions()["AAPL"]
    score = 0.9 * 0.9 * 0.5 ** (1 / 24)  # news di 1h fa, emivita "days" = 24h
    assert rep.signals["AAPL"].score == pytest.approx(score)
    assert pos.qty == int(100_000 * 0.05 * score / 200)  # = 19
    assert store.opened_at("AAPL") == NOW

    # Secondo ciclo: niente nuove news, nessuna nuova chiamata a Claude, nessun doppio ingresso
    rep2 = agent.run_cycle()
    assert rep2.news_new == 0 and len(msgs.calls) == 1
    assert broker.positions()["AAPL"].qty == pos.qty


def test_stop_loss_closes_position(tmp_path):
    agent, store, broker, _, prices = make_agent(tmp_path)
    agent.run_cycle()
    prices["AAPL"] = 190.0  # -5%
    rep = agent.run_cycle()
    assert "AAPL" not in broker.positions()
    assert any("stop loss" in a for a in rep.actions)
    assert store.opened_at("AAPL") is None


def test_dry_run_sends_no_orders(tmp_path):
    agent, store, broker, _, _ = make_agent(tmp_path)
    rep = agent.run_cycle(dry_run=True)
    assert broker.positions() == {}
    assert any(a.startswith("[DRY-RUN] AAPL enter_long") for a in rep.actions)
    assert store.recent_decisions()[0][6] == "dry_run"


def test_failed_batch_is_retried(tmp_path):
    state = {"fail": True}

    def flaky(kwargs):
        if state["fail"]:
            return response({}, stop_reason="max_tokens")
        return bullish_aapl(kwargs)

    agent, _, broker, msgs, _ = make_agent(tmp_path, flaky)
    rep = agent.run_cycle()
    assert rep.news_analyzed == 0 and rep.warnings
    assert broker.positions() == {}
    state["fail"] = False
    agent.run_cycle()
    assert len(msgs.calls) == 2
    assert "AAPL" in broker.positions()


def test_refusal_marks_batch_without_impacts(tmp_path):
    agent, _, broker, _, _ = make_agent(tmp_path, lambda kw: response({}, stop_reason="refusal"))
    rep = agent.run_cycle()
    assert rep.news_analyzed == 2
    assert broker.positions() == {}
