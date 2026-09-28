"""Strict helpers for bounded Medienservice Sachsen source acquisition.

RFC 9309 treats an unavailable robots file differently from an unreachable
service.  This module permits live access only when ``robots.txt`` is an
explicit 404/410, or when a valid 200 response allows the requested paths.
Every other status and every malformed or disallowing policy fails closed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import httpx

ORIGIN = "https://www.medienservice.sachsen.de"
ARTICLE_PATH = re.compile(r"/medien/news/(\d+)$")
SEARCH_PATH = "/medien/news/search.json"
ROBOTS_URL = ORIGIN + "/robots.txt"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCK_TAGS = frozenset({"address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
                        "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
                        "main", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"})
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                       "param", "source", "track", "wbr"})


@dataclass(frozen=True)
class SourceSpec:
    institution_id: str
    publisher: str
    archive_url: str
    user_agent: str


@dataclass(frozen=True)
class RobotsPolicy:
    parser: RobotFileParser | None
    delay: float
    status: int

    def allows(self, user_agent: str, url: str) -> bool:
        return self.parser is None or self.parser.can_fetch(user_agent, url)


@dataclass(frozen=True)
class Article:
    source_id: str
    source_url: str
    title: str
    published: str
    body: str


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _body_text(value: object) -> str:
    lines = [" ".join(part.split()) for part in str(value or "").splitlines() if part.strip()]
    return "\n".join(lines)


def canonical_article_url(value: str) -> tuple[str, str]:
    parsed = urlparse(urljoin(ORIGIN, value))
    match = ARTICLE_PATH.fullmatch(parsed.path)
    if ((parsed.scheme, parsed.netloc) != ("https", "www.medienservice.sachsen.de")
            or match is None or parsed.query or parsed.fragment):
        raise ValueError("Unexpected Medienservice article URL")
    return match[1], f"{ORIGIN}{parsed.path}"


def archive_url_allowed(value: str, institution_id: str) -> bool:
    parsed = urlparse(value)
    if ((parsed.scheme, parsed.netloc) != ("https", "www.medienservice.sachsen.de")
            or parsed.path.rstrip("/") != "/medien" or parsed.fragment):
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    institution_values = []
    for key, values in query.items():
        if "institution" in key.casefold():
            institution_values.extend(values)
        if len(key) > 100 or any(len(item) > 500 for item in values):
            return False
    return institution_values == [institution_id]


def search_url(
    spec: SourceSpec, *, first_searched: str, year: int, page: int, through: date | None = None
) -> str:
    if not first_searched or len(first_searched) > 200 or not 2000 <= year <= datetime.now(UTC).year:
        raise ValueError("Invalid Medienservice search snapshot or year")
    if page < 1 or page > 10000:
        raise ValueError("Medienservice search page is outside the bounded range")
    end = min(date(year, 12, 31), through or datetime.now(UTC).date())
    if end.year != year:
        end = date(year, 12, 31)
    query = [
        ("search[first_searched]", first_searched),
        ("search[query]", ""),
        ("search[from]", f"01.01.{year}"),
        ("search[to]", end.strftime("%d.%m.%Y")),
        ("search[institution_ids][]", spec.institution_id),
        ("search[filter][]", "press_releases"),
        ("page", str(page)),
    ]
    return f"{ORIGIN}{SEARCH_PATH}?{urlencode(query)}"


def search_url_allowed(value: str, institution_id: str) -> bool:
    parsed = urlparse(value)
    if ((parsed.scheme, parsed.netloc, parsed.path) != ("https", "www.medienservice.sachsen.de", SEARCH_PATH)
            or parsed.fragment):
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    required = {
        "search[first_searched]", "search[query]", "search[from]", "search[to]",
        "search[institution_ids][]", "search[filter][]", "page",
    }
    if set(query) != required or query["search[institution_ids][]"] != [institution_id]:
        return False
    if query["search[filter][]"] != ["press_releases"] or query["search[query]"] != [""]:
        return False
    if len(query["search[first_searched]"]) != 1 or not query["search[first_searched]"][0]:
        return False
    if not all(len(values) == 1 for values in query.values()):
        return False
    if not query["page"][0].isdecimal() or not 1 <= int(query["page"][0]) <= 10000:
        return False
    return bool(re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", query["search[from]"][0])
                and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", query["search[to]"][0]))


def robots_policy(response: httpx.Response, spec: SourceSpec, *, minimum_delay: float = 4.0) -> RobotsPolicy:
    """Validate a robots response using the bounded RFC 9309 project policy."""
    if minimum_delay < 4:
        raise ValueError("Medienservice delay must be at least four seconds")
    if str(response.url) != ROBOTS_URL:
        raise ValueError("Medienservice robots response changed URL")
    if response.status_code in {404, 410}:
        return RobotsPolicy(None, minimum_delay, response.status_code)
    if response.status_code != 200:
        raise ValueError(f"Medienservice robots unavailable with HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").casefold()
    text = response.text
    if "text/html" in content_type or text.lstrip().startswith("<"):
        raise ValueError("Medienservice robots response is HTML, not robots rules")
    meaningful = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError("Malformed Medienservice robots rule")
        meaningful.append(line)
    if not meaningful or not any(line.partition(":")[0].strip().casefold() == "user-agent"
                                 for line in meaningful):
        raise ValueError("Medienservice robots rules have no user-agent group")
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(text.splitlines())
    article_probe = ORIGIN + "/medien/news/1"
    if not parser.can_fetch(spec.user_agent, spec.archive_url) or not parser.can_fetch(
        spec.user_agent, article_probe
    ):
        raise ValueError("Medienservice robots.txt disallows required source paths")
    delay = max(minimum_delay, float(parser.crawl_delay(spec.user_agent) or 0))
    rate = parser.request_rate(spec.user_agent)
    if rate is not None:
        if rate.requests <= 0 or rate.seconds <= 0:
            raise ValueError("Invalid Medienservice robots request-rate")
        delay = max(delay, rate.seconds / rate.requests)
    return RobotsPolicy(parser, delay, 200)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, frozenset[str]]] = []
        self.links: list[dict] = []
        self._active_link: dict | None = None
        self.canonical: list[str] = []
        self.meta: dict[str, list[str]] = {}
        self.meta_sequence: list[tuple[str, str]] = []
        self.json_ld: list[str] = []
        self._script: list[str] | None = None
        self.h1: list[str] = []
        self._h1_depth: int | None = None
        self.article_candidates: list[list[str]] = []
        self._article_candidate: list[str] | None = None
        self.main: list[str] = []
        self.all_text: list[str] = []
        self._article_depth: int | None = None
        self._main_depth: int | None = None
        self.times: list[str] = []
        self.inputs: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        classes = frozenset(attrs.get("class", "").casefold().split())
        inside_content_row = any("content-row" in ancestor_classes
                                 for _, ancestor_classes in self.stack)
        if tag not in VOID_TAGS:
            self.stack.append((tag, classes))
        depth = len(self.stack)
        if tag == "main" and self._main_depth is None:
            self._main_depth = depth
        if (self._article_depth is None and tag == "div" and "content-col-wide" in classes
                and inside_content_row):
            self._article_depth = depth
            self._article_candidate = []
        if tag == "h1" and self._h1_depth is None:
            self._h1_depth = depth
        if tag == "a":
            self._active_link = {"href": attrs.get("href", ""), "rel": attrs.get("rel", ""),
                                 "title": attrs.get("title", ""), "aria": attrs.get("aria-label", ""),
                                 "text": []}
        if tag == "link" and "canonical" in attrs.get("rel", "").casefold().split():
            self.canonical.append(attrs.get("href", ""))
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").casefold()
            if key and attrs.get("content"):
                self.meta.setdefault(key, []).append(attrs["content"])
                self.meta_sequence.append((key, attrs["content"]))
        if tag == "input":
            key = attrs.get("id") or attrs.get("name")
            if key:
                self.inputs.setdefault(key, []).append(attrs.get("value", ""))
        if tag == "time" and attrs.get("datetime"):
            self.times.append(attrs["datetime"])
        if tag == "script" and "ld+json" in attrs.get("type", "").casefold():
            self._script = []
        if tag in BLOCK_TAGS:
            if self._article_candidate is not None:
                self._article_candidate.append("\n")
            if self._main_depth is not None:
                self.main.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
            return
        self.all_text.append(data)
        if self._active_link is not None:
            self._active_link["text"].append(data)
        if self._h1_depth is not None:
            self.h1.append(data)
        if self._article_candidate is not None:
            self._article_candidate.append(data)
        if self._main_depth is not None:
            self.main.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in VOID_TAGS:
            return
        depth = len(self.stack)
        if tag == "script" and self._script is not None:
            self.json_ld.append("".join(self._script))
            self._script = None
        if tag == "a" and self._active_link is not None:
            self._active_link["text"] = _clean_text("".join(self._active_link["text"]))
            self.links.append(self._active_link)
            self._active_link = None
        if self._h1_depth == depth:
            self._h1_depth = None
        if self._article_depth == depth:
            if self._article_candidate is not None:
                self.article_candidates.append(self._article_candidate)
            self._article_candidate = None
            self._article_depth = None
        if self._main_depth == depth:
            self._main_depth = None
        if self.stack:
            self.stack.pop()


def _json_objects(chunks: list[str]) -> list[dict]:
    objects: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for chunk in chunks:
        try:
            visit(json.loads(chunk))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid Medienservice JSON-LD") from exc
    return objects


def listing_page(page: str, response_url: str, spec: SourceSpec) -> tuple[list[tuple[str, str]], str | None]:
    if not archive_url_allowed(response_url, spec.institution_id):
        raise ValueError("Medienservice archive response lost its institution filter")
    parser = _PageParser()
    parser.feed(page)
    visible = _clean_text("".join(parser.all_text))
    id_marker = re.search(
        rf"(?is)(?:institution_ids|institution-id|institution_id).{{0,160}}{re.escape(spec.institution_id)}",
        page,
    )
    if spec.publisher.casefold() not in visible.casefold() and id_marker is None:
        raise ValueError("Medienservice archive did not verify the requested institution")
    records: dict[str, str] = {}
    for link in parser.links:
        try:
            ident, url = canonical_article_url(link["href"])
        except ValueError:
            continue
        title = _clean_text(link["text"])
        if len(title) >= 3:
            records.setdefault(ident, url)
    if not records:
        raise ValueError("Medienservice archive contains no parseable article links")
    next_urls = set()
    for link in parser.links:
        marker = " ".join((link["rel"], link["title"], link["aria"], link["text"])).casefold()
        if ("next" not in marker and "näch" not in marker and "weiter" not in marker
                and link["text"] not in {">", "›", "»"}):
            continue
        candidate = urljoin(response_url, link["href"])
        if not archive_url_allowed(candidate, spec.institution_id):
            raise ValueError("Medienservice next-page URL changed institution or origin")
        if candidate != response_url:
            next_urls.add(candidate)
    if len(next_urls) > 1:
        raise ValueError("Medienservice archive exposes ambiguous next-page links")
    return list(records.items()), next(iter(next_urls), None)


def landing_snapshot(page: str, response_url: str, spec: SourceSpec) -> str:
    if response_url != spec.archive_url or not archive_url_allowed(response_url, spec.institution_id):
        raise ValueError("Medienservice landing response lost its institution filter")
    parser = _PageParser()
    parser.feed(page)
    values = parser.inputs.get("search_first_searched", [])
    if len(values) != 1 or not values[0] or len(values[0]) > 200:
        raise ValueError("Medienservice landing page has no unique search snapshot")
    visible = _clean_text("".join(parser.all_text))
    id_marker = re.search(
        rf"(?is)(?:institution_ids|institution-id|institution_id).{{0,160}}{re.escape(spec.institution_id)}",
        page,
    )
    if spec.publisher.casefold() not in visible.casefold() and id_marker is None:
        raise ValueError("Medienservice landing page did not verify the requested institution")
    return values[0]


def search_records(payload: bytes, response_url: str, spec: SourceSpec) -> tuple[list[tuple[str, str]], bool]:
    if not search_url_allowed(response_url, spec.institution_id):
        raise ValueError("Medienservice search response URL is invalid")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Medienservice search returned invalid JSON") from exc
    if not isinstance(value, dict) or not {"teaser", "disable", "up_to_date"} <= set(value):
        raise ValueError("Medienservice search JSON schema changed")
    if not isinstance(value["teaser"], list) or len(value["teaser"]) > 6 or not isinstance(value["disable"], bool):
        raise ValueError("Medienservice search JSON fields are invalid")
    records: dict[str, str] = {}
    for teaser in value["teaser"]:
        if not isinstance(teaser, str):
            raise TypeError("Medienservice teaser is not HTML")
        parser = _PageParser()
        parser.feed(teaser)
        found = []
        for link in parser.links:
            try:
                found.append(canonical_article_url(link["href"]))
            except ValueError:
                continue
        unique = dict(found)
        if len(unique) != 1:
            raise ValueError("Medienservice teaser does not identify exactly one article")
        records.update(unique)
    if not records and not value["disable"]:
        raise ValueError("Medienservice search returned an empty nonterminal page")
    return list(records.items()), value["disable"]


def _publisher_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        names = []
        if isinstance(value.get("name"), str):
            names.append(value["name"])
        return names
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_publisher_names(item))
        return result
    return []


def _publication(value: str) -> str:
    value = _clean_text(value)
    if not value:
        raise ValueError("Medienservice article has no publication timestamp")
    try:
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}", value):
            stamp = datetime.strptime(value, "%d.%m.%Y %H:%M").replace(
                tzinfo=ZoneInfo("Europe/Berlin")
            )
        elif len(value) == 10:
            stamp = datetime.combine(date.fromisoformat(value), datetime.min.time(), ZoneInfo("Europe/Berlin"))
        else:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ZoneInfo("Europe/Berlin"))
    except ValueError as exc:
        raise ValueError("Invalid Medienservice publication timestamp") from exc
    if not 2000 <= stamp.year <= datetime.now(UTC).year + 1:
        raise ValueError("Medienservice publication timestamp is outside the accepted range")
    return stamp.isoformat()


def article_page(page: str, response_url: str, spec: SourceSpec) -> Article:
    ident, canonical_url = canonical_article_url(response_url)
    parser = _PageParser()
    parser.feed(page)
    objects = _json_objects(parser.json_ld)
    id_positions = [index for index, pair in enumerate(parser.meta_sequence)
                    if pair == ("id", ident)]
    if len(id_positions) != 1:
        raise ValueError("Medienservice article ID metadata was not verified")
    article_meta = parser.meta_sequence[id_positions[0]:]
    next_id = next((index for index, pair in enumerate(article_meta[1:], start=1)
                    if pair[0] == "id"), len(article_meta))
    article_meta = article_meta[:next_id]
    article_urls = []
    for key, value in article_meta:
        if key == "url":
            article_urls.append(canonical_article_url(value)[1])
    if article_urls != [canonical_url]:
        raise ValueError("Medienservice article URL metadata was not verified")

    publishers = [value for key, value in article_meta if key == "author"]
    for item in objects:
        publishers.extend(_publisher_names(item.get("publisher")))
        publishers.extend(_publisher_names(item.get("author")))
    if all(_clean_text(name).casefold() != spec.publisher.casefold() for name in publishers):
        raise ValueError("Medienservice article publisher mismatch")

    title_candidates = []
    title_candidates.extend(value for key, value in article_meta if key in {"title", "headline"})
    for item in objects:
        title_candidates.extend(item.get(key) for key in ("headline", "name") if isinstance(item.get(key), str))
    title_candidates.extend(parser.meta.get("og:title", []))
    title_candidates.append("".join(parser.h1))
    title = next((_clean_text(value) for value in title_candidates if len(_clean_text(value)) >= 3), "")
    if not title:
        raise ValueError("Medienservice article title extraction failed")

    date_candidates = []
    date_candidates.extend(value for key, value in article_meta if key == "date")
    for item in objects:
        if isinstance(item.get("datePublished"), str):
            date_candidates.append(item["datePublished"])
    for key in ("article:published_time", "datepublished", "date", "dc.date"):
        date_candidates.extend(parser.meta.get(key, []))
    date_candidates.extend(parser.times)
    published = _publication(next((value for value in date_candidates if _clean_text(value)), ""))

    subtitles = [_clean_text(value) for key, value in article_meta if key == "subtitle"]
    subtitles = list(dict.fromkeys(value for value in subtitles if value))
    if len(subtitles) != 1:
        raise ValueError("Medienservice article has no unique publication subtitle")
    marker = subtitles[0]
    short_marker_match = re.search(r"\bNr\.\s*\d+[|/]\d+\b", marker, re.IGNORECASE)
    markers = [marker]
    if short_marker_match:
        markers.append(short_marker_match[0])
    candidates = [
        _body_text(value) for value in ("".join(parts) for parts in parser.article_candidates)
        if len(_body_text(value)) >= 30
    ]
    matching = [
        candidate for candidate in candidates
        if any(item.casefold() in candidate.casefold() for item in markers)
    ]
    if len(matching) != 1:
        raise ValueError("Medienservice article body container is missing or ambiguous")
    body = matching[0]
    return Article(ident, canonical_url, title, published, body)


def source_get(
    client: httpx.Client,
    url: str,
    spec: SourceSpec,
    policy: RobotsPolicy,
    last_request: list[float],
    *,
    headers: dict[str, str] | None = None,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> httpx.Response:
    parsed = urlparse(url)
    is_article = (parsed.scheme, parsed.netloc) == ("https", "www.medienservice.sachsen.de") \
        and ARTICLE_PATH.fullmatch(parsed.path) is not None and not parsed.query
    if not (archive_url_allowed(url, spec.institution_id)
            or search_url_allowed(url, spec.institution_id) or is_article):
        raise ValueError("Unexpected Medienservice request URL")
    if not policy.allows(spec.user_agent, url):
        raise ValueError("Medienservice robots.txt disallows requested URL")
    sleeper(max(0.0, policy.delay - (monotonic() - last_request[0])))
    last_request[0] = monotonic()
    response = client.get(url, headers=headers)
    if response.status_code in REDIRECT_STATUSES:
        raise ValueError("Medienservice source redirect is not accepted")
    if response.status_code != 304 and response.status_code != 200:
        response.raise_for_status()
        raise ValueError(f"Medienservice source returned HTTP {response.status_code}")
    if str(response.url) != url:
        raise ValueError("Medienservice response URL differs from request URL")
    return response
