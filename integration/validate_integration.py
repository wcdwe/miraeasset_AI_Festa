#!/usr/bin/env python3
"""Fail-fast validation for the combined pension data contract."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "integrated"
VALIDATION = ROOT / "data" / "validation"


def rows(name):
    with (OUT / name).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def unique(values):
    values = list(values)
    return len(values) == len(set(values))


def main():
    errors = []
    products = rows("fund_products.csv")
    product_codes = {r["product_code"] for r in products}
    funds = {r["fund_id"] for r in products}
    if len(products) != 100 or not unique(r["product_code"] for r in products):
        errors.append("fund_products must contain 100 unique product codes")
    if len(funds) != 92:
        errors.append(f"expected 92 canonical funds, got {len(funds)}")
    for filename in (
        "product_master_enrichment.csv", "class_enrichment.csv",
        "performance_periodic_enrichment.csv", "performance_yearly.csv",
        "fund_aum_enrichment.csv", "asset_mix.csv", "trade_rules.csv",
        "manager_info.csv", "product_charges.csv",
    ):
        for row in rows(filename):
            code = row.get("product_code")
            if code and code not in product_codes:
                errors.append(f"{filename}: orphan product {code}")
                break
    class_rows = rows("class_enrichment.csv")
    mapping_counts = Counter(r["mapping_status"] for r in class_rows)
    if sum(mapping_counts.values()) != 1172:
        errors.append("class enrichment row count changed")
    yearly = rows("performance_yearly.csv")
    flagged = [r for r in yearly if r["quality_status"] != "NORMAL"]
    if len(flagged) != 1:
        errors.append(f"expected one quarantined yearly-return outlier, got {len(flagged)}")
    rag_audit = json.loads((VALIDATION / "integration_rag_audit.json").read_text(encoding="utf-8"))
    if rag_audit["status"] != "passed" or rag_audit["funds_covered"] != 92:
        errors.append("integrated RAG does not cover all 92 funds")
    if rag_audit["short_chunks_under_80"]:
        errors.append("integrated RAG contains sub-80-character fragments")
    summary = {
        "status": "failed" if errors else "passed",
        "errors": errors,
        "products": len(products), "funds": len(funds),
        "class_mapping": dict(mapping_counts),
        "yearly_outliers_quarantined": len(flagged),
        "rag_chunks": rag_audit["chunks"],
    }
    (VALIDATION / "integration_validation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
