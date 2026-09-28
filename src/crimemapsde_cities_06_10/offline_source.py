"""Local checkpoint for manually supplied official source files.

This reads a manually saved JSONL file only. It never fetches source articles,
and every imported unit remains excluded from city-map publication.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .city_scope import scope_counts
from .local_document import verify_local_document
from .storage import accept, connect, discover


def probe_robots(origin: str, target: str, user_agent: str, *, client=None) -> RobotFileParser | None:
    """Apply RFC 9309 to a robots probe while failing closed on ambiguous errors."""
    owned_client = client is None
    if owned_client:
        client = httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": user_agent})
    try:
        url = origin + "/robots.txt"
        response = client.get(url)
        if response.status_code in {404, 410} and str(response.url) == url:
            return None
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
    lines = raw.decode("utf-8").splitlines()
    if len(lines) > 500:
        raise ValueError("Split local JSONL into batches of at most 500 lines")
    prepared = []
    attachment_bytes = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"Blank JSONL record at line {number}")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"JSONL line {number} must be an object")
        evidence = None
        if "source_file" in record or "source_file_sha256" in record:
            if not record.get("source_file") or not record.get("source_file_sha256"):
                raise ValueError(f"Local source file and SHA-256 required at line {number}")
            evidence = verify_local_document(
                record["source_file"], record["source_file_sha256"],
                record.get("body", ""), base_dir=source_file.parent,
            )
            attachment_bytes += Path(evidence["path"]).stat().st_size
            if attachment_bytes > 100_000_000:
                raise ValueError("Local source files exceed 100 MB per manifest")
        prepared.append((record, evidence))
    digest = hashlib.sha256(
        raw + "".join(evidence["sha256"] if evidence else "-" for _, evidence in prepared).encode()
    ).hexdigest()
    db = connect(db_path)
    db.executescript(
        """CREATE TABLE IF NOT EXISTS offline_file_cursors (
             path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, next_line INTEGER NOT NULL,
             updated REAL NOT NULL);
           CREATE TABLE IF NOT EXISTS offline_source_units (
             id TEXT PRIMARY KEY, publisher TEXT NOT NULL, record_type TEXT NOT NULL,
             manually_supplied INTEGER NOT NULL, publication_eligible INTEGER NOT NULL);
           CREATE TABLE IF NOT EXISTS local_evidence (
             id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
             format TEXT NOT NULL, text_matches_file INTEGER NOT NULL);"""
    )
    row = db.execute("SELECT sha256,next_line FROM offline_file_cursors WHERE path=?",
                     (str(source_file),)).fetchone()
    offset = row["next_line"] if row and row["sha256"] == digest else 0
    stats = {"input_lines": len(lines), "start_line": offset, "processed": 0,
             "new": 0, "revised": 0, "unchanged": 0,
             "archive_complete": False, "publication_ready": False}
    try:
        for index in range(offset, min(len(lines), offset + limit)):
            record, evidence = prepared[index]
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
            prior_evidence = db.execute(
                "SELECT sha256 FROM local_evidence WHERE id=?", (ident,)
            ).fetchone()
            prior_file_sha = prior_evidence["sha256"] if prior_evidence else None
            discover(db, [{"id": ident, "url": record["url"], "title": title,
                           "published": published.isoformat(), "district": ""}], time.time())
            result = accept(db, ident, body, {}, time.time())
            file_sha = evidence["sha256"] if evidence else None
            if result == "unchanged" and prior_file_sha != file_sha:
                current = db.execute("SELECT revision,sha256 FROM reports WHERE id=?", (ident,)).fetchone()
                revision = current["revision"] + 1
                db.execute("UPDATE reports SET revision=? WHERE id=?", (revision, ident))
                db.execute(
                    "INSERT INTO revisions VALUES(?,?,?,?)", (ident, revision, current["sha256"], time.time())
                )
                result = "revised"
            db.execute("DELETE FROM local_evidence WHERE id=?", (ident,))
            if evidence:
                db.execute(
                    "INSERT INTO local_evidence VALUES(?,?,?,?,?)",
                    (ident, evidence["path"], evidence["sha256"], evidence["format"],
                     int(evidence["text_matches_file"])),
                )
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


def review_rows(
    db_path: str | Path, *, publisher: str, host: str, article_path: re.Pattern,
    limit: int = 100, offset: int = 0,
) -> list[dict]:
    """Export complete local source units after read-only hash and file checks."""
    if not 1 <= limit <= 200:
        raise ValueError("Review limit must be 1–200")
    if offset < 0:
        raise ValueError("Review offset must be nonnegative")
    path = Path(db_path)
    if not path.is_file():
        raise ValueError("Local source checkpoint is missing")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        has_evidence = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_evidence'"
        ).fetchone() is not None
        has_offline = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='offline_source_units'"
        ).fetchone() is not None
        has_online = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sachsen_source_units'"
        ).fetchone() is not None
        evidence_columns = (
            "e.path AS evidence_path,e.sha256 AS file_sha256,e.format AS file_format,"
            "e.text_matches_file"
            if has_evidence else
            "NULL AS evidence_path,NULL AS file_sha256,NULL AS file_format,NULL AS text_matches_file"
        )
        evidence_join = "LEFT JOIN local_evidence e ON e.id=r.id" if has_evidence else ""
        offline_columns = (
            "o.publisher AS offline_publisher,o.record_type AS offline_record_type"
            if has_offline else "NULL AS offline_publisher,NULL AS offline_record_type"
        )
        offline_join = "LEFT JOIN offline_source_units o ON o.id=r.id" if has_offline else ""
        online_columns = (
            "s.publisher AS online_publisher,s.record_type AS online_record_type,"
            "s.source_sha256 AS canonical_source_sha256,s.raw_html_sha256,s.source_verified"
            if has_online else
            "NULL AS online_publisher,NULL AS online_record_type,"
            "NULL AS canonical_source_sha256,NULL AS raw_html_sha256,NULL AS source_verified"
        )
        online_join = "LEFT JOIN sachsen_source_units s ON s.id=r.id" if has_online else ""
        rows = []
        seen = 0
        for row in db.execute(
            f"""SELECT r.*,{offline_columns},{online_columns},{evidence_columns} FROM reports r
               {offline_join} {online_join} {evidence_join}
               WHERE {'s.id IS NOT NULL OR ' if has_online else ''}{'o.id IS NOT NULL' if has_offline else '0'}
               ORDER BY r.published,r.id"""
        ):
            is_online = row["online_publisher"] is not None
            if is_online == (row["offline_publisher"] is not None):
                raise ValueError("Source unit must be exactly one of online or locally supplied")
            source_publisher = row["online_publisher"] or row["offline_publisher"]
            record_type = row["online_record_type"] or row["offline_record_type"]
            url = urlparse(row["url"])
            match = article_path.fullmatch(url.path)
            if (
                source_publisher != publisher or record_type != "multi_event_bulletin"
                or url.scheme != "https" or url.netloc not in {host, "www." + host} or not match
                or match[1] != row["id"] or url.query or url.fragment
            ):
                raise ValueError("Source identity mismatch")
            try:
                published = datetime.fromisoformat(row["published"])
            except ValueError as exc:
                raise ValueError("Source publication date invalid") from exc
            if published.tzinfo is None:
                raise ValueError("Source publication date has no time zone")
            body = row["body"]
            if (not isinstance(body, str) or len(body) < 30
                    or hashlib.sha256(body.encode()).hexdigest() != row["sha256"]):
                raise ValueError("Source body hash mismatch")
            revision = db.execute(
                "SELECT sha256 FROM revisions WHERE id=? AND revision=?", (row["id"], row["revision"])
            ).fetchone()
            if revision is None or revision["sha256"] != row["sha256"]:
                raise ValueError("Source revision hash mismatch")
            if is_online:
                expected_source = hashlib.sha256(json.dumps(
                    {
                        "source_id": row["id"], "source_url": row["url"],
                        "publisher": source_publisher, "title": row["title"],
                        "published": row["published"], "body": body,
                    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if (not row["source_verified"] or row["canonical_source_sha256"] != expected_source
                        or not isinstance(row["raw_html_sha256"], str)
                        or len(row["raw_html_sha256"]) != 64):
                    raise ValueError("Online source provenance mismatch")
            if row["file_sha256"]:
                checked = verify_local_document(row["evidence_path"], row["file_sha256"], body)
                if checked["format"] != row["file_format"] or int(checked["text_matches_file"]) != row["text_matches_file"]:
                    raise ValueError("Offline source file metadata mismatch")
            if seen < offset:
                seen += 1
                continue
            rows.append({
                "city": "leipzig", "source_id": row["id"], "source_url": row["url"],
                "published": row["published"], "title": row["title"], "source_body": body,
                "source_sha256": row["sha256"], "revision": row["revision"],
                "source_file_sha256": row["file_sha256"], "source_file_format": row["file_format"],
                "source_file_text_matches": bool(row["text_matches_file"]) if row["file_sha256"] else None,
                "source_file_path": row["evidence_path"], "review_status": "pending",
                "source_verified": is_online, "publication_ready": False,
                "record_type": record_type,
                "canonical_source_sha256": row["canonical_source_sha256"],
                "raw_html_sha256": row["raw_html_sha256"],
            })
            if len(rows) >= limit:
                break
        return rows
