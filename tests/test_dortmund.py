import httpx
import pytest

from crimemapsde_cities_06_10 import dortmund
from crimemapsde_cities_06_10.city_scope import city_only_reports, record_city_scope
from crimemapsde_cities_06_10.storage import accept, connect


def listing(records, next_page=""):
    rows = []
    for slug, published, teaser, location in records:
        rows.append(
            '<div class="views-row"><div class="press-list"><div class="row-wrapper">'
            f'<h2 class="field-title"><a href="/presse/{slug}">Meldung {slug}</a></h2>'
            f'<div class="date-time"><time datetime="{published}">Datum</time></div>'
            f'<div class="field-teaser">{teaser}</div>'
            f'<div class="combined-location">{location}</div>'
            '</div></div></div>'
        )
    pager = (
        '<nav><ul class="pager-show-more"><li class="pager__item">'
        f'<a href="?page={next_page}" title="Zur nächsten Seite">mehr</a>'
        '</li></ul></nav>' if next_page else ""
    )
    return ('<div class="view-id-list_view_press_releases_solr">'
            '<div class="view-content">' + "".join(rows) + '</div>' + pager + '</div>')


def article(author="Polizei Dortmund", place="Nordmarkt", native_id="217132"):
    return (
        '<article class="node node--type--press-release">'
        f'<div class="field--name-field-press-release-author">{author}</div>'
        '<div class="field--name-body"><p>Am Nordmarkt in Dortmund wurde ein Mann beraubt.</p>'
        f'<p>Die Tat ereignete sich nahe {place}; die Ermittlungen dauern an.</p></div>'
        '</article><aside><p>Andere Stadt, anderer Fall</p></aside>'
        f'<script>"currentPath":"node\\/{native_id}"</script>'
    )


def test_native_listing_and_body_keep_source_identity_without_city_assumption():
    page = listing([
        ("raub-am-nordmarkt", "2026-09-27T10:10:15+02:00", "Lfd. Nr.: 0805 Tat am Nordmarkt", "Polizei Dortmund | PLZ: 44145"),
        ("fall-in-hamm", "2026-09-26T10:10:15+02:00", "Lfd. Nr.: 0804 Fall in Hamm", "Polizei Dortmund"),
    ], next_page="1")
    rows = dortmund.listing_rows(page)
    assert len(rows) == 2
    assert rows[0]["id"] == "raub-am-nordmarkt"
    assert rows[0]["district"] == ""
    assert dortmund.next_url(page) == dortmund.ARCHIVE + "?page=1"
    body, node_id = dortmund.article_body(article())
    assert node_id == "217132" and "Nordmarkt" in body
    assert "Andere Stadt" not in body
    with pytest.raises(ValueError, match="publisher"):
        dortmund.article_body(article(author="Polizei Hamm"))
    with pytest.raises(ValueError, match="pagination"):
        dortmund.next_url(page.replace('href="?page=1"', 'href="https://elsewhere.test/"'))


def test_robots_failure_blocks_archive_and_city_review_expires_on_revision(tmp_path):
    with pytest.raises(ValueError, match="invalid"):
        dortmund.source_robots("<html>error</html>")
    with pytest.raises(ValueError, match="disallows"):
        dortmund.source_robots("User-agent: *\nDisallow: /presse/\n")
    db = connect(tmp_path / "police.sqlite")
    row = dortmund.listing_rows(listing([
        ("raub-am-nordmarkt", "2026-09-27T10:10:15+02:00", "Lfd. Nr.: 0805", "Polizei Dortmund"),
    ]))[0]
    dortmund.discover(db, [row], 1)
    body, _ = dortmund.article_body(article())
    accept(db, row["id"], body, {}, 2)
    assert city_only_reports(db) == []
    record_city_scope(db, row["id"], "in_city", "Am Nordmarkt in Dortmund wurde ein Mann beraubt.", 3)
    assert len(city_only_reports(db)) == 1
    accept(db, row["id"], body + " Später kam ein neuer Hinweis hinzu.", {}, 4)
    assert city_only_reports(db) == []
    db.close()


def test_bounded_native_archive_resumes_and_retains_node_id(tmp_path, monkeypatch):
    requested = []
    head = listing([
        ("raub-am-nordmarkt", "2026-09-27T10:10:15+02:00", "Lfd. Nr.: 0805 Tat", "Polizei Dortmund"),
    ], next_page="1")
    old = listing([
        ("fall-in-hamm", "2025-09-26T10:10:15+02:00", "Lfd. Nr.: 0804 Fall", "Polizei Dortmund"),
    ])

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /admin/\n")
        if request.url.path == "/presse/pressemitteilungen" and not request.url.query:
            return httpx.Response(200, text=head)
        if request.url.path == "/presse/pressemitteilungen" and request.url.query:
            return httpx.Response(200, text=old)
        if request.url.path == "/presse/raub-am-nordmarkt":
            return httpx.Response(200, text=article())
        raise AssertionError(request.url)

    real_client = httpx.Client
    monkeypatch.setattr(dortmund.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(dortmund.time, "sleep", lambda _: None)
    path = tmp_path / "dortmund.sqlite"
    first = dortmund.sync(path, 2026, pages=1, limit=1)
    assert first["new"] == 1 and first["historical_scan_complete"] is False
    assert first["city_scope"]["pending"] == 1
    second = dortmund.sync(path, 2026, pages=1, limit=1)
    assert second["head_refreshed"] is True
    assert second["historical_scan_complete"] is True
    assert second["coverage_complete"] is False
    assert requested.count(dortmund.ARCHIVE + "?page=1") == 1
    db = connect(path)
    stored = db.execute("SELECT id,url,sha256,revision FROM reports").fetchone()
    assert stored["id"] == "raub-am-nordmarkt" and len(stored["sha256"]) == 64
    assert stored["revision"] == 1
    ids = db.execute("SELECT police_number,native_node_id FROM dortmund_source_ids").fetchone()
    assert tuple(ids) == ("0805", "217132")
    db.close()


def test_source_get_retries_temporary_response_without_leaving_origin(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503 if len(calls) == 1 else 200, text="ready")

    monkeypatch.setattr(dortmund.time, "sleep", lambda _: None)
    robots = dortmund.source_robots("User-agent: *\nDisallow: /admin/\n")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = dortmund._get(client, robots, dortmund.ARCHIVE, [0], 1)
    assert result.text == "ready"
    assert calls == [dortmund.ARCHIVE, dortmund.ARCHIVE]
