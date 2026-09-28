"""Bounded intake from the native Polizei Dortmund press-release archive.

The police directorate publishes cases from Dortmund, Lünen, highways, and
sometimes investigations elsewhere. All city membership stays pending review.
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

from .city_scope import scope_counts
from .storage import accept, connect, discover, fail

ORIGIN = "https://dortmund.polizei.nrw"
ARCHIVE = ORIGIN + "/presse/pressemitteilungen"
USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Dortmund police archive)"
ARTICLE_PATH = re.compile(r"/presse/([a-z0-9][a-z0-9-]+)$")
PAGE_QUERY = re.compile(r"page=(\d+)$")
NEXT_PAGE = re.compile(r'<a\s+href="\?page=(\d+)"\s+title="Zur nächsten Seite"')
REPORT_NUMBER = re.compile(r"\bLfd\.\s*Nr\.\s*:\s*(\d+)", re.IGNORECASE)


class ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.archive_depth = None
        self.view_depth = None
        self.row_depth = None
        self.field = None
        self.field_depth = None
        self.current = None
        self.seen = 0
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div":
            self.depth += 1
            if "view-id-list_view_press_releases_solr" in classes:
                self.archive_depth = self.depth
            if ("view-content" in classes and self.view_depth is None
                    and self.archive_depth is not None):
                self.view_depth = self.depth
            elif self.view_depth is not None and "views-row" in classes and self.row_depth is None:
                self.row_depth = self.depth
                self.current = {"id": "", "url": "", "title": "", "published": "",
                                "district": "", "teaser": "", "listing_location": ""}
                self.seen += 1
            if self.current is not None and "field-teaser" in classes:
                self.field, self.field_depth = "teaser", self.depth
            elif self.current is not None and "combined-location" in classes:
                self.field, self.field_depth = "listing_location", self.depth
        elif self.current is not None and tag == "h2" and "field-title" in classes:
            self.field, self.field_depth = "title", None
        elif self.current is not None and tag == "a" and self.field == "title":
            url = urljoin(ORIGIN, attrs.get("href", ""))
            parsed = urlparse(url)
            match = ARTICLE_PATH.fullmatch(parsed.path)
            if (parsed.scheme == "https" and parsed.netloc == "dortmund.polizei.nrw"
                    and match and not parsed.query and not parsed.fragment):
                self.current["url"] = url
                self.current["id"] = match[1]
        elif self.current is not None and tag == "time":
            self.current["published"] = attrs.get("datetime", "")

    def handle_data(self, data):
        if self.current is not None and self.field:
            self.current[self.field] += data

    def handle_endtag(self, tag):
        if tag == "h2" and self.field == "title":
            self.field = None
        elif tag == "div":
            if self.field_depth == self.depth:
                self.field = self.field_depth = None
            if self.row_depth == self.depth:
                row = self.current
                if row and all(row[key] for key in ("id", "url", "title", "published")):
                    row["title"] = " ".join(row["title"].split())
                    row["teaser"] = " ".join(row["teaser"].split())
                    row["listing_location"] = " ".join(row["listing_location"].split())
                    datetime.fromisoformat(row["published"])
                    self.rows.append(row)
                self.current = None
                self.row_depth = None
            if self.view_depth == self.depth:
                self.view_depth = None
            if self.archive_depth == self.depth:
                self.archive_depth = None
            self.depth -= 1


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.article_depth = None
        self.author_depth = None
        self.body_depth = None
        self.author = ""
        self.body = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "").split()
        if tag == "div":
            self.depth += 1
            if self.article_depth is not None and "field--name-field-press-release-author" in classes:
                self.author_depth = self.depth
            elif self.article_depth is not None and "field--name-body" in classes:
                self.body_depth = self.depth
        elif tag == "article" and "node--type--press-release" in classes:
            self.article_depth = self.depth
        elif tag in {"p", "br", "li"} and self.body_depth is not None:
            self.body.append(" ")

    def handle_data(self, data):
        if self.author_depth is not None:
            self.author += data
        if self.body_depth is not None:
            self.body.append(data)

    def handle_endtag(self, tag):
        if tag == "div":
            if self.author_depth == self.depth:
                self.author_depth = None
            if self.body_depth == self.depth:
                self.body_depth = None
            self.depth -= 1
        elif tag == "article" and self.article_depth == self.depth:
            self.article_depth = None


def listing_rows(page: str) -> list[dict]:
    parser = ListingParser()
    parser.feed(page)
    if not parser.seen or parser.seen != len(parser.rows):
        raise ValueError("Dortmund native archive contains no or unparsed rows")
    if len({row["id"] for row in parser.rows}) != len(parser.rows):
        raise ValueError("Dortmund native archive contains duplicate article slugs")
    return parser.rows


def article_body(page: str) -> tuple[str, str]:
    parser = ArticleParser()
    parser.feed(page)
    body = " ".join(" ".join(parser.body).split())
    node = re.search(r'"currentPath":"node/(\d+)"', page.replace("\\/", "/"))
    if parser.author.strip() != "Polizei Dortmund" or len(body) < 30 or not node:
        raise ValueError("Dortmund article publisher, body, or node ID check failed")
    return body, node[1]


def next_url(page: str) -> str | None:
    match = NEXT_PAGE.search(page)
    if not match:
        if "pager-show-more" in page:
            raise ValueError("Dortmund native pagination changed")
        return None
    number = int(match[1])
    if number < 1 or number > 10000:
        raise ValueError("Dortmund archive page outside bounded range")
    return f"{ARCHIVE}?page={number}"


def source_robots(page: str) -> RobotFileParser:
    if not re.search(r"(?im)^\s*user-agent\s*:", page):
        raise ValueError("Missing or invalid Dortmund robots.txt")
    robots = RobotFileParser()
    robots.parse(page.splitlines())
    if not robots.can_fetch(USER_AGENT, ARCHIVE):
        raise ValueError("robots.txt disallows Dortmund archive")
    return robots


def _get(client, robots, url: str, last_request: list[float], delay: float, headers=None):
    for _ in range(4):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "dortmund.polizei.nrw" or parsed.fragment:
            raise ValueError("Unexpected Dortmund source origin")
        listing = parsed.path == "/presse/pressemitteilungen" and (
            not parsed.query or PAGE_QUERY.fullmatch(parsed.query)
        )
        article = ARTICLE_PATH.fullmatch(parsed.path) and not parsed.query
        if not (listing or article):
            raise ValueError("Unexpected Dortmund source path")
        if not robots.can_fetch(USER_AGENT, url):
            raise ValueError("robots.txt disallows " + url)
        for attempt in range(3):
            time.sleep(max(0, delay - (time.monotonic() - last_request[0])))
            last_request[0] = time.monotonic()
            try:
                response = client.get(url, headers=headers)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt == 2:
                    response.raise_for_status()
                retry_after = response.headers.get("retry-after", "")
                wait = int(retry_after) if retry_after.isdigit() else 2**attempt
                time.sleep(min(30, max(delay, wait)))
                continue
            break
        if response.status_code in {301, 302, 303, 307, 308}:
            target = urljoin(str(response.url), response.headers["location"])
            if urlparse(target).path != parsed.path:
                raise ValueError("Dortmund source redirect changed record path")
            url = target
            continue
        if response.status_code != 304:
            response.raise_for_status()
        return response
    raise ValueError("Too many Dortmund source redirects")


def sync(path: str | Path, year: int, *, pages: int = 1, limit: int = 5, delay: float = 1.0) -> dict:
    if pages < 1 or pages > 10 or limit < 1 or limit > 100 or delay < 1:
        raise ValueError("Use 1-10 pages, 1-100 articles and delay >= 1 second")
    db = connect(path)
    db.executescript(
        """CREATE TABLE IF NOT EXISTS dortmund_archive_cursor (
             year INTEGER PRIMARY KEY, next_url TEXT, pages_scanned INTEGER NOT NULL,
             complete INTEGER NOT NULL, updated REAL NOT NULL);
           CREATE TABLE IF NOT EXISTS dortmund_source_ids (
             id TEXT PRIMARY KEY, police_number TEXT, native_node_id TEXT,
             listing_location TEXT NOT NULL, updated REAL NOT NULL);"""
    )
    started = time.time()
    stats = {"year": year, "requested_pages": pages, "archive_pages": 0,
             "head_refreshed": False, "discovered": 0, "new": 0,
             "revised": 0, "unchanged": 0, "failed": 0,
             "historical_scan_complete": False, "coverage_complete": False}
    db.execute("INSERT INTO runs(started) VALUES(?)", (started,))
    db.commit()
    try:
        with httpx.Client(timeout=25, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
            for attempt in range(3):
                try:
                    response = client.get(ORIGIN + "/robots.txt")
                    response.raise_for_status()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == 2:
                        raise
                    time.sleep(2**attempt)
            if response.status_code != 200 or str(response.url) != ORIGIN + "/robots.txt":
                raise ValueError("Unexpected Dortmund robots.txt response")
            robots = source_robots(response.text)
            delay = max(delay, float(robots.crawl_delay(USER_AGENT) or 0))
            last_request = [time.monotonic()]
            seen_ids = set()

            def scan(url: str) -> tuple[list[dict], str | None]:
                page = _get(client, robots, url, last_request, delay).text
                rows = listing_rows(page)
                selected = [row for row in rows if row["published"].startswith(str(year))]
                discover(db, selected, time.time())
                for row in selected:
                    number = REPORT_NUMBER.search(row["teaser"])
                    db.execute(
                        """INSERT INTO dortmund_source_ids
                           (id,police_number,native_node_id,listing_location,updated)
                           VALUES(?,?,NULL,?,?) ON CONFLICT(id) DO UPDATE SET
                           police_number=excluded.police_number,
                           listing_location=excluded.listing_location,updated=excluded.updated""",
                        (row["id"], number[1] if number else None,
                         row["listing_location"], time.time()),
                    )
                db.commit()
                seen_ids.update(row["id"] for row in selected)
                stats["discovered"] = len(seen_ids)
                stats["archive_pages"] += 1
                return rows, next_url(page)

            state = db.execute(
                "SELECT next_url,pages_scanned,complete FROM dortmund_archive_cursor WHERE year=?",
                (year,),
            ).fetchone()
            cursor = state["next_url"] if state and not state["complete"] else ARCHIVE
            scanned = state["pages_scanned"] if state else 0
            if cursor != ARCHIVE or (state and state["complete"]):
                scan(ARCHIVE)
                stats["head_refreshed"] = True
            url = cursor
            seen_pages = set()
            for _ in range(0 if state and state["complete"] else pages):
                if url in seen_pages:
                    raise ValueError("Dortmund native pagination loop")
                seen_pages.add(url)
                rows, following = scan(url)
                scanned += 1
                oldest_before_year = min(row["published"][:4] for row in rows) < str(year)
                complete = following is None or oldest_before_year
                db.execute(
                    """INSERT INTO dortmund_archive_cursor
                       (year,next_url,pages_scanned,complete,updated) VALUES(?,?,?,?,?)
                       ON CONFLICT(year) DO UPDATE SET next_url=excluded.next_url,
                       pages_scanned=excluded.pages_scanned,complete=excluded.complete,
                       updated=excluded.updated""",
                    (year, following, scanned, int(complete), time.time()),
                )
                db.commit()
                if complete:
                    break
                url = following
            state = db.execute(
                "SELECT pages_scanned,complete FROM dortmund_archive_cursor WHERE year=?", (year,)
            ).fetchone()
            stats["cursor_pages_scanned"] = state["pages_scanned"]
            stats["historical_scan_complete"] = bool(state["complete"])
            stats["coverage_complete"] = bool(state["complete"] and year < datetime.now(UTC).year)
            pending = db.execute(
                """SELECT * FROM reports WHERE published>=? AND published<? AND retry_after<=?
                   AND (body IS NULL OR checked IS NULL OR checked<? OR published>=?)
                   ORDER BY body IS NOT NULL, COALESCE(checked,0), published DESC LIMIT ?""",
                (f"{year}-01-01", f"{year + 1}-01-01", started,
                 started - 7 * 86400,
                 datetime.fromtimestamp(started - 2 * 86400, UTC).isoformat()[:19], limit),
            ).fetchall()
            for row in pending:
                headers = {}
                if row["etag"]:
                    headers["If-None-Match"] = row["etag"]
                if row["modified"]:
                    headers["If-Modified-Since"] = row["modified"]
                try:
                    response = _get(client, robots, row["url"], last_request, delay, headers)
                    if response.status_code == 304:
                        if row["body"] is None:
                            raise ValueError("304 without cached Dortmund body")
                        db.execute(
                            "UPDATE reports SET checked=?,error=NULL,failures=0,retry_after=0 WHERE id=?",
                            (time.time(), row["id"]),
                        )
                        db.commit()
                        result = "unchanged"
                    else:
                        body, native_id = article_body(response.text)
                        result = accept(db, row["id"], body, response.headers, time.time())
                        db.execute(
                            "UPDATE dortmund_source_ids SET native_node_id=? WHERE id=?",
                            (native_id, row["id"]),
                        )
                        db.commit()
                    stats[result] += 1
                except (httpx.HTTPError, ValueError) as exc:
                    fail(db, row["id"], f"{type(exc).__name__}: {exc}", time.time(),
                         exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
                    stats["failed"] += 1
            bounds = (f"{year}-01-01", f"{year + 1}-01-01")
            stats["stored"] = db.execute(
                "SELECT count(*) FROM reports WHERE published>=? AND published<? AND body IS NOT NULL", bounds
            ).fetchone()[0]
            stats["pending"] = db.execute(
                "SELECT count(*) FROM reports WHERE published>=? AND published<? AND body IS NULL", bounds
            ).fetchone()[0]
            stats["errors"] = db.execute(
                "SELECT count(*) FROM reports WHERE published>=? AND published<? AND error IS NOT NULL", bounds
            ).fetchone()[0]
            stats["city_scope"] = scope_counts(db)
    except Exception as exc:
        stats["fatal_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        db.execute("UPDATE runs SET finished=?,summary=? WHERE started=?",
                   (time.time(), json.dumps(stats), started))
        db.commit()
        db.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".runtime/cities/dortmund/police.sqlite")
    parser.add_argument("--year", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    if args.year < 2015 or args.year > datetime.now(UTC).year:
        parser.error("Use an available year")
    import fcntl

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = sync(args.db, args.year, pages=args.pages, limit=args.limit, delay=args.delay)
    print(json.dumps(result, indent=2))
    if (result["failed"] or result["pending"] or result["errors"]
            or not result["coverage_complete"] or result["city_scope"]["pending"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
