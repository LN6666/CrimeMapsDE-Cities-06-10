import httpx
import pytest

from crimemapsde_cities_06_10 import dusseldorf
from crimemapsde_cities_06_10.dusseldorf import (
    _source_get,
    _source_robots,
    article_body,
    city_only_reports,
    listing_rows,
    record_city_scope,
    sync,
)
from crimemapsde_cities_06_10.storage import accept, connect, discover

LISTING = '''<link rel="next" href="/blaulicht/nr/13248/30">
<article class="news" data-label="6359792"><div class="date">27.09.2026 &ndash; 11:23</div>
<a href="/regional/Duesseldorf" class="news-topic">Düsseldorf</a>
<h3 class="news-headline-clamp"><a href="https://www.presseportal.de/blaulicht/pm/13248/6359792">
POL-D: Flingern - Schwer verletzter Mann</a></h3></article>
<article class="news" data-label="6359783"><div class="date">27.09.2026 &ndash; 11:20</div>
<a href="/regional/Duesseldorf" class="news-topic">Düsseldorf</a>
<h3 class="news-headline-clamp"><a href="https://www.presseportal.de/blaulicht/pm/13248/6359783">
POL-D: Wuppertal - A 46 - Verkehrsunfall</a></h3></article>'''

ARTICLE = '''<nav>Düsseldorf Polizei Presse</nav><article class="col eight story mbs">
<p class="date">27.09.2026</p><p class="customer"><a>Polizei Düsseldorf</a></p>
<h1>POL-D: Flingern - Schwer verletzter Mann</h1>
<p><i>Düsseldorf (ots)</i></p><p>In Flingern auf der Birkenstraße wurde ein Mann verletzt.</p>
<p>Zeuginnen und Zeugen werden gesucht.</p>
<p class="contact-headline">Rückfragen der Medien bitte an:</p>
<p>Polizei Düsseldorf - Am Polizeipräsidium</p></article>
<article class="news"><p>Another event in Wuppertal</p></article>'''


def test_newsroom_keeps_source_ids_but_does_not_infer_city_from_topic():
    rows = listing_rows(LISTING)
    assert [row["id"] for row in rows] == ["6359792", "6359783"]
    assert rows[0]["published"] == "2026-09-27T11:23:00+02:00"
    assert rows[0]["district"] == rows[1]["district"] == ""
    assert "Wuppertal" in rows[1]["title"]
    with pytest.raises(ValueError, match="unparsed"):
        listing_rows(LISTING.replace("/13248/6359783", "/99999/6359783"))
    with pytest.raises(ValueError, match="duplicate"):
        first_article = LISTING[LISTING.index("<article"):LISTING.index("</article>") + 10]
        listing_rows(LISTING + first_article)


def test_article_body_requires_matching_police_publisher_and_omits_contacts():
    body = article_body(ARTICLE)
    assert "Birkenstraße" in body
    assert "Am Polizeipräsidium" not in body
    assert "Another event" not in body
    with pytest.raises(ValueError, match="publisher"):
        article_body(ARTICLE.replace("Polizei Düsseldorf</a>", "Anderer Herausgeber</a>"))


def test_article_body_preserves_preformatted_source_lists():
    page = ARTICLE.replace(
        "<p>Zeuginnen und Zeugen werden gesucht.</p>",
        "<pre>Bilanz:\n1. Verstoß gegen das Versammlungsgesetz\n"
        "2. Drei weitere Strafanzeigen</pre>"
        "<p>Zeuginnen und Zeugen werden gesucht.</p>",
    )
    body = article_body(page)
    assert "Bilanz: 1. Verstoß gegen das Versammlungsgesetz" in body
    assert "2. Drei weitere Strafanzeigen" in body
    assert "Am Polizeipräsidium" not in body


def test_city_export_requires_current_source_bound_city_decision(tmp_path):
    db = connect(tmp_path / "police.sqlite")
    local, outside = listing_rows(LISTING)
    discover(db, [local, outside], 1)
    accept(db, local["id"], article_body(ARTICLE), {}, 2)
    accept(db, outside["id"], "Dieser Verkehrsunfall ereignete sich auf der A 46 bei Wuppertal.", {}, 2)
    assert city_only_reports(db) == []
    with pytest.raises(ValueError, match="source evidence"):
        record_city_scope(db, local["id"], "in_city", "Düsseldorf", 3)
    with pytest.raises(ValueError, match="quote the fetched body"):
        record_city_scope(db, outside["id"], "in_city", "Düsseldorf Polizeipräsidium", 3)
    record_city_scope(db, outside["id"], "out_of_city", "auf der A 46 bei Wuppertal", 3)
    assert city_only_reports(db) == []
    record_city_scope(db, local["id"], "in_city", "In Flingern auf der Birkenstraße wurde ein Mann verletzt.", 3)
    assert [row["id"] for row in city_only_reports(db)] == [local["id"]]
    accept(db, local["id"], article_body(ARTICLE) + " Die Ermittlungen dauern an.", {}, 4)
    assert city_only_reports(db) == []
    db.close()


def test_disallowed_or_external_article_is_never_requested():
    class Client:
        def get(self, *_args, **_kwargs):
            raise AssertionError("No request expected")

    class Robots:
        def can_fetch(self, *_args):
            return False

    with pytest.raises(ValueError, match="robots.txt disallows"):
        _source_get(Client(), Robots(), "https://www.presseportal.de/blaulicht/nr/13248", 1, [0])
    with pytest.raises(ValueError, match="origin"):
        _source_get(Client(), Robots(), "https://elsewhere.test/blaulicht/pm/13248/1", 1, [0])
    with pytest.raises(ValueError, match="path"):
        _source_get(Client(), Robots(), "https://www.presseportal.de/blaulicht/pm/999/1", 1, [0])


def test_robots_must_contain_rules_and_allow_newsroom():
    with pytest.raises(ValueError, match="invalid"):
        _source_robots("<html>temporarily unavailable</html>")
    with pytest.raises(ValueError, match="disallows"):
        _source_robots("User-agent: *\nDisallow: /\n")
    assert _source_robots("User-agent: *\nDisallow: /images/\n").can_fetch(
        "CrimeMapsDE-Cities-06-10", "https://www.presseportal.de/blaulicht/pm/13248/6359792"
    )


def test_full_archive_resumes_older_page_and_refreshes_new_head(tmp_path, monkeypatch):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/blaulicht/nr/13248":
            return httpx.Response(200, text=LISTING)
        if request.url.path == "/blaulicht/nr/13248/30":
            older = LISTING.replace("27.09.2026", "27.09.2025").replace(
                '<link rel="next" href="/blaulicht/nr/13248/30">', ""
            )
            return httpx.Response(200, text=older)
        if request.url.path.startswith("/blaulicht/pm/13248/"):
            return httpx.Response(200, text=ARTICLE)
        raise AssertionError(request.url)

    real_client = httpx.Client
    monkeypatch.setattr(
        dusseldorf.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    path = tmp_path / "police.sqlite"
    first = sync(path, 2026, full=True, max_pages=1, limit=1, delay=0)
    assert first["archive_complete"] is False
    assert first["cursor_pages_scanned"] == 1
    second = sync(path, 2026, full=True, max_pages=1, limit=1, delay=0)
    assert second["head_refreshed"] is True
    assert second["archive_complete"] is True
    assert second["cursor_pages_scanned"] == 2
    assert requested.count("/blaulicht/nr/13248/30") == 1


@pytest.mark.parametrize("status", [429, 503, "invalid_publisher"])
def test_first_article_source_error_stops_remaining_requests(tmp_path, monkeypatch, status):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/blaulicht/nr/13248":
            return httpx.Response(200, text=LISTING)
        if request.url.path == "/blaulicht/pm/13248/6359792":
            if status == "invalid_publisher":
                return httpx.Response(200, text=ARTICLE.replace(
                    "Polizei Düsseldorf</a>", "Anderer Herausgeber</a>"
                ))
            return httpx.Response(status, text="source unavailable")
        raise AssertionError(f"Unexpected request after source error: {request.url}")

    real_client = httpx.Client
    monkeypatch.setattr(
        dusseldorf.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(dusseldorf.time, "sleep", lambda _: None)
    result = sync(tmp_path / "police.sqlite", 2026, full=True, max_pages=1, limit=2, delay=1)
    assert requested == ["/robots.txt", "/blaulicht/nr/13248", "/blaulicht/pm/13248/6359792"]
    assert result["failed"] == 1 and result["pending"] == 2
    assert result["stopped_on_source_error"]["source_id"] == "6359792"
    assert result["stopped_on_source_error"]["http_status"] == (
        None if status == "invalid_publisher" else status
    )
