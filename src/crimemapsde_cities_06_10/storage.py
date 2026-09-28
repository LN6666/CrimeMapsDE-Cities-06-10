"""Shared local SQLite checkpoint for publisher-authored police announcements."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
 id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL, published TEXT NOT NULL,
 district TEXT NOT NULL, body TEXT, sha256 TEXT, etag TEXT, modified TEXT,
 checked REAL, retry_after REAL DEFAULT 0, failures INTEGER DEFAULT 0, error TEXT,
 first_seen REAL NOT NULL, revision INTEGER DEFAULT 0, http_status INTEGER);
CREATE TABLE IF NOT EXISTS revisions (
 id TEXT, revision INTEGER, sha256 TEXT, observed REAL, PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS runs (started REAL PRIMARY KEY, finished REAL, summary TEXT);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def discover(db: sqlite3.Connection, records: list[dict], now: float) -> None:
    for record in records:
        db.execute(
            """INSERT INTO reports(id,url,title,published,district,first_seen)
               VALUES(:id,:url,:title,:published,:district,:now)
               ON CONFLICT(id) DO UPDATE SET url=excluded.url,title=excluded.title,
               published=excluded.published,
               district=COALESCE(NULLIF(excluded.district,''),reports.district)""",
            {**record, "now": now},
        )
    db.commit()


def accept(db: sqlite3.Connection, ident: str, body: str, headers, now: float) -> str:
    body = " ".join(body.split())
    if len(body) < 30:
        raise ValueError("Article body extraction failed; keeping previous version")
    digest = hashlib.sha256(body.encode()).hexdigest()
    old = db.execute("SELECT sha256,revision FROM reports WHERE id=?", (ident,)).fetchone()
    if old is None:
        raise ValueError("Article must be discovered before accepting a body")
    changed = old["sha256"] != digest
    revision = old["revision"] + int(changed)
    db.execute(
        """UPDATE reports SET body=?,sha256=?,etag=?,modified=?,checked=?,error=NULL,
           http_status=200,failures=0,retry_after=0,revision=? WHERE id=?""",
        (body, digest, headers.get("etag"), headers.get("last-modified"), now, revision, ident),
    )
    if changed:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (ident, revision, digest, now))
    db.commit()
    return "new" if old["sha256"] is None else "revised" if changed else "unchanged"


def fail(db: sqlite3.Connection, ident: str, error: str, now: float, status: int | None = None) -> None:
    row = db.execute("SELECT failures FROM reports WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise ValueError("Article must be discovered before recording a failure")
    failures = row["failures"] + 1
    delay = min(86400, 300 * 2 ** min(failures - 1, 8))
    db.execute(
        "UPDATE reports SET error=?,failures=?,retry_after=?,http_status=? WHERE id=?",
        (error[:200], failures, now + delay, status, ident),
    )
    db.commit()
