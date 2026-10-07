"""Write synthetic_compensation_dataset.csv from offers.json.

offers.json is the source of truth: one record per profile, each built from a
real pay data point with its own date between WINDOW_START and WINDOW_END
(an H1B filing's submit date, a job posting's posted date, a pay scale's
effective date, or an article's publication date), cited as
``<url> | <what the page shows>``. Labels carry only the pay components the
source gives (H1B filings and most postings give base pay only). For a posting,
base is the midpoint of the posted range and P25/P75 are its ends.

Run from anywhere: python build_dataset.py
"""

import csv
import json
from datetime import date

from paths import HERE

OFFERS = HERE / "offers.json"
OUTPUT = HERE / "synthetic_compensation_dataset.csv"
WINDOW_START, WINDOW_END = date(2025, 10, 1), date(2026, 10, 7)

COLUMNS = [
    "record_id", "industry", "company", "company_type", "company_size", "job_family", "job_title", "levelsfyi_title",
    "level", "years_of_experience", "years_at_company", "experience_basis", "city", "state_region", "country",
    "base_salary_usd", "bonus_usd", "stock_comp_annual_usd", "commission_usd", "total_comp_usd",
    "total_comp_p25_usd", "total_comp_p75_usd", "source_kind", "data_date", "calibration_source",
]
PAY = {"base": "base_salary_usd", "bonus": "bonus_usd", "stock": "stock_comp_annual_usd", "commission": "commission_usd"}


def row(offer: dict) -> dict:
    when = date.fromisoformat(offer["data_date"])
    if not WINDOW_START <= when <= WINDOW_END:
        raise ValueError(f"{offer['record_id']}: data_date {when} is outside {WINDOW_START}..{WINDOW_END}")
    out = {c: offer.get(c) for c in COLUMNS if c in offer}
    out.update({field: int(offer[key]) for key, field in PAY.items()})
    out["total_comp_usd"] = sum(out[f] for f in PAY.values())
    out["total_comp_p25_usd"] = round(float(offer["p25"])) if offer.get("p25") else ""
    out["total_comp_p75_usd"] = round(float(offer["p75"])) if offer.get("p75") else ""
    out["state_region"] = offer.get("state_region") or "—"  # the CSV's "no region" marker
    out["calibration_source"] = f"{offer['source_url']} | {offer['source_note']}"
    return out


def main() -> None:
    offers = sorted(json.loads(OFFERS.read_text()), key=lambda o: o["record_id"])
    unsourced = [o["record_id"] for o in offers if o["status"] != "replaced"]
    if unsourced:
        raise ValueError(f"Records without a dated source: {unsourced}")
    with OUTPUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(row(o) for o in offers)
    print(f"Wrote {len(offers)} records to {OUTPUT.name}")


if __name__ == "__main__":
    main()
