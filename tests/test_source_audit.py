"""Synthetic contract checks; no live source material is included."""

from __future__ import annotations

import json
import sqlite3

import pytest

from crimemapsde_cities_06_10.city_scope import record_city_scope
from crimemapsde_cities_06_10.registry import CITIES
from crimemapsde_cities_06_10.source_audit import audit_source
from crimemapsde_cities_06_10.storage import accept, connect, discover

BODY = "Am Nordmarkt in Dortmund wurde ein Mann beraubt. Die Ermittlungen dauern weiterhin an."
EVIDENCE = "Am Nordmarkt in Dortmund wurde ein Mann beraubt."


def add_report(db, ident="test-article", body=BODY, url=None):
    discover(db, [{"id": ident, "url": url or f"https://dortmund.polizei.nrw/presse/{ident}",
                   "title": "Synthetischer Testbericht", "published": "2026-09-27T12:00:00+02:00",
                   "district": ""}], 1)
    if body is not None:
        accept(db, ident, body, {}, 2)


def test_registry_has_stable_five_city_contract():
    assert list(CITIES) == ["dusseldorf", "stuttgart", "leipzig", "dortmund", "bremen"]
    assert [spec.metric_epsg for spec in CITIES.values()] == [25832, 25832, 25833, 25832, 25832]
    assert all(spec.slug == slug and spec.source_url.startswith("https://")
               for slug, spec in CITIES.items())


def test_online_source_exports_only_hash_bound_scope_candidates_without_body(tmp_path):
    path = tmp_path / "dortmund.sqlite"
    db = connect(path)
    add_report(db)
    record_city_scope(db, "test-article", "in_city", EVIDENCE, 3)
    db.execute(
        "CREATE TABLE dortmund_source_ids (id TEXT PRIMARY KEY, native_node_id TEXT, police_number TEXT)"
    )
    db.execute("INSERT INTO dortmund_source_ids VALUES ('test-article','217132','0805')")
    db.commit()
    db.close()

    result = audit_source("dortmund", path)
    manifest = json.dumps(result)
    assert BODY not in manifest and EVIDENCE not in manifest
    assert result["counts"] == {"source_records": 1, "location_candidates": 1}
    candidate = result["location_candidates"][0]
    assert candidate["source_id"] == "test-article"
    assert candidate["source_url"].endswith("/test-article")
    assert candidate["source_date"] == "2026-09-27T12:00:00+02:00"
    assert candidate["native_node_id"] == "217132" and candidate["police_number"] == "0805"
    assert len(candidate["source_sha256"]) == 64 and candidate["revision"] == 1
    assert candidate["scope_status"] == "in_city" and candidate["review_status"] == "pending"
    assert result["records"][0]["scope_reviewed_at"] == 3
    assert result["readiness"]["archive_complete"] is False
    assert result["readiness"]["source_verified"] is False
    assert result["readiness"]["publication_ready"] is False

    db = sqlite3.connect(path)
    db.execute("UPDATE reports SET body=? WHERE id='test-article'", (BODY + " Untracked edit.",))
    db.commit()
    db.close()
    tampered = audit_source("dortmund", path)
    assert tampered["location_candidates"] == []
    assert "source_hash_mismatch" in tampered["readiness"]["blocking_reasons"]


def test_missing_body_and_revision_expire_city_scope(tmp_path):
    path = tmp_path / "dusseldorf.sqlite"
    db = connect(path)
    add_report(db, "1001", None, "https://www.presseportal.de/blaulicht/pm/13248/1001")
    add_report(db, "1002", url="https://www.presseportal.de/blaulicht/pm/13248/1002")
    record_city_scope(db, "1002", "in_city", EVIDENCE, 3)
    accept(db, "1002", BODY + " Der Bericht wurde später ergänzt.", {}, 4)
    db.close()
    result = audit_source("dusseldorf", path)
    by_id = {row["source_id"]: row for row in result["records"]}
    assert by_id["1001"]["body_available"] is False
    assert by_id["1001"]["scope_status"] == "pending"
    assert by_id["1002"]["revision"] == 2
    assert by_id["1002"]["scope_status"] == "stale"
    assert result["location_candidates"] == []
    assert {"source_body_missing", "city_scope_review_pending"}.issubset(
        result["readiness"]["blocking_reasons"]
    )


def test_stuttgart_listing_lead_is_not_city_scope_and_audit_is_read_only(tmp_path):
    path = tmp_path / "stuttgart.sqlite"
    db = connect(path)
    add_report(db, "1001", url="https://www.presseportal.de/blaulicht/pm/110977/1001")
    db.execute("CREATE TABLE stuttgart_review_leads (id TEXT PRIMARY KEY, listing_location TEXT)")
    db.execute("INSERT INTO stuttgart_review_leads VALUES ('1001','Stuttgart')")
    db.commit()
    db.close()
    result = audit_source("stuttgart", path)
    assert result["records"][0]["scope_status"] == "pending"
    assert result["location_candidates"] == []
    db = sqlite3.connect(path)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "city_scope_decisions" not in tables
    db.close()


def test_bremen_newsroom_article_can_enter_review_candidates_but_not_publication(tmp_path):
    path = tmp_path / "bremen.sqlite"
    db = connect(path)
    add_report(
        db,
        "6359801",
        url="https://www.presseportal.de/blaulicht/pm/35235/6359801",
    )
    record_city_scope(db, "6359801", "in_city", EVIDENCE, 3)
    db.close()
    result = audit_source("bremen", path)
    assert result["counts"] == {"source_records": 1, "location_candidates": 1}
    assert result["records"][0]["manually_supplied"] is False
    assert result["records"][0]["source_record_type"] == "single_article"
    assert "automatic_source_access_blocked" not in result["readiness"]["blocking_reasons"]
    assert result["readiness"]["publication_ready"] is False


@pytest.mark.parametrize("city,record_type", [
    ("leipzig", "multi_event_bulletin"), ("bremen", "multi_announcement_archive_page"),
])
def test_offline_units_cannot_become_location_candidates(tmp_path, city, record_type):
    path = tmp_path / f"{city}.sqlite"
    db = connect(path)
    url = ("https://medienservice.sachsen.de/medien/news/1100200" if city == "leipzig" else
           "https://www.polizei.bremen.de/news/pressestelle/pressemeldungen-ab-11092026-69054")
    ident = "1100200" if city == "leipzig" else "69054"
    add_report(db, ident, url=url)
    record_city_scope(db, ident, "in_city", EVIDENCE, 3)
    db.execute(
        "CREATE TABLE offline_source_units (id TEXT PRIMARY KEY, publisher TEXT, "
        "record_type TEXT, manually_supplied INTEGER, publication_eligible INTEGER)"
    )
    db.execute("INSERT INTO offline_source_units VALUES (?,?,?,1,0)",
               (ident, "Synthetic Police", record_type))
    db.commit()
    db.close()
    result = audit_source(city, path)
    assert result["records"][0]["scope_status"] == "in_city"
    assert result["records"][0]["manually_supplied"] is True
    assert result["records"][0]["source_unit_publication_eligible"] is False
    assert result["records"][0]["location_candidate"] is False
    assert result["location_candidates"] == []
    if city == "leipzig":
        assert "automatic_source_access_blocked" in result["readiness"]["blocking_reasons"]
    else:
        assert "automatic_source_access_blocked" not in result["readiness"]["blocking_reasons"]
        assert "manual_source_authenticity_unverified" in result["readiness"]["blocking_reasons"]


def test_wrong_source_url_cannot_become_candidate(tmp_path):
    path = tmp_path / "dortmund.sqlite"
    db = connect(path)
    add_report(db, url="https://unrelated.example/presse/test-article")
    record_city_scope(db, "test-article", "in_city", EVIDENCE, 3)
    db.close()
    result = audit_source("dortmund", path)
    assert result["records"][0]["source_identity_valid"] is False
    assert result["location_candidates"] == []
    assert "source_identity_invalid" in result["readiness"]["blocking_reasons"]


def test_unmatched_city_evidence_cannot_become_candidate(tmp_path):
    path = tmp_path / "dortmund.sqlite"
    db = connect(path)
    add_report(db)
    record_city_scope(db, "test-article", "in_city", EVIDENCE, 3)
    db.execute("UPDATE city_scope_decisions SET evidence=? WHERE id='test-article'",
               ("Synthetische Behauptung ohne Zitat aus dem Originalbericht.",))
    db.commit()
    db.close()
    result = audit_source("dortmund", path)
    assert result["records"][0]["scope_status"] == "invalid_evidence"
    assert result["location_candidates"] == []


def test_missing_or_unknown_local_source_fails_closed(tmp_path):
    result = audit_source("leipzig", tmp_path / "missing.sqlite")
    assert result["records"] == [] and result["location_candidates"] == []
    assert "local_database_missing" in result["readiness"]["blocking_reasons"]
    with pytest.raises(ValueError, match="Unknown city"):
        audit_source("not-a-city", tmp_path / "missing.sqlite")
