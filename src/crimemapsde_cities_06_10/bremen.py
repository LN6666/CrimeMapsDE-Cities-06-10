"""Checkpoint Polizei Bremen's signed Presseportal newsroom.

The native Bremen archive groups several announcements on one page and its
robots.txt is blank. That path remains offline-only. Polizei Bremen also
publishes individual, publisher-labelled announcements through Presseportal;
this module can collect that newsroom with robots checks, bounded pagination,
request throttling and a resumable local SQLite checkpoint.

Neither source proves complete archive coverage or city-map eligibility.
Every fetched announcement still needs source-first semantic and city-scope
review before it can become a map candidate.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import httpx

from .city_scope import scope_counts
from .offline_source import ingest_jsonl, probe_robots
from .storage import accept, connect, discover, fail

# Native grouped archive: retained only for manually supplied historical pages.
ORIGIN = "https://www.polizei.bremen.de"
ARCHIVE = ORIGIN + "/news/pressestelle/pressearchiv-5034"
ARTICLE_PATH = re.compile(r"/news/pressestelle/pressemeldungen-ab-[a-z0-9-]+-(\d+)$")
PUBLISHER = "Polizei Bremen"
USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Bremen native press archive)"

# Per-announcement publisher channel: safe automated intake path.
PRESS_ORIGIN = "https://www.presseportal.de"
NEWSROOM_ID = "35235"
NEWSROOM = f"{PRESS_ORIGIN}/blaulicht/nr/{NEWSROOM_ID}"
NEWSROOM_ARTICLE_PATH = re.compile(rf"/blaulicht/pm/{NEWSROOM_ID}/(\d+)$")
NEWSROOM_PAGE_PATH = re.compile(rf"/blaulicht/nr/{NEWSROOM_ID}(?:/\d+)?$")
NEXT_PAGE = re.compile(rf'<link\s+rel="next"\s+href="(/blaulicht/nr/{NEWSROOM_ID}/\d+)"')
PRESS_USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Polizei Bremen newsroom index)"


class NewsroomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self.articles_seen = 0
        self.current: dict[str, str] | None = None
        self.field: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "article" and "news" in classes:
            self.articles_seen += 1
            ident = attrs.get("data-label", "")
            if ident.isdigit():
                self.current = {
                    "id": ident,
                    "url": "",
                    "title": "",
                    "published": "",
                    "district": "",
                }
        if self.current is None:
            return
        if tag == "div" and "date" in classes:
            self.field = "published"
        elif tag == "h3" and "news-headline-clamp" in classes:
            self.field = "title"
        elif tag == "a" and self.field == "title":
            url = urljoin(PRESS_ORIGIN, attrs.get("href", ""))
            parsed = urlparse(url)
            match = NEWSROOM_ARTICLE_PATH.fullmatch(parsed.path)
            if (
                parsed.scheme == "https"
                and parsed.netloc == "www.presseportal.de"
                and match
                and match[1] == self.current["id"]
                and not parsed.query
                and not parsed.fragment
            ):
                self.current["url"] = url

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.field:
            self.current[self.field] += data

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag in {"div", "h3"}:
            self.field = None
        if tag == "article":
            row = self.current
            if row["url"] and row["title"] and row["published"]:
                published = " ".join(row["published"].replace("–", " ").split())
                row["published"] = (
                    datetime.strptime(published, "%d.%m.%Y %H:%M")
                    .replace(tzinfo=ZoneInfo("Europe/Berlin"))
                    .isoformat()
                )
                row["title"] = " ".join(row["title"].split())
                self.rows.append(row)
            self.current = None
            self.field = None


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_story = False
        self.after_heading = False
        self.in_paragraph = False
        self.in_customer = False
        self.stopped = False
        self.customer = ""
        self.parts: list[str] = []
        self.current: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        classes = dict(attrs).get("class", "").split()
        if tag == "article" and "story" in classes:
            self.in_story = True
        elif self.in_story and tag == "p" and "customer" in classes:
            self.in_customer = True
        elif self.in_story and self.after_heading and tag == "p":
            if "contact-headline" in classes or "originator" in classes:
                self.stopped = True
            elif not self.stopped:
                self.in_paragraph = True
                self.current = []

    def handle_data(self, data: str) -> None:
        if self.in_customer:
            self.customer += data
        if self.in_paragraph:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_story:
            return
        if tag == "h1":
            self.after_heading = True
        elif tag == "p":
            self.in_customer = False
            if self.in_paragraph:
                paragraph = " ".join(" ".join(self.current).split())
                if paragraph and not paragraph.startswith("Schneller informiert:"):
                    self.parts.append(paragraph)
                self.in_paragraph = False
        elif tag == "article":
            self.in_story = False


def listing_rows(page: str) -> list[dict[str, str]]:
    parser = NewsroomParser()
    parser.feed(page)
    if parser.articles_seen == 0 or parser.articles_seen != len(parser.rows):
        raise ValueError("Bremen newsroom list contains no or unparsed articles")
    if len({row["id"] for row in parser.rows}) != len(parser.rows):
        raise ValueError("Bremen newsroom list contains duplicate article IDs")
    if [row["published"] for row in parser.rows] != sorted(
        (row["published"] for row in parser.rows), reverse=True
    ):
        raise ValueError("Bremen newsroom order changed")
    return parser.rows


def article_body(page: str) -> str:
    parser = ArticleParser()
    parser.feed(page)
    body = " ".join(parser.parts)
    if parser.customer.strip() != PUBLISHER or len(body) < 30:
        raise ValueError("Bremen article parser or publisher check failed")
    return body


def _source_robots(page: str) -> RobotFileParser:
    if not re.search(r"(?im)^\s*user-agent\s*:", page):
        raise ValueError("Missing or invalid Presseportal robots.txt")
    robots = RobotFileParser()
    robots.parse(page.splitlines())
    if not robots.can_fetch(PRESS_USER_AGENT, NEWSROOM):
        raise ValueError("robots.txt disallows Polizei Bremen newsroom")
    return robots


def _fetch_robots(client):
    """Fetch source policy with bounded backoff; never treat rate limiting as permission."""
    url = PRESS_ORIGIN + "/robots.txt"
    for attempt in range(3):
        try:
            response = client.get(url)
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
            continue
        if response.status_code == 429:
            response.raise_for_status()
        if response.status_code in {500, 502, 503, 504}:
            if attempt == 2:
                response.raise_for_status()
            retry_after = response.headers.get("retry-after", "")
            wait = int(retry_after) if retry_after.isdigit() else 2**attempt
            time.sleep(min(30, max(1, wait)))
            continue
        response.raise_for_status()
        if str(response.url) != url:
            raise ValueError("Unexpected Presseportal robots.txt response")
        return response
    raise RuntimeError("Presseportal robots retry loop exhausted")


def _source_get(client, robots, url, delay, last_request, headers=None):
    expected_path = urlparse(url).path
    for _ in range(4):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.presseportal.de":
            raise ValueError("Unexpected Polizei Bremen newsroom origin")
        if not NEWSROOM_PAGE_PATH.fullmatch(parsed.path) and not NEWSROOM_ARTICLE_PATH.fullmatch(
            parsed.path
        ):
            raise ValueError("Unexpected Polizei Bremen newsroom path")
        if not robots.can_fetch(PRESS_USER_AGENT, url):
            raise ValueError("robots.txt disallows " + url)
        time.sleep(max(0, delay - (time.monotonic() - last_request[0])))
        last_request[0] = time.monotonic()
        response = client.get(url, headers=headers)
        if response.status_code in (301, 302, 303, 307, 308):
            target = urljoin(str(response.url), response.headers["location"])
            if urlparse(target).path != expected_path:
                raise ValueError("Polizei Bremen newsroom redirect changed record path")
            url = target
            continue
        if response.status_code != 304:
            response.raise_for_status()
        return response
    raise ValueError("Too many Polizei Bremen newsroom redirects")


def sync_newsroom(
    db_path: str | Path,
    year: int,
    *,
    full: bool = False,
    limit: int = 30,
    delay: float = 1.0,
    max_pages: int | None = None,
) -> dict:
    """Collect a bounded newsroom batch without claiming source completeness."""
    current_year = datetime.now(UTC).year
    if not 2015 <= year <= current_year or not 1 <= limit <= 500 or delay < 0:
        raise ValueError("Use an available year, 1-500 articles and a nonnegative delay")
    if max_pages is not None and not 1 <= max_pages <= 100:
        raise ValueError("Archive page cap must be from 1 to 100")
    db = connect(db_path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS bremen_archive_cursor (
             year INTEGER PRIMARY KEY, next_url TEXT, pages_scanned INTEGER NOT NULL,
             complete INTEGER NOT NULL, updated REAL NOT NULL)"""
    )
    db.commit()
    started = time.time()
    stats = {
        "year": year,
        "source": "police_authored_presseportal_newsroom",
        "full_archive_scan": full,
        "archive_complete": False,
        "archive_pages": 0,
        "discovered": 0,
        "new": 0,
        "revised": 0,
        "unchanged": 0,
        "failed": 0,
    }
    db.execute("INSERT INTO runs(started) VALUES(?)", (started,))
    db.commit()
    try:
        with httpx.Client(
            timeout=25, follow_redirects=False, headers={"User-Agent": PRESS_USER_AGENT}
        ) as client:
            robots_response = _fetch_robots(client)
            robots = _source_robots(robots_response.text)
            delay = max(delay, float(robots.crawl_delay(PRESS_USER_AGENT) or 0))
            last_request = [time.monotonic()]
            state = db.execute(
                "SELECT next_url,pages_scanned,complete FROM bremen_archive_cursor WHERE year=?",
                (year,),
            ).fetchone()
            resume = full and state and not state["complete"] and state["next_url"]
            url = state["next_url"] if resume else NEWSROOM
            scanned = state["pages_scanned"] if state else 0
            reached_target_year = bool(state and state["pages_scanned"])
            seen_ids: set[str] = set()
            seen_pages: set[str] = set()

            def scan(page_url: str):
                page = _source_get(client, robots, page_url, delay, last_request).text
                rows = listing_rows(page)
                following_match = NEXT_PAGE.search(page)
                following = urljoin(PRESS_ORIGIN, following_match[1]) if following_match else None
                if len(rows) == 30 and following is None:
                    raise ValueError("Full Bremen newsroom page lost its pagination link")
                selected = [row for row in rows if row["published"].startswith(str(year))]
                discover(db, selected, time.time())
                seen_ids.update(row["id"] for row in selected)
                stats["discovered"] = len(seen_ids)
                stats["archive_pages"] += 1
                return rows, following

            if resume:
                head_rows, _ = scan(NEWSROOM)
                reached_target_year = reached_target_year or any(
                    row["published"].startswith(str(year)) for row in head_rows
                )
                stats["head_refreshed"] = True
            page_limit = 2 if state and state["complete"] else (100 if full else 2)
            if max_pages is not None:
                page_limit = min(page_limit, max_pages)
            for _ in range(page_limit):
                if url in seen_pages:
                    raise ValueError("Bremen newsroom pagination loop")
                seen_pages.add(url)
                rows, following = scan(url)
                years = [int(row["published"][:4]) for row in rows]
                reached_target_year = reached_target_year or year in years
                complete = max(years) < year or following is None
                if full and not (state and state["complete"]):
                    scanned += 1
                    db.execute(
                        """INSERT INTO bremen_archive_cursor(year,next_url,pages_scanned,complete,updated)
                           VALUES(?,?,?,?,?) ON CONFLICT(year) DO UPDATE SET
                           next_url=excluded.next_url,pages_scanned=excluded.pages_scanned,
                           complete=excluded.complete,updated=excluded.updated""",
                        (year, following, scanned, int(complete), time.time()),
                    )
                    db.commit()
                if complete:
                    stats["archive_complete"] = reached_target_year
                    break
                url = following
            if state and state["complete"]:
                stats["archive_complete"] = True
            if full:
                stats["cursor_pages_scanned"] = scanned
            if not reached_target_year:
                raise ValueError("Requested year not found in bounded Bremen newsroom scan")

            pending = db.execute(
                """SELECT * FROM reports WHERE retry_after<=? AND
                   (body IS NULL OR checked IS NULL OR checked<? OR published>=?)
                   ORDER BY body IS NOT NULL,COALESCE(checked,0),published DESC LIMIT ?""",
                (
                    started,
                    started - 7 * 86400,
                    datetime.fromtimestamp(started - 2 * 86400, UTC).isoformat()[:19],
                    limit,
                ),
            ).fetchall()
            for row in pending:
                headers = {}
                if row["etag"]:
                    headers["If-None-Match"] = row["etag"]
                if row["modified"]:
                    headers["If-Modified-Since"] = row["modified"]
                try:
                    response = _source_get(
                        client, robots, row["url"], delay, last_request, headers
                    )
                    if response.status_code == 304:
                        if row["body"] is None:
                            raise ValueError("304 without cached Bremen body")
                        db.execute(
                            """UPDATE reports SET checked=?,error=NULL,failures=0,retry_after=0
                               WHERE id=?""",
                            (time.time(), row["id"]),
                        )
                        db.commit()
                        result = "unchanged"
                    else:
                        result = accept(
                            db, row["id"], article_body(response.text), response.headers, time.time()
                        )
                    stats[result] += 1
                except (httpx.HTTPError, ValueError) as exc:
                    status = (
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    fail(db, row["id"], f"{type(exc).__name__}: {exc}", time.time(), status)
                    stats["failed"] += 1
                    # A source-wide rate limit is not an article failure. Stop the
                    # bounded run immediately so the caller can respect the saved
                    # retry checkpoint instead of sending the same doomed request
                    # for every remaining article in the batch.
                    if status == 429:
                        stats["rate_limited"] = True
                        break
            stats["stored"] = db.execute(
                "SELECT count(*) FROM reports WHERE body IS NOT NULL"
            ).fetchone()[0]
            stats["pending"] = db.execute(
                "SELECT count(*) FROM reports WHERE body IS NULL"
            ).fetchone()[0]
            stats["errors"] = db.execute(
                "SELECT count(*) FROM reports WHERE error IS NOT NULL"
            ).fetchone()[0]
            stats["city_scope"] = scope_counts(db)
    finally:
        db.execute(
            "UPDATE runs SET finished=?,summary=? WHERE started=?",
            (time.time(), json.dumps(stats), started),
        )
        db.commit()
        db.close()
    return stats


def sync_offline(db_path: str | Path, input_path: str | Path, *, limit: int = 10) -> dict:
    """Retain the old native grouped-page path for manually saved backfill only."""
    return ingest_jsonl(
        db_path,
        input_path,
        publisher=PUBLISHER,
        host="www.polizei.bremen.de",
        article_path=ARTICLE_PATH,
        record_type="multi_announcement_archive_page",
        limit=limit,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".runtime/cities/bremen/police.sqlite")
    parser.add_argument("--input", help="Manually saved native grouped-page JSONL")
    parser.add_argument("--probe-native-robots", action="store_true")
    parser.add_argument("--year", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args()
    if (
        args.year < 2015
        or args.year > datetime.now(UTC).year
        or args.limit < 1
        or args.limit > 500
        or (args.input and args.limit > 100)
        or args.delay < 1
        or (args.max_pages is not None and not 1 <= args.max_pages <= 100)
    ):
        parser.error("Use an available year, 1-500 articles and delay >= 1 second")
    if args.probe_native_robots:
        probe_robots(ORIGIN, ARCHIVE, USER_AGENT)
        print("Native robots rules verified; the Presseportal collector remains separate")
        return
    import fcntl

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.input:
            result = sync_offline(args.db, args.input, limit=args.limit)
        else:
            result = sync_newsroom(
                args.db,
                args.year,
                full=args.full,
                limit=args.limit,
                delay=args.delay,
                max_pages=args.max_pages,
            )
    print(json.dumps(result, indent=2))
    if result.get("failed") or result.get("pending") or result.get("errors"):
        raise SystemExit(2)
    if args.input:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
