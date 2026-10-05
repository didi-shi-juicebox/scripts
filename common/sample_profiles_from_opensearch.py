#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["opensearch-py", "requests", "boto3"]
# ///
"""Sample a diverse test/validation set of profiles from an OpenSearch profile index.

    ./sample_profiles_from_opensearch.py --size 1000 --out profile_sample_1000.csv
    ./sample_profiles_from_opensearch.py --size 1000 --dry-run     # print the query, contact nothing

Defaults to ``main-index-v33`` on the ``people-data-domain-v5`` domain.

How it works
  1. Candidate pool. Draw a uniform random pool of profiles (default 20x the sample size) with
     ``random_score`` queries. Only the handful of fields used for stratification are fetched,
     from doc values, so the big profile documents are never loaded.
  2. Balanced selection. Pick --size profiles from the pool so that every dimension (role, level,
     years of experience, country, company size by default) is spread across its values, with
     at most --max-per-company profiles per company.
  3. Write a CSV (profile_id, company, title, then the stratification fields) and print, for each
     dimension, the population share next to the sample share.

--balance sets how hard the sample is pushed away from the natural distribution: 0 keeps each
value's population share (a plain random sample), 1 gives every value an equal share, and the
default 0.5 targets the square root of the population share, which lifts rare values without
drowning out the common ones. Population shares are estimated from the pool.

Cost: each pool request scores every matching profile once (a full pass over the index), and
requests run one at a time. The default for --size 1000 is 4 requests. Use --pool-file to keep
the pool on disk and re-run the selection with different settings without querying again.

Auth: set OPENSEARCH_USERNAME and OPENSEARCH_PASSWORD for basic auth; otherwise requests are
signed with your default AWS credentials (SigV4).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_HOST = "search-people-data-domain-v5-wdt6ssypdvpoe26stzpghvwike.us-east-1.es.amazonaws.com"
DEFAULT_INDEX = "main-index-v33"
MAX_RESULT_WINDOW = 10_000
UNKNOWN = "unknown"

# Fields read from doc values for every pool candidate (all keyword/integer in the index mapping).
POOL_FIELDS = [
    "id",
    "linkedin_url",
    "job_company_name",
    "job_company_id",
    "job_company_size",
    "job_company_industry",
    "job_title",
    "job_title_role",
    "job_title_sub_role",
    "job_title_levels",
    "inferred_years_experience",
    "location_country",
    "location_region",
    "location_metro",
    "location_continent",
]

# A profile can carry several levels; stratify on the most senior one.
LEVEL_RANK = ["cxo", "owner", "partner", "vp", "director", "manager", "senior", "entry", "training", "unpaid"]
YOE_BUCKETS = [(2, "0-2"), (5, "3-5"), (10, "6-10"), (15, "11-15"), (20, "16-20")]


def primary_level(doc: dict) -> str:
    levels = doc.get("job_title_levels") or []
    ranked = [lvl for lvl in LEVEL_RANK if lvl in levels]
    return ranked[0] if ranked else (levels[0] if levels else UNKNOWN)


def yoe_bucket(doc: dict) -> str:
    yoe = doc.get("inferred_years_experience")
    if yoe is None:
        return UNKNOWN
    for upper, label in YOE_BUCKETS:
        if yoe <= upper:
            return label
    return "21+"


def _field(name: str):
    return lambda doc: doc.get(name) or UNKNOWN


# Dimension name -> function giving a profile's value for it.
DIMENSIONS = {
    "role": _field("job_title_role"),
    "sub_role": _field("job_title_sub_role"),
    "level": primary_level,
    "yoe": yoe_bucket,
    "country": _field("location_country"),
    "region": _field("location_region"),
    "metro": _field("location_metro"),
    "continent": _field("location_continent"),
    "company_size": _field("job_company_size"),
    "industry": _field("job_company_industry"),
}
DEFAULT_DIMENSIONS = "role,level,yoe,country,company_size"


# ---------------------------------------------------------------------------
# Candidate pool
# ---------------------------------------------------------------------------


def make_client(host: str, region: str):
    from opensearchpy import OpenSearch, RequestsHttpConnection

    username, password = os.environ.get("OPENSEARCH_USERNAME"), os.environ.get("OPENSEARCH_PASSWORD")
    if username and password:
        auth = (username, password)
    else:
        import boto3
        from opensearchpy import AWSV4SignerAuth

        credentials = boto3.Session().get_credentials()
        if credentials is None:
            sys.exit("No credentials: set OPENSEARCH_USERNAME and OPENSEARCH_PASSWORD, or configure AWS credentials.")
        auth = AWSV4SignerAuth(credentials, region, "es")

    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        http_compress=True,
        timeout=600,
        # A retry would silently re-run a full pass over the index; fail instead.
        max_retries=0,
    )


def build_query(require: list[str], extra_filter: dict | None, seed: int) -> dict:
    filters = [{"exists": {"field": field}} for field in require]
    if extra_filter:
        filters.append(extra_filter)
    return {
        "function_score": {
            "query": {
                "bool": {
                    "filter": filters,
                    # Same exclusions the product search applies.
                    "must_not": [{"term": {"is_twin_duplicate": True}}, {"term": {"hidden": True}}],
                }
            },
            "random_score": {"seed": seed, "field": "_seq_no"},
            "boost_mode": "replace",
        }
    }


def build_search_body(require: list[str], extra_filter: dict | None, seed: int, size: int, count_total: bool) -> dict:
    return {
        "size": size,
        "_source": False,
        "docvalue_fields": POOL_FIELDS,
        "track_total_hits": count_total,
        "query": build_query(require, extra_filter, seed),
    }


def hit_to_doc(hit: dict) -> dict:
    fields = hit.get("fields", {})
    doc = {name: (fields[name][0] if fields.get(name) else None) for name in POOL_FIELDS}
    doc["job_title_levels"] = fields.get("job_title_levels") or []
    doc["id"] = doc["id"] or hit["_id"]
    return doc


def fetch_pool(client, index: str, pool_size: int, page_size: int, require: list[str], extra_filter: dict | None, seed: int) -> list[dict]:
    """Draw ~pool_size distinct random profiles. Each request is an independent random draw."""
    pool: dict[str, dict] = {}
    request = 0
    while len(pool) < pool_size:
        size = min(page_size, MAX_RESULT_WINDOW)
        body = build_search_body(require, extra_filter, seed + request, size, count_total=request == 0)
        resp = client.search(index=index, body=body)
        shards = resp.get("_shards", {})
        if resp.get("timed_out") or shards.get("failed"):
            # Partial results would bias the sample towards the shards that answered.
            sys.exit(f"Search returned partial results (timed_out={resp.get('timed_out')}, shards={shards}); aborting.")
        if request == 0:
            print(f"matching profiles in {index}: {resp['hits']['total']['value']:,}")
        before = len(pool)
        for hit in resp["hits"]["hits"]:
            doc = hit_to_doc(hit)
            pool.setdefault(doc["id"], doc)
        request += 1
        print(f"\rpool: {len(pool):,}/{pool_size:,} candidates ({request} requests)", end="", flush=True)
        if len(pool) == before:  # fewer matching profiles than the pool we asked for
            break
    print()
    return list(pool.values())


def save_pool(pool: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
        for doc in pool:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)


def load_pool(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Balanced selection
# ---------------------------------------------------------------------------


def company_key(doc: dict) -> str | None:
    return doc.get("job_company_id") or doc.get("job_company_name")


def target_shares(pool_keys: list[tuple], n_dims: int, balance: float) -> list[dict]:
    """Per dimension, the share of the sample each value should get: population share ** (1 - balance)."""
    targets = []
    for d in range(n_dims):
        counts = Counter(key[d] for key in pool_keys)
        weights = {value: (count / len(pool_keys)) ** (1 - balance) for value, count in counts.items()}
        total = sum(weights.values())
        targets.append({value: weight / total for value, weight in weights.items()})
    return targets


def select(pool: list[dict], size: int, dims: list[str], balance: float, max_per_company: int, rng: random.Random) -> list[dict]:
    """Greedily pick profiles, each time taking one whose dimension values are the most
    under-represented relative to their targets."""
    keys = [tuple(DIMENSIONS[d](doc) for d in dims) for doc in pool]
    targets = target_shares(keys, len(dims), balance)

    # Profiles with identical values across all dimensions are interchangeable; group them.
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for key, doc in zip(keys, pool):
        cells[key].append(doc)
    for docs in cells.values():
        rng.shuffle(docs)

    selected: list[dict] = []
    counts = [Counter() for _ in dims]
    per_company: Counter = Counter()

    def cost(key: tuple) -> float:
        # How full each of the profile's values already is, as a fraction of its target count.
        return sum(counts[d][value] / (targets[d][value] * size) for d, value in enumerate(key))

    def run(cap: int) -> dict[tuple, list[dict]]:
        """Fill `selected` from `cells`; return the profiles skipped because of the company cap."""
        skipped: dict[tuple, list[dict]] = defaultdict(list)
        # Costs only ever grow, so a stale heap entry is a lower bound: re-check the top entry
        # and take it only if it is still current.
        heap = [(cost(key), rng.random(), key) for key in cells]
        heapq.heapify(heap)
        while heap and len(selected) < size:
            stale_cost, tiebreak, key = heapq.heappop(heap)
            current = cost(key)
            if current > stale_cost:
                heapq.heappush(heap, (current, tiebreak, key))
                continue
            docs = cells[key]
            while docs:
                doc = docs.pop()
                company = company_key(doc)
                if company is not None and per_company[company] >= cap:
                    skipped[key].append(doc)
                    continue
                selected.append(doc)
                if company is not None:
                    per_company[company] += 1
                for d, value in enumerate(key):
                    counts[d][value] += 1
                break
            if docs:
                heapq.heappush(heap, (cost(key), tiebreak, key))
        return skipped

    cap = max_per_company
    skipped = run(cap)
    while len(selected) < size and skipped:
        # The pool ran out of new companies: loosen the cap one step at a time.
        cap += 1
        cells = skipped
        skipped = run(cap)
    if cap > max_per_company:
        print(f"note: not enough distinct companies in the pool; allowed up to {cap} profiles per company to reach {len(selected):,}")
    return selected


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    ("profile_id", lambda d: d.get("id")),
    ("company", lambda d: d.get("job_company_name")),
    ("title", lambda d: d.get("job_title")),
    ("role", lambda d: d.get("job_title_role")),
    ("sub_role", lambda d: d.get("job_title_sub_role")),
    ("level", lambda d: None if primary_level(d) == UNKNOWN else primary_level(d)),
    ("levels", lambda d: "|".join(d.get("job_title_levels") or [])),
    ("years_experience", lambda d: d.get("inferred_years_experience")),
    ("yoe_bucket", yoe_bucket),
    ("country", lambda d: d.get("location_country")),
    ("region", lambda d: d.get("location_region")),
    ("metro", lambda d: d.get("location_metro")),
    ("continent", lambda d: d.get("location_continent")),
    ("company_id", lambda d: d.get("job_company_id")),
    ("company_size", lambda d: d.get("job_company_size")),
    ("industry", lambda d: d.get("job_company_industry")),
    ("linkedin_url", lambda d: d.get("linkedin_url")),
]


def write_csv(docs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([name for name, _ in CSV_COLUMNS])
        for doc in docs:
            writer.writerow([get(doc) for _, get in CSV_COLUMNS])


def print_report(pool: list[dict], selected: list[dict], dims: list[str], top: int = 12) -> None:
    companies = {company_key(d) for d in selected if company_key(d) is not None}
    print(f"\nselected {len(selected):,} profiles from {len(companies):,} distinct companies")
    for dim in dims:
        value_of = DIMENSIONS[dim]
        pool_counts = Counter(value_of(d) for d in pool)
        sample_counts = Counter(value_of(d) for d in selected)
        print(f"\n{dim}: {len(sample_counts):,} of {len(pool_counts):,} values seen in the pool are in the sample")
        print(f"  {'value':<32} {'population':>10} {'sample':>8}")
        for value, count in sample_counts.most_common(top):
            print(f"  {str(value)[:32]:<32} {pool_counts[value] / len(pool):>10.1%} {count / len(selected):>8.1%}")
        if len(sample_counts) > top:
            print(f"  ... {len(sample_counts) - top:,} more values")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", type=int, required=True, help="number of profiles in the sample")
    parser.add_argument("--out", type=Path, default=None, help="output CSV (default: profile_sample_<size>.csv)")
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--host", default=os.environ.get("OPENSEARCH_HOST", DEFAULT_HOST), help="OpenSearch domain endpoint")
    parser.add_argument("--region", default="us-east-1", help="AWS region, for SigV4 auth")
    parser.add_argument("--dimensions", default=DEFAULT_DIMENSIONS, help=f"comma-separated, from: {', '.join(DIMENSIONS)} (default: {DEFAULT_DIMENSIONS})")
    parser.add_argument("--balance", type=float, default=0.5, help="0 = population proportions, 1 = equal share per value (default: 0.5)")
    parser.add_argument("--max-per-company", type=int, default=1, help="cap on profiles from one company (default: 1)")
    parser.add_argument("--require", default="job_title,job_company_name", help='comma-separated fields a profile must have; "" for none (default: job_title,job_company_name)')
    parser.add_argument("--filter", default=None, help="""extra query DSL filter as JSON, e.g. '{"term": {"location_country": "united states"}}'""")
    parser.add_argument("--pool-factor", type=int, default=20, help="pool size as a multiple of --size (default: 20)")
    parser.add_argument("--page-size", type=int, default=5000, help=f"profiles per pool request, max {MAX_RESULT_WINDOW:,} (default: 5000)")
    parser.add_argument("--pool-file", type=Path, default=None, help="save the pool here; if the file already exists, reuse it instead of querying")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true", help="print the first pool request and the plan, then exit")
    args = parser.parse_args()

    dims = [d.strip() for d in args.dimensions.split(",") if d.strip()]
    unknown_dims = [d for d in dims if d not in DIMENSIONS]
    if unknown_dims or not dims:
        parser.error(f"unknown dimensions {unknown_dims}; choose from: {', '.join(DIMENSIONS)}")
    if not 0 <= args.balance <= 1:
        parser.error("--balance must be between 0 and 1")
    if args.size < 1 or args.max_per_company < 1 or args.pool_factor < 1 or args.page_size < 1:
        parser.error("--size, --max-per-company, --pool-factor and --page-size must be at least 1")

    require = [f.strip() for f in args.require.split(",") if f.strip()]
    extra_filter = json.loads(args.filter) if args.filter else None
    page_size = min(args.page_size, MAX_RESULT_WINDOW)
    pool_size = max(args.size * args.pool_factor, 10_000)
    out_path = args.out or Path(f"profile_sample_{args.size}.csv")

    if args.dry_run:
        print(f"host: {args.host}\nindex: {args.index}")
        print(f"pool: {pool_size:,} candidates in {-(-pool_size // page_size)} requests of {page_size:,}")
        print(json.dumps(build_search_body(require, extra_filter, args.seed, page_size, count_total=True), indent=2))
        return 0

    if args.pool_file and args.pool_file.exists():
        pool = load_pool(args.pool_file)
        print(f"reusing {len(pool):,} candidates from {args.pool_file} (delete it to query again)")
    else:
        try:
            client = make_client(args.host, args.region)
        except ImportError as e:
            sys.exit(f"Missing dependency ({e.name}). Run the script with uv, which installs it automatically:\n  uv run {sys.argv[0]} ...")
        pool = fetch_pool(client, args.index, pool_size, page_size, require, extra_filter, args.seed)
        if args.pool_file:
            save_pool(pool, args.pool_file)
            print(f"saved pool -> {args.pool_file}")

    if not pool:
        sys.exit("No profiles matched; nothing to sample.")
    if len(pool) < args.size:
        print(f"warning: only {len(pool):,} candidates available for a sample of {args.size:,}", file=sys.stderr)

    selected = select(pool, args.size, dims, args.balance, args.max_per_company, random.Random(args.seed))
    write_csv(selected, out_path)
    print_report(pool, selected, dims)
    print(f"\nwrote {len(selected):,} profiles -> {out_path}")
    return 0 if len(selected) == args.size else 1


if __name__ == "__main__":
    sys.exit(main())
