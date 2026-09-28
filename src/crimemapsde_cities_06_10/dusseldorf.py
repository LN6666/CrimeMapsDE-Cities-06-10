"""Checkpoint the Düsseldorf police newsroom without assuming newsroom == city.

Polizei Düsseldorf also publishes Autobahnpolizei and neighbouring-city reports.
Every fetched article starts outside the city-map input until a source-hash-bound
city-scope decision confirms that its incident scene is within Düsseldorf.
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

from .storage import accept, connect, discover, fail

ORIGIN = "https://www.presseportal.de"
NEWSROOM = ORIGIN + "/blaulicht/nr/13248"
ARTICLE_PATH = re.compile(r"/blaulicht/pm/13248/(\d+)$")
NEXT_PAGE = re.compile(r'<link\s+rel="next"\s+href="(/blaulicht/nr/13248/\d+)"')
USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Dusseldorf police newsroom index)"


class NewsroomParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.articles_seen = 0
        self.current = None
        self.field = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "article" and "news" in classes:
            self.articles_seen += 1
            ident = attrs.get("data-label", "")
            if ident.isdigit():
                self.current = {"id": ident, "url": "", "title": "", "published": "", "district": ""}
        if self.current is None:
            return
        if tag == "div" and "date" in classes:
            self.field = "published"
        elif tag == "h3" and "news-headline-clamp" in classes:
            self.field = "title"
        elif tag == "a" and self.field == "title":
            url = urljoin(ORIGIN, attrs.get("href", ""))
            parsed = urlparse(url)
            match = ARTICLE_PATH.fullmatch(parsed.path)
            if (parsed.scheme == "https" and parsed.netloc == "www.presseportal.de"
                    and match and match[1] == self.current["id"]
                    and not parsed.query and not parsed.fragment):
                self.current["url"] = url

    def handle_data(self, data):
        if self.current is not None and self.field:
            self.current[self.field] += data

    def handle_endtag(self, tag):
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
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_story = False
        self.after_heading = False
        self.in_paragraph = False
        self.in_customer = False
        self.stopped = False
        self.customer = ""
        self.parts = []
        self.current = []

    def handle_starttag(self, tag, attrs):
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
        elif tag == "p":
            self.in_customer = False
            if self.in_paragraph:
                paragraph = " ".join(" ".join(self.current).split())
                if paragraph and not paragraph.startswith("Schneller informiert:"):
                    self.parts.append(paragraph)
                self.in_paragraph = False
        elif tag == "article":
            self.in_story = False


def listing_rows(page):
    parser = NewsroomParser()
    parser.feed(page)
    if parser.articles_seen != len(parser.rows) or not parser.rows:
        raise ValueError("Düsseldorf newsroom list contains unparsed articles")
    if len({row["id"] for row in parser.rows}) != len(parser.rows):
        raise ValueError("Düsseldorf newsroom list contains duplicate article IDs")
    return parser.rows


def article_body(page):
    parser = ArticleParser()
    parser.feed(page)
    body = " ".join(parser.parts)
    if parser.customer.strip() != "Polizei Düsseldorf" or len(body) < 30:
        raise ValueError("Düsseldorf article parser or publisher check failed")
    return body


def ensure_scope_table(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS city_scope_decisions (
         id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, verdict TEXT NOT NULL,
         evidence TEXT NOT NULL, reviewed REAL NOT NULL,
         FOREIGN KEY(id) REFERENCES reports(id))"""
    )
    db.commit()


def record_city_scope(db, ident, verdict, evidence, now):
    """Record a checked incident-city decision, tied to the current source body.

    Evidence must quote the fetched body, not just its title or publisher city.
    A publisher dateline alone is not incident-location evidence.
    Mixed-city reports remain uncertain until each incident has its own treatment.
    """
    if verdict not in {"in_city", "out_of_city", "uncertain"}:
        raise ValueError("Invalid city-scope verdict")
    evidence = " ".join(evidence.split())
    if len(evidence) < 20:
        raise ValueError("City-scope decision requires specific source evidence")
    row = db.execute("SELECT sha256,body FROM reports WHERE id=?", (ident,)).fetchone()
    if row is None or row["sha256"] is None or row["body"] is None:
        raise ValueError("City-scope decision requires a fetched article")
    if evidence.casefold() not in " ".join(row["body"].split()).casefold():
        raise ValueError("City-scope evidence must quote the fetched body")
    if verdict == "in_city" and re.fullmatch(
        r"Düsseldorf\s*\(ots\).*", evidence, re.IGNORECASE
    ):
        raise ValueError("Publisher dateline is not incident-scene evidence")
    ensure_scope_table(db)
    db.execute(
        """INSERT INTO city_scope_decisions(id,sha256,verdict,evidence,reviewed)
         VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sha256=excluded.sha256,
         verdict=excluded.verdict,evidence=excluded.evidence,reviewed=excluded.reviewed""",
        (ident, row["sha256"], verdict, evidence, now),
    )
    db.commit()


def city_only_reports(db):
    """Only current-body, explicitly reviewed Düsseldorf-city announcements."""
    ensure_scope_table(db)
    return db.execute(
        """SELECT r.* FROM reports AS r JOIN city_scope_decisions AS s ON s.id=r.id
         WHERE r.body IS NOT NULL AND s.sha256=r.sha256 AND s.verdict='in_city'
         ORDER BY r.published, r.id"""
    ).fetchall()


def _source_robots(page):
    if not re.search(r"(?im)^\s*user-agent\s*:", page):
        raise ValueError("Missing or invalid Presseportal robots.txt")
    robots = RobotFileParser()
    robots.parse(page.splitlines())
    if not robots.can_fetch(USER_AGENT, NEWSROOM):
        raise ValueError("robots.txt disallows Düsseldorf newsroom")
    return robots


def _source_get(client, robots, url, delay, last_request, headers=None):
    for _ in range(4):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.presseportal.de":
            raise ValueError("Unexpected Düsseldorf newsroom origin")
        if parsed.path != "/blaulicht/nr/13248" and not re.fullmatch(
            r"/blaulicht/nr/13248/\d+", parsed.path
        ) and not ARTICLE_PATH.fullmatch(parsed.path):
            raise ValueError("Unexpected Düsseldorf newsroom path")
        if not robots.can_fetch(USER_AGENT, url):
            raise ValueError("robots.txt disallows " + url)
        time.sleep(max(0, delay - (time.monotonic() - last_request[0])))
        last_request[0] = time.monotonic()
        response = client.get(url, headers=headers)
        if response.status_code in (301, 302, 303, 307, 308):
            url = urljoin(str(response.url), response.headers["location"])
            continue
        if response.status_code != 304:
            response.raise_for_status()
        return response
    raise ValueError("Too many Düsseldorf newsroom redirects")


def sync(path, year, *, full=False, limit=30, delay=1.0, max_pages=None):
    if max_pages is not None and not 1 <= max_pages <= 100:
        raise ValueError("Archive page cap must be from 1 to 100")
    db = connect(path)
    ensure_scope_table(db)
    db.execute(
        """CREATE TABLE IF NOT EXISTS dusseldorf_archive_cursor (
         year INTEGER PRIMARY KEY, next_url TEXT, pages_scanned INTEGER NOT NULL,
         complete INTEGER NOT NULL, updated REAL NOT NULL)"""
    )
    db.commit()
    started = time.time()
    stats = {"year": year, "full_archive_scan": full, "archive_complete": False,
             "archive_pages": 0, "discovered": 0, "new": 0, "revised": 0,
             "unchanged": 0, "failed": 0}
    db.execute("INSERT INTO runs(started) VALUES(?)", (started,))
    db.commit()
    with httpx.Client(timeout=25, follow_redirects=False,
                      headers={"User-Agent": USER_AGENT}) as client:
        robots_response = client.get(ORIGIN + "/robots.txt")
        robots_response.raise_for_status()
        robots = _source_robots(robots_response.text)
        delay = max(delay, float(robots.crawl_delay(USER_AGENT) or 0))
        last_request = [time.monotonic()]
        state = db.execute(
            "SELECT next_url,pages_scanned,complete FROM dusseldorf_archive_cursor WHERE year=?",
            (year,),
        ).fetchone()
        resume = full and state and not state["complete"] and state["next_url"]
        url = state["next_url"] if resume else NEWSROOM
        scanned = state["pages_scanned"] if state else 0
        seen_urls = set()
        reached_target_year = bool(state and state["pages_scanned"])
        if resume:
            # Refresh the newest announcements while continuing the older archive.
            head = _source_get(client, robots, NEWSROOM, delay, last_request).text
            current = [r for r in listing_rows(head) if r["published"].startswith(str(year))]
            discover(db, current, time.time())
            stats["discovered"] += len(current)
            stats["archive_pages"] += 1
            stats["head_refreshed"] = True
        page_limit = 2 if state and state["complete"] else (100 if full else 2)
        if max_pages is not None:
            page_limit = min(page_limit, max_pages)
        for _ in range(page_limit):
            if url in seen_urls:
                raise ValueError("Düsseldorf newsroom pagination loop")
            seen_urls.add(url)
            page = _source_get(client, robots, url, delay, last_request).text
            rows = listing_rows(page)
            years = [int(row["published"][:4]) for row in rows]
            matches = [row for row in rows if row["published"].startswith(str(year))]
            if matches:
                reached_target_year = True
                discover(db, matches, time.time())
                stats["discovered"] += len(matches)
            stats["archive_pages"] += 1
            older_than_target = max(years) < year
            next_page = NEXT_PAGE.search(page)
            complete = older_than_target or not next_page
            next_url = urljoin(ORIGIN, next_page[1]) if next_page else None
            if full and not (state and state["complete"]):
                scanned += 1
                db.execute(
                    """INSERT INTO dusseldorf_archive_cursor(year,next_url,pages_scanned,complete,updated)
                       VALUES(?,?,?,?,?) ON CONFLICT(year) DO UPDATE SET
                       next_url=excluded.next_url,pages_scanned=excluded.pages_scanned,
                       complete=excluded.complete,updated=excluded.updated""",
                    (year, next_url, scanned, int(complete), time.time()),
                )
                db.commit()
            if complete:
                stats["archive_complete"] = reached_target_year
                break
            url = next_url
        if state and state["complete"]:
            stats["archive_complete"] = True
        if full:
            stats["cursor_pages_scanned"] = scanned
        if not reached_target_year:
            raise ValueError("Requested year not found in bounded Düsseldorf archive scan")
        pending = db.execute(
            """SELECT * FROM reports WHERE retry_after<=? AND
               (body IS NULL OR checked IS NULL OR checked<? OR published>=?)
               ORDER BY body IS NOT NULL, COALESCE(checked,0), published DESC LIMIT ?""",
            (started, started - 7 * 86400,
             datetime.fromtimestamp(started - 2 * 86400, UTC).isoformat()[:19], limit),
        ).fetchall()
        for row in pending:
            headers = {}
            if row["etag"]:
                headers["If-None-Match"] = row["etag"]
            if row["modified"]:
                headers["If-Modified-Since"] = row["modified"]
            try:
                response = _source_get(client, robots, row["url"], delay, last_request, headers)
                if response.status_code == 304:
                    if row["body"] is None:
                        raise ValueError("304 without cached Düsseldorf body")
                    db.execute("UPDATE reports SET checked=?,error=NULL,failures=0,retry_after=0 WHERE id=?",
                               (time.time(), row["id"]))
                    db.commit()
                    result = "unchanged"
                else:
                    result = accept(db, row["id"], article_body(response.text),
                                    response.headers, time.time())
                stats[result] += 1
            except (httpx.HTTPError, ValueError) as exc:
                fail(db, row["id"], f"{type(exc).__name__}: {exc}", time.time(),
                     exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
                stats["failed"] += 1
        stats["stored"] = db.execute("SELECT count(*) FROM reports WHERE body IS NOT NULL").fetchone()[0]
        stats["pending"] = db.execute("SELECT count(*) FROM reports WHERE body IS NULL").fetchone()[0]
        stats["errors"] = db.execute("SELECT count(*) FROM reports WHERE error IS NOT NULL").fetchone()[0]
        stats["city_scope_approved"] = len(city_only_reports(db))
        counts = {row["verdict"]: row["n"] for row in db.execute(
            """SELECT s.verdict,count(*) AS n FROM city_scope_decisions AS s
               JOIN reports AS r ON r.id=s.id AND r.sha256=s.sha256
               WHERE r.body IS NOT NULL GROUP BY s.verdict"""
        )}
        stats["city_scope_out_of_city"] = counts.get("out_of_city", 0)
        stats["city_scope_uncertain"] = counts.get("uncertain", 0)
        stats["city_scope_pending"] = stats["stored"] - sum(counts.values())
    db.execute("UPDATE runs SET finished=?,summary=? WHERE started=?",
               (time.time(), json.dumps(stats), started))
    db.commit()
    db.close()
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".runtime/cities/dusseldorf/police.sqlite")
    parser.add_argument("--year", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()
    if (args.year < 2015 or args.year > datetime.now(UTC).year or args.limit < 1
            or args.delay < 1 or (args.max_pages is not None and not 1 <= args.max_pages <= 100)):
        parser.error("Use available year, positive limit and delay >= 1 second")
    import fcntl

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = sync(args.db, args.year, full=args.full, limit=args.limit,
                      delay=args.delay, max_pages=args.max_pages)
    print(json.dumps(result, indent=2))
    if result["failed"] or result["pending"] or result["errors"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
