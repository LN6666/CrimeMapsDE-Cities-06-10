import httpx
import pytest

from crimemapsde_cities_06_10 import stuttgart
from crimemapsde_cities_06_10.storage import accept, connect


def listing(records, next_page=""):
    link = f'<link rel="next" href="{next_page}">' if next_page else ""
    articles = []
    for ident, date, location in records:
        articles.append(
            f'<article class="news" data-label="{ident}">'
            f'<div class="date">{date}</div>'
            f'<a class="news-topic">{location}</a>'
            f'<h3 class="news-headline-clamp"><a '
            f'href="https://www.presseportal.de/blaulicht/pm/110977/{ident}">'
            f'POL-S: Synthetische Meldung {ident}</a></h3></article>'
        )
    return link + "".join(articles)


def article(publisher="Polizeipräsidium Stuttgart", place="Marktplatz"):
    return (
        '<nav>Fremde Straße, die nicht zur Meldung gehört</nav>'
        '<article class="col eight story mbs">'
        f'<p class="customer"><a>{publisher}</a></p>'
        '<h1>POL-S: Synthetische Meldung</h1>'
        f'<p>Stuttgart (ots) - Eine Person berichtete über einen Vorfall am {place}. '
        'Der eigentliche Sachverhalt wird hier nur für einen Parser-Test beschrieben.</p>'
        '<p class="contact-headline">Rückfragen bitte an:</p>'
        '<p>Kontakt an der Falschestraße</p></article>'
        '<article class="news"><p>Verwandte Meldung an der Irrestraße</p></article>'
    )


def test_listing_publisher_identity_and_city_lead_are_conservative():
    page = listing(
        [
            ("6359823", "27.09.2026 &ndash; 12:03", "Stuttgart-Bad Cannstatt"),
            ("6352287", "15.09.2026 &ndash; 11:07", "Markgröningen/-Stuttgart"),
        ],
        "/blaulicht/nr/110977/30",
    )
    rows = stuttgart.listing_rows(page)
    assert [row["id"] for row in rows] == ["6359823", "6352287"]
    assert rows[0]["published"] == "2026-09-27T12:03:00"
    assert stuttgart.scope_lead(rows[0]["locations"]) == (
        "stuttgart_review_lead", "Stuttgart-Bad Cannstatt",
    )
    assert stuttgart.scope_lead(rows[1]["locations"]) == (
        "needs_review", "Markgröningen/-Stuttgart",
    )
    assert stuttgart.scope_lead(["Stuttgart", "Ludwigsburg"])[0] == "needs_review"
    assert stuttgart.next_url(page) == "https://www.presseportal.de/blaulicht/nr/110977/30"
    with pytest.raises(ValueError, match="unparsed"):
        stuttgart.listing_rows(page.replace("/pm/110977/6359823", "/pm/110974/6359823"))
    with pytest.raises(ValueError, match="pagination"):
        stuttgart.next_url('<link rel="next" href="https://outside.test/blaulicht/nr/110977/30">')


def test_article_excludes_navigation_contacts_and_foreign_publisher():
    body = stuttgart.article_body(article())
    assert "Marktplatz" in body
    assert "Fremde Straße" not in body
    assert "Falschestraße" not in body
    assert "Irrestraße" not in body
    with pytest.raises(ValueError, match="publisher"):
        stuttgart.article_body(article(publisher="Polizeipräsidium Ludwigsburg"))


class FakeClient:
    def __init__(self, pages, requested):
        self.pages = pages
        self.requested = requested

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, headers=None):
        self.requested.append(url)
        request = httpx.Request("GET", url, headers=headers)
        status, body = self.pages[url]
        return httpx.Response(status, text=body, request=request)


def test_bounded_scan_resumes_and_keeps_body_and_hash_local(tmp_path, monkeypatch):
    requested = []
    robots = "User-agent: *\nDisallow: /images/\n"
    head = listing(
        [
            ("6359823", "27.09.2026 &ndash; 12:03", "Stuttgart-Bad Cannstatt"),
            ("6352287", "15.09.2026 &ndash; 11:07", "Markgröningen/-Stuttgart"),
        ],
        "/blaulicht/nr/110977/30",
    )
    second = listing(
        [
            ("6351000", "01.09.2026 &ndash; 10:00", "Stuttgart-Mitte"),
            ("5999999", "31.12.2025 &ndash; 10:00", "Stuttgart"),
        ]
    )
    pages = {
        stuttgart.ORIGIN + "/robots.txt": (200, robots),
        stuttgart.NEWSROOM: (200, head),
        stuttgart.NEWSROOM + "/30": (200, second),
        **{
            stuttgart.ORIGIN + f"/blaulicht/pm/110977/{ident}": (200, article())
            for ident in ("6359823", "6352287", "6351000")
        },
    }
    monkeypatch.setattr(stuttgart.httpx, "Client", lambda **_: FakeClient(pages, requested))
    monkeypatch.setattr(stuttgart.time, "sleep", lambda _: None)
    db_path = tmp_path / "stuttgart.sqlite"

    first = stuttgart.sync(db_path, 2026, pages=1, limit=1)
    assert first["archive_pages"] == 1
    assert first["new"] == 1
    assert first["historical_scan_complete"] is False
    assert first["coverage_complete"] is False
    assert stuttgart.NEWSROOM + "/30" not in requested

    requested.clear()
    second_run = stuttgart.sync(db_path, 2026, pages=1, limit=1)
    assert second_run["head_refreshed"] is True
    assert second_run["historical_scan_complete"] is True
    assert second_run["coverage_complete"] is False  # Current year is still open.
    assert stuttgart.NEWSROOM + "/30" in requested

    db = connect(db_path)
    assert db.execute("SELECT count(*) FROM reports").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM reports WHERE body IS NOT NULL").fetchone()[0] == 2
    row = db.execute("SELECT id,url,sha256,revision FROM reports WHERE id='6359823'").fetchone()
    assert row["url"] == stuttgart.ORIGIN + "/blaulicht/pm/110977/6359823"
    assert len(row["sha256"]) == 64 and row["revision"] == 1
    assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
    assert accept(db, "6359823", stuttgart.article_body(article(place="Neuer Platz")), {}, 9) == "revised"
    revised = db.execute("SELECT sha256,revision FROM reports WHERE id='6359823'").fetchone()
    assert revised["sha256"] != row["sha256"] and revised["revision"] == 2
    assert db.execute("SELECT count(*) FROM revisions WHERE id='6359823'").fetchone()[0] == 2
    outside = db.execute(
        "SELECT scope_hint FROM stuttgart_review_leads WHERE id='6352287'"
    ).fetchone()[0]
    assert outside == "needs_review"
    db.close()

    third_run = stuttgart.sync(db_path, 2026, pages=1, limit=1)
    assert third_run["head_refreshed"] is True
    assert third_run["cursor_pages_scanned"] == 2


def test_robots_disallow_stops_before_archive_request(tmp_path, monkeypatch):
    requested = []
    pages = {stuttgart.ORIGIN + "/robots.txt": (200, "User-agent: *\nDisallow: /blaulicht/\n")}
    monkeypatch.setattr(stuttgart.httpx, "Client", lambda **_: FakeClient(pages, requested))
    db_path = tmp_path / "blocked.sqlite"
    with pytest.raises(ValueError, match="robots.txt disallows"):
        stuttgart.sync(db_path, 2026, pages=1, limit=1)
    assert requested == [stuttgart.ORIGIN + "/robots.txt"]
    db = connect(db_path)
    assert db.execute("SELECT count(*) FROM reports").fetchone()[0] == 0
    db.close()
