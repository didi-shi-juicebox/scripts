# Synthetic compensation eval set

Builds `datasets/synthetic/synthetic_compensation_dataset.json` for the
compensation-estimate POC in juicebox-data
(`databricks/experiments/comp_estimate_poc`).

## Files

- `offers.json`: the source of truth, one record per profile. Each is one real pay
  data point with its own date between 2025-10-01 and 2026-10-07: an H1B filing
  (`h1b`, base only), a job posting with a published range (`posting`; base = range
  midpoint, P25/P75 = the range), an official pay scale (`pay_scale`) or a dated pay
  report (`article`). `source_url` and `source_note` say what the page shows.
  Every company is in the POC's levels.fyi company list.
- `histories.json`: each profile's career history and summary. Written, not sourced.
- `build_dataset.py`: `offers.json` -> `synthetic_compensation_dataset.csv`; refuses
  undated or out-of-window records.
- `clean_synthetic_dataset.py`: -> `synthetic_compensation_dataset_cleaned.csv`
  (levels.fyi vocabulary); fails if a company isn't in the POC's list.
- `build_eval_json.py`: CSV + histories -> the POC's JSON.
- `build_edge_cases.py`: the POC's `synthetic_edge_cases.json`, awkward profiles derived
  from the core set (one change each), with an expected outcome. Not scored yet.
- `paths.py`: where the POC is (`COMP_POC_DIR`, default
  `~/workspace/juicebox-data/databricks/experiments/comp_estimate_poc`).

## Rebuild

```
cd ~/workspace/scripts/comp_synthetic_eval
python3 build_dataset.py && python3 clean_synthetic_dataset.py && python3 build_eval_json.py && python3 build_edge_cases.py
cd ~/workspace/juicebox-data/databricks/experiments/comp_estimate_poc && uv run --group dev pytest tests/test_synthetic_dataset.py
```

Set `COMP_POC_DIR` to build into another checkout, e.g. a git worktree.
