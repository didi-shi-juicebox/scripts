"""Randomly sample juicebox-ai's ``profile_compensation_estimates`` Firestore collection.

juicebox-ai caches every grounded compensation estimate here (``utils/compensation/cache.js``):
one document per profile id, with the company/title it was made for, the estimated range,
the LLM explanation and the 10 reference rows offered to the LLM. These are real production
requests, so a random sample of them is the raw material for the validation and test sets.

    pip install google-cloud-firestore
    gcloud auth application-default login
    python scripts/export_comp_estimates.py --project juicebox-ai-dev --n 1000     # prod ("juicebox-ai" is dev)

Sampling without reading the whole collection: Firestore has no random query, but the
document ids are profile ids, which are effectively random strings. Each probe draws a
random id and reads the first document at or after it (one read per probe), so N samples
cost about N reads. A document that follows a large gap in the id space is somewhat more
likely to be picked, but gaps are unrelated to the profiles themselves, so this doesn't skew
the sample toward any kind of profile.

Outputs (in --out-dir, default ./comp_estimates_sample):
  sample_<N>.jsonl               the sampled documents (full records)
  sample_<N>_profile_ids.csv     profile_id,company,title,updatedAt (for the POC's profile_ids_source)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import string
import time
from datetime import datetime

FIELDS = ["id", "company", "title", "totalMin", "totalMax", "explanation", "referenceData", "updatedAt"]
# Characters profile ids are made of (Firestore auto-ids, PDL-style ids), in Firestore's byte order.
ID_ALPHABET = "".join(sorted("-_" + string.digits + string.ascii_letters))


def _jsonable(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def to_record(doc_id: str, data: dict) -> dict:
    record = {"doc_id": doc_id, "profile_id": data.get("id") or doc_id}
    record.update({k: data.get(k) for k in FIELDS})
    extra = {k: v for k, v in data.items() if k not in FIELDS}
    if extra:
        record["extra"] = extra  # keep anything unexpected rather than drop it
    return record


def firestore_lookup(project: str, collection: str):
    """``key -> (doc_id, data)`` of the first document whose id is >= key, wrapping past the end."""
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    coll = firestore.Client(project=project).collection(collection)

    def first_at_or_after(key: str):
        query = coll.where(filter=FieldFilter("__name__", ">=", coll.document(key))).order_by("__name__").limit(1)
        page = list(query.stream()) or list(coll.order_by("__name__").limit(1).stream())
        return (page[0].id, page[0].to_dict() or {}) if page else None

    return first_at_or_after


def random_sample(first_at_or_after, n: int, seed: int, since: str | None = None,
                  max_probes: int | None = None) -> tuple[list[dict], int]:
    rng = random.Random(seed)
    max_probes = max_probes or n * 20
    seen, records, probes = set(), [], 0
    started = time.monotonic()
    while len(records) < n and probes < max_probes:
        probes += 1
        hit = first_at_or_after("".join(rng.choice(ID_ALPHABET) for _ in range(8)))
        if hit is None or hit[0] in seen:
            continue
        doc_id, data = hit
        seen.add(doc_id)
        record = to_record(doc_id, data)
        if since and (record.get("updatedAt") or "") < since:
            continue
        records.append(record)
        if len(records) % 50 == 0:
            print(f"\r{len(records):,}/{n:,} sampled ({probes:,} probes, {time.monotonic() - started:.0f}s)",
                  end="", flush=True)
    print()
    return records, probes


def write_outputs(records: list[dict], out_dir: str, n: int) -> None:
    os.makedirs(out_dir, exist_ok=True)
    jsonl_path = os.path.join(out_dir, f"sample_{n}.jsonl")
    csv_path = os.path.join(out_dir, f"sample_{n}_profile_ids.csv")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, default=_jsonable, ensure_ascii=False) + "\n")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["profile_id", "company", "title", "updatedAt"])
        for record in records:
            writer.writerow([record["profile_id"], record.get("company"), record.get("title"),
                             _jsonable(record.get("updatedAt")) if record.get("updatedAt") else ""])
    print(f"Wrote {len(records):,} records:\n  {jsonl_path}\n  {csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help='Firebase project: "juicebox-ai-dev" (prod) or "juicebox-ai" (dev)')
    parser.add_argument("--collection", default="profile_compensation_estimates")
    parser.add_argument("--n", type=int, default=1000, help="number of documents to sample")
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--since", default=None, help="ISO date, e.g. 2026-08-10: keep only estimates updated since")
    parser.add_argument("--out-dir", default="comp_estimates_sample")
    args = parser.parse_args()

    print(f"Firebase project: {args.project} | collection: {args.collection} | n={args.n} seed={args.seed}"
          + (f" since={args.since}" if args.since else ""))
    records, probes = random_sample(firestore_lookup(args.project, args.collection), args.n, args.seed, args.since)
    if len(records) < args.n:
        print(f"Warning: only {len(records)} documents after {probes} probes (collection smaller than n, "
              "or --since too restrictive)")
    write_outputs(records, args.out_dir, args.n)


if __name__ == "__main__":
    main()
