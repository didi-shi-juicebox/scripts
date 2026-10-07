#!/usr/bin/env python3
"""Build test and validation eval sets from estimates joined with their profiles.

    ./build_eval_sets.py comp_estimates_sample/sample_1000_with_profiles.jsonl

Input: the output of ``join_estimates_with_profiles.py``. Output, next to the input:
``test_<N>.json`` and ``validation_<N>.json``, in the shape of juicebox-ai's
``test/evals/sets/compensation-intelligence.json`` (inputs ``profile_data`` and
``compensation_data`` for the ``compensation_estimate`` prompt, plus ``expected``).

Selection
  Only estimates that are still usable are eligible: the profile was found, its current company
  and title still equal the ones the estimate was made for, the range is valid, and reference
  rows were stored. Eligible estimates are shuffled with --seed and dealt into the two sets,
  keeping every company in one set only, so the sets share no company's reference data.

What is exact and what is rebuilt
  compensation_data  exact: the stored reference rows, formatted as the product formats them.
  expected           the PRODUCTION MODEL'S OWN range and explanation, not a reviewed answer.
                     Every case is tagged "unreviewed".
  profile_data       rebuilt from the profile as it is in OpenSearch today, following the
                     product's formatter. Tenure and open-ended durations are measured to the
                     date of the estimate. It can differ from what the model saw at the time.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# JavaScript-compatible formatting (the product's formatters are JavaScript)
# ---------------------------------------------------------------------------


def js_number(value) -> float:
    """Number(value): NaN for anything that is not numeric."""
    if isinstance(value, bool) or value is None:
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def js_truthy(value) -> bool:
    if value is None or value == "" or value is False:
        return False
    return not (isinstance(value, (int, float)) and (value == 0 or math.isnan(value)))


def js_string(value) -> str:
    """How a template literal prints a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def js_round(value: float) -> int:
    return math.floor(value + 0.5)


def years(months: float) -> str:
    """Math.round((months / 12) * 10) / 10, printed as JavaScript prints it."""
    return js_string(js_round(months / 12 * 10) / 10)


# ---------------------------------------------------------------------------
# compensation_data: port of formatCompensationDataForLLM (utils/compensation/dataTransformer.js)
# ---------------------------------------------------------------------------


def format_salary(value) -> int:
    number = js_number(value)
    return 0 if not js_truthy(value) or math.isnan(number) else js_round(number)


def format_yoe(value) -> str:
    number = js_number(value)
    return "N/A" if not js_truthy(value) or math.isnan(number) else f"{number:.2f}"


def field(row: dict, name: str) -> str:
    return js_string(row[name]) if name in row else "undefined"


def format_compensation_data(rows: list[dict]) -> str:
    is_minerva = rows[0].get("dataSource") == "minerva"
    blocks = []
    for index, row in enumerate(rows, start=1):
        salary = lambda name: format_salary(row.get(name))  # noqa: E731
        count = row.get("count") if js_truthy(row.get("count")) else 1
        if is_minerva:
            lines = [
                f"{index}. Company: {field(row, 'company')}",
                f"   Title: {field(row, 'title')}",
                f"   Search Relevance: {field(row, 'search_relevance_score')}",
                f"   Base Salary (Min/Median/Max): {salary('base_salary_min')}/{salary('base_salary')}/{salary('base_salary_max')}",
                f"   Additional Pay (Min/Median/Max): {salary('addl_pay_min')}/{salary('addl_pay')}/{salary('addl_pay_max')}",
                f"   Total Compensation (Min/Median/Max): {salary('tc_min')}/{salary('total_comp')}/{salary('tc_max')}",
            ]
            if js_truthy(row.get("location")):
                lines.append(f"   Location: {row['location']}")
        else:
            lines = [
                f"{index}. Company: {field(row, 'company')}",
                f"   Title: {field(row, 'title')}",
                f"   Level: {field(row, 'level')}",
                f"   Years of Experience Average: {format_yoe(row.get('yoe_avg'))}",
                f"   Search Relevance: {field(row, 'search_relevance_score')}",
                f"   Base Salary (P25/P50/P75): {salary('base_salary_p25')}/{salary('base_salary')}/{salary('base_salary_p75')}",
                f"   Stock Grant (P25/P50/P75): {salary('stock_grant_p25')}/{salary('stock_grant')}/{salary('stock_grant_p75')}",
                f"   Bonus (P25/P50/P75): {salary('bonus_p25')}/{salary('bonus')}/{salary('bonus_p75')}",
                f"   Total Compensation (P25/P50/P75): {salary('tc_p25')}/{salary('total_comp')}/{salary('tc_p75')}",
                f"   Location: {field(row, 'location')}",
            ]
        lines.append(f"   Sample Count: {js_string(count)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# profile_data: port of formatProfileDataForLLM and the engine code that feeds it
# ---------------------------------------------------------------------------


def parse_month(value) -> tuple[int, int] | None:
    """(year, month) from "YYYY", "YYYY-MM" or "YYYY-MM-DD"."""
    if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
        return None
    month = int(value[5:7]) if len(value) >= 7 and value[5:7].isdigit() else 1
    return int(value[:4]), month


def months_between(start, end) -> int | None:
    a, b = parse_month(start), parse_month(end)
    return None if a is None or b is None else (b[0] - a[0]) * 12 + (b[1] - a[1])


def parse_location(location: str | None) -> tuple[str, str, str]:
    parts = [p.strip().lower() for p in location.split(", ")] if location and location.strip() else []
    city = state = country = None
    if len(parts) >= 3:
        city, state, country = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        city, country = parts
    elif len(parts) == 1:
        country = parts[0]
    return city or "unknown", state or "unknown", country or "unknown"


def experience_history(profile: dict) -> list[dict]:
    history = []
    for exp in profile.get("experience") or []:
        title = exp.get("title")
        title = (title.get("name") if isinstance(title, dict) else title) or None
        if not title:
            continue
        history.append({
            "title": title,
            "company": (exp.get("company") or {}).get("name") or "Unknown Company",
            "duration_months": exp.get("duration_months"),
            "start_date": exp.get("start_date"),
            "end_date": exp.get("end_date"),
            "summary": exp.get("summary"),
            "is_current": exp.get("is_primary"),
        })
    return history


def experience_duration(exp: dict, as_of: str) -> str:
    if js_truthy(exp["duration_months"]):
        return f"{years(exp['duration_months'])} years"
    end = exp["end_date"] or (as_of if exp["is_current"] else None)
    months = months_between(exp["start_date"], end) if exp["start_date"] and end else None
    return f"{years(months)} years" if months is not None else "Unknown duration"


def current_job_location(profile: dict) -> str:
    primary = next((e for e in profile.get("experience") or [] if e.get("is_primary")), None)
    return ((primary or {}).get("location_names") or [None])[0] or profile.get("location_name") or ""


def current_tenure_months(profile: dict, as_of: str) -> int:
    """Months in the current job as of the estimate; 0 when the start date is unknown."""
    primary = next((e for e in profile.get("experience") or [] if e.get("is_primary")), None)
    start = (primary or {}).get("start_date") or profile.get("job_start_date")
    return max(months_between(start, as_of) or 0, 0)


def format_profile_data(profile: dict, as_of: str) -> tuple[str, dict]:
    city, state, country = parse_location(current_job_location(profile))
    total_years = years(profile.get("total_experience_months") or 0)
    tenure_years = years(current_tenure_months(profile, as_of))
    lines = [
        f"Company: {profile.get('job_company_name')}",
        f"Job Title: {profile.get('job_title')}",
        f"Location: {city}, {state}, {country}",
        f"Total Experience: {total_years} years",
        f"Current Tenure: {tenure_years} years",
    ]
    if (profile.get("job_summary") or "").strip():
        lines.append(f"Job Description: {profile['job_summary']}")
    history = experience_history(profile)
    if history:
        lines.append("\nWork Experience History:")
        for index, exp in enumerate(history, start=1):
            dates = " - ".join(d for d in (exp["start_date"], exp["end_date"]) if d) or "Unknown dates"
            lines.append(f"  {index}. {exp['title']} at {exp['company']} ({experience_duration(exp, as_of)}, {dates})")
            if (exp["summary"] or "").strip():
                lines.append(f"     Summary: {exp['summary'].strip()}")
    facts = {"location": ", ".join(p for p in (city, state) if p != "unknown"), "country": country,
             "total_experience": f"{total_years} years", "current_tenure": f"{tenure_years} years"}
    return "\n".join(lines), facts


# ---------------------------------------------------------------------------
# Selection and output
# ---------------------------------------------------------------------------


def ineligible_reason(row: dict) -> str | None:
    estimate = row["estimate"]
    if row["profile"] is None:
        return "profile not found"
    if not row["same_company_title"]:
        return "company or title changed since the estimate"
    low, high = estimate.get("totalMin") or 0, estimate.get("totalMax") or 0
    if low <= 0 or high < low:
        return "invalid range"
    if not estimate.get("referenceData"):
        return "no reference rows"
    return None


def deal(rows: list[dict], size: int, names: list[str], rng: random.Random) -> dict[str, list[dict]]:
    """Shuffle, then deal rows into the sets, keeping each company in a single set."""
    rows = list(rows)
    rng.shuffle(rows)
    sets: dict[str, list[dict]] = {name: [] for name in names}
    company_set: dict[str, str] = {}
    for row in rows:
        company = row["estimate"]["company"]
        open_sets = [name for name in names if len(sets[name]) < size]
        if not open_sets:
            break
        # A new company goes to whichever open set is currently smaller.
        target = company_set.setdefault(company, min(open_sets, key=lambda name: len(sets[name])))
        if len(sets[target]) < size:
            sets[target].append(row)
    return sets


def to_case(row: dict) -> dict:
    estimate, profile = row["estimate"], row["profile"]
    as_of = estimate["updatedAt"]
    profile_data, facts = format_profile_data(profile, as_of)
    expected = (
        f"min_total_yearly_compensation: {estimate['totalMin']}\n"
        f"max_total_yearly_compensation: {estimate['totalMax']}\n\n"
        f"Explanation: {estimate.get('explanation') or ''}"
    )
    return {
        "id": row["profile_id"],
        "inputs": {
            "profile_data": profile_data,
            "compensation_data": format_compensation_data(estimate["referenceData"]),
        },
        "expected": expected,
        "tags": ["unreviewed"],
        "metadata": {
            "category": "unreviewed",
            "company": estimate["company"],
            "job_title": estimate["title"],
            **facts,
            "timestamp": as_of,
            "profile_id": row["profile_id"],
            "opensearch_id": row["opensearch_id"],
            "data_source": estimate["referenceData"][0].get("dataSource"),
            "reference_rows": len(estimate["referenceData"]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("joined", type=Path, help="output of join_estimates_with_profiles.py")
    parser.add_argument("--size", type=int, default=100, help="cases per set (default: 100)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", type=Path, default=None, help="default: next to the input")
    args = parser.parse_args()

    with open(args.joined, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    reasons = Counter(ineligible_reason(row) for row in rows)
    eligible = [row for row in rows if ineligible_reason(row) is None]
    print(f"{len(eligible):,} of {len(rows):,} estimates are eligible")
    for reason, count in reasons.most_common():
        if reason:
            print(f"  excluded, {reason}: {count:,}")

    names = ["test", "validation"]
    sets = deal(eligible, args.size, names, random.Random(args.seed))
    out_dir = args.out_dir or args.joined.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        cases = [to_case(row) for row in sets[name]]
        dataset = {
            "name": f"Compensation estimates from production traffic ({name})",
            "promptName": "compensation_estimate",
            "source": {
                "collection": "profile_compensation_estimates",
                "sampledFrom": args.joined.name,
                "builtAt": date.today().isoformat(),
                "seed": args.seed,
                "note": "expected is the production model's own output, not a reviewed answer; "
                        "profile_data is rebuilt from the profile as it is in OpenSearch today",
            },
            "cases": cases,
        }
        path = out_dir / f"{name}_{args.size}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dataset, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"wrote {len(cases):,} cases -> {path}")

    if any(len(sets[name]) < args.size for name in names):
        print(f"warning: not enough eligible estimates to fill both sets of {args.size}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
