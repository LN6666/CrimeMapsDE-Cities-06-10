"""Read-only, body-free source manifest for downstream review and intake."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .registry import CITIES

SCHEMA_VERSION = 1
OFFLINE_CITIES = frozenset({"leipzig"})
ARTICLE_ORIGINS = {
    "dusseldorf": (("www.presseportal.de", re.compile(r"/blaulicht/pm/13248/(\d+)$")),),
    "stuttgart": (("www.presseportal.de", re.compile(r"/blaulicht/pm/110977/(\d+)$")),),
    "leipzig": (("medienservice.sachsen.de", re.compile(r"/medien/news/(\d+)$")),),
    "dortmund": (("dortmund.polizei.nrw", re.compile(r"/presse/([a-z0-9][a-z0-9-]+)$")),),
    "bremen": (
        ("www.presseportal.de", re.compile(r"/blaulicht/pm/35235/(\d+)$")),
        ("www.polizei.bremen.de", re.compile(
            r"/news/pressestelle/pressemeldungen-ab-[a-z0-9-]+-(\d+)$"
        )),
    ),
}


def _source_identity_valid(city: str, ident: str, url: str) -> bool:
    if not isinstance(ident, str) or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if city == "dortmund" and parsed.path == "/presse/pressemitteilungen":
        return False
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        return False
    for host, pattern in ARTICLE_ORIGINS[city]:
        match = pattern.fullmatch(parsed.path)
        if parsed.netloc == host and match and match[1] == ident:
            return True
    return False


def _source_date_valid(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _scope_rows(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    if not _has_table(db, "city_scope_decisions"):
        return {}
    return {row["id"]: row for row in db.execute(
        "SELECT id,sha256,verdict,evidence,reviewed FROM city_scope_decisions"
    )}


def _offline_rows(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    if not _has_table(db, "offline_source_units"):
        return {}
    return {row["id"]: row for row in db.execute(
        "SELECT id,publisher,record_type,manually_supplied,publication_eligible "
        "FROM offline_source_units"
    )}


def _dortmund_ids(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    if not _has_table(db, "dortmund_source_ids"):
        return {}
    return {row["id"]: row for row in db.execute(
        "SELECT id,native_node_id,police_number FROM dortmund_source_ids"
    )}


def _checkpoint_scans(db: sqlite3.Connection, city: str) -> list[dict]:
    table = {"dusseldorf": "dusseldorf_archive_cursor",
             "stuttgart": "stuttgart_archive_cursor",
             "dortmund": "dortmund_archive_cursor",
             "bremen": "bremen_archive_cursor"}.get(city)
    if table is None or not _has_table(db, table):
        return []
    complete = "historical_scan_complete" if city == "stuttgart" else "complete"
    return [{"year": row["year"], "pages_scanned": row["pages_scanned"],
             "historical_scan_complete": bool(row["done"])} for row in db.execute(
                 f"SELECT year,pages_scanned,{complete} AS done FROM {table} ORDER BY year"
             )]


def _record(row: sqlite3.Row, *, city: str, scope: sqlite3.Row | None,
            offline: sqlite3.Row | None, native: sqlite3.Row | None) -> dict:
    body = row["body"]
    digest = row["sha256"]
    body_available = isinstance(body, str) and bool(body.strip())
    body_hash_valid = bool(
        body_available and digest and hashlib.sha256(body.encode("utf-8")).hexdigest() == digest
    )
    scope_status = "pending"
    scope_evidence_valid = False
    if scope is not None:
        if not body_hash_valid or scope["sha256"] != digest:
            scope_status = "stale"
        elif scope["verdict"] in {"in_city", "out_of_city", "uncertain"}:
            raw_evidence = scope["evidence"]
            evidence = " ".join(raw_evidence.split()) if isinstance(raw_evidence, str) else ""
            scope_evidence_valid = len(evidence) >= 20 and evidence.casefold() in body.casefold()
            scope_status = scope["verdict"] if scope_evidence_valid else "invalid_evidence"
        else:
            scope_status = "invalid_verdict"
    record_type = offline["record_type"] if offline else (
        "unknown_offline_unit" if city in OFFLINE_CITIES else "single_article"
    )
    manual_source = city in OFFLINE_CITIES or offline is not None
    unit_eligible = bool(offline and offline["publication_eligible"]) if manual_source else True
    source_identity_valid = _source_identity_valid(city, row["id"], row["url"])
    source_date_valid = _source_date_valid(row["published"])
    revision_valid = isinstance(row["revision"], int) and row["revision"] >= 1
    location_candidate = bool(
        source_identity_valid and source_date_valid and revision_valid and body_hash_valid
        and scope_status == "in_city" and scope_evidence_valid
        and record_type == "single_article" and unit_eligible and not manual_source
    )
    return {
        "source_id": row["id"],
        "source_url": row["url"],
        "source_date": row["published"],
        "source_date_valid": source_date_valid,
        "source_sha256": digest,
        "source_identity_valid": source_identity_valid,
        "revision": row["revision"],
        "revision_valid": revision_valid,
        "source_record_type": record_type,
        "source_publisher": offline["publisher"] if offline else None,
        "manually_supplied": bool(offline["manually_supplied"]) if offline else manual_source,
        "source_unit_publication_eligible": bool(offline["publication_eligible"]) if offline else None,
        "native_node_id": native["native_node_id"] if native else None,
        "police_number": native["police_number"] if native else None,
        "body_available": body_available,
        "body_hash_valid": body_hash_valid,
        "scope_status": scope_status,
        "scope_evidence_valid": scope_evidence_valid,
        "scope_reviewed_at": scope["reviewed"] if scope else None,
        "review_status": "pending",
        "source_error_present": bool(row["error"]),
        "location_candidate": location_candidate,
    }


def audit_source(city: str, db_path: str | Path | None = None) -> dict:
    """Inspect a local checkpoint without modifying it or exposing report text.

    A location candidate is only a reference for later source-backed review. It
    has no coordinates or publication authority. Offline multi-event units can
    never become candidates through this manifest.
    """
    if city not in CITIES:
        raise ValueError(f"Unknown city slug: {city}")
    spec = CITIES[city]
    path = Path(db_path or spec.default_db)
    records: list[dict] = []
    scans: list[dict] = []
    reasons = {
        "archive_coverage_unverified", "source_verification_pending",
        "article_review_pending", "owner_batch_approval_missing", "publication_pipeline_missing",
    }
    if city in OFFLINE_CITIES:
        reasons.update({"automatic_source_access_blocked", "manual_source_authenticity_unverified",
                        "multi_event_units_unsplit"})
    if not path.is_file():
        reasons.add("local_database_missing")
    else:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                if not _has_table(db, "reports"):
                    reasons.add("local_database_incompatible")
                else:
                    scopes = _scope_rows(db)
                    offline = _offline_rows(db)
                    native = _dortmund_ids(db) if city == "dortmund" else {}
                    scans = _checkpoint_scans(db, city)
                    records = [_record(row, city=city, scope=scopes.get(row["id"]),
                                       offline=offline.get(row["id"]), native=native.get(row["id"]))
                               for row in db.execute(
                                   "SELECT id,url,published,body,sha256,revision,error "
                                   "FROM reports ORDER BY published,id"
                               )]
        except sqlite3.DatabaseError:
            records = []
            scans = []
            reasons.add("local_database_incompatible")
    if any(not row["body_available"] for row in records):
        reasons.add("source_body_missing")
    if any(row["body_available"] and not row["body_hash_valid"] for row in records):
        reasons.add("source_hash_mismatch")
    if any(not row["source_identity_valid"] for row in records):
        reasons.add("source_identity_invalid")
    if any(not row["source_date_valid"] or not row["revision_valid"] for row in records
           if row["body_available"]):
        reasons.add("source_metadata_invalid")
    if any(row["scope_status"] in {"pending", "stale", "invalid_evidence", "invalid_verdict"}
           for row in records):
        reasons.add("city_scope_review_pending")
    if any(row["source_error_present"] for row in records):
        reasons.add("source_fetch_errors_present")
    if any(row["manually_supplied"] for row in records):
        reasons.add("manual_source_authenticity_unverified")
    if any(row["source_record_type"] != "single_article" for row in records):
        reasons.add("multi_event_units_unsplit")
    candidates = [
        {key: row[key] for key in (
            "source_id", "source_url", "source_date", "source_sha256", "revision",
            "scope_status", "review_status", "native_node_id", "police_number",
        )} for row in records if row["location_candidate"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "city": spec.export(),
        "readiness": {
            "archive_complete": False,
            "source_verified": False,
            "publication_ready": False,
            "blocking_reasons": sorted(reasons),
        },
        "checkpoint_scans": scans,
        "counts": {"source_records": len(records), "location_candidates": len(candidates)},
        "records": records,
        "location_candidates": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", choices=sorted(CITIES), required=True)
    parser.add_argument("--db", type=Path, help="Local checkpoint; defaults to the city registry path")
    parser.add_argument("--out", type=Path, help="Write the body-free JSON manifest here")
    args = parser.parse_args()
    payload = json.dumps(audit_source(args.city, args.db), ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
