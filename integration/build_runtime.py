#!/usr/bin/env python3
"""Build the SQLite/FTS runtime from extracted and integrated sources."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
DB = ROOT / "data" / "integrated" / "structured_store.db"
STAGING = ROOT / "data" / "staging" / "suhyeon"


def run(*args):
    subprocess.run([PY, *map(str, args)], cwd=ROOT, check=True)


def main():
    run(ROOT / "scripts" / "build_structured_store.py", "--output", DB)
    run(
        ROOT / "scripts" / "build_product_facts_db.py",
        "--db", DB,
        "--product-master", STAGING / "product_master.json",
        "--class-fees", STAGING / "class_fees.json",
        "--class-returns", STAGING / "class_returns.json",
        "--manager-info", STAGING / "manager_info.json",
        "--fund-aum", STAGING / "fund_aum.json",
        "--class-meaning", STAGING / "class_meaning.json",
        "--class-charges", STAGING / "class_charges.json",
        "--yearly-returns", STAGING / "yearly_returns.json",
        "--trade-rules", STAGING / "trade_rules.json",
        "--product-charges", STAGING / "product_charges.json",
        "--asset-mix", STAGING / "asset_mix.json",
    )
    run(ROOT / "integration" / "load_integrated_rag.py")


if __name__ == "__main__":
    main()
