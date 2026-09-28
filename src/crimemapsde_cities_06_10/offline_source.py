"""Local checkpoint for official pages whose robots rules cannot be verified.

This reads a manually saved JSONL file only. It never fetches source articles,
and every imported unit remains excluded from city-map publication.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .city_scope import scope_counts
from .storage import accept, connect, discover


def probe_robots(origin: str, target: str, user_agent: str, *, client=None) -> RobotFileParser:
    """Fail closed on 404, blank, redirected, or malformed robots.txt."""
    owned_client = client is None
    if owned_client:
        client = httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": user_agent})
    try:
        url = origin + "/robots.txt"
        response = client.get(url)
        response.raise_for_status()
        if response.status_code != 200 or str(response.url) != url:
            raise ValueError("Unexpected official robots.txt response")
        if not re.search(r"(?im)^\s*user-agent\s*:", response.text):
            raise ValueError("Missing or invalid official robots.txt")
        robots = RobotFileParser()
        robots.parse(response.text.splitlines())
        if not robots.can_fetch(user_agent, target):
            raise ValueError("robots.txt disallows official archive")
        return robots
    finally:
        if owned_client:
            client.close()


def ingest_jsonl(
    db_path: str | Path, input_path: str | Path, *, publisher: str,
    host: str, article_path: re.Pattern, record_type: str,
    limit: int = 10,
) -> dict:
    """Import at most ``limit`` locally supplied source units, resuming by file hash.

    A source unit can be a multi-incident media bulletin or archive page. It is
    never automatically split into crimes or approved for city publication.
    """
    if limit < 1 or limit > 100:
        raise ValueError("Use 1-100 local records per run")
    source_file = Path(input_path).resolve()
    raw = source_file.read_bytes()
    if len(raw) > 20_000_000:
        raise ValueError("Local input is too large for one checkpoint file")
    digest = hashlib.sha256(raw).hexdigest()
    lines = raw.decode("utf-8").splitlines()
    db = connect(db_path)
    db.executescript(
        """CREATE TABLE IF NOT EXISTS offline_file_cursors (
             path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, next_line INTEGER NOT NULL,
             updated REAL NOT NULL);
           CREATE TABLE IF NOT EXISTS offline_source_units (
             id TEXT PRIMARY KEY, publisher TEXT NOT NULL, record_type TEXT NOT NULL,
             manually_supplied INTEGER NOT NULL, publication_eligible INTEGER NOT NULL);"""
    )
    row = db.execute("SELECT sha256,next_line FROM offline_file_cursors WHERE path=?",
                     (str(source_file),)).fetchone()
    offset = row["next_line"] if row and row["sha256"] == digest else 0
    stats = {"input_lines": len(lines), "start_line": offset, "processed": 0,
             "new": 0, "revised": 0, "unchanged": 0,
             "archive_complete": False, "publication_ready": False}
    try:
        for index in range(offset, min(len(lines), offset + limit)):
            if not lines[index].strip():
                raise ValueError(f"Blank JSONL record at line {index + 1}")
            record = json.loads(lines[index])
            if record.get("publisher") != publisher:
                raise ValueError(f"Wrong official publisher at line {index + 1}")
            url = urlparse(record.get("url", ""))
            match = article_path.fullmatch(url.path)
            if (url.scheme != "https" or url.netloc != host or not match
                    or url.query or url.fragment):
                raise ValueError(f"Wrong official source URL at line {index + 1}")
            published = datetime.fromisoformat(record.get("published", ""))
            if published.tzinfo is None:
                raise ValueError(f"Missing publication time zone at line {index + 1}")
            title = " ".join(record.get("title", "").split())
            body = " ".join(record.get("body", "").split())
            if len(title) < 5 or len(body) < 30 or len(body) > 2_000_000:
                raise ValueError(f"Incomplete source text at line {index + 1}")
            ident = match[1]
            discover(db, [{"id": ident, "url": record["url"], "title": title,
                           "published": published.isoformat(), "district": ""}], time.time())
            result = accept(db, ident, body, {}, time.time())
            db.execute(
                """INSERT INTO offline_source_units
                   (id,publisher,record_type,manually_supplied,publication_eligible)
                   VALUES(?,?,?,1,0) ON CONFLICT(id) DO UPDATE SET
                   publisher=excluded.publisher,record_type=excluded.record_type,
                   manually_supplied=1,publication_eligible=0""",
                (ident, publisher, record_type),
            )
            db.execute(
                """INSERT INTO offline_file_cursors(path,sha256,next_line,updated)
                   VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,
                   next_line=excluded.next_line,updated=excluded.updated""",
                (str(source_file), digest, index + 1, time.time()),
            )
            db.commit()
            stats[result] += 1
            stats["processed"] += 1
        stats["next_line"] = offset + stats["processed"]
        stats["file_scan_complete"] = stats["next_line"] == len(lines)
        stats["city_scope"] = scope_counts(db)
    finally:
        db.close()
    return stats
