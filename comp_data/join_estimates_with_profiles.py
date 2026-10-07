#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["opensearch-py", "requests", "boto3", "google-cloud-firestore"]
# ///
"""Join sampled compensation estimates with their profiles from OpenSearch.

    ./join_estimates_with_profiles.py comp_estimates_sample/sample_1000.jsonl

Input: the JSON lines written by ``export_comp_estimates.py`` (one Firestore estimate per line).
Output: ``<input>_with_profiles.jsonl``, one line per estimate:

    {"profile_id": ..., "opensearch_id": ... | null, "match": "id" | "id_field" | "twin" | null,
     "same_company_title": true | false | null, "estimate": {...}, "profile": {...} | null}

Estimates are keyed by whatever id the product had for the person, which is one of two things:
  - the OpenSearch profile id, when the estimate was requested from a search result, or
  - a ``sourcing_contacts`` document id (20 characters, a hash of org + profile id), when it was
    requested from a shortlist or the sidebar. That document's ``pdlId`` is the profile id.
So ids that are not found in OpenSearch directly are resolved through Firestore first; only the
``pdlId`` field of each contact is read.

Each profile id is then looked up three ways, stopping at the first that finds it:
  id        the OpenSearch document id
  id_field  the document's ``id`` field
  twin      ``twin_member_ids`` on the surviving profile, for profiles since merged into a twin

``same_company_title`` says whether the profile's current company and title still equal the ones
the estimate was made for; when they differ the profile has moved on since the estimate.

Only the profile fields listed in PROFILE_FIELDS are fetched (no contact details). Auth is the
same as ``common/sample_profiles_from_opensearch.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from sample_profiles_from_opensearch import DEFAULT_HOST, DEFAULT_INDEX, make_client  # noqa: E402

# What the compensation prompt's profile text is built from, plus the fields useful for slicing.
PROFILE_FIELDS = [
    "id",
    "linkedin_url",
    "job_company_name",
    "job_company_linkedin_url",
    "job_company_size",
    "job_company_industry",
    "job_title",
    "job_title_role",
    "job_title_sub_role",
    "job_title_levels",
    "job_summary",
    "job_start_date",
    "location_name",
    "location_locality",
    "location_region",
    "location_metro",
    "location_country",
    "inferred_years_experience",
    "total_experience_months",
    "current_tenure",
    "average_tenure",
    "is_twin_duplicate",
    "hidden",
    "experience.title",
    "experience.company.name",
    "experience.company.linkedin_url",
    "experience.company.size",
    "experience.company.industry",
    "experience.start_date",
    "experience.end_date",
    "experience.duration_months",
    "experience.is_primary",
    "experience.location_names",
    "experience.summary",
]
BATCH = 100


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def check_complete(resp: dict) -> None:
    shards = resp.get("_shards", {})
    if resp.get("timed_out") or shards.get("failed"):
        # A dropped shard would look like profiles that don't exist.
        sys.exit(f"Search returned partial results (timed_out={resp.get('timed_out')}, shards={shards}); aborting.")


def fetch_by_document_id(client, index: str, ids: list[str]) -> dict[str, dict]:
    found = {}
    for batch in chunks(ids, BATCH):
        resp = client.mget(index=index, body={"ids": batch}, _source_includes=PROFILE_FIELDS)
        for doc in resp["docs"]:
            if doc.get("found"):
                found[doc["_id"]] = doc.get("_source", {})
    return found


def fetch_by_field(client, index: str, field: str, ids: list[str], skip_twin_duplicates: bool) -> dict[str, dict]:
    """Profiles whose `field` holds one of the ids, keyed by the id that matched."""
    wanted = set(ids)
    matches: dict[str, list[dict]] = {}
    for batch in chunks(ids, BATCH):
        query = {"bool": {"filter": [{"terms": {field: batch}}]}}
        if skip_twin_duplicates:
            query["bool"]["must_not"] = [{"term": {"is_twin_duplicate": True}}]
        resp = client.search(
            index=index,
            body={"size": len(batch) * 5, "query": query, "_source": PROFILE_FIELDS + [field]},
        )
        check_complete(resp)
        for hit in resp["hits"]["hits"]:
            source = hit["_source"]
            values = source.get(field) or []
            for value in values if isinstance(values, list) else [values]:
                if value in wanted:
                    matches.setdefault(value, []).append(source)
    # An id claimed by more than one profile is ambiguous; leave it unmatched.
    return {value: sources[0] for value, sources in matches.items() if len(sources) == 1}


def find_profiles(client, index: str, ids: list[str]) -> dict[str, tuple[str, dict]]:
    """id -> (how it matched, profile) for every id found in OpenSearch."""
    profiles: dict[str, tuple[str, dict]] = {}
    lookups = [
        ("id", lambda missing: fetch_by_document_id(client, index, missing)),
        ("id_field", lambda missing: fetch_by_field(client, index, "id", missing, skip_twin_duplicates=False)),
        ("twin", lambda missing: fetch_by_field(client, index, "twin_member_ids", missing, skip_twin_duplicates=True)),
    ]
    for match, lookup in lookups:
        missing = [i for i in dict.fromkeys(ids) if i not in profiles]
        if not missing:
            break
        found = lookup(missing)
        profiles.update({i: (match, source) for i, source in found.items()})
        print(f"  by {match:<9} {len(found):>5,} of {len(missing):,} found")
    return profiles


def contact_profile_ids(project: str, contact_ids: list[str]) -> dict[str, str]:
    """sourcing_contacts id -> the profile id (``pdlId``) it points at."""
    from google.cloud import firestore

    db = firestore.Client(project=project)
    resolved = {}
    for batch in chunks(contact_ids, BATCH):
        refs = [db.collection("sourcing_contacts").document(i) for i in batch]
        for snap in db.get_all(refs, field_paths=["pdlId"]):
            pdl_id = snap.get("pdlId") if snap.exists else None
            if isinstance(pdl_id, str) and pdl_id:
                resolved[snap.id] = pdl_id
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("estimates", type=Path, help="JSON lines of sampled estimates")
    parser.add_argument("--out", type=Path, default=None, help="output file (default: <input>_with_profiles.jsonl)")
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--host", default=os.environ.get("OPENSEARCH_HOST", DEFAULT_HOST), help="OpenSearch domain endpoint")
    parser.add_argument("--region", default="us-east-1", help="AWS region, for SigV4 auth")
    parser.add_argument("--project", default="juicebox-ai-dev", help='Firebase project holding sourcing_contacts (default: "juicebox-ai-dev", i.e. prod)')
    args = parser.parse_args()

    with open(args.estimates, encoding="utf-8") as f:
        estimates = [json.loads(line) for line in f if line.strip()]
    ids = [e["profile_id"] for e in estimates]
    out_path = args.out or args.estimates.with_name(args.estimates.stem + "_with_profiles.jsonl")

    try:
        client = make_client(args.host, args.region)
    except ImportError as e:
        sys.exit(f"Missing dependency ({e.name}). Run the script with uv, which installs it automatically:\n  uv run {sys.argv[0]} ...")

    unique_ids = list(dict.fromkeys(ids))
    print(f"looking up {len(unique_ids):,} estimate ids in {args.index}")
    direct = find_profiles(client, args.index, unique_ids)

    # The rest are contact ids: resolve them to profile ids, then look those up.
    contact_ids = [i for i in unique_ids if i not in direct]
    opensearch_ids = {i: i for i in direct}
    if contact_ids:
        resolved = contact_profile_ids(args.project, contact_ids)
        print(f"resolved {len(resolved):,} of {len(contact_ids):,} remaining ids through sourcing_contacts ({args.project})")
        opensearch_ids.update(resolved)
        print(f"looking up {len(set(resolved.values())):,} resolved profile ids in {args.index}")
        via_contact = find_profiles(client, args.index, list(dict.fromkeys(resolved.values())))
    else:
        via_contact = {}

    def profile_for(estimate_id: str) -> tuple[str | None, dict | None]:
        if estimate_id in direct:
            return direct[estimate_id]
        return via_contact.get(opensearch_ids.get(estimate_id), (None, None))

    matches, same = Counter(), Counter()
    with open(out_path, "w", encoding="utf-8") as out:
        for estimate in estimates:
            match, profile = profile_for(estimate["profile_id"])
            same_input = None
            if profile is not None:
                # Copy: one profile can back several estimates (the same person under two orgs).
                profile = {k: v for k, v in profile.items() if k != "twin_member_ids"}
                same_input = (
                    profile.get("job_company_name") == estimate.get("company")
                    and profile.get("job_title") == estimate.get("title")
                )
            matches[match] += 1
            same[same_input] += 1
            row = {
                "profile_id": estimate["profile_id"],
                "opensearch_id": opensearch_ids.get(estimate["profile_id"]),
                "match": match,
                "same_company_title": same_input,
                "estimate": estimate,
                "profile": profile,
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    total = len(estimates)
    print(f"\n{total - matches[None]:,} of {total:,} estimates joined to a profile ({matches[None]:,} not found)")
    print(f"  same company and title as the estimate: {same[True]:,}")
    print(f"  company or title has changed:           {same[False]:,}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
