"""Checkpointed Stuttgart police newsroom intake, with no map publication.

The Stuttgart police site links its *Pressemeldungen* to this publisher-authored
Presseportal newsroom. Its separate native *News & Presse* list contains agency
news and is not a replacement press-release archive. A newsroom location is a
review lead only: even this city police publisher sometimes relays cases outside
the Stuttgart city boundary.
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

import httpx

from .storage import accept, connect, discover, fail

ORIGIN = "https://www.presseportal.de"
NEWSROOM = ORIGIN + "/blaulicht/nr/110977"
PUBLISHER = "Polizeipräsidium Stuttgart"
USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Stuttgart police newsroom index)"
ARTICLE_PATH = re.compile(r"/blaulicht/pm/110977/(\d+)$")
PAGE_PATH = re.compile(r"/blaulicht/nr/110977(?:/\d+)?$")
NEXT_PAGE = re.compile(r'<link\s+rel="next"\s+href="([^"]+)"')
CITY_LOCATION = re.compile(r"Stuttgart(?:-[\wÄÖÜäöüß ]+)?$", re.IGNORECASE)


class NewsroomParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.articles_seen = 0
        self.rows = []
        self.current = None
        self.field = None
        self.in_location = False
        self.location = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "article" and "news" in classes:
            self.articles_seen += 1
            if attrs.get("data-label", "").isdigit():
                self.current = {
                    "id": attrs["data-label"], "url": "", "title": "",
                    "published": "", "district": "", "locations": [],
                }
        if self.current is None:
            return
        if tag == "div" and "date" in classes:
            self.field = "published"
        elif tag == "h3" and "news-headline-clamp" in classes:
            self.field = "title"
        elif tag == "a" and "news-topic" in classes:
            self.in_location = True
            self.location = []
        elif tag == "a" and self.field == "title":
            url = urljoin(ORIGIN, attrs.get("href", ""))
            parsed = urlparse(url)
            match = ARTICLE_PATH.fullmatch(parsed.path)
            if (
                parsed.scheme == "https" and parsed.netloc == "www.presseportal.de"
                and not parsed.query and not parsed.fragment
                and match and match[1] == self.current["id"]
            ):
                self.current["url"] = url

    def handle_data(self, data):
        if self.current is not None:
            if self.field:
                self.current[self.field] += data
            if self.in_location:
                self.location.append(data)

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == "a" and self.in_location:
            value = " ".join("".join(self.location).split())
            if value:
                self.current["locations"].append(value)
            self.in_location = False
        elif tag in {"div", "h3"}:
            self.field = None
        elif tag == "article":
            row = self.current
            if row["url"] and row["title"] and row["published"]:
                row["published"] = datetime.strptime(  # noqa: DTZ007
                    " ".join(row["published"].replace("–", " ").split()),
                    "%d.%m.%Y %H:%M",
                ).isoformat()
                row["title"] = " ".join(row["title"].split())
                self.rows.append(row)
            self.current = None
            self.field = None


def listing_rows(page: str) -> list[dict]:
    parser = NewsroomParser()
    parser.feed(page)
    if not parser.articles_seen or parser.articles_seen != len(parser.rows):
        raise ValueError("Stuttgart newsroom list contains no or unparsed articles")
    dates = [row["published"] for row in parser.rows]
    if dates != sorted(dates, reverse=True):
        raise ValueError("Stuttgart newsroom publication order changed")
    return parser.rows


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_story = False
        self.after_heading = False
        self.in_paragraph = False
        self.stopped = False
        self.parts = []
        self.current = []
        self.in_customer = False
        self.customer = ""

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "").split()
        if tag == "article" and "story" in classes:
            self.in_story = True
        elif self.in_story and tag == "p" and "customer" in classes:
            self.in_customer = True
        elif self.in_story and self.after_heading and tag in {"p", "pre"}:
            if tag == "p" and ("contact-headline" in classes or "originator" in classes):
                self.stopped = True
            elif not self.stopped:
                self.in_paragraph = True
                self.current = []

    def handle_data(self, data):
        if self.in_customer:
            self.customer += data
        if self.in_paragraph:
            self.current.append(data)

    def handle_endtag(self, tag):
        if not self.in_story:
            return
        if tag == "h1":
            self.after_heading = True
        elif tag in {"p", "pre"}:
            self.in_customer = False
            if self.in_paragraph:
                paragraph = " ".join(" ".join(self.current).split())
                if paragraph and not paragraph.startswith("Schneller informiert:"):
                    self.parts.append(paragraph)
                self.in_paragraph = False
        elif tag == "article":
            self.in_story = False


def article_body(page: str) -> str:
    parser = ArticleParser()
    parser.feed(page)
    body = " ".join(parser.parts)
    if parser.customer.strip() != PUBLISHER or len(body) < 30:
        raise ValueError("Stuttgart article parser or publisher check failed")
    return body


def scope_lead(locations: list[str]) -> tuple[str, str]:
    """Classify the *listing label* for review, never the incident scene."""
    if len(locations) == 1 and CITY_LOCATION.fullmatch(locations[0]):
        return "stuttgart_review_lead", locations[0]
    return "needs_review", " | ".join(locations)


def next_url(page: str) -> str | None:
    match = NEXT_PAGE.search(page)
    if not match:
        return None
    url = urljoin(ORIGIN, match[1])
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "www.presseportal.de"
        or not PAGE_PATH.fullmatch(parsed.path) or parsed.query or parsed.fragment
    ):
        raise ValueError("Unexpected Stuttgart newsroom pagination link")
    return url


def sync(path: str | Path, year: int, *, pages: int = 1, limit: int = 5, delay: float = 1.0) -> dict:
    """Resume a bounded archive scan and recheck a bounded number of articles."""
    if pages < 1 or pages > 10 or limit < 1 or limit > 100 or delay < 1:
        raise ValueError("Use 1-10 pages, 1-100 articles and delay >= 1 second")
    db = connect(path)
    db.executescript(
        """CREATE TABLE IF NOT EXISTS stuttgart_archive_cursor (
             year INTEGER PRIMARY KEY, next_url TEXT, pages_scanned INTEGER NOT NULL,
             historical_scan_complete INTEGER NOT NULL, updated REAL NOT NULL);
           CREATE TABLE IF NOT EXISTS stuttgart_review_leads (
             id TEXT PRIMARY KEY, listing_location TEXT NOT NULL,
             scope_hint TEXT NOT NULL, updated REAL NOT NULL);"""
    )
    started = time.time()
    stats = {
        "year": year, "requested_pages": pages, "archive_pages": 0,
        "head_refreshed": False, "discovered": 0, "new": 0,
        "revised": 0, "unchanged": 0, "failed": 0,
        "historical_scan_complete": False, "coverage_complete": False,
    }
    db.execute("INSERT INTO runs(started) VALUES(?)", (started,))
    db.commit()
    try:
        with httpx.Client(
            timeout=25, follow_redirects=False, headers={"User-Agent": USER_AGENT},
        ) as client:
            robots_response = client.get(ORIGIN + "/robots.txt")
            robots_response.raise_for_status()
            if (
                robots_response.status_code != 200
                or str(robots_response.url) != ORIGIN + "/robots.txt"
                or "user-agent:" not in robots_response.text.lower()
            ):
                raise ValueError("Missing or unexpected newsroom robots.txt")
            robots = RobotFileParser()
            robots.parse(robots_response.text.splitlines())
            delay = max(delay, float(robots.crawl_delay(USER_AGENT) or 0))
            last_request = time.monotonic()

            def get(url: str, headers: dict | None = None):
                nonlocal last_request
                for _ in range(4):
                    parsed = urlparse(url)
                    if (
                        parsed.scheme != "https" or parsed.netloc != "www.presseportal.de"
                        or not (PAGE_PATH.fullmatch(parsed.path) or ARTICLE_PATH.fullmatch(parsed.path))
                        or parsed.query or parsed.fragment
                    ):
                        raise ValueError("Unexpected Stuttgart newsroom URL")
                    if not robots.can_fetch(USER_AGENT, url):
                        raise ValueError("robots.txt disallows " + url)
                    time.sleep(max(0, delay - (time.monotonic() - last_request)))
                    last_request = time.monotonic()
                    response = client.get(url, headers=headers)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(str(response.url), response.headers["location"])
                        if urlparse(target).path != parsed.path:
                            raise ValueError("Stuttgart newsroom redirect changed record path")
                        url = target
                        continue
                    if response.status_code != 304:
                        response.raise_for_status()
                    return response
                raise ValueError("Too many Stuttgart newsroom redirects")

            seen_ids = set()

            def scan(url: str):
                page = get(url).text
                rows = listing_rows(page)
                selected = [row for row in rows if row["published"].startswith(str(year))]
                discover(db, selected, time.time())
                for row in selected:
                    hint, location = scope_lead(row["locations"])
                    db.execute(
                        """INSERT INTO stuttgart_review_leads(id,listing_location,scope_hint,updated)
                           VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                           listing_location=excluded.listing_location,
                           scope_hint=excluded.scope_hint,updated=excluded.updated""",
                        (row["id"], location, hint, time.time()),
                    )
                db.commit()
                seen_ids.update(row["id"] for row in selected)
                stats["discovered"] = len(seen_ids)
                stats["archive_pages"] += 1
                return rows, next_url(page)

            state = db.execute(
                """SELECT next_url,pages_scanned,historical_scan_complete
                   FROM stuttgart_archive_cursor WHERE year=?""", (year,),
            ).fetchone()
            cursor = state["next_url"] if state and not state["historical_scan_complete"] else NEWSROOM
            scanned = state["pages_scanned"] if state else 0
            if cursor != NEWSROOM or (state and state["historical_scan_complete"]):
                scan(NEWSROOM)
                stats["head_refreshed"] = True
            url = cursor
            seen_pages = set()
            for _ in range(0 if state and state["historical_scan_complete"] else pages):
                if url in seen_pages:
                    raise ValueError("Stuttgart newsroom pagination loop")
                seen_pages.add(url)
                rows, following = scan(url)
                scanned += 1
                oldest_before_year = rows[-1]["published"][:4] < str(year)
                complete = following is None or oldest_before_year
                db.execute(
                    """INSERT INTO stuttgart_archive_cursor
                       (year,next_url,pages_scanned,historical_scan_complete,updated)
                       VALUES(?,?,?,?,?) ON CONFLICT(year) DO UPDATE SET
                       next_url=excluded.next_url,pages_scanned=excluded.pages_scanned,
                       historical_scan_complete=excluded.historical_scan_complete,
                       updated=excluded.updated""",
                    (year, following, scanned, int(complete), time.time()),
                )
                db.commit()
                if complete:
                    break
                url = following
            state = db.execute(
                "SELECT pages_scanned,historical_scan_complete FROM stuttgart_archive_cursor WHERE year=?",
                (year,),
            ).fetchone()
            stats["cursor_pages_scanned"] = state["pages_scanned"]
            stats["historical_scan_complete"] = bool(state["historical_scan_complete"])
            # An ongoing year's new issues and post-hoc corrections still need revisits.
            stats["coverage_complete"] = bool(state["historical_scan_complete"] and year < datetime.now(UTC).year)
            pending = db.execute(
                """SELECT * FROM reports WHERE published>=? AND published<? AND retry_after<=?
                   AND (body IS NULL OR checked IS NULL OR checked<? OR published>=?)
                   ORDER BY body IS NOT NULL, COALESCE(checked,0), published DESC LIMIT ?""",
                (
                    f"{year}-01-01", f"{year + 1}-01-01", started,
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
                    response = get(row["url"], headers)
                    if response.status_code == 304:
                        if row["body"] is None:
                            raise ValueError("304 without cached Stuttgart article")
                        db.execute(
                            "UPDATE reports SET checked=?,error=NULL,failures=0,retry_after=0 WHERE id=?",
                            (time.time(), row["id"]),
                        )
                        db.commit()
                        result = "unchanged"
                    else:
                        result = accept(db, row["id"], article_body(response.text), response.headers, time.time())
                    stats[result] += 1
                except (httpx.HTTPError, ValueError) as exc:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    fail(
                        db, row["id"], f"{type(exc).__name__}: {exc}", time.time(),
                        status,
                    )
                    stats["failed"] += 1
                    stats["stopped_on_source_error"] = {
                        "source_id": row["id"], "http_status": status,
                        "error_type": type(exc).__name__,
                    }
                    break
            bounds = (f"{year}-01-01", f"{year + 1}-01-01")
            stats["stored"] = db.execute(
                "SELECT count(*) FROM reports WHERE published>=? AND published<? AND body IS NOT NULL", bounds,
            ).fetchone()[0]
            stats["pending"] = db.execute(
                "SELECT count(*) FROM reports WHERE published>=? AND published<? AND body IS NULL", bounds,
            ).fetchone()[0]
            stats["errors"] = db.execute(
                "SELECT count(*) FROM reports WHERE published>=? AND published<? AND error IS NOT NULL", bounds,
            ).fetchone()[0]
    except Exception as exc:
        stats["fatal_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        db.execute("UPDATE runs SET finished=?,summary=? WHERE started=?", (time.time(), json.dumps(stats), started))
        db.commit()
        db.close()
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".runtime/cities/stuttgart/police.sqlite")
    parser.add_argument("--year", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    if args.year < 2015 or args.year > datetime.now(UTC).year:
        parser.error("Use an available year")
    if args.pages < 1 or args.pages > 10 or args.limit < 1 or args.limit > 100 or args.delay < 1:
        parser.error("Use 1-10 pages, 1-100 articles and delay >= 1 second")
    import fcntl

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = sync(args.db, args.year, pages=args.pages, limit=args.limit, delay=args.delay)
    print(json.dumps(result, indent=2))
    if result["failed"] or result["pending"] or result["errors"] or not result["coverage_complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
