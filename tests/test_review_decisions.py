import hashlib
import json

import pytest

from crimemapsde_cities_06_10.review_decisions import (
    current_supported_decisions,
    import_decisions,
    review_summary,
)
from crimemapsde_cities_06_10.storage import accept, connect, discover

CITY = "dortmund"
IDENT = "synthetic-source"
URL = "https://dortmund.polizei.nrw/presse/synthetic-source"
BODY = (
    "Am Nordmarkt in Dortmund wurde ein Mann beraubt. "
    "An der Münsterstraße beschädigte eine zweite Person ein Fahrzeug. "
    "Die Festnahme erfolgte später im Stadtteil Innenstadt-West."
)
QUOTE_ONE = "Am Nordmarkt in Dortmund wurde ein Mann beraubt."
QUOTE_TWO = "An der Münsterstraße beschädigte eine zweite Person ein Fahrzeug."
QUOTE_THREE = "Die Festnahme erfolgte später im Stadtteil Innenstadt-West."


def add_source(db, ident=IDENT, url=URL, body=BODY):
    discover(
        db,
        [
            {
                "id": ident,
                "url": url,
                "title": "Synthetische Meldung",
                "published": "2026-09-29T12:00:00+02:00",
                "district": "",
            }
        ],
        1,
    )
    accept(db, ident, body, {}, 2)


def decision_files(tmp_path, *, ident=IDENT, url=URL, body=BODY):
    digest = hashlib.sha256(body.encode()).hexdigest()
    identity = {
        "schema_version": 1,
        "city": CITY,
        "source_id": ident,
        "source_url": url,
        "source_sha256": digest,
    }
    review = {
        **identity,
        "verdict": "supported",
        "evidence_quotes": [QUOTE_ONE],
        "review_note": "The full source was read and the explicit incidents were inventoried.",
        "reviewer": "source-first-llm-v1",
        "reviewed_at": "2026-09-29T12:30:00+02:00",
    }
    scope = {
        **identity,
        "scope_verdict": "in_city",
        "evidence_quotes": [QUOTE_ONE],
    }
    scenes = {
        "schema_version": 1,
        "city": CITY,
        "articles": [
            {
                **identity,
                "incident_count": 2,
                "incidents_complete": True,
                "formal_locations_complete": True,
                "incidents": [
                    {
                        "incident_id": f"{ident}:incident:1",
                        "evidence_quotes": [QUOTE_ONE],
                        "formal_location_ids": [f"{ident}:location:1"],
                    },
                    {
                        "incident_id": f"{ident}:incident:2",
                        "evidence_quotes": [QUOTE_TWO],
                        "formal_location_ids": [
                            f"{ident}:location:2",
                            f"{ident}:location:3",
                        ],
                    },
                ],
                "formal_locations": [
                    {
                        "location_id": f"{ident}:location:1",
                        "label": "Nordmarkt, Dortmund",
                        "role": "incident",
                        "precision": "place",
                        "city_scope": "in_city",
                        "evidence_quotes": [QUOTE_ONE],
                        "coordinates": [7.466, 51.518],
                    },
                    {
                        "location_id": f"{ident}:location:2",
                        "label": "Münsterstraße",
                        "role": "incident",
                        "precision": "street",
                        "city_scope": "in_city",
                        "evidence_quotes": [QUOTE_TWO],
                        "coordinates": None,
                    },
                    {
                        "location_id": f"{ident}:location:3",
                        "label": "Innenstadt-West",
                        "role": "arrest",
                        "precision": "district",
                        "city_scope": "in_city",
                        "evidence_quotes": [QUOTE_THREE],
                        "coordinates": None,
                    },
                ],
            }
        ],
    }
    review_path = tmp_path / "review-decisions.delta.ndjson"
    scope_path = tmp_path / "scope-decisions.delta.ndjson"
    scene_path = tmp_path / "scene-decisions.delta.json"

    def write():
        review_path.write_text(json.dumps(review, ensure_ascii=False) + "\n", encoding="utf-8")
        scope_path.write_text(json.dumps(scope, ensure_ascii=False) + "\n", encoding="utf-8")
        scene_path.write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")

    write()
    return review, scope, scenes, write, review_path, scope_path, scene_path


def run_import(db, files, imported_at=3):
    *_, review_path, scope_path, scene_path = files
    return import_decisions(
        db,
        city=CITY,
        review_path=review_path,
        scope_path=scope_path,
        scene_path=scene_path,
        imported_at=imported_at,
    )


def test_imports_hash_bound_multiple_incidents_and_locations(tmp_path):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    result = run_import(db, files)
    assert result["validated"] == 1
    assert (result["inserted"], result["changed"], result["unchanged"]) == (1, 0, 0)
    assert result["review_counts"] == {
        "supported": 1,
        "needs_correction": 0,
        "uncertain": 0,
        "pending": 0,
        "stale": 0,
    }
    assert result["all_current_reviews_supported"] is True
    assert result["owner_approval_required"] is True
    assert result["owner_approved"] is False and result["publication_ready"] is False
    accepted = current_supported_decisions(db, CITY)
    assert len(accepted) == 1
    assert accepted[0]["scene_inventory"]["incident_count"] == 2
    assert len(accepted[0]["scene_inventory"]["formal_locations"]) == 3
    repeated = run_import(db, files, imported_at=4)
    assert (repeated["inserted"], repeated["changed"], repeated["unchanged"]) == (0, 0, 1)
    assert db.execute("SELECT count(*) FROM llm_review_history").fetchone()[0] == 1
    db.close()


def test_explicit_zero_incident_and_empty_location_inventory_is_allowed(tmp_path):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    _, scope, scenes, write, *_ = files
    scope["scope_verdict"] = "out_of_city"
    article = scenes["articles"][0]
    article["incident_count"] = 0
    article["incidents"] = []
    article["formal_locations"] = []
    write()
    result = run_import(db, files)
    assert result["review_counts"]["supported"] == 1
    decision = current_supported_decisions(db, CITY)[0]
    assert decision["scene_inventory"]["incidents"] == []
    assert decision["scene_inventory"]["formal_locations_complete"] is True
    db.close()


def test_semantic_supplement_preserves_unknown_time_and_explicit_poi_assessments(tmp_path):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    run_import(db, files)
    legacy = current_supported_decisions(db, CITY)[0]
    _, _, scenes, write, *_ = files
    article = scenes["articles"][0]
    article["incidents"][0].update(
        event_time={"display": "Original offence time not disclosed", "date": None,
                    "precision": "unknown", "evidence_quote": QUOTE_ONE},
        details="Robbery at Nordmarkt; later arrest remains a separate location role.",
    )
    article["formal_locations"][0]["poi_contexts"] = [
        {"kind": "public_space", "scope": "named_object", "radius_m": 0,
         "evidence_quote": QUOTE_ONE}
    ]
    article["formal_locations"][1]["poi_contexts"] = []
    write()
    result = run_import(db, files, imported_at=4)
    assert (result["changed"], result["unchanged"]) == (1, 0)
    current = current_supported_decisions(db, CITY)[0]
    assert current["source_sha256"] == legacy["source_sha256"]
    inventory = current["scene_inventory"]
    assert inventory["incidents"][0]["event_time"]["date"] is None
    assert inventory["incidents"][0]["event_time"]["precision"] == "unknown"
    assert inventory["incidents"][0]["details"] == article["incidents"][0]["details"]
    assert inventory["formal_locations"][0]["poi_contexts"] == article["formal_locations"][0]["poi_contexts"]
    assert inventory["formal_locations"][1]["poi_contexts"] == []
    assert inventory["formal_locations"][2]["role"] == "arrest"
    assert "poi_contexts" not in inventory["formal_locations"][2]
    assert run_import(db, files, imported_at=5)["unchanged"] == 1
    assert db.execute("SELECT count(*) FROM llm_review_history").fetchone()[0] == 2
    assert result["owner_approved"] is False and result["publication_ready"] is False
    db.close()


@pytest.mark.parametrize("component", ["event_time", "poi_context"])
def test_invalid_supplement_evidence_cannot_replace_the_previous_review(tmp_path, component):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    run_import(db, files)
    prior = current_supported_decisions(db, CITY)
    _, _, scenes, write, *_ = files
    article = scenes["articles"][0]
    fabricated = "Invented location or time evidence absent from the original narrative."
    if component == "event_time":
        article["incidents"][0]["event_time"] = {
            "display": "Unknown", "date": None, "precision": "unknown",
            "evidence_quote": fabricated,
        }
    else:
        article["formal_locations"][0]["poi_contexts"] = [
            {"kind": "public_space", "scope": "named_object", "radius_m": 0,
             "evidence_quote": fabricated}
        ]
    write()
    with pytest.raises(ValueError, match="absent from the current source body"):
        run_import(db, files, imported_at=4)
    assert current_supported_decisions(db, CITY) == prior
    assert db.execute("SELECT count(*) FROM llm_review_history").fetchone()[0] == 1
    db.close()


@pytest.mark.parametrize("component", ["review", "scope", "incident", "location"])
def test_every_semantic_level_requires_a_verbatim_full_text_quote(tmp_path, component):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    review, scope, scenes, write, *_ = files
    bad_quote = "This invented quotation does not occur anywhere in the official source."
    if component == "review":
        review["evidence_quotes"] = [bad_quote]
    elif component == "scope":
        scope["evidence_quotes"] = [bad_quote]
    elif component == "incident":
        scenes["articles"][0]["incidents"][0]["evidence_quotes"] = [bad_quote]
    else:
        scenes["articles"][0]["formal_locations"][0]["evidence_quotes"] = [bad_quote]
    write()
    with pytest.raises(ValueError, match="absent from the current source body"):
        run_import(db, files)
    assert "llm_review_decisions" not in {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    db.close()


@pytest.mark.parametrize("failure", ["count", "completeness", "reference", "district_point"])
def test_structurally_incomplete_scene_inventories_are_rejected(tmp_path, failure):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    _, _, scenes, write, *_ = files
    article = scenes["articles"][0]
    if failure == "count":
        article["incident_count"] = 1
    elif failure == "completeness":
        article["formal_locations_complete"] = False
    elif failure == "reference":
        article["incidents"][0]["formal_location_ids"] = [f"{IDENT}:location:missing"]
    else:
        article["formal_locations"][2]["coordinates"] = [7.4, 51.5]
    write()
    with pytest.raises(ValueError):
        run_import(db, files)
    db.close()


@pytest.mark.parametrize("failure", ["city", "url", "hash", "set"])
def test_cross_file_or_source_identity_mismatches_are_rejected(tmp_path, failure):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    review, scope, scenes, write, *_ = files
    if failure == "city":
        review["city"] = "bremen"
    elif failure == "url":
        scope["source_url"] = "https://unrelated.example/wrong"
    elif failure == "hash":
        scenes["articles"][0]["source_sha256"] = "0" * 64
    else:
        scope["source_id"] = "another-source"
    write()
    with pytest.raises(ValueError):
        run_import(db, files)
    db.close()


def test_source_revision_expires_old_decision_and_rejects_old_bundle(tmp_path):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    imported = run_import(db, files)
    old_digest = imported["decision_set_digest"]
    accept(db, IDENT, BODY + " Der Bericht wurde nachträglich ergänzt.", {}, 4)
    summary = review_summary(db, CITY)
    assert summary["review_counts"]["stale"] == 1
    assert summary["all_current_reviews_supported"] is False
    assert summary["decision_set_digest"] != old_digest
    assert current_supported_decisions(db, CITY) == []
    with pytest.raises(ValueError, match="stale"):
        run_import(db, files)
    db.close()


def test_changed_decision_changes_digest_and_preserves_runtime_history(tmp_path):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    first = run_import(db, files)
    review, _, _, write, *_ = files
    review["review_note"] = "A second full-text review changed the documented rationale."
    write()
    second = run_import(db, files, imported_at=5)
    assert second["changed"] == 1 and second["inserted"] == 0
    assert second["decision_set_digest"] != first["decision_set_digest"]
    assert second["owner_approved"] is False and second["publication_ready"] is False
    assert db.execute("SELECT count(*) FROM llm_review_history").fetchone()[0] == 2
    db.close()


def test_reviewed_moving_transit_route_roundtrip(tmp_path):
    db = connect(tmp_path / "sources.sqlite")
    route_quote = "Der Vorfall geschah in der fahrenden Stadtbahn U15."
    add_source(db, body=BODY + " " + route_quote)
    files = decision_files(tmp_path, body=BODY + " " + route_quote)
    _, _, scenes, write, *_ = files
    location = scenes["articles"][0]["formal_locations"][0]
    location.update(precision="route", coordinates=None, label="Moving U15; actual segment unknown",
                    evidence_quotes=[route_quote], transit_route={
                        "mode": "subway", "line": "U15", "extent": "full_line",
                        "evidence_quote": route_quote,
                    })
    write()
    assert run_import(db, files)["inserted"] == 1
    stored = current_supported_decisions(db, CITY)[0]["scene_inventory"]["formal_locations"][0]
    assert stored["transit_route"] == location["transit_route"]
    assert stored["coordinates"] is None
    assert run_import(db, files, imported_at=4)["unchanged"] == 1
    db.close()


@pytest.mark.parametrize("invalid", ["metadata", "coordinates", "evidence"])
def test_transit_route_rejects_omission_points_and_fabricated_evidence(tmp_path, invalid):
    db = connect(tmp_path / "sources.sqlite")
    add_source(db)
    files = decision_files(tmp_path)
    run_import(db, files)
    prior = current_supported_decisions(db, CITY)
    _, _, scenes, write, *_ = files
    location = scenes["articles"][0]["formal_locations"][0]
    location.update(precision="route", coordinates=None, transit_route={
        "mode": "subway", "line": "U15", "extent": "full_line", "evidence_quote": QUOTE_ONE,
    })
    if invalid == "metadata":
        location.pop("transit_route")
    elif invalid == "coordinates":
        location["coordinates"] = [7.4, 51.5]
    else:
        location["transit_route"]["evidence_quote"] = "Fabricated journey evidence."
    write()
    with pytest.raises(ValueError):
        run_import(db, files)
    assert current_supported_decisions(db, CITY) == prior
    assert db.execute("SELECT count(*) FROM llm_review_history").fetchone()[0] == 1
    db.close()
