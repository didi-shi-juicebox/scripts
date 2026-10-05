#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-cloud-firestore"]
# ///
"""Download a whole Firestore collection as gzipped JSON lines and report its record count.

    ./download_firestore_collection.py                 # download, then confirm the count
    ./download_firestore_collection.py --count-only    # just ask Firestore for the count

Defaults to the ``profile_compensation_estimates`` collection in "Juicebox AI Prod".
Naming gotcha: that project's id is ``juicebox-ai-dev`` (``juicebox-ai`` is dev).

Auth uses Application Default Credentials: run ``gcloud auth application-default login`` once.

The output has one JSON object per line, in the same shape as the existing export: ``doc_id``,
``profile_id``, the known fields, and any unexpected fields under ``extra``. It is written to a
temporary file and moved into place at the end, so a failed run never leaves a partial file.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

FIELDS = ["id", "company", "title", "totalMin", "totalMax", "explanation", "referenceData", "updatedAt"]


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def to_record(doc_id: str, data: dict) -> dict:
    record = {"doc_id": doc_id, "profile_id": data.get("id") or doc_id}
    record.update({k: data.get(k) for k in FIELDS})
    extra = {k: v for k, v in data.items() if k not in FIELDS}
    if extra:
        record["extra"] = extra  # keep anything unexpected rather than drop it
    return record


def server_count(coll) -> int:
    """Count documents with a server-side aggregation, without downloading them."""
    return int(coll.count().get()[0][0].value)


def download(coll, out_path: Path, page_size: int) -> int:
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    started = time.monotonic()
    downloaded = 0
    cursor = None
    with gzip.open(tmp_path, "wt", encoding="utf-8") as out:
        # Page by document id so no single request has to stream the whole collection.
        while True:
            query = coll.order_by("__name__").limit(page_size)
            if cursor is not None:
                query = query.start_after(cursor)
            page = list(query.stream())
            if not page:
                break
            for snap in page:
                record = to_record(snap.id, snap.to_dict() or {})
                out.write(json.dumps(record, default=_jsonable, ensure_ascii=False) + "\n")
            cursor = page[-1]
            downloaded += len(page)
            rate = downloaded / max(time.monotonic() - started, 1e-6)
            print(f"\rdownloaded {downloaded:,} docs ({rate:,.0f}/s)", end="", flush=True)
    print()
    os.replace(tmp_path, out_path)
    return downloaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="juicebox-ai-dev", help='Firebase project id (default: "juicebox-ai-dev", i.e. Juicebox AI Prod)')
    parser.add_argument("--collection", default="profile_compensation_estimates")
    parser.add_argument("--out", type=Path, default=None, help="output file (default: <collection>.jsonl.gz next to this script)")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--count-only", action="store_true", help="print the collection's record count and exit")
    args = parser.parse_args()

    try:
        from google.cloud import firestore
    except ImportError:
        sys.exit(
            "google-cloud-firestore is not installed in this Python environment.\n"
            f"Run the script with uv, which installs it automatically:\n  uv run {sys.argv[0]} ...\n"
            "or install it yourself: pip install google-cloud-firestore"
        )

    coll = firestore.Client(project=args.project).collection(args.collection)
    print(f"Firebase project: {args.project} | collection: {args.collection}")

    count_before = server_count(coll)
    print(f"Firestore count: {count_before:,}")
    if args.count_only:
        return 0

    out_path = args.out or Path(__file__).resolve().parent / f"{args.collection}.jsonl.gz"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded = download(coll, out_path, args.page_size)
    count_after = server_count(coll)

    print(f"Downloaded {downloaded:,} records -> {out_path} ({out_path.stat().st_size / 1e6:,.1f} MB)")
    if downloaded == count_after:
        print(f"OK: downloaded count matches Firestore count ({count_after:,})")
        return 0
    # The collection is live, so a small drift during the download is expected.
    print(f"MISMATCH: downloaded {downloaded:,}, Firestore count was {count_before:,} before and {count_after:,} after", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
