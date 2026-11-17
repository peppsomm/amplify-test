"""Persistenza su SQLite: news viste, impatti, decisioni (audit log) e stato operativo."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Direction, Horizon, Impact, NewsAnalysis, NewsItem, StoredImpact

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    source TEXT, headline TEXT, summary TEXT, url TEXT,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    analyzed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS impacts (
    news_id TEXT NOT NULL REFERENCES news(id),
    symbol TEXT NOT NULL, direction TEXT NOT NULL,
    magnitude REAL NOT NULL, confidence REAL NOT NULL,
    horizon TEXT NOT NULL, rationale TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS impacts_symbol ON impacts(symbol);
CREATE TABLE IF NOT EXISTS decisions (
    ts TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
    score REAL, qty REAL, price REAL,
    status TEXT NOT NULL, order_id TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS positions_meta (
    symbol TEXT PRIMARY KEY, opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_equity (
    day TEXT NOT NULL, bucket TEXT NOT NULL, start_equity REAL NOT NULL,
    PRIMARY KEY (day, bucket)
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    # --- news -------------------------------------------------------------
    def add_news(self, items: list[NewsItem], now: datetime) -> int:
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO news (id, source, headline, summary, url, published_at, fetched_at) VALUES (?,?,?,?,?,?,?)",
            [(n.id, n.source, n.headline, n.summary, n.url, _iso(n.published_at), _iso(now)) for n in items],
        )
        self.db.commit()
        return cur.rowcount

    def pending_news(self, limit: int) -> list[NewsItem]:
        rows = self.db.execute(
            "SELECT id, source, headline, summary, url, published_at FROM news WHERE analyzed = 0 "
            "ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            NewsItem(id=r[0], source=r[1], headline=r[2], summary=r[3] or "", url=r[4] or "", published_at=_dt(r[5]))
            for r in rows
        ]

    def save_analyses(self, analyses: list[NewsAnalysis], done_ids: set[str], now: datetime) -> None:
        rows = [
            (a.news_id, i.symbol, i.direction.value, i.magnitude, i.confidence, i.horizon.value, i.rationale, _iso(now))
            for a in analyses
            for i in a.impacts
        ]
        self.db.executemany("INSERT INTO impacts VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.executemany("UPDATE news SET analyzed = 1 WHERE id = ?", [(i,) for i in done_ids])
        self.db.commit()

    def skip_stale_news(self, older_than: datetime) -> int:
        """Marca come elaborate (senza analisi) le news rimaste in coda troppo a lungo."""
        cur = self.db.execute("UPDATE news SET analyzed = 1 WHERE analyzed = 0 AND published_at < ?", (_iso(older_than),))
        self.db.commit()
        return cur.rowcount

    def recent_impacts(self, since: datetime) -> list[StoredImpact]:
        rows = self.db.execute(
            "SELECT i.news_id, n.published_at, n.headline, i.symbol, i.direction, i.magnitude, i.confidence, i.horizon, i.rationale "
            "FROM impacts i JOIN news n ON n.id = i.news_id WHERE n.published_at >= ?",
            (_iso(since),),
        ).fetchall()
        return [
            StoredImpact(
                news_id=r[0],
                published_at=_dt(r[1]),
                headline=r[2],
                impact=Impact(
                    symbol=r[3], direction=Direction(r[4]), magnitude=r[5], confidence=r[6], horizon=Horizon(r[7]), rationale=r[8] or ""
                ),
            )
            for r in rows
        ]

    # --- decisioni e stato -----------------------------------------------
    def log_decision(self, ts, symbol, action, score, qty, price, status, order_id="", reason="") -> None:
        self.db.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
            (_iso(ts), symbol, action, score, qty, price, status, order_id, reason),
        )
        self.db.commit()

    def recent_decisions(self, limit: int = 20) -> list[tuple]:
        return self.db.execute(
            "SELECT ts, symbol, action, score, qty, price, status, reason FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def last_trade_at(self, symbol: str) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(ts) FROM decisions WHERE symbol = ? AND status IN ('filled', 'submitted')", (symbol,)
        ).fetchone()
        return _dt(row[0]) if row and row[0] else None

    def set_opened(self, symbol: str, ts: datetime) -> None:
        self.db.execute("INSERT OR REPLACE INTO positions_meta VALUES (?, ?)", (symbol, _iso(ts)))
        self.db.commit()

    def clear_opened(self, symbol: str) -> None:
        self.db.execute("DELETE FROM positions_meta WHERE symbol = ?", (symbol,))
        self.db.commit()

    def opened_at(self, symbol: str) -> datetime | None:
        row = self.db.execute("SELECT opened_at FROM positions_meta WHERE symbol = ?", (symbol,)).fetchone()
        return _dt(row[0]) if row else None

    def start_equity(self, day: str, bucket: str, equity: float) -> float:
        """Equity di inizio giornata (registrata al primo ciclo del giorno)."""
        self.db.execute("INSERT OR IGNORE INTO daily_equity VALUES (?, ?, ?)", (day, bucket, equity))
        self.db.commit()
        return self.db.execute("SELECT start_equity FROM daily_equity WHERE day = ? AND bucket = ?", (day, bucket)).fetchone()[0]
