"""Trasforma gli impatti stimati da Claude in segnali e decisioni.

score(strumento) = somma su news recenti di
    segno(direzione) * magnitude * confidence * 0.5 ** (età / emivita(orizzonte))

Le news a orizzonte breve perdono peso in fretta, quelle a orizzonte lungo più lentamente.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from .config import StrategyConfig
from .models import Horizon, Signal, StoredImpact

HALF_LIFE_HOURS = {Horizon.INTRADAY: 4.0, Horizon.DAYS: 24.0, Horizon.WEEKS: 96.0}


class Action(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    HOLD = "hold"


def compute_signals(
    impacts: list[StoredImpact], symbols: list[str], now: datetime, cfg: StrategyConfig
) -> dict[str, Signal]:
    signals = {s: Signal(symbol=s) for s in symbols}
    contributions: dict[str, list[tuple[float, str]]] = {s: [] for s in symbols}
    for si in impacts:
        imp = si.impact
        if imp.symbol not in signals or imp.confidence < cfg.min_confidence:
            continue
        age_h = max(0.0, (now - si.published_at).total_seconds() / 3600)
        if age_h > cfg.lookback_hours:
            continue
        decay = 0.5 ** (age_h / HALF_LIFE_HOURS[imp.horizon])
        c = imp.direction.sign * imp.magnitude * imp.confidence * decay
        if c == 0:
            continue
        sig = signals[imp.symbol]
        sig.score += c
        sig.n_impacts += 1
        contributions[imp.symbol].append((c, f"{c:+.2f} {si.headline[:90]} — {imp.rationale}"))
    for s, items in contributions.items():
        signals[s].reasons = [r for _, r in sorted(items, key=lambda x: -abs(x[0]))[:3]]
    return signals


def decide(signal: Signal, position_qty: float, cfg: StrategyConfig, allow_short: bool) -> Action:
    score = signal.score
    if position_qty > 0:
        return Action.EXIT if score <= -cfg.exit_threshold else Action.HOLD
    if position_qty < 0:
        return Action.EXIT if score >= cfg.exit_threshold else Action.HOLD
    if score >= cfg.entry_threshold:
        return Action.ENTER_LONG
    if score <= -cfg.entry_threshold and allow_short:
        return Action.ENTER_SHORT
    return Action.HOLD
