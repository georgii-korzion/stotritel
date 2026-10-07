"""Состояние в SQLite: позиции, отправленные алерты, циклы, служебные сообщения (раздел 8 ТЗ)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .rules import PositionKey, PositionUpdate, PrevPosition

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    brand_key    TEXT NOT NULL,
    title_key    TEXT NOT NULL,
    region       TEXT NOT NULL,
    brand        TEXT NOT NULL,
    title        TEXT NOT NULL,
    last_price   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    had_gap      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (brand_key, title_key, region)
);
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY,
    cycle_id  INTEGER,
    brand_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    region    TEXT NOT NULL,
    rule      TEXT NOT NULL,
    price     TEXT NOT NULL,
    sent_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_by_time ON alerts (sent_at);
CREATE TABLE IF NOT EXISTS cycles (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- live | file
    attempt_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- started | ok | error
    error_kind    TEXT,
    http_status   INTEGER,
    retry_after_s INTEGER,
    sha256        TEXT,
    offers        INTEGER,
    positions     INTEGER,
    alerts        INTEGER,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS cycles_by_time ON cycles (source, attempt_at);
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    sent_at    TEXT,
    dropped    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def ts(dt: datetime) -> str:
    """Время для базы: UTC, ISO 8601, одинаковая длина — строки сравниваются как время."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Cycle:
    id: int
    source: str
    attempt_at: datetime
    status: str
    error_kind: str | None
    http_status: int | None
    retry_after_s: int | None
    sha256: str | None
    offers: int | None


@dataclass(frozen=True)
class OutboxItem:
    id: int
    created_at: datetime
    kind: str
    text: str


class Store:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            if self.path.exists():
                self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            else:
                self.db = sqlite3.connect(":memory:")
                self.db.executescript(SCHEMA)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    # --- циклы -------------------------------------------------------------

    def start_cycle(self, at: datetime, source: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO cycles (source, attempt_at, status) VALUES (?, ?, 'started')",
                (source, ts(at)),
            )
        return cur.lastrowid

    def finish_cycle(self, cycle_id: int, at: datetime, status: str, **fields) -> None:
        self._finish_cycle(cycle_id, at, status, **fields)
        self.db.commit()

    def _finish_cycle(self, cycle_id: int, at: datetime, status: str, *, error_kind=None,
                      http_status=None, retry_after_s=None, sha256=None, offers=None,
                      positions=None, alerts=None, note=None) -> None:
        self.db.execute(
            """UPDATE cycles SET finished_at=?, status=?, error_kind=?, http_status=?, retry_after_s=?,
                   sha256=?, offers=?, positions=?, alerts=?, note=? WHERE id=?""",
            (ts(at), status, error_kind, http_status, retry_after_s, sha256, offers, positions,
             alerts, note, cycle_id),
        )

    def cycle_status(self, cycle_id: int) -> str | None:
        row = self.db.execute("SELECT status FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        return row["status"] if row else None

    def mark_interrupted(self) -> int:
        """Циклы, оборванные рестартом, считаются ошибочными."""
        with self.db:
            cur = self.db.execute(
                "UPDATE cycles SET status='error', error_kind='interrupted' WHERE status='started'"
            )
        return cur.rowcount

    def recent_cycles(self, source: str = "live", limit: int = 20) -> list[Cycle]:
        rows = self.db.execute(
            "SELECT * FROM cycles WHERE source=? ORDER BY attempt_at DESC, id DESC LIMIT ?",
            (source, limit),
        ).fetchall()
        return [self._cycle(r) for r in rows]

    def last_success(self, source: str | None = None) -> Cycle | None:
        q = "SELECT * FROM cycles WHERE status='ok'"
        args: tuple = ()
        if source:
            q += " AND source=?"
            args = (source,)
        row = self.db.execute(q + " ORDER BY attempt_at DESC, id DESC LIMIT 1", args).fetchone()
        return self._cycle(row) if row else None

    @staticmethod
    def _cycle(r: sqlite3.Row) -> Cycle:
        return Cycle(r["id"], r["source"], from_ts(r["attempt_at"]), r["status"], r["error_kind"],
                     r["http_status"], r["retry_after_s"], r["sha256"], r["offers"])

    def stats_since(self, since: datetime) -> tuple[int, int, int]:
        """(циклов, ошибок, алертов) живых циклов с момента since."""
        cycles, errors = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(status='error'), 0) FROM cycles "
            "WHERE source='live' AND attempt_at>=? AND status!='started'",
            (ts(since),),
        ).fetchone()
        alerts = self.db.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT cycle_id, brand_key, title_key, region "
            "FROM alerts WHERE sent_at>=?)",
            (ts(since),),
        ).fetchone()[0]
        return cycles, errors, alerts

    # --- позиции -----------------------------------------------------------

    def load_positions(self) -> dict[PositionKey, PrevPosition]:
        rows = self.db.execute(
            "SELECT brand_key, title_key, region, last_price, last_seen_at, had_gap FROM positions"
        ).fetchall()
        return {
            (r["brand_key"], r["title_key"], r["region"]): PrevPosition(
                Decimal(r["last_price"]), from_ts(r["last_seen_at"]), bool(r["had_gap"])
            )
            for r in rows
        }

    def save_cycle_result(self, cycle_id: int, at: datetime, updates: Iterable[PositionUpdate],
                          clear_gap: Iterable[PositionKey], **cycle_fields) -> None:
        """Новое состояние позиций и итог цикла — одной транзакцией."""
        with self.db:
            self.db.executemany(
                """INSERT INTO positions (brand_key, title_key, region, brand, title, last_price, last_seen_at, had_gap)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (brand_key, title_key, region) DO UPDATE SET
                       brand=excluded.brand, title=excluded.title, last_price=excluded.last_price,
                       last_seen_at=excluded.last_seen_at, had_gap=excluded.had_gap""",
                [(*u.key, u.brand, u.title, str(u.price), ts(u.seen_at), int(u.had_gap)) for u in updates],
            )
            self.db.executemany(
                "UPDATE positions SET had_gap=0 WHERE brand_key=? AND title_key=? AND region=?",
                list(clear_gap),
            )
            self._finish_cycle(cycle_id, at, "ok", **cycle_fields)

    # --- алерты ------------------------------------------------------------

    def record_alert(self, cycle_id: int | None, key: PositionKey, rules: Iterable[str],
                     price: Decimal, sent_at: datetime) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO alerts (cycle_id, brand_key, title_key, region, rule, price, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(cycle_id, *key, rule, str(price), ts(sent_at)) for rule in rules],
            )

    def alerted_since(self, since: datetime) -> dict[tuple[PositionKey, str], Decimal]:
        """Минимальная цена, о которой уже писали, по (позиция, правило) с момента since."""
        out: dict[tuple[PositionKey, str], Decimal] = {}
        for r in self.db.execute(
            "SELECT brand_key, title_key, region, rule, price FROM alerts WHERE sent_at>=?", (ts(since),)
        ):
            k = ((r["brand_key"], r["title_key"], r["region"]), r["rule"])
            price = Decimal(r["price"])
            if k not in out or price < out[k]:
                out[k] = price
        return out

    # --- служебные сообщения -----------------------------------------------

    def enqueue(self, kind: str, text: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT INTO outbox (created_at, kind, text) VALUES (?, ?, ?)", (ts(at), kind, text))

    def pending(self) -> list[OutboxItem]:
        rows = self.db.execute(
            "SELECT id, created_at, kind, text FROM outbox WHERE sent_at IS NULL AND dropped=0 ORDER BY id"
        ).fetchall()
        return [OutboxItem(r["id"], from_ts(r["created_at"]), r["kind"], r["text"]) for r in rows]

    def mark_sent(self, item_id: int, at: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE outbox SET sent_at=? WHERE id=?", (ts(at), item_id))

    def mark_dropped(self, item_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE outbox SET dropped=1 WHERE id=?", (item_id,))

    # --- meta --------------------------------------------------------------

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
