import json

import httpx
import pytest

from crimemapsde_cities_06_10 import bremen
from crimemapsde_cities_06_10.bremen import (
    _fetch_robots,
    _source_get,
    _source_robots,
    article_body,
    listing_rows,
    sync_newsroom,
)
from crimemapsde_cities_06_10.offline_source import probe_robots
from crimemapsde_cities_06_10.storage import connect

LISTING = '''<link rel="next" href="/blaulicht/nr/35235/30">
<article class="news" data-label="6359801"><div class="date">27.09.2026 &ndash; 11:23</div>
<h3 class="news-headline-clamp"><a href="https://www.presseportal.de/blaulicht/pm/35235/6359801">
POL-HB: Nr.: 0701 --Synthetischer Testbericht--</a></h3></article>
<article class="news" data-label="6359799"><div class="date">27.09.2026 &ndash; 10:20</div>
<h3 class="news-headline-clamp"><a href="https://www.presseportal.de/blaulicht/pm/35235/6359799">
POL-HB: Nr.: 0700 --Zweiter Testbericht--</a></h3></article>'''

ARTICLE = '''<article class="col eight story mbs">
<p class="customer"><a>Polizei Bremen</a></p>
<h1>POL-HB: Nr.: 0701 --Synthetischer Testbericht--</h1>
<p><i>Bremen (ots)</i></p>
<p>Ort: Bremen-Mitte, Teststraße. Dort wurde ein synthetischer Vorfall gemeldet.</p>
<p>Die Ermittlungen dauern an.</p>
<p class="contact-headline">Rückfragen bitte an:</p>
<p>Pressestelle Polizei Bremen</p></article>'''


def test_bremen_newsroom_parsers_require_publisher_and_canonical_ids():
    rows = listing_rows(LISTING)
    assert [row["id"] for row in rows] == ["6359801", "6359799"]
    assert rows[0]["published"] == "2026-09-27T11:23:00+02:00"
    assert all(row["district"] == "" for row in rows)
    assert "Teststraße" in article_body(ARTICLE)
    assert "Pressestelle Polizei Bremen" not in article_body(ARTICLE)
    with pytest.raises(ValueError, match="publisher"):
        article_body(ARTICLE.replace("Polizei Bremen</a>", "Anderer Herausgeber</a>"))
    with pytest.raises(ValueError, match="unparsed"):
        listing_rows(LISTING.replace("/35235/6359799", "/99999/6359799"))


def test_bremen_newsroom_robots_and_paths_fail_closed():
    with pytest.raises(ValueError, match="invalid"):
        _source_robots("<html>not robots</html>")
    with pytest.raises(ValueError, match="disallows"):
        _source_robots("User-agent: *\nDisallow: /\n")

    class Client:
        def get(self, *_args, **_kwargs):
            raise AssertionError("No request expected")

    class Robots:
        def can_fetch(self, *_args):
            return False

    with pytest.raises(ValueError, match="robots.txt disallows"):
        _source_get(Client(), Robots(), bremen.NEWSROOM, 0, [0])
    with pytest.raises(ValueError, match="origin"):
        _source_get(Client(), Robots(), "https://elsewhere.test/blaulicht/pm/35235/1", 0, [0])
    with pytest.raises(ValueError, match="path"):
        _source_get(Client(), Robots(), "https://www.presseportal.de/blaulicht/pm/1/1", 0, [0])


def test_bremen_full_newsroom_scan_resumes_and_refreshes_head(tmp_path, monkeypatch):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/blaulicht/nr/35235":
            return httpx.Response(200, text=LISTING)
        if request.url.path == "/blaulicht/nr/35235/30":
            older = LISTING.replace("27.09.2026", "27.09.2025").replace(
                '<link rel="next" href="/blaulicht/nr/35235/30">', ""
            )
            return httpx.Response(200, text=older)
        if request.url.path.startswith("/blaulicht/pm/35235/"):
            return httpx.Response(200, text=ARTICLE)
        raise AssertionError(request.url)

    real_client = httpx.Client
    monkeypatch.setattr(
        bremen.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    path = tmp_path / "police.sqlite"
    first = sync_newsroom(path, 2026, full=True, max_pages=1, limit=1, delay=0)
    assert first["archive_complete"] is False
    assert first["cursor_pages_scanned"] == 1
    assert first["new"] == 1 and first["pending"] == 1
    second = sync_newsroom(path, 2026, full=True, max_pages=1, limit=1, delay=0)
    assert second["head_refreshed"] is True
    assert second["archive_complete"] is True
    assert second["cursor_pages_scanned"] == 2
    assert requested.count("/blaulicht/nr/35235/30") == 1


def test_bremen_rate_limit_stops_article_batch(tmp_path, monkeypatch):
    requested_articles = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/blaulicht/nr/35235":
            return httpx.Response(
                200,
                text=LISTING.replace(
                    '<link rel="next" href="/blaulicht/nr/35235/30">', ""
                ),
            )
        if request.url.path.startswith("/blaulicht/pm/35235/"):
            requested_articles.append(request.url.path)
            return httpx.Response(429, text="slow down")
        raise AssertionError(request.url)

    real_client = httpx.Client
    monkeypatch.setattr(
        bremen.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = sync_newsroom(tmp_path / "police.sqlite", 2026, limit=2, delay=0)
    assert result["rate_limited"] is True
    assert result["failed"] == 1
    assert len(requested_articles) == 1


def test_bremen_first_robots_429_stops_without_retry():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "30"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _fetch_robots(client)
    assert requested == ["/robots.txt"]


def test_bremen_offline_group_page_is_kept_whole_and_quarantined(tmp_path):
    input_file = tmp_path / "bremen.jsonl"
    record = {
        "publisher": "Polizei Bremen",
        "url": "https://www.polizei.bremen.de/news/pressestelle/pressemeldungen-ab-11092026-69054",
        "published": "2026-09-11T10:00:00+02:00",
        "title": "Pressemeldungen ab 11.09.2026",
        "body": ("Zeugen nach Verkehrsunfall gesucht. Ort: Bremen-Huchting. "
                 "Taschendieb verhaftet. Ort: Bremen-Mitte. Mehrere Meldungen bleiben getrennt zu prüfen."),
    }
    input_file.write_text(json.dumps(record) + "\n")
    path = tmp_path / "police.sqlite"
    result = bremen.sync_offline(path, input_file, limit=1)
    assert result["new"] == 1 and result["publication_ready"] is False
    assert result["city_scope"]["pending"] == 1
    again = bremen.sync_offline(path, input_file, limit=1)
    assert again["processed"] == 0 and again["file_scan_complete"] is True
    db = connect(path)
    source = db.execute("SELECT id,url,sha256,revision FROM reports").fetchone()
    assert source["id"] == "69054" and len(source["sha256"]) == 64
    assert source["revision"] == 1
    assert db.execute("SELECT record_type,publication_eligible FROM offline_source_units").fetchone()[:] == (
        "multi_announcement_archive_page", 0,
    )
    db.close()


def test_bremen_blank_robots_and_nonofficial_url_block_processing(tmp_path):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, text="\n")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ValueError, match="invalid"):
        probe_robots(bremen.ORIGIN, bremen.ARCHIVE, bremen.USER_AGENT, client=client)
    assert requested == [bremen.ORIGIN + "/robots.txt"]

    input_file = tmp_path / "wrong.jsonl"
    input_file.write_text(json.dumps({
        "publisher": "Polizei Bremen", "url": "https://www.presseportal.de/blaulicht/pm/35235/6359792",
        "published": "2026-09-11T10:00:00+02:00", "title": "Beispielhafte Meldung",
        "body": "Ein ausreichend langer synthetischer Quellenbericht aus einer anderen Domain.",
    }) + "\n")
    with pytest.raises(ValueError, match="source URL"):
        bremen.sync_offline(tmp_path / "wrong.sqlite", input_file)
