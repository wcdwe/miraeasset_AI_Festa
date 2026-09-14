#!/usr/bin/env python3
"""Build integrated datasets with suhyeon as the runtime data authority.

The existing data/processed files remain an immutable comparison baseline.
Class details and quantitative runtime values come from suhyeon staging. This
script writes only to data/integrated and data/validation/integration_*.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
STAGING = ROOT / "data" / "staging" / "suhyeon"
OUT = ROOT / "data" / "integrated"
VALIDATION = ROOT / "data" / "validation"


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows, fields=None):
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        seen = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_json(name):
    return json.loads((STAGING / name).read_text(encoding="utf-8"))


def code_from_file(name):
    match = re.fullmatch(r"R2_(.+)\.pdf", name or "", re.I)
    return match.group(1) if match else None


def norm_label(value):
    value = str(value or "").lower()
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def number(value):
    if value is None or str(value).strip() in {"", "-"}:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def same_number(a, b, tolerance=0.0002):
    left, right = number(a), number(b)
    return left is not None and right is not None and abs(left - right) <= tolerance


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main():
    required = [
        "product_master.json", "class_meaning.json", "class_fees.json",
        "class_charges.json", "class_returns.json", "yearly_returns.json",
        "fund_aum.json", "asset_mix.json", "trade_rules.json",
        "manager_info.json", "institution_facts.json", "product_charges.json",
    ]
    missing = [name for name in required if not (STAGING / name).exists()]
    if missing:
        raise SystemExit(f"Missing staging inputs: {missing}")

    OUT.mkdir(parents=True, exist_ok=True)

    documents = read_csv(PROCESSED / "documents.csv")
    funds = read_csv(PROCESSED / "funds.csv")
    classes = read_csv(PROCESSED / "classes.csv")
    performance = read_csv(PROCESSED / "performance.csv")
    aum = read_csv(PROCESSED / "aum.csv")
    duplicate_rows = read_csv(VALIDATION / "duplicate_pdf_files.csv")

    doc_by_code = {}
    for doc in documents:
        code = code_from_file(doc["file_name"])
        if code:
            doc_by_code[code] = doc

    duplicate_group_by_code = {}
    group_codes = defaultdict(list)
    for row in duplicate_rows:
        code = code_from_file(row["file_name"])
        if code:
            duplicate_group_by_code[code] = row["duplicate_group"]
            group_codes[row["duplicate_group"]].append(code)

    canonical_by_group = {}
    for group, codes in group_codes.items():
        present = [code for code in codes if code in doc_by_code]
        if not present:
            continue
        present.sort(key=lambda code: (doc_by_code[code]["doc_id"], code))
        canonical_by_group[group] = present[0]

    product_master = load_json("product_master.json")
    master_codes = {row["product_code"] for row in product_master}
    product_map = {}
    product_rows = []
    alias_rows = []
    mapping_errors = []
    for code in sorted(master_codes):
        group = duplicate_group_by_code.get(code, "")
        canonical_code = canonical_by_group.get(group, code)
        source_doc = doc_by_code.get(code) or doc_by_code.get(canonical_code)
        if source_doc is None:
            mapping_errors.append({"product_code": code, "reason": "NO_DOCUMENT_OR_DUPLICATE_MAPPING"})
            continue
        mapping_method = "DIRECT_DOCUMENT" if code in doc_by_code else "EXACT_DUPLICATE_GROUP"
        row = {
            "fund_id": source_doc["fund_id"],
            "product_code": code,
            "source_doc_id": source_doc["doc_id"],
            "canonical_product_code": canonical_code,
            "duplicate_group_id": group,
            "is_canonical": str(code == canonical_code).lower(),
            "mapping_method": mapping_method,
            "mapping_confidence": "1.0",
            "review_status": "VERIFIED_BY_DOCUMENT_INDEX",
        }
        product_rows.append(row)
        product_map[code] = row
        alias_rows.append({
            "product_code": code,
            "fund_id": source_doc["fund_id"],
            "doc_id": source_doc["doc_id"],
            "file_name": f"R2_{code}.pdf",
            "duplicate_group_id": group,
            "duplicate_type": "EXACT_FILE" if group else "UNIQUE",
            "canonical_product_code": canonical_code,
            "is_canonical": str(code == canonical_code).lower(),
        })

    write_csv(OUT / "fund_products.csv", product_rows)
    write_csv(OUT / "document_aliases.csv", alias_rows)
    write_csv(VALIDATION / "integration_mapping_errors.csv", mapping_errors,
              ["product_code", "reason"])

    # Flatten product master while retaining field-level provenance.
    product_flat = []
    conflicts = []
    fund_by_id = {row["fund_id"]: row for row in funds}
    for row in product_master:
        code = row["product_code"]
        mapped = product_map.get(code)
        if not mapped:
            continue
        flat = {"fund_id": mapped["fund_id"], "product_code": code}
        for field in ("product_name", "asset_type", "risk_level"):
            item = row.get(field) or {}
            flat[field] = item.get("value")
            flat[f"{field}_page"] = item.get("page")
            flat[f"{field}_method"] = item.get("method")
            flat[f"{field}_confidence"] = item.get("confidence")
            flat[f"{field}_evidence"] = item.get("evidence")
        product_flat.append(flat)
        mine = fund_by_id[mapped["fund_id"]]
        comparisons = {
            "risk_grade": (mine.get("risk_grade"), flat["risk_level"]),
            "asset_type_l1": (norm_label(mine.get("asset_type_l1")).removesuffix("형"),
                              norm_label(flat["asset_type"]).removesuffix("형")),
        }
        for field, (current, incoming) in comparisons.items():
            if str(current) and str(incoming) and str(current) != str(incoming):
                specificity = field == "asset_type_l1" and str(current) in str(incoming)
                conflicts.append({
                    "entity_type": "fund", "entity_id": mapped["fund_id"],
                    "product_code": code, "class_code": "", "field_name": field,
                    "current_value": current, "incoming_value": incoming,
                    "current_source": mine.get("source_doc_id", ""),
                    "incoming_source": f"product_master:{flat.get(field + '_page', '')}",
                    "resolution": "KEEP_CURRENT_L1_STORE_INCOMING_AS_DETAIL" if specificity else "",
                    "resolution_reason": "Incoming asset type is a more specific subtype" if specificity else "",
                    "review_status": "RESOLVED" if specificity else "REVIEW_REQUIRED",
                })
    write_csv(OUT / "product_master_enrichment.csv", product_flat)

    # Conservative class matching: normalized label, then account/channel filters.
    classes_by_fund = defaultdict(list)
    for row in classes:
        classes_by_fund[row["fund_id"]].append(row)

    meanings = load_json("class_meaning.json")
    fees = load_json("class_fees.json")
    charges = load_json("class_charges.json")
    meaning_by_key = {(r["product_code"], r["class_code"]): r for r in meanings}
    fee_by_key = {(r["product_code"], r["class_code"]): r for r in fees}
    charge_by_key = {(r["product_code"], r["class_code"]): r for r in charges}
    all_class_keys = sorted(set(meaning_by_key) | set(fee_by_key) | set(charge_by_key))
    class_mapping = {}
    class_rows = []
    for code, class_code in all_class_keys:
        mapped = product_map.get(code)
        if not mapped:
            continue
        meaning = meaning_by_key.get((code, class_code), {})
        candidates = [r for r in classes_by_fund[mapped["fund_id"]]
                      if norm_label(r.get("class_name_normalized")) == norm_label(class_code)]
        if len(candidates) > 1 and meaning.get("account_type"):
            filtered = [r for r in candidates if r.get("account_type") == meaning["account_type"]]
            if filtered:
                candidates = filtered
        if len(candidates) > 1 and meaning.get("channel"):
            filtered = [r for r in candidates if r.get("channel") == meaning["channel"]]
            if filtered:
                candidates = filtered
        if len(candidates) == 1:
            class_id, status = candidates[0]["class_id"], "EXACT_NORMALIZED_MATCH"
        elif not candidates:
            class_id, status = "", "NEW_CLASS"
        else:
            class_id, status = "", "AMBIGUOUS"
        class_mapping[(code, class_code)] = class_id
        fee = fee_by_key.get((code, class_code), {})
        charge = charge_by_key.get((code, class_code), {})
        class_rows.append({
            "fund_id": mapped["fund_id"], "product_code": code,
            "team_class_code": class_code, "class_id": class_id,
            "mapping_status": status,
            "raw_label": meaning.get("raw_label"), "fee_type": meaning.get("fee_type"),
            "channel": meaning.get("channel"), "account_type": meaning.get("account_type"),
            "retail": meaning.get("retail"), "eligibility": charge.get("eligibility"),
            "front_load_fee_text": charge.get("front_load_fee"),
            "back_load_fee_text": charge.get("back_load_fee"),
            "redemption_fee_text": charge.get("redemption_fee"),
            "switch_fee_text": charge.get("switch_fee"),
            "total_fee_pct": fee.get("total_fee"),
            "distribution_fee_pct": fee.get("distribution_fee"),
            "peer_avg_fee_pct": fee.get("peer_avg_fee"),
            "total_fee_and_cost_pct": fee.get("total_fee_and_cost"),
            "cost_projection_per_10m": json_text(fee.get("cost_projection_per_10m")),
            "fee_breakdown": json_text(fee.get("fee_breakdown")),
            "source_pages": json_text(fee.get("source_pages")),
            "method": fee.get("method"), "confidence": fee.get("confidence"),
            "evidence": fee.get("evidence"),
        })
        if class_id and fee:
            mine = next(r for r in classes if r["class_id"] == class_id)
            for field, mine_field, team_field in (
                ("total_fee", "total_fee", "total_fee"),
                ("sales_fee", "sales_fee", "distribution_fee"),
                ("total_expense_ratio", "total_expense_ratio", "total_fee_and_cost"),
            ):
                if not same_number(mine.get(mine_field), fee.get(team_field)):
                    current_value, incoming_value = mine.get(mine_field), fee.get(team_field)
                    conflicts.append({
                        "entity_type": "class", "entity_id": class_id,
                        "product_code": code, "class_code": class_code,
                        "field_name": field, "current_value": current_value,
                        "incoming_value": incoming_value,
                        "current_source": mine.get("source_doc_id", ""),
                        "incoming_source": f"class_fees:{fee.get('page', '')}",
                        "resolution": "USE_INCOMING_SUHYEON",
                        "resolution_reason": "Suhyeon is authoritative for class and quantitative runtime data",
                        "review_status": "RESOLVED",
                    })
    write_csv(OUT / "class_enrichment.csv", class_rows)

    # Long-form periodic returns.
    periodic_rows = []
    for row in load_json("class_returns.json"):
        code = row["product_code"]
        mapped = product_map.get(code)
        if not mapped:
            continue
        class_code = row.get("class_code") or ""
        class_id = class_mapping.get((code, class_code), "") if class_code else ""
        for period, value in (row.get("values") or {}).items():
            periodic_rows.append({
                "fund_id": mapped["fund_id"], "product_code": code,
                "class_id": class_id, "team_class_code": class_code,
                "row_kind": row.get("row_kind"), "period": period.upper(),
                "return_pct": value, "inception_date": row.get("inception_date"),
                "source_page": row.get("page"), "source_pages": json_text(row.get("source_pages")),
                "method": row.get("method"), "confidence": row.get("confidence"),
                "evidence": row.get("evidence"),
            })
    write_csv(OUT / "performance_periodic_enrichment.csv", periodic_rows)

    yearly_rows = []
    for row in load_json("yearly_returns.json"):
        mapped = product_map.get(row["product_code"])
        if not mapped:
            continue
        value = number(row.get("return_pct"))
        quality = "REVIEW_REQUIRED" if value is not None and (value < -100 or value > 500) else "NORMAL"
        yearly_rows.append({
            "fund_id": mapped["fund_id"], "product_code": row["product_code"],
            "class_id": class_mapping.get((row["product_code"], row.get("class_code") or ""), ""),
            "team_class_code": row.get("class_code"), "row_kind": row.get("row_kind"),
            "year_rank": row.get("year_rank"), "period": row.get("period"),
            "return_pct": row.get("return_pct"), "source_page": row.get("page"),
            "quality_status": quality,
        })
    write_csv(OUT / "performance_yearly.csv", yearly_rows)

    aum_rows = []
    for row in load_json("fund_aum.json"):
        mapped = product_map.get(row["product_code"])
        if not mapped:
            continue
        multiplier = 1_000_000 if row.get("unit") == "백만원" else 1
        for index, value in enumerate(row.get("net_asset_total") or []):
            aum_rows.append({
                "fund_id": mapped["fund_id"], "product_code": row["product_code"],
                "period_rank": index + 1, "aum_type": "순자산총액",
                "aum_value_raw": value, "aum_unit": row.get("unit"),
                "aum_value_krw": int(value * multiplier) if value is not None else "",
                "as_of_date": "", "source_page": row.get("page"),
                "method": row.get("method"), "confidence": row.get("confidence"),
                "quality_status": "REVIEW_REQUIRED_MISSING_DATE",
                "evidence": row.get("evidence"),
            })
    write_csv(OUT / "fund_aum_enrichment.csv", aum_rows)

    asset_rows = []
    for row in load_json("asset_mix.json"):
        mapped = product_map.get(row["product_code"])
        if not mapped:
            continue
        for item in row.get("items") or []:
            asset_rows.append({
                "fund_id": mapped["fund_id"], "product_code": row["product_code"],
                "asset_category": item.get("asset"), "amount": item.get("amount"),
                "allocation_pct": item.get("pct"), "total_amount": row.get("total_amount"),
                "as_of_date": row.get("as_of"), "source_page": row.get("page"),
                "method": row.get("method"), "pct_derived": row.get("pct_derived"),
            })
    write_csv(OUT / "asset_mix.csv", asset_rows)

    def mapped_rows(source_name, transform):
        result = []
        for row in load_json(source_name):
            code = row.get("product_code")
            mapped = product_map.get(code) if code else None
            result.append(transform(row, mapped))
        return result

    write_csv(OUT / "trade_rules.csv", mapped_rows("trade_rules.json", lambda r, m: {
        "fund_id": m["fund_id"] if m else "", "product_code": r.get("product_code"),
        "kind": r.get("kind"), "rule_text": r.get("text"), "source_page": r.get("page"),
    }))
    write_csv(OUT / "manager_info.csv", mapped_rows("manager_info.json", lambda r, m: {
        "fund_id": m["fund_id"] if m else "", "product_code": r.get("product_code"),
        "name": r.get("name"), "birth_year": r.get("birth_year"),
        "manager_fund_count": r.get("manager_fund_count"),
        "manager_aum_100m_won": r.get("manager_aum_100m_won"), "career": r.get("career"),
        "source_page": r.get("page"), "method": r.get("method"),
        "confidence": r.get("confidence"), "is_product_aum": r.get("is_product_aum"),
        "evidence": r.get("evidence"),
    }))
    write_csv(OUT / "product_charges.csv", mapped_rows("product_charges.json", lambda r, m: {
        "fund_id": m["fund_id"] if m else "", "product_code": r.get("product_code"),
        "redemption_note": r.get("redemption_note"),
    }))
    write_csv(OUT / "institution_facts.csv", load_json("institution_facts.json"))

    write_csv(VALIDATION / "integration_conflicts.csv", conflicts)

    status_counts = defaultdict(int)
    for row in class_rows:
        status_counts[row["mapping_status"]] += 1
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if not mapping_errors else "failed",
        "baseline": {"documents": len(documents), "funds": len(funds), "classes": len(classes),
                     "performance": len(performance), "aum": len(aum)},
        "integrated": {
            "fund_products": len(product_rows), "document_aliases": len(alias_rows),
            "product_master": len(product_flat), "class_enrichment": len(class_rows),
            "periodic_return_rows": len(periodic_rows), "yearly_return_rows": len(yearly_rows),
            "fund_aum_history_rows": len(aum_rows), "asset_mix_rows": len(asset_rows),
        },
        "class_mapping_status": dict(status_counts),
        "mapping_errors": len(mapping_errors),
        "conflicts_total": len(conflicts),
        "conflicts_for_review": sum(r["review_status"] == "REVIEW_REQUIRED" for r in conflicts),
        "conflicts_resolved_by_policy": sum(r["review_status"] == "RESOLVED" for r in conflicts),
        "yearly_return_outliers": sum(r["quality_status"] != "NORMAL" for r in yearly_rows),
        "invariants": {
            "existing_processed_untouched": True,
            "runtime_class_and_quantitative_source": "suhyeon",
            "all_team_products_mapped": len(product_rows) == len(master_codes),
            "product_code_unique": len(product_rows) == len({r['product_code'] for r in product_rows}),
        },
    }
    (VALIDATION / "integration_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
