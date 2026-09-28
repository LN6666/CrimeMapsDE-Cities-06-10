"""Bounded Leipzig intake from the official Medienservice Sachsen archive.

The live collector rechecks robots.txt on every run.  An explicit 404/410 is
handled as an unavailable robots file under RFC 9309 section 2.3.1.3; 2xx
rules must parse and allow access, while all other failures stop the run.
Every police bulletin stays whole and pending source-backed scene review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .offline_source import ingest_jsonl, review_rows
from .sachsen_medienservice import (
    ORIGIN,
    ROBOTS_URL,
    Article,
    SourceSpec,
    article_page,
    landing_snapshot,
    robots_policy,
    search_records,
    search_url,
    source_get,
)
from .storage import connect, fail

ARCHIVE = ORIGIN + "/medien/?search%5Binstitution_ids%5D%5B%5D=10976"
ARTICLE_PATH = re.compile(r"/medien/news/(\d+)$")
PUBLISHER = "Polizeidirektion Leipzig"
USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Leipzig official media archive)"
SPEC = SourceSpec("10976", PUBLISHER, ARCHIVE, USER_AGENT)

ONLINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sachsen_archive_cursor (
 year INTEGER PRIMARY KEY, next_page INTEGER NOT NULL, pages_scanned INTEGER NOT NULL,
 complete INTEGER NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sachsen_queue (
 id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
 failures INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
 error TEXT, http_status INTEGER
);
CREATE TABLE IF NOT EXISTS sachsen_source_units (
 id TEXT PRIMARY KEY, publisher TEXT NOT NULL, institution_id TEXT NOT NULL,
 record_type TEXT NOT NULL, source_sha256 TEXT NOT NULL, raw_html_sha256 TEXT NOT NULL,
 source_verified INTEGER NOT NULL, observed REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sachsen_source_revisions (
 id TEXT NOT NULL, revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
 body_sha256 TEXT NOT NULL, observed REAL NOT NULL, PRIMARY KEY(id,revision)
);
"""


def sync_offline(db_path: str | Path, input_path: str | Path, *, limit: int = 10) -> dict:
    return ingest_jsonl(
        db_path, input_path, publisher=PUBLISHER, host="medienservice.sachsen.de",
        article_path=ARTICLE_PATH, record_type="multi_event_bulletin", limit=limit,
    )


def _canonical_digest(article: Article) -> str:
    payload = json.dumps(
        {
            "source_id": article.source_id,
            "source_url": article.source_url,
            "publisher": PUBLISHER,
            "title": article.title,
            "published": article.published,
            "body": article.body,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _accept_online(
    db: sqlite3.Connection, article: Article, raw_html: bytes, headers, now: float
) -> str:
    body_digest = hashlib.sha256(article.body.encode()).hexdigest()
    source_digest = _canonical_digest(article)
    raw_digest = hashlib.sha256(raw_html).hexdigest()
    old = db.execute("SELECT * FROM reports WHERE id=?", (article.source_id,)).fetchone()
    old_source = db.execute(
        "SELECT source_sha256 FROM sachsen_source_units WHERE id=?", (article.source_id,)
    ).fetchone()
    changed = old is None or old_source is None or old_source["source_sha256"] != source_digest
    revision = (old["revision"] if old else 0) + int(changed)
    if old is None:
        db.execute(
            """INSERT INTO reports
               (id,url,title,published,district,body,sha256,etag,modified,checked,retry_after,
                failures,error,first_seen,revision,http_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,0,0,NULL,?,?,200)""",
            (
                article.source_id, article.source_url, article.title, article.published, "",
                article.body, body_digest, headers.get("etag"), headers.get("last-modified"),
                now, now, revision,
            ),
        )
        result = "new"
    else:
        db.execute(
            """UPDATE reports SET url=?,title=?,published=?,district='',body=?,sha256=?,etag=?,
               modified=?,checked=?,retry_after=0,failures=0,error=NULL,http_status=200,revision=?
               WHERE id=?""",
            (
                article.source_url, article.title, article.published, article.body, body_digest,
                headers.get("etag"), headers.get("last-modified"), now, revision, article.source_id,
            ),
        )
        result = "revised" if changed else "unchanged"
    db.execute(
        """INSERT INTO sachsen_source_units
           (id,publisher,institution_id,record_type,source_sha256,raw_html_sha256,source_verified,observed)
           VALUES(?,?,?,'multi_event_bulletin',?,?,1,?)
           ON CONFLICT(id) DO UPDATE SET publisher=excluded.publisher,
           institution_id=excluded.institution_id,record_type=excluded.record_type,
           source_sha256=excluded.source_sha256,raw_html_sha256=excluded.raw_html_sha256,
           source_verified=1,observed=excluded.observed""",
        (article.source_id, PUBLISHER, SPEC.institution_id, source_digest, raw_digest, now),
    )
    if changed:
        db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?)",
            (article.source_id, revision, body_digest, now),
        )
        db.execute(
            "INSERT INTO sachsen_source_revisions VALUES(?,?,?,?,?)",
            (article.source_id, revision, source_digest, body_digest, now),
        )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='city_scope_decisions'"
        ).fetchone():
            db.execute("DELETE FROM city_scope_decisions WHERE id=?", (article.source_id,))
    # A direct official fetch supersedes an earlier manually supplied copy.
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='offline_source_units'"
    ).fetchone():
        db.execute("DELETE FROM offline_source_units WHERE id=?", (article.source_id,))
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_evidence'"
    ).fetchone():
        db.execute("DELETE FROM local_evidence WHERE id=?", (article.source_id,))
    db.execute(
        "UPDATE sachsen_queue SET failures=0,retry_after=0,error=NULL,http_status=200 WHERE id=?",
        (article.source_id,),
    )
    db.commit()
    return result


def _queue_fail(db: sqlite3.Connection, ident: str, exc: Exception, now: float) -> None:
    row = db.execute("SELECT failures FROM sachsen_queue WHERE id=?", (ident,)).fetchone()
    failures = row["failures"] + 1
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    db.execute(
        """UPDATE sachsen_queue SET failures=?,retry_after=?,error=?,http_status=? WHERE id=?""",
        (
            failures,
            now + min(86400, 300 * 2 ** min(failures - 1, 8)),
            f"{type(exc).__name__}: {exc}"[:300],
            status,
            ident,
        ),
    )
    if db.execute("SELECT 1 FROM reports WHERE id=?", (ident,)).fetchone():
        fail(db, ident, f"{type(exc).__name__}: {exc}", now, status)
    db.commit()


def sync_live(
    db_path: str | Path,
    year: int,
    *,
    max_pages: int = 1,
    limit: int = 2,
    delay: float = 4.0,
    client: httpx.Client | None = None,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> dict:
    """Fetch a bounded official date-filtered archive slice and article bodies."""
    if (not 2000 <= year <= datetime.now(UTC).year or not 1 <= max_pages <= 50
            or not 1 <= limit <= 100 or delay < 4):
        raise ValueError("Use an available year, 1-50 pages, 1-100 articles and delay >= 4 seconds")
    db = connect(db_path)
    db.executescript(ONLINE_SCHEMA)
    started = time.time()
    stats = {
        "year": year, "archive_pages": 0, "discovered": 0, "new": 0, "revised": 0,
        "unchanged": 0, "failed": 0, "robots_status": None, "archive_complete": False,
        "publication_ready": False,
    }
    db.execute("INSERT INTO runs(started) VALUES(?)", (started,))
    db.commit()
    owned = client is None
    if client is None:
        client = httpx.Client(timeout=30, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    try:
        with client if owned else nullcontext(client) as session:
            robots_response = session.get(ROBOTS_URL)
            policy = robots_policy(robots_response, SPEC, minimum_delay=delay)
            stats["robots_status"] = policy.status
            last_request = [monotonic()]
            landing = source_get(
                session, ARCHIVE, SPEC, policy, last_request, sleeper=sleeper, monotonic=monotonic
            )
            snapshot = landing_snapshot(landing.text, str(landing.url), SPEC)
            state = db.execute(
                "SELECT next_page,pages_scanned,complete FROM sachsen_archive_cursor WHERE year=?",
                (year,),
            ).fetchone()
            was_complete = bool(state and state["complete"])
            page_number = state["next_page"] if state and not was_complete else 1
            pages_scanned = state["pages_scanned"] if state else 0
            for _ in range(1 if was_complete else max_pages):
                url = search_url(SPEC, first_searched=snapshot, year=year, page=page_number)
                response = source_get(
                    session, url, SPEC, policy, last_request, sleeper=sleeper, monotonic=monotonic
                )
                records, terminal = search_records(response.content, str(response.url), SPEC)
                observed = time.time()
                for ident, record_url in records:
                    db.execute(
                        """INSERT INTO sachsen_queue(id,url,first_seen,last_seen)
                           VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                           url=excluded.url,last_seen=excluded.last_seen""",
                        (ident, record_url, observed, observed),
                    )
                db.commit()
                stats["archive_pages"] += 1
                stats["discovered"] += len(records)
                pages_scanned += 1
                if not was_complete:
                    db.execute(
                        """INSERT INTO sachsen_archive_cursor(year,next_page,pages_scanned,complete,updated)
                           VALUES(?,?,?,?,?) ON CONFLICT(year) DO UPDATE SET
                           next_page=excluded.next_page,pages_scanned=excluded.pages_scanned,
                           complete=excluded.complete,updated=excluded.updated""",
                        (year, page_number if terminal else page_number + 1, pages_scanned,
                         int(terminal), time.time()),
                    )
                    db.commit()
                if terminal:
                    break
                page_number += 1

            pending = db.execute(
                """SELECT q.*,r.body,r.etag,r.modified FROM sachsen_queue q
                   LEFT JOIN reports r ON r.id=q.id WHERE q.retry_after<=?
                   AND (r.id IS NULL OR r.checked IS NULL OR r.checked<?)
                   ORDER BY r.id IS NOT NULL,q.id DESC LIMIT ?""",
                (started, started - 7 * 86400, limit),
            ).fetchall()
            for row in pending:
                headers = {}
                if row["etag"]:
                    headers["If-None-Match"] = row["etag"]
                if row["modified"]:
                    headers["If-Modified-Since"] = row["modified"]
                try:
                    response = source_get(
                        session, row["url"], SPEC, policy, last_request, headers=headers,
                        sleeper=sleeper, monotonic=monotonic,
                    )
                    if response.status_code == 304:
                        if row["body"] is None:
                            raise ValueError("304 without cached Medienservice article")
                        db.execute(
                            "UPDATE reports SET checked=?,error=NULL,failures=0,retry_after=0 WHERE id=?",
                            (time.time(), row["id"]),
                        )
                        db.execute(
                            "UPDATE sachsen_queue SET error=NULL,failures=0,retry_after=0 WHERE id=?",
                            (row["id"],),
                        )
                        db.commit()
                        stats["unchanged"] += 1
                        continue
                    article = article_page(response.text, str(response.url), SPEC)
                    if article.source_id != row["id"] or article.source_url != row["url"]:
                        raise ValueError("Medienservice article identity differs from search result")
                    if not article.published.startswith(f"{year:04d}-"):
                        raise ValueError("Medienservice search returned an article outside the requested year")
                    stats[_accept_online(db, article, response.content, response.headers, time.time())] += 1
                except (httpx.HTTPError, ValueError) as exc:
                    _queue_fail(db, row["id"], exc, time.time())
                    stats["failed"] += 1
                    stats["stopped_on_source_error"] = {
                        "source_id": row["id"], "source_url": row["url"],
                        "error_type": type(exc).__name__,
                    }
                    break
            scan = db.execute(
                "SELECT next_page,pages_scanned,complete FROM sachsen_archive_cursor WHERE year=?",
                (year,),
            ).fetchone()
            stats["next_page"] = scan["next_page"] if scan else 1
            stats["pages_scanned"] = scan["pages_scanned"] if scan else 0
            stats["archive_complete"] = bool(scan and scan["complete"])
            stats["stored"] = db.execute(
                "SELECT count(*) FROM reports WHERE published LIKE ? AND id IN "
                "(SELECT id FROM sachsen_source_units WHERE source_verified=1)", (f"{year:04d}-%",)
            ).fetchone()[0]
            stats["pending"] = db.execute(
                """SELECT count(*) FROM sachsen_queue q LEFT JOIN reports r ON r.id=q.id
                   WHERE r.id IS NULL"""
            ).fetchone()[0]
            stats["errors"] = db.execute(
                "SELECT count(*) FROM sachsen_queue WHERE error IS NOT NULL"
            ).fetchone()[0]
    except Exception as exc:
        stats["fatal_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        db.execute(
            "UPDATE runs SET finished=?,summary=? WHERE started=?", (time.time(), json.dumps(stats), started)
        )
        db.commit()
        db.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".runtime/cities/leipzig/police.sqlite")
    parser.add_argument("--input", help="Manually saved official JSONL in an ignored local directory")
    parser.add_argument("--export-review", help="Local NDJSON for source-first review")
    parser.add_argument("--live", action="store_true", help="bounded public Medienservice intake")
    parser.add_argument("--year", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--delay", type=float, default=4.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--review-offset", type=int, default=0)
    parser.add_argument("--probe-robots", action="store_true")
    args = parser.parse_args()
    if args.probe_robots:
        with httpx.Client(timeout=30, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
            policy = robots_policy(client.get(ROBOTS_URL), SPEC, minimum_delay=args.delay)
        print(json.dumps({"robots_status": policy.status, "minimum_delay": policy.delay}))
        return
    if not args.input and not args.export_review and not args.live:
        parser.error("Provide --live, --input, --export-review or --probe-robots")
    if args.input and args.live:
        parser.error("Run live and local-file intake separately")
    if args.limit < 1 or args.limit > 100:
        parser.error("Use 1-100 local records")
    result = None
    if args.input or args.live:
        import fcntl

        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        with open(args.db + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = (
                sync_offline(args.db, args.input, limit=args.limit)
                if args.input else
                sync_live(
                    args.db, args.year, max_pages=args.max_pages, limit=args.limit, delay=args.delay
                )
            )
        print(json.dumps(result, indent=2))
    if args.export_review:
        rows = review_rows(
            args.db, publisher=PUBLISHER, host="medienservice.sachsen.de",
            article_path=ARTICLE_PATH, limit=args.limit, offset=args.review_offset,
        )
        output = Path(args.export_review)
        if (output.suffix != ".ndjson" or output.resolve() == Path(args.db).resolve()
                or (args.input and output.resolve() == Path(args.input).resolve())):
            parser.error("Review output must be a distinct .ndjson file")
        if not output.resolve().is_relative_to(Path.cwd().resolve() / ".runtime"):
            parser.error("Review output must be under .runtime/")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        print(json.dumps({"review_rows": len(rows), "output": str(output)}))
    if args.input or (result and (result.get("failed") or result.get("errors"))):
        raise SystemExit(2)  # A local, unreviewed subset is never publication-ready.


if __name__ == "__main__":
    main()
