"""SQLite storage for NAV history and fetch logs."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable

from .config import DB_PATH, Fund

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    isin        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    currency    TEXT NOT NULL,
    group_name  TEXT NOT NULL,
    manager     TEXT,
    inception   TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS nav (
    isin    TEXT NOT NULL,
    date    TEXT NOT NULL,
    nav     REAL NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (isin, date)
);
CREATE INDEX IF NOT EXISTS idx_nav_isin_date ON nav (isin, date);
CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    isin        TEXT NOT NULL,
    provider    TEXT NOT NULL,
    status      TEXT NOT NULL,
    rows        INTEGER NOT NULL DEFAULT 0,
    first_date  TEXT,
    last_date   TEXT,
    message     TEXT
);
CREATE TABLE IF NOT EXISTS provider_ids (
    isin        TEXT NOT NULL,
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    label       TEXT,
    PRIMARY KEY (isin, provider)
);
"""

# Higher number wins when two sources disagree about the same date.
SOURCE_PRIORITY = {
    "manual": 100,
    "bamosz": 60,
    "dskam": 60,
    "ft": 50,
    "morningstar": 45,
    "investing": 40,
    "investing-tvc": 35,
    "bamosz-latest": 30,
}


def source_priority(source: str) -> int:
    base = source.split(":")[0]
    return SOURCE_PRIORITY.get(base, 10)


class Database:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # -- funds -----------------------------------------------------------
    def upsert_funds(self, funds: Iterable[Fund]) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.conn.executemany(
            """
            INSERT INTO funds (isin, name, currency, group_name, manager, inception, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(isin) DO UPDATE SET
                name=excluded.name, currency=excluded.currency, group_name=excluded.group_name,
                manager=excluded.manager, inception=excluded.inception, updated_at=excluded.updated_at
            """,
            [
                (f.isin, f.name, f.currency, f.group, f.manager,
                 f.inception.isoformat() if f.inception else None, now)
                for f in funds
            ],
        )
        self.conn.commit()

    # -- nav -------------------------------------------------------------
    def insert_navs(self, isin: str, rows: Iterable[tuple[dt.date, float]], source: str) -> int:
        """Insert NAV rows. Existing rows are replaced only if the new source has higher priority."""
        prio = source_priority(source)
        inserted = 0
        cur = self.conn.cursor()
        for date, nav in rows:
            if nav is None or nav <= 0:
                continue
            d = date.isoformat()
            existing = cur.execute("SELECT source FROM nav WHERE isin=? AND date=?", (isin, d)).fetchone()
            if existing is None:
                cur.execute("INSERT INTO nav (isin, date, nav, source) VALUES (?, ?, ?, ?)", (isin, d, float(nav), source))
                inserted += 1
            elif source_priority(existing["source"]) < prio:
                cur.execute("UPDATE nav SET nav=?, source=? WHERE isin=? AND date=?", (float(nav), source, isin, d))
                inserted += 1
        self.conn.commit()
        return inserted

    def nav_series(self, isin: str) -> list[tuple[dt.date, float]]:
        rows = self.conn.execute("SELECT date, nav FROM nav WHERE isin=? ORDER BY date", (isin,)).fetchall()
        return [(dt.date.fromisoformat(r["date"]), float(r["nav"])) for r in rows]

    def nav_range(self, isin: str) -> tuple[dt.date | None, dt.date | None, int]:
        r = self.conn.execute(
            "SELECT MIN(date) AS a, MAX(date) AS b, COUNT(*) AS n FROM nav WHERE isin=?", (isin,)
        ).fetchone()
        a = dt.date.fromisoformat(r["a"]) if r["a"] else None
        b = dt.date.fromisoformat(r["b"]) if r["b"] else None
        return a, b, int(r["n"])

    def nav_sources(self, isin: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM nav WHERE isin=? GROUP BY source", (isin,)
        ).fetchall()
        return {r["source"]: int(r["n"]) for r in rows}

    def delete_fund_navs(self, isin: str) -> int:
        cur = self.conn.execute("DELETE FROM nav WHERE isin=?", (isin,))
        self.conn.commit()
        return cur.rowcount

    # -- provider ids ----------------------------------------------------
    def get_provider_id(self, isin: str, provider: str) -> str | None:
        r = self.conn.execute(
            "SELECT external_id FROM provider_ids WHERE isin=? AND provider=?", (isin, provider)
        ).fetchone()
        return r["external_id"] if r else None

    def set_provider_id(self, isin: str, provider: str, external_id: str, label: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO provider_ids (isin, provider, external_id, label) VALUES (?, ?, ?, ?)",
            (isin, provider, external_id, label),
        )
        self.conn.commit()

    # -- log -------------------------------------------------------------
    def log(self, run_at: str, isin: str, provider: str, status: str, rows: int = 0,
            first_date: dt.date | None = None, last_date: dt.date | None = None, message: str = "") -> None:
        self.conn.execute(
            "INSERT INTO fetch_log (run_at, isin, provider, status, rows, first_date, last_date, message)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_at, isin, provider, status, rows,
             first_date.isoformat() if first_date else None,
             last_date.isoformat() if last_date else None, message[:2000]),
        )
        self.conn.commit()

    def last_run_logs(self) -> list[sqlite3.Row]:
        r = self.conn.execute("SELECT MAX(run_at) AS m FROM fetch_log").fetchone()
        if not r or not r["m"]:
            return []
        return self.conn.execute(
            "SELECT * FROM fetch_log WHERE run_at=? ORDER BY isin, id", (r["m"],)
        ).fetchall()

    def last_run_at(self) -> str | None:
        r = self.conn.execute("SELECT MAX(run_at) AS m FROM fetch_log").fetchone()
        return r["m"] if r else None

    def close(self) -> None:
        self.conn.close()
