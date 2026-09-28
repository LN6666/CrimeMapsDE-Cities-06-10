"""Fail-closed Leipzig source entrypoint with a local-only bulletin checkpoint.

The Saxon police site directs readers to Medienservice Sachsen. Both hosts
currently return 404 for robots.txt; this adapter will not crawl them. A
manually supplied official bulletin is kept whole for later subcase review.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .offline_source import ingest_jsonl, probe_robots

ORIGIN = "https://medienservice.sachsen.de"
ARCHIVE = ORIGIN + "/medien/?search%5Binstitution_ids%5D%5B%5D=10976"
ARTICLE_PATH = re.compile(r"/medien/news/(\d+)$")
PUBLISHER = "Polizeidirektion Leipzig"
USER_AGENT = "CrimeMapsDE-Cities-06-10/0.1 (Leipzig official media archive)"


def sync_offline(db_path: str | Path, input_path: str | Path, *, limit: int = 10) -> dict:
    return ingest_jsonl(
        db_path, input_path, publisher=PUBLISHER, host="medienservice.sachsen.de",
        article_path=ARTICLE_PATH, record_type="multi_event_bulletin", limit=limit,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".runtime/cities/leipzig/police.sqlite")
    parser.add_argument("--input", help="Manually saved official JSONL in an ignored local directory")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--probe-robots", action="store_true")
    args = parser.parse_args()
    if args.probe_robots:
        probe_robots(ORIGIN, ARCHIVE, USER_AGENT)
        print("Robots rules verified; automated article intake is not implemented")
        return
    if not args.input:
        parser.error("Provide --input for local source units or --probe-robots")
    if args.limit < 1 or args.limit > 100:
        parser.error("Use 1-100 local records")
    import fcntl

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = sync_offline(args.db, args.input, limit=args.limit)
    print(json.dumps(result, indent=2))
    raise SystemExit(2)  # A local, unreviewed subset is never publication-ready.


if __name__ == "__main__":
    main()
