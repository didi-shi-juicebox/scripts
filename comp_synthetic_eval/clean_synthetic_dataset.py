"""Clean synthetic_compensation_dataset.csv into synthetic_compensation_dataset_cleaned.csv.

Keeps only the columns the eval needs and rewrites company, job title and level
into the vocabulary of the levels.fyi reference data (lowercase names as they
appear in compensation-index-levelsfyi-v4). Levels.fyi has no industry field, so
industry is rewritten into the vocabulary of profiles' `job_company_industry`.
Reports any company outside the POC's levels.fyi company list (the engine can't
serve those).

Run from anywhere: python clean_synthetic_dataset.py
"""

import csv
import json
import re

from paths import ELIGIBLE_COMPANIES, HERE

SOURCE = HERE / "synthetic_compensation_dataset.csv"
OUTPUT = HERE / "synthetic_compensation_dataset_cleaned.csv"

COLUMNS = [
    "industry", "company", "job_family", "job_title", "level", "years_of_experience", "years_at_company",
    "city", "state_region", "country", "base_salary_usd", "bonus_usd", "stock_comp_annual_usd", "commission_usd",
    "total_comp_usd", "total_comp_p25_usd", "total_comp_p75_usd", "source_kind", "data_date", "calibration_source",
]

# Companies whose levels.fyi name differs from the lowercased source name.
COMPANY_ALIASES = {
    "anduril": "anduril industries",
    "boston consulting group": "bcg",
    "costco": "costco wholesale",
    "epic systems": "epic",
    "mckinsey & company": "mckinsey",
    "omnicom": "omnicom group",
    "u.s. federal government": "u.s. government",
    "verizon business": "verizon",
    "walgreens": "walgreens boots alliance",
}

# Location codes in the source -> the lowercase names levels.fyi and profiles use.
COUNTRIES = {"USA": "united states", "UK": "united kingdom"}
US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "DC": "district of columbia", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}
OTHER_REGIONS = {
    "CDMX": "mexico city", "England": "england", "KA": "karnataka", "Leinster": "leinster",
    "NH": "noord-holland", "TS": "telangana", "ZH": "zurich", "ON": "ontario", "BC": "british columbia",
}


def location(row: dict) -> tuple[str, str | None, str | None]:
    """(country, state, city) in levels.fyi spelling; None where the source has none."""
    country = COUNTRIES.get(row["country"], row["country"].lower())
    regions = US_STATES if row["country"] == "USA" else OTHER_REGIONS
    city = row["city"].strip().lower()
    return country, regions.get(row["state_region"].strip()), None if not city or city.startswith("remote") else city


INDUSTRIES = {
    "Advertising & Marketing": "marketing and advertising",
    "Aerospace & Defense": "defense & space",
    "Business Services": "facilities services",
    "Consulting": "management consulting",
    "Education": "primary/secondary education",
    "Energy": "oil & energy",
    "Financial Services": "financial services",
    "Food & Agriculture": "food & beverages",
    "Government": "government administration",
    "Healthcare": "hospital & health care",
    "Healthcare (Med Devices)": "medical devices",
    "Hospitality & Food Service": "restaurants",
    "Legal Services": "law practice",
    "Logistics & Transportation": "logistics and supply chain",
    "Manufacturing": "machinery",
    "Media & Entertainment": "computer games",
    "Nonprofit": "non-profit organization management",
    "Pharma & Biotech": "biotechnology",
    "Real Estate & Construction": "construction",
    "Retail & Consumer": "retail",
    "Technology": "computer software",
    "Telecommunications": "telecommunications",
}

# Companies whose industry is more specific than their source category's default.
COMPANY_INDUSTRIES = {
    "amazon": "internet",
    "bcg": "management consulting",
    "capital one": "banking",
    "cbre": "commercial real estate",
    "cisco": "computer networking",
    "evercore": "investment banking",
    "goldman sachs": "investment banking",
    "grainger": "wholesale",
    "hsbc": "banking",
    "intel": "semiconductors",
    "kpmg": "accounting",
    "lazard": "investment banking",
    "meta": "internet",
    "nextera energy": "utilities",
    "piper sandler": "investment banking",
    "sherwin-williams": "chemicals",
    "spotify": "internet",
    "u.s. navy": "military",
    "united states department of justice": "government administration",
    "walgreens boots alliance": "retail",
}


def clean_company(name: str) -> str:
    base = re.sub(r"\s*\((seed|series [a-z])\)\s*$", "", name.strip(), flags=re.IGNORECASE).lower()
    return COMPANY_ALIASES.get(base, base)


def clean_row(row: dict) -> dict:
    out = {c: str(row[c]).strip() for c in COLUMNS}
    company = clean_company(row["company"])
    out["company"] = company
    out["industry"] = COMPANY_INDUSTRIES.get(company, INDUSTRIES[row["industry"]])
    out["job_title"] = row["levelsfyi_title"].strip()  # the role as levels.fyi names it
    out["level"] = row["level"].lower()
    if out["state_region"] == "—":
        out["state_region"] = ""
    return out


def main() -> None:
    with SOURCE.open(newline="", encoding="utf-8") as fh:
        rows = [clean_row(r) for r in csv.DictReader(fh)]
    with OUTPUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    known = set(json.loads(ELIGIBLE_COMPANIES.read_text()))
    unmatched = sorted({r["company"] for r in rows} - known)
    print(f"Wrote {len(rows)} rows to {OUTPUT.name}")
    if unmatched:
        raise SystemExit(f"Not in the POC's levels.fyi company list (the engine can't serve them): {unmatched}")


if __name__ == "__main__":
    main()
