from datetime import datetime, timedelta, timezone

import pytest

from trading_agent.brokers.local_paper import LocalPaperBroker, forex_market_open, us_equity_market_open
from trading_agent.config import RiskConfig, StrategyConfig
from trading_agent.models import AssetClass, Direction, Horizon, Impact, Instrument, Position, StoredImpact
from trading_agent.risk import RiskManager
from trading_agent.strategy import Action, compute_signals, decide

NOW = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)  # martedì, 11:00 a New York
AAPL = Instrument("AAPL", AssetClass.EQUITY)
EURUSD = Instrument("EUR_USD", AssetClass.FOREX)
USDJPY = Instrument("USD_JPY", AssetClass.FOREX)


def imp(symbol, direction, mag=0.8, conf=0.9, horizon=Horizon.DAYS, age_h=0.0):
    return StoredImpact(
        news_id=f"n{symbol}{age_h}{direction}",
        published_at=NOW - timedelta(hours=age_h),
        headline="headline",
        impact=Impact(symbol=symbol, direction=Direction(direction), magnitude=mag, confidence=conf, horizon=horizon, rationale="r"),
    )


# --- modelli --------------------------------------------------------------
def test_impact_clamps_values():
    i = Impact(symbol="AAPL", direction="bullish", magnitude=3, confidence=-1, horizon="days", rationale="")
    assert i.magnitude == 1.0 and i.confidence == 0.0


def test_forex_notional_and_pnl():
    assert EURUSD.usd_notional(10_000, 1.10) == pytest.approx(11_000)
    assert USDJPY.usd_notional(10_000, 150) == 10_000
    # long 10k USD_JPY da 150 a 151: +10k JPY = ~66.2 USD
    assert USDJPY.usd_pnl(10_000, 150, 151) == pytest.approx(10_000 / 151)
    assert EURUSD.usd_pnl(-10_000, 1.10, 1.09) == pytest.approx(100)


def test_cross_pairs_rejected():
    with pytest.raises(ValueError):
        Instrument("EUR_GBP", AssetClass.FOREX).validate()


# --- strategia ------------------------------------------------------------
def test_signal_sums_and_decays():
    cfg = StrategyConfig()
    sig = compute_signals([imp("AAPL", "bullish"), imp("AAPL", "bullish", age_h=24)], ["AAPL"], NOW, cfg)["AAPL"]
    # 0.72 fresca + 0.72 * 0.5 (un'emivita "days")
    assert sig.score == pytest.approx(0.72 + 0.36)
    assert sig.n_impacts == 2


def test_signal_ignores_low_confidence_old_and_unknown():
    cfg = StrategyConfig(min_confidence=0.5, lookback_hours=48)
    impacts = [imp("AAPL", "bullish", conf=0.3), imp("AAPL", "bullish", age_h=72), imp("TSLA", "bullish")]
    assert compute_signals(impacts, ["AAPL"], NOW, cfg)["AAPL"].n_impacts == 0


def test_intraday_decays_faster_than_weeks():
    cfg = StrategyConfig()
    s = compute_signals(
        [imp("AAPL", "bullish", horizon=Horizon.INTRADAY, age_h=12), imp("MSFT", "bullish", horizon=Horizon.WEEKS, age_h=12)],
        ["AAPL", "MSFT"],
        NOW,
        cfg,
    )
    assert s["AAPL"].score < s["MSFT"].score


def test_decide():
    cfg = StrategyConfig(entry_threshold=0.6, exit_threshold=0.25)
    from trading_agent.models import Signal

    assert decide(Signal("X", 0.7), 0, cfg, allow_short=False) is Action.ENTER_LONG
    assert decide(Signal("X", -0.7), 0, cfg, allow_short=False) is Action.HOLD
    assert decide(Signal("X", -0.7), 0, cfg, allow_short=True) is Action.ENTER_SHORT
    assert decide(Signal("X", -0.3), 10, cfg, allow_short=False) is Action.EXIT
    assert decide(Signal("X", -0.1), 10, cfg, allow_short=False) is Action.HOLD
    assert decide(Signal("X", 0.3), -10, cfg, allow_short=True) is Action.EXIT


# --- rischio --------------------------------------------------------------
def test_sizing_respects_position_and_exposure_limits():
    rm = RiskManager(RiskConfig(max_position_pct=0.05, max_gross_exposure_pct=1.0, max_open_positions=3))
    r = rm.size_entry(AAPL, 1, 1.0, 200.0, 100_000, 0, 0)
    assert r.qty == 25  # 5000 / 200
    r = rm.size_entry(AAPL, 1, 0.5, 200.0, 100_000, 0, 0)
    assert r.qty == 12  # metà dimensione con score 0.5
    r = rm.size_entry(AAPL, 1, 1.0, 200.0, 100_000, 99_000, 0)
    assert r.qty == 5  # solo 1000 USD di spazio residuo
    assert rm.size_entry(AAPL, 1, 1.0, 200.0, 100_000, 0, 3).qty == 0
    assert rm.size_entry(EURUSD, -1, 1.0, 1.10, 100_000, 0, 0).qty == -4545


def test_exit_rules():
    rm = RiskManager(RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.06, max_holding_hours=72))
    long = Position("AAPL", 10, 100.0)
    assert "stop loss" in rm.exit_reason(AAPL, long, 96.9, NOW, NOW)
    assert "take profit" in rm.exit_reason(AAPL, long, 106.5, NOW, NOW)
    assert rm.exit_reason(AAPL, long, 101, NOW, NOW) is None
    assert "durata" in rm.exit_reason(AAPL, long, 101, NOW - timedelta(hours=80), NOW)
    short = Position("AAPL", -10, 100.0)
    assert "stop loss" in rm.exit_reason(AAPL, short, 103.5, NOW, NOW)


def test_daily_loss_and_cooldown():
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03, cooldown_minutes=60))
    assert rm.daily_loss_breached(100_000, 96_900)
    assert not rm.daily_loss_breached(100_000, 98_000)
    assert rm.in_cooldown(NOW - timedelta(minutes=30), NOW)
    assert not rm.in_cooldown(NOW - timedelta(minutes=90), NOW)


# --- broker locale --------------------------------------------------------
def test_local_broker_round_trip_pnl():
    prices = {"AAPL": 100.0, "EUR_USD": 1.10}
    b = LocalPaperBroker(None, lambda i: prices[i.symbol], {"AAPL": AAPL, "EUR_USD": EURUSD}, 10_000, slippage_bps=0, clock=lambda: NOW)
    b.submit_market_order(AAPL, 10)
    b.submit_market_order(AAPL, 10)
    prices["AAPL"] = 110.0
    b.submit_market_order(AAPL, 10)
    pos = b.positions()["AAPL"]
    assert pos.qty == 30 and pos.avg_price == pytest.approx(310 / 3)
    assert b.account().equity == pytest.approx(10_000 + 200)
    b.close_position(AAPL)
    assert "AAPL" not in b.positions()
    assert b.account().equity == pytest.approx(10_200)

    b.submit_market_order(EURUSD, -10_000)
    prices["EUR_USD"] = 1.09
    assert b.account().equity == pytest.approx(10_300)


def test_local_broker_reversal():
    prices = {"AAPL": 100.0}
    b = LocalPaperBroker(None, lambda i: prices[i.symbol], {"AAPL": AAPL}, 0, slippage_bps=0, clock=lambda: NOW)
    b.submit_market_order(AAPL, 10)
    prices["AAPL"] = 105.0
    b.submit_market_order(AAPL, -15)  # chiude 10 (+50) e apre short 5 a 105
    pos = b.positions()["AAPL"]
    assert pos.qty == -5 and pos.avg_price == 105.0
    assert b.state["cash"] == pytest.approx(50)


def test_market_hours():
    assert us_equity_market_open(NOW)
    assert not us_equity_market_open(datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc))  # domenica
    assert not forex_market_open(datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc))  # sabato
    assert forex_market_open(datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc))  # domenica sera NY
