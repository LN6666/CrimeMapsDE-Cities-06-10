import json

import httpx
import pytest

from crimemapsde_cities_06_10 import bremen
from crimemapsde_cities_06_10.offline_source import probe_robots
from crimemapsde_cities_06_10.storage import connect


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
