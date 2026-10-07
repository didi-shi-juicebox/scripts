"""Where the compensation-estimate POC lives; the built eval set is written into it.

Set COMP_POC_DIR to use another checkout (e.g. a git worktree).
"""

import os
from pathlib import Path

HERE = Path(__file__).parent
POC_DIR = Path(os.environ.get(
    "COMP_POC_DIR", "~/workspace/juicebox-data/databricks/experiments/comp_estimate_poc")).expanduser()
ELIGIBLE_COMPANIES = POC_DIR / "src/comp_poc/data/levelsfyi_company_names.json"
EVAL_SET = POC_DIR / "datasets/synthetic/synthetic_compensation_dataset.json"
