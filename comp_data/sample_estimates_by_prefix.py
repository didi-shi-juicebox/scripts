#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-cloud-firestore"]
# ///
"""Sample a large share of ``profile_compensation_estimates`` with few requests.

    ./sample_estimates_by_prefix.py --target 1000000

Document ids are effectively random strings, so every document whose id starts with a
randomly chosen two-character prefix is a random slice of the collection. The script
shuffles all prefixes (seeded), reads whole prefixes in id-ordered pages, and stops after
the first prefix that takes the total past --target. A whole prefix is always finished,
so the sample is not biased toward the start of a prefix.

Kind to production: one request per --page-size documents, a pause between requests, and
only the small fields listed in FIELDS are fetched (not the explanation or reference rows).

Defaults to "Juicebox AI Prod", whose project id is ``juicebox-ai-dev`` (``juicebox-ai`` is dev).
Resumable: rerun the same command and finished prefixes are skipped.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import string
import sys
import time
from datetime import datetime
from pathlib import Path

FIELDS = ["id", "company", "title", "totalMin", "totalMax", "updatedAt"]
ID_ALPHABET = sorted("-_" + string.digits + string.ascii_letters)
AFTER_ALL_IDS = "~"  # sorts after every id character


def _jsonable(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def read_prefix(coll, prefix: str, page_size: int, pause: float):
    """Yield every document whose id starts with ``prefix``, a page at a time."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    base = (
        coll.where(filter=FieldFilter("__name__", ">=", coll.document(prefix)))
        .where(filter=FieldFilter("__name__", "<", coll.document(prefix + AFTER_ALL_IDS)))
        .order_by("__name__")
        .select(FIELDS)
        .limit(page_size)
    )
    last = None
    while True:
        page = list((base.start_after(last) if last is not None else base).stream())
        yield from page
        if len(page) < page_size:
            return
        last = page[-1]
        time.sleep(pause)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="juicebox-ai-dev", help='Firebase project id (default: "juicebox-ai-dev", i.e. prod)')
    parser.add_argument("--collection", default="profile_compensation_estimates")
    parser.add_argument("--target", type=int, required=True, help="stop after the prefix that takes the total past this")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "comp_estimates_sample" / "sample_by_prefix.jsonl.gz")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--pause", type=float, default=0.3, help="seconds to wait between requests")
    parser.add_argument("--seed", default="comp-prefix-1")
    args = parser.parse_args()

    from google.cloud import firestore

    coll = firestore.Client(project=args.project).collection(args.collection)
    prefixes = [a + b for a in ID_ALPHABET for b in ID_ALPHABET]
    random.Random(args.seed).shuffle(prefixes)

    state_path = args.out.with_name(args.out.name + ".state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {"done": [], "count": 0}
    if state["done"] and not args.out.exists():
        sys.exit(f"{state_path} lists finished prefixes but {args.out} is missing; delete the state file to start over.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Firebase project: {args.project} | collection: {args.collection} | target: {args.target:,}")

    started = time.monotonic()
    for prefix in prefixes:
        if state["count"] >= args.target:
            break
        if prefix in state["done"]:
            continue
        lines = [
            json.dumps({"doc_id": snap.id, **{k: (snap.to_dict() or {}).get(k) for k in FIELDS}},
                       default=_jsonable, ensure_ascii=False)
            for snap in read_prefix(coll, prefix, args.page_size, args.pause)
        ]
        # Written only once the whole prefix is in hand, so a crash never leaves half a prefix.
        with gzip.open(args.out, "at", encoding="utf-8") as out:
            out.writelines(line + "\n" for line in lines)
        state["done"].append(prefix)
        state["count"] += len(lines)
        state_path.write_text(json.dumps(state))
        print(f"{state['count']:>9,} records | {len(state['done']):>4} prefixes | {time.monotonic() - started:,.0f}s", flush=True)
        time.sleep(args.pause)

    share = len(state["done"]) / len(prefixes)
    print(f"Done: {state['count']:,} records from {len(state['done'])} of {len(prefixes)} prefixes ({share:.1%} of the id space) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
