import hashlib
import json

import httpx
import pytest

from crimemapsde_cities_06_10 import leipzig
from crimemapsde_cities_06_10.offline_source import probe_robots, review_rows
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


def test_leipzig_robots_and_official_identity_fail_closed(tmp_path):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(404, text="<html>not found</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(httpx.HTTPStatusError):
        probe_robots(leipzig.ORIGIN, leipzig.ARCHIVE, leipzig.USER_AGENT, client=client)
    assert requested == [leipzig.ORIGIN + "/robots.txt"]

    input_file = tmp_path / "wrong.jsonl"
    row = bulletin("1100200", "Ort: Leipzig. Ein genügend langer synthetischer Quellenbericht ohne Punkt.")
    row["publisher"] = "Andere Polizeidirektion"
    input_file.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="publisher"):
        leipzig.sync_offline(tmp_path / "wrong.sqlite", input_file)


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
