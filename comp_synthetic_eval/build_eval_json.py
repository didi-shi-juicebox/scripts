"""Convert synthetic_compensation_dataset.csv into the POC's synthetic_compensation_dataset.json.

Writes into the POC checkout (``paths.EVAL_SET``; set COMP_POC_DIR for another
checkout, e.g. a worktree). A JSON array with one object per record:

- ``profile``: a row shaped like Hyperion's ``combined_profiles`` (the columns
  ``sample_profiles`` keeps), so the pipeline scores it like a real profile:
  ``sample_profiles --profiles_source`` loads it, and ``build_compensation_input``
  derives location, tenure and history from it. Unknown location parts are left
  out. ``job_summary`` and ``experience`` come from ``histories.json``: a
  written career for each record (current role first, real prior employers,
  a believable title ladder), checked against the record's experience and
  tenure by the tests. Total experience is measured from the first role's
  start to ``AS_OF``. The engine measures tenure from the run date, so tenure
  grows by the time elapsed since ``AS_OF``.
- ``compensation``: the record's pay in USD, the source's P25/P75 range when it
  gives one (else null), and the label's source.
- ``labels``: the record id and the normalised fields from
  ``clean_synthetic_dataset`` that a profile has no slot for.

Run from anywhere: python build_eval_json.py
"""

import csv
import json

from clean_synthetic_dataset import SOURCE, clean_row, location
from paths import EVAL_SET, HERE

OUTPUT = EVAL_SET
HISTORIES = HERE / "histories.json"
AS_OF = (2026, 10)  # (year, month) the histories are written to

PAY_FIELDS = ["base_salary_usd", "bonus_usd", "stock_comp_annual_usd", "commission_usd", "total_comp_usd"]


def months_before_as_of(year_month: str) -> int:
    year, month = (int(p) for p in year_month.split("-"))
    return (AS_OF[0] - year) * 12 + AS_OF[1] - month


def experience(history: dict, location_name: str) -> list[dict]:
    return [{"title": {"name": role["title"]}, "company": {"name": role["company"]},
             "start_date": role["start"], "end_date": role["end"], "is_primary": i == 0,
             "location_names": [location_name] if i == 0 and location_name else []}
            for i, role in enumerate(history["experience"])]


def build_record(row: dict, history: dict) -> dict:
    cleaned = clean_row(row)
    country, state, city = location(row)
    location_name = ", ".join(p for p in ((city, state, country) if city else (country,)) if p)
    roles = experience(history, location_name)
    career = months_before_as_of(roles[-1]["start_date"])
    p25, p75 = row.get("total_comp_p25_usd"), row.get("total_comp_p75_usd")
    return {
        "profile": {
            "id": row["record_id"],
            "job_company_name": cleaned["company"],
            "job_title": row["job_title"].strip().lower(),
            "job_company_linkedin_url": None,
            "location_name": location_name,
            "job_summary": history["job_summary"],
            "experience": roles,
            "total_experience_months": career,
            "average_tenure": round(career / len(roles), 1),
        },
        "compensation": {
            **{field: int(row[field]) for field in PAY_FIELDS},
            "total_comp_p25_usd": int(p25) if p25 else None,
            "total_comp_p75_usd": int(p75) if p75 else None,
            "source": row["calibration_source"].strip(),
            "source_kind": row.get("source_kind") or None,
            "data_date": row.get("data_date") or None,
        },
        "labels": {
            "record_id": row["record_id"],
            "industry": cleaned["industry"],
            "job_family": cleaned["job_family"],
            "levelsfyi_title": cleaned["job_title"],
            "level": cleaned["level"],
        },
    }


def main() -> None:
    with SOURCE.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    histories = {h["record_id"]: h for h in json.loads(HISTORIES.read_text())}
    missing = [r["record_id"] for r in rows if r["record_id"] not in histories]
    if missing:
        raise ValueError(f"No history in {HISTORIES.name} for {missing}")
    records = [build_record(r, histories[r["record_id"]]) for r in rows]
    OUTPUT.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(records)} records to {OUTPUT}")


if __name__ == "__main__":
    main()
