import json

import httpx
import pytest

from crimemapsde_cities_06_10 import leipzig
from crimemapsde_cities_06_10.offline_source import probe_robots
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
