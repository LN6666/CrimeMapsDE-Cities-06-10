import hashlib
import json

import httpx
import pytest

from crimemapsde_cities_06_10 import leipzig
from crimemapsde_cities_06_10.offline_source import probe_robots, review_rows
from crimemapsde_cities_06_10.sachsen_medienservice import robots_policy
from crimemapsde_cities_06_10.storage import connect


def bulletin(ident, body):
    return {
        "publisher": "Polizeidirektion Leipzig",
        "url": f"https://medienservice.sachsen.de/medien/news/{ident}",
        "published": "2026-09-27T11:32:00+02:00",
        "title": "Mehrere synthetische Meldungen aus dem Direktionsbereich",
        "body": body,
    }


def test_offline_bulletins_checkpoint_and_revision_are_not_publishable(tmp_path):
    input_file = tmp_path / "official.jsonl"
    first_body = "Ort: Lossatal. Ein Testfall außerhalb der Stadt. Ort: Leipzig. Ein weiterer Testfall."
    second_body = "Ort: Markranstädt. Ein synthetischer Fall wurde im Amtsbereich aufgenommen."
    rows = [bulletin("1100200", first_body), bulletin("1100201", second_body)]
    input_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    db_path = tmp_path / "leipzig.sqlite"
    first = leipzig.sync_offline(db_path, input_file, limit=1)
    assert first["processed"] == 1 and first["file_scan_complete"] is False
    second = leipzig.sync_offline(db_path, input_file, limit=1)
    assert second["processed"] == 1 and second["file_scan_complete"] is True
    db = connect(db_path)
    assert db.execute("SELECT count(*) FROM reports").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM offline_source_units WHERE publication_eligible=0").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
    row = db.execute("SELECT url,sha256,revision,district FROM reports WHERE id='1100200'").fetchone()
    assert row["url"].endswith("/1100200") and len(row["sha256"]) == 64
    assert row["revision"] == 1 and row["district"] == ""
    db.close()

    rows[0]["body"] = first_body + " Der Quelltext wurde später korrigiert."
    input_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    revised = leipzig.sync_offline(db_path, input_file, limit=1)
    assert revised["revised"] == 1 and revised["start_line"] == 0
    db = connect(db_path)
    assert db.execute("SELECT revision FROM reports WHERE id='1100200'").fetchone()[0] == 2
    db.close()


def test_leipzig_unavailable_robots_and_official_identity(tmp_path):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(404, text="<html>not found</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert probe_robots(leipzig.ORIGIN, leipzig.ARCHIVE, leipzig.USER_AGENT, client=client) is None
    assert requested == [leipzig.ORIGIN + "/robots.txt"]

    input_file = tmp_path / "wrong.jsonl"
    row = bulletin("1100200", "Ort: Leipzig. Ein genügend langer synthetischer Quellenbericht ohne Punkt.")
    row["publisher"] = "Andere Polizeidirektion"
    input_file.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="publisher"):
        leipzig.sync_offline(tmp_path / "wrong.sqlite", input_file)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_live_robots_ambiguous_or_unreachable_statuses_fail_closed(status):
    response = httpx.Response(status, request=httpx.Request("GET", leipzig.ORIGIN + "/robots.txt"))
    with pytest.raises(ValueError, match="robots"):
        robots_policy(response, leipzig.SPEC)


def test_live_robots_200_must_parse_and_allow_required_paths():
    request = httpx.Request("GET", leipzig.ORIGIN + "/robots.txt")
    allowed = httpx.Response(
        200, request=request, headers={"content-type": "text/plain"},
        text="User-agent: *\nDisallow: /private/\nCrawl-delay: 7\n",
    )
    assert robots_policy(allowed, leipzig.SPEC).delay == 7
    denied = httpx.Response(
        200, request=request, headers={"content-type": "text/plain"},
        text="User-agent: *\nDisallow: /medien/\n",
    )
    with pytest.raises(ValueError, match="disallows"):
        robots_policy(denied, leipzig.SPEC)
    malformed = httpx.Response(
        200, request=request, headers={"content-type": "text/plain"}, text="this is not robots"
    )
    with pytest.raises(ValueError, match="Malformed"):
        robots_policy(malformed, leipzig.SPEC)


def _landing():
    return """<html><body>Polizeidirektion Leipzig
    <input value="2026-09-28 17:59:06 UTC" type="hidden"
     name="search[first_searched]" id="search_first_searched" /></body></html>"""


def _article(ident="1100200", publisher="Polizeidirektion Leipzig", token="one"):
    return f"""<html><head><meta name="date" content="2019-01-01" />
    <meta name="author" content="Referat Kommunikation" />
    <meta name="id" content="{ident}" />
    <meta name="url" content="{leipzig.ORIGIN}/medien/news/{ident}" />
    <meta name="title" content="Mehrere Meldungen" />
    <meta name="date" content="28.09.2026 15:21" />
    <meta name="author" content="{publisher}" />
    <meta name="csrf-token" content="{token}" /></head><body>
    <h1 id="page-title">Mehrere Meldungen</h1><div class="row content-row">
    <div class="content-col-wide"><div class="row"><h2>Medieninformation Nr. 341|26</h2>
    <div class="col"><h3>Erster Sachverhalt</h3><p>Ort: Leipzig<br />Zeit: Sonntag</p>
    <p>Ein vollständiger synthetischer Polizeibericht für die Quellenprüfung.</p></div></div></div>
    <div class="content-col-small">Kontakt und Navigation, die nicht zum Bericht gehören.</div>
    </div></body></html>"""


def _search_payload(*idents):
    return json.dumps({
        "teaser": [f'<div><a href="/medien/news/{ident}">Meldung {ident}</a></div>' for ident in idents],
        "disable": True,
        "up_to_date": True,
    }).encode()


def test_live_sync_uses_public_search_checkpoint_and_canonical_source_hash(tmp_path):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        if request.url.path == "/medien/":
            return httpx.Response(200, request=request, text=_landing())
        if request.url.path == "/medien/news/search.json":
            return httpx.Response(200, request=request, content=_search_payload("1100200"))
        if request.url.path == "/medien/news/1100200":
            return httpx.Response(200, request=request, text=_article(), headers={"etag": '"raw-one"'})
        raise AssertionError(request.url)

    db_path = tmp_path / "live.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": leipzig.USER_AGENT}) as client:
        stats = leipzig.sync_live(
            db_path, 2026, max_pages=1, limit=2, client=client, sleeper=lambda _seconds: None
        )
    assert requested == ["/robots.txt", "/medien/", "/medien/news/search.json", "/medien/news/1100200"]
    assert stats["robots_status"] == 404 and stats["new"] == 1
    assert stats["archive_complete"] is True and stats["stored"] == 1 and stats["pending"] == 0
    with connect(db_path) as db:
        report = db.execute("SELECT * FROM reports").fetchone()
        provenance = db.execute("SELECT * FROM sachsen_source_units").fetchone()
        assert report["id"] == "1100200" and report["revision"] == 1
        assert "Kontakt und Navigation" not in report["body"]
        assert hashlib.sha256(report["body"].encode()).hexdigest() == report["sha256"]
        assert provenance["publisher"] == leipzig.PUBLISHER
        assert provenance["institution_id"] == "10976" and provenance["source_verified"] == 1
        assert len(provenance["source_sha256"]) == len(provenance["raw_html_sha256"]) == 64
    exported = review_rows(
        db_path, publisher=leipzig.PUBLISHER, host="medienservice.sachsen.de",
        article_path=leipzig.ARTICLE_PATH,
    )
    assert len(exported) == 1 and exported[0]["source_verified"] is True


def test_first_bad_article_stops_and_persists_queue_error(tmp_path):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(410, request=request)
        if request.url.path == "/medien/":
            return httpx.Response(200, request=request, text=_landing())
        if request.url.path == "/medien/news/search.json":
            return httpx.Response(200, request=request, content=_search_payload("1100200", "1100201"))
        if request.url.path == "/medien/news/1100201":
            return httpx.Response(200, request=request, text=_article("1100201", "Andere Behörde"))
        raise AssertionError(request.url)

    db_path = tmp_path / "failed.sqlite"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = leipzig.sync_live(
            db_path, 2026, max_pages=1, limit=2, client=client, sleeper=lambda _seconds: None
        )
    assert stats["failed"] == 1 and stats["stored"] == 0 and stats["pending"] == 2
    assert "/medien/news/1100200" not in requested
    with connect(db_path) as db:
        failed = db.execute("SELECT * FROM sachsen_queue WHERE id='1100201'").fetchone()
        assert failed["failures"] == 1 and "publisher mismatch" in failed["error"]


def test_saved_html_file_hash_and_full_text_gate_review(tmp_path):
    body = "Ort: Leipzig. Dies ist ein längerer synthetischer Bericht über zwei getrennte Vorfälle."
    saved = tmp_path / "saved.html"
    saved.write_text(f"<html><article>{body}</article></html>")
    record = bulletin("1100200", body)
    record.update(source_file="saved.html", source_file_sha256=hashlib.sha256(saved.read_bytes()).hexdigest())
    manifest = tmp_path / "official.jsonl"
    manifest.write_text(json.dumps(record) + "\n")
    db_path = tmp_path / "leipzig.sqlite"
    assert leipzig.sync_offline(db_path, manifest)["new"] == 1
    rows = review_rows(
        db_path, publisher=leipzig.PUBLISHER, host="medienservice.sachsen.de",
        article_path=leipzig.ARTICLE_PATH,
    )
    assert len(rows) == 1
    assert rows[0]["source_file_sha256"] == record["source_file_sha256"]
    assert rows[0]["source_file_text_matches"] is True
    assert rows[0]["source_verified"] is False and rows[0]["publication_ready"] is False
    saved.write_text(saved.read_text() + "<!-- changed -->")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        review_rows(db_path, publisher=leipzig.PUBLISHER, host="medienservice.sachsen.de",
                    article_path=leipzig.ARTICLE_PATH)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        leipzig.sync_offline(db_path, manifest)  # completed cursor must not skip a changed source file


def test_pdf_transcription_change_revises_even_when_body_is_same(tmp_path):
    body = "Ort: Leipzig. Ein vollständiger synthetischer Quellenbericht zu einem lokalen Vorfall."
    pdf = tmp_path / "saved.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfirst synthetic fixture\n%%EOF")
    record = bulletin("1100200", body)
    record.update(source_file="saved.pdf", source_file_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    manifest = tmp_path / "official.jsonl"
    manifest.write_text(json.dumps(record) + "\n")
    db_path = tmp_path / "leipzig.sqlite"
    assert leipzig.sync_offline(db_path, manifest)["new"] == 1
    pdf.write_bytes(b"%PDF-1.4\nsecond synthetic fixture\n%%EOF")
    record["source_file_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(record) + "\n")
    assert leipzig.sync_offline(db_path, manifest)["revised"] == 1
    rows = review_rows(db_path, publisher=leipzig.PUBLISHER, host="medienservice.sachsen.de",
                       article_path=leipzig.ARTICLE_PATH)
    assert rows[0]["revision"] == 2 and rows[0]["source_file_text_matches"] is False
