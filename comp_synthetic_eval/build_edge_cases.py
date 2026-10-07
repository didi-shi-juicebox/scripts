"""Build the POC's synthetic_edge_cases.json: profiles shaped like the awkward ones in production.

Not scored yet; kept to track how the pipeline handles them. Each case is a core
record from synthetic_compensation_dataset.json with ONE thing changed (or, for
owners, a small hand-built profile), modelled on patterns measured on a sample
of Hyperion's combined_profiles (the source of main-index-v33): missing titles
or companies (~33% / ~4%), missing total experience (~40% of profiles with a
title and company, which the engine shows as "0 years"), titles that aren't in
English (~6%), owner/founder titles (~2%), abbreviations, titles listing several
roles, junk titles ("mr", "n / a"), country-only locations. No real person's
profile is copied.

Each record adds an ``edge`` object:
- ``edge_type``: what was changed.
- ``expected``: ``estimate`` (the base record's label still applies),
  ``judge_only`` (an estimate is fine but no label applies) or ``decline``
  (no estimate is the right answer).
- ``base_record`` and ``change``: where it came from and what changed.

Run after build_eval_json.py: python build_edge_cases.py
"""

import copy
import json

from paths import EVAL_SET

OUTPUT = EVAL_SET.with_name("synthetic_edge_cases.json")


def retitle(r, title):
    r["profile"]["job_title"] = title
    r["profile"]["experience"][0]["title"]["name"] = title


def drop_experience_months(r):
    r["profile"]["total_experience_months"] = None
    r["profile"]["average_tenure"] = None


def drop_history(r):
    drop_experience_months(r)
    r["profile"]["experience"] = []


def drop_current_role(r):
    """No current job: title and company empty, earlier roles kept."""
    p = r["profile"]
    p["job_title"] = p["job_company_name"] = None
    p["experience"] = p["experience"][1:]
    if p["experience"]:
        p["experience"][0]["is_primary"] = True


def relocate(r, location):
    r["profile"]["location_name"] = location
    if r["profile"]["experience"]:
        r["profile"]["experience"][0]["location_names"] = [location]


def no_company(r):
    r["profile"]["job_company_name"] = None
    r["profile"]["experience"][0]["company"]["name"] = None


def no_title(r):
    r["profile"]["job_title"] = None
    r["profile"]["experience"][0]["title"]["name"] = None


def unserved_company(r, company):
    r["profile"]["job_company_name"] = company
    r["profile"]["experience"][0]["company"]["name"] = company


# (edge_type, expected, base_record, change description, function)
CASES = [
    ("abbreviated_title", "estimate", "SYN-0040", "title 'cna'", lambda r: retitle(r, "cna")),
    ("abbreviated_title", "estimate", "SYN-0006", "title 'csr'", lambda r: retitle(r, "csr")),
    ("abbreviated_title", "estimate", "SYN-0001", "title 'tpm'", lambda r: retitle(r, "tpm")),
    ("abbreviated_title", "estimate", "SYN-0058", "title 'principal swe'", lambda r: retitle(r, "principal swe")),
    ("abbreviated_title", "estimate", "SYN-0094", "title 'rn ii'", lambda r: retitle(r, "rn ii")),
    ("non_english_title", "estimate", "SYN-0019", "Dutch title", lambda r: retitle(r, "productontwerper")),
    ("non_english_title", "estimate", "SYN-0100", "German title",
     lambda r: retitle(r, "leitender forschungssoftwareentwickler")),
    ("non_english_title", "estimate", "SYN-0032", "Polish title", lambda r: retitle(r, "specjalista ds. data science")),
    ("non_english_title", "estimate", "SYN-0047", "Spanish title", lambda r: retitle(r, "ingeniero de datos senior")),
    ("non_english_title", "estimate", "SYN-0095", "French title (Canada)",
     lambda r: retitle(r, "gestionnaire de produit principal")),
    ("multi_role_title", "estimate", "SYN-0007", "certification appended",
     lambda r: retitle(r, "senior consultant | aws certified solutions architect")),
    ("multi_role_title", "estimate", "SYN-0035", "two roles", lambda r: retitle(r, "staff security engineer / appsec lead")),
    ("multi_role_title", "estimate", "SYN-0011", "side role appended",
     lambda r: retitle(r, "physician assistant & clinical preceptor")),
    ("title_names_company", "estimate", "SYN-0039", "company inside title",
     lambda r: retitle(r, "machine learning engineer ii at spotify")),
    ("title_names_company", "estimate", "SYN-0072", "company inside title",
     lambda r: retitle(r, "senior associate @ kpmg advisory")),
    ("vague_title", "estimate", "SYN-0037", "title 'engineer'", lambda r: retitle(r, "engineer")),
    ("vague_title", "estimate", "SYN-0086", "title 'scientist'", lambda r: retitle(r, "scientist")),
    ("vague_title", "estimate", "SYN-0085", "title 'analyst'", lambda r: retitle(r, "analyst")),
    ("look_alike_title", "estimate", "SYN-0033", "'product owner' (a PM role, not an owner)",
     lambda r: retitle(r, "product owner")),
    ("missing_experience_months", "estimate", "SYN-0013", "no total months (entry level)", drop_experience_months),
    ("missing_experience_months", "estimate", "SYN-0012", "no total months (senior)", drop_experience_months),
    ("missing_experience_months", "estimate", "SYN-0049", "no total months (director)", drop_experience_months),
    ("missing_experience_months", "estimate", "SYN-0025", "no total months (physician)", drop_experience_months),
    ("no_history", "estimate", "SYN-0044", "no job history, no total months", drop_history),
    ("no_history", "estimate", "SYN-0083", "no job history, no total months", drop_history),
    ("country_only_location", "estimate", "SYN-0096", "location 'united states'", lambda r: relocate(r, "united states")),
    ("country_only_location", "estimate", "SYN-0098", "location 'india'", lambda r: relocate(r, "india")),
    ("region_country_location", "estimate", "SYN-0036", "location 'texas, united states'",
     lambda r: relocate(r, "texas, united states")),
    ("no_current_role", "decline", "SYN-0029", "no current title or company; earlier roles kept", drop_current_role),
    ("no_title", "decline", "SYN-0050", "title empty, company kept", no_title),
    ("no_company", "decline", "SYN-0018", "company empty, title kept", no_company),
    ("junk_title", "decline", "SYN-0057", "title 'mr'", lambda r: retitle(r, "mr")),
    ("junk_title", "decline", "SYN-0014", "title 'n / a'", lambda r: retitle(r, "n / a")),
    ("not_working", "decline", "SYN-0067", "title 'retired'", lambda r: retitle(r, "retired")),
    ("not_working", "decline", "SYN-0092", "title 'open to work'", lambda r: retitle(r, "open to work")),
    ("unserved_company", "decline", "SYN-0017", "company not in levels.fyi or Minerva",
     lambda r: unserved_company(r, "brightline health ai")),
]

# Owners: an estimate is reasonable, but no salary data point describes owner income.
OWNERS = [
    {"id": "state farm agency owner", "title": "agency owner", "company": "state farm",
     "location": "columbus, ohio, united states", "summary": "I run a State Farm agency serving auto, home and life customers.",
     "experience": [("agency owner", "state farm", "2019-03", None), ("insurance agent", "state farm", "2015-06", "2019-02"),
                    ("account manager", "nationwide", "2012-08", "2015-05")]},
    {"id": "mcdonald's owner/operator", "title": "owner / operator", "company": "mcdonald's",
     "location": "phoenix, arizona, united states", "summary": "",
     "experience": [("owner / operator", "mcdonald's", "2018-10", None),
                    ("restaurant general manager", "chipotle mexican grill", "2012-04", "2018-09"),
                    ("shift manager", "panera bread", "2009-07", "2012-03")]},
    {"id": "anytime fitness franchise owner", "title": "franchise owner", "company": "anytime fitness",
     "location": "charlotte, north carolina, united states", "summary": "Owner of two Anytime Fitness clubs in Charlotte.",
     "experience": [("franchise owner", "anytime fitness", "2020-01", None),
                    ("fitness manager", "planet fitness", "2014-05", "2019-12")]},
]


def owner_record(spec: dict) -> dict:
    roles = [{"title": {"name": t}, "company": {"name": c}, "start_date": s, "end_date": e, "is_primary": i == 0,
              "location_names": [spec["location"]] if i == 0 else []}
             for i, (t, c, s, e) in enumerate(spec["experience"])]
    first = roles[-1]["start_date"]
    months = (2026 - int(first[:4])) * 12 + 10 - int(first[5:])
    return {
        "profile": {"id": None, "job_company_name": spec["company"], "job_title": spec["title"],
                    "job_company_linkedin_url": None, "location_name": spec["location"], "job_summary": spec["summary"],
                    "experience": roles, "total_experience_months": months,
                    "average_tenure": round(months / len(roles), 1)},
        "compensation": None, "labels": None,
    }


def main() -> None:
    core = {r["profile"]["id"]: r for r in json.loads(EVAL_SET.read_text())}
    records = []
    for edge_type, expected, base, change, fn in CASES:
        r = copy.deepcopy(core[base])
        fn(r)
        if expected == "decline":
            r["compensation"] = None
        records.append((r, {"edge_type": edge_type, "expected": expected, "base_record": base, "change": change}))
    for spec in OWNERS:
        records.append((owner_record(spec), {"edge_type": "owner_title", "expected": "judge_only", "base_record": None,
                                             "change": spec["id"]}))
    out = []
    for i, (r, edge) in enumerate(records, 1):
        r["profile"]["id"] = f"EDGE-{i:03d}"
        if r.get("labels"):
            r["labels"]["record_id"] = r["profile"]["id"]
        out.append({**r, "edge": edge})
    OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(out)} edge cases to {OUTPUT}")


if __name__ == "__main__":
    main()
