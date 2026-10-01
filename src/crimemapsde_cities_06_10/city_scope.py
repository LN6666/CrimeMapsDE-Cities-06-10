"""Source-bound city-boundary decisions shared by the new city collectors."""

from __future__ import annotations

import re
import sqlite3


def ensure_scope_table(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS city_scope_decisions (
             id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, verdict TEXT NOT NULL,
             evidence TEXT NOT NULL, reviewed REAL NOT NULL,
             FOREIGN KEY(id) REFERENCES reports(id))"""
    )
    db.commit()


def record_city_scope(
    db: sqlite3.Connection, ident: str, verdict: str, evidence: str, now: float,
) -> None:
    """Require a quote from the current body; mixed-city reports stay uncertain.

    The reviewer must decide whether the quoted passage names an incident scene.
    An outlet label, police headquarters, suspect residence, or dateline is not
    proof of city scope.
    """
    if verdict not in {"in_city", "out_of_city", "uncertain"}:
        raise ValueError("Invalid city-scope verdict")
    evidence = " ".join(evidence.split())
    if len(evidence) < 20:
        raise ValueError("City-scope decision requires specific source evidence")
    row = db.execute("SELECT sha256,body FROM reports WHERE id=?", (ident,)).fetchone()
    if row is None or row["sha256"] is None or row["body"] is None:
        raise ValueError("City-scope decision requires a fetched article")
    if evidence.casefold() not in " ".join(row["body"].split()).casefold():
        raise ValueError("City-scope evidence must quote the fetched body")
    if verdict == "in_city" and re.fullmatch(
        r"[\wÄÖÜäöüß -]+\s*\(ots\).*", evidence, re.IGNORECASE,
    ):
        raise ValueError("Publisher dateline is not incident-scene evidence")
    ensure_scope_table(db)
    db.execute(
        """INSERT INTO city_scope_decisions(id,sha256,verdict,evidence,reviewed)
           VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sha256=excluded.sha256,
           verdict=excluded.verdict,evidence=excluded.evidence,reviewed=excluded.reviewed""",
        (ident, row["sha256"], verdict, evidence, now),
    )
    db.commit()


def city_only_reports(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return only rows approved for city scope against their present source hash."""
    ensure_scope_table(db)
    return db.execute(
        """SELECT r.* FROM reports AS r JOIN city_scope_decisions AS s ON s.id=r.id
           WHERE r.body IS NOT NULL AND s.sha256=r.sha256 AND s.verdict='in_city'
           ORDER BY r.published,r.id"""
    ).fetchall()


def scope_counts(db: sqlite3.Connection) -> dict[str, int]:
    ensure_scope_table(db)
    counts = {row["verdict"]: row["n"] for row in db.execute(
        """SELECT s.verdict,count(*) AS n FROM city_scope_decisions AS s
           JOIN reports AS r ON r.id=s.id AND r.sha256=s.sha256
           WHERE r.body IS NOT NULL GROUP BY s.verdict"""
    )}
    stored = db.execute("SELECT count(*) FROM reports WHERE body IS NOT NULL").fetchone()[0]
    return {"in_city": counts.get("in_city", 0),
            "out_of_city": counts.get("out_of_city", 0),
            "uncertain": counts.get("uncertain", 0),
            "pending": stored - sum(counts.values())}
