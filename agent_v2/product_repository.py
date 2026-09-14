"""One class-granular fact/query contract for Fast Paths and Planner tasks."""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from .schemas import Evidence, QueryFilter
from .structured_request import FACT_FIELDS

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data/integrated/structured_store.db"
FIELDS = {"risk_level", "asset_type", "account_type", "class_code", "total_fee",
          "total_fee_and_cost", "distribution_fee", "aum", "return_since_inception",
          *[f"return_{n}y" for n in (1, 2, 3, 5)]}
LABELS = {"risk_level": "위험등급", "asset_type": "자산유형", "account_type": "계좌 가입대상",
          "class_code": "판매 클래스", "total_fee": "총보수", "total_fee_and_cost": "총보수·비용",
          "distribution_fee": "판매보수", "aum": "상품 순자산(원)",
          "return_since_inception": "설정 이후 수익률",
          **{f"return_{n}y": f"{n}년 수익률" for n in (1, 2, 3, 5)}}
ASSETS = {"채권형": ("채권", "국공채"), "주식형": ("주식", "주식-파생형"),
          "국공채형": ("국공채",), "혼합형": ("주식혼합-재간접형",)}


def connect(path=None):
    conn = sqlite3.connect((Path(path or DB_PATH).resolve().as_uri() + "?mode=ro"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def class_rows(path=None):
    conn = connect(path)
    try:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.*, cm.class_code, cm.account_type, cm.retail, cm.channel,
                   cm.description, cm.raw_label, cm.page AS meaning_page,
                   cc.eligibility, cc.page AS eligibility_page,
                   cf.total_fee, cf.total_fee_and_cost, cf.distribution_fee,
                   cf.as_of AS fee_as_of, cf.page AS fee_page,
                   cr.return_1y, cr.return_2y, cr.return_3y, cr.return_5y,
                   cr.return_since_inception, cr.page AS return_page,
                   fa.net_asset_won AS aum, fa.page AS aum_page
            FROM product_master p
            LEFT JOIN class_meaning cm ON cm.product_code=p.product_code
            LEFT JOIN class_charges cc ON cc.product_code=cm.product_code AND cc.class_code=cm.class_code
            LEFT JOIN class_fees cf ON cf.product_code=cm.product_code AND cf.class_code=cm.class_code
            LEFT JOIN class_returns cr ON cr.product_code=cm.product_code AND cr.class_code=cm.class_code
                 AND cr.row_kind='class_return'
            LEFT JOIN fund_aum fa ON fa.product_code=p.product_code
            ORDER BY p.product_code, cm.class_code
        """)]
        variants = {}
        for item in conn.execute("SELECT * FROM class_fee_sources"):
            record = dict(item)
            try: value = float(record["value"])
            except (TypeError, ValueError): continue
            variants.setdefault((record["product_code"], record["class_code"], record["field"]), []).append(
                {"value": value, "table": record["source"], "page": record["page"]})
        for row in rows:
            conflicts = {}
            for field in ("total_fee", "total_fee_and_cost", "distribution_fee"):
                values = variants.get((row["product_code"], row["class_code"], field), [])
                if len({v["value"] for v in values}) > 1:
                    conflicts[field] = values
                    row[field] = None
            row["_conflicts"] = conflicts
        return rows
    finally:
        conn.close()


def matches(value, item):
    op, target = item.operator.value, item.value
    if op == "is_null": return value is None
    if op == "is_not_null": return value is not None
    if value is None: return False
    if op == "eq": return value == target
    if op == "ne": return value != target
    if op == "in": return value in target if isinstance(target, list) else False
    if op == "contains": return str(target).casefold() in str(value).casefold()
    try: left, right = float(value), float(target)
    except (TypeError, ValueError): return False
    return {"lt": left < right, "lte": left <= right, "gt": left > right, "gte": left >= right}.get(op, False)


def account_match(row, item):
    target = str(item.value).upper()
    if target not in {"IRP", "DC", "DB", "연금저축"}:
        return matches(row.get("account_type"), item)
    if item.operator.value not in {"eq", "contains"}:
        raise ValueError("계좌 조건은 정확한 가입대상 eq/contains만 지원함")
    if target == "연금저축":
        return row.get("account_type") in {"개인연금", "연금저축"}
    text = " ".join(str(row.get(f) or "") for f in ("eligibility", "raw_label", "description"))
    expressions = {"IRP": r"\bIRP\b|개인형\s*퇴직|개인\s*퇴직\s*계좌",
                   "DC": r"\bDC\b|확정\s*기여", "DB": r"\bDB\b|확정\s*급여"}
    if re.search(expressions[target], text, re.I):
        return True
    if row.get("account_type") == "퇴직연금":
        return None  # no evidence of absence or of specific account eligibility
    return False


def row_matches(row, item):
    if item.field in row.get("_conflicts", {}): return None
    if item.field == "account_type": return account_match(row, item)
    if item.field == "asset_type" and isinstance(item.value, str) and item.value in ASSETS:
        contained = row.get("asset_type") in ASSETS[item.value]
        if item.operator.value == "eq": return contained
        if item.operator.value == "ne": return not contained
    return matches(row.get(item.field), item)


def source_names():
    path = ROOT / "data/integrated/document_aliases.csv"
    if not path.exists(): return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {r["product_code"]: r["file_name"] for r in csv.DictReader(handle)}


def query(plan, *, codes=None, classes=None, fields=None, path=None, rows=None):
    fields = list(dict.fromkeys(FACT_FIELDS.get(f, f) for f in (fields or plan.metrics or plan.required_facts)))
    filters = list(plan.filters)
    unknown = set(fields) | {f.field for f in filters} | {s.get("field") for s in plan.sort}
    unknown -= FIELDS
    if unknown: raise ValueError(f"지원되지 않은 정형 항목: {sorted(str(x) for x in unknown)}")
    if not fields: raise ValueError("조회할 지표가 지정되지 않음")
    matched, uncertain = [], []
    class_sensitive = bool(set(fields) - {"risk_level", "asset_type", "aum"}) or any(
        f.field not in {"risk_level", "asset_type", "aum"} for f in filters)
    for row in rows if rows is not None else class_rows(path):
        if codes and row["product_code"] not in codes: continue
        if classes and (row.get("class_code") or "").casefold() not in {c.casefold() for c in classes}: continue
        if class_sensitive and not classes and row.get("retail") != 1: continue
        judgments = [row_matches(row, f) for f in filters]
        if any(x is False for x in judgments): continue
        (uncertain if any(x is None for x in judgments) else matched).append(row)
    # Collapse only identical product-level rows. Never take min fee from a
    # different class or combine a return and eligibility across classes.
    if not class_sensitive and not classes:
        matched = list({r["product_code"]: r for r in reversed(matched)}.values())[::-1]
        for r in matched: r["class_code"] = None
    for s in reversed(plan.sort):
        field = s["field"]
        if s.get("direction") not in {"asc", "desc"}: raise ValueError("잘못된 정렬 방향")
        matched.sort(key=lambda r: r.get(field) if r.get(field) is not None else 0,
                     reverse=s["direction"] == "desc")
        matched.sort(key=lambda r: r.get(field) is None)
    all_count = len({r["product_code"] for r in matched})
    # Limits are product counts; retain every matching class of selected products.
    if plan.limit and not plan.return_all:
        chosen = list(dict.fromkeys(r["product_code"] for r in matched))[:plan.limit]
        matched = [r for r in matched if r["product_code"] in chosen]
    names = source_names()
    evidence = []
    for row_index, row in enumerate(matched, 1):
        for field in fields:
            product_level = field in {"risk_level", "asset_type", "aum"}
            value = row.get(field)
            page_field = "return_page" if field.startswith("return_") else "fee_page" if field in {
                "total_fee", "total_fee_and_cost", "distribution_fee"} else "aum_page" if field == "aum" else "eligibility_page" if field == "account_type" else "meaning_page" if field == "class_code" else None
            page = row.get(page_field) if page_field else None
            source = names.get(row["product_code"], "structured_store.db") if page else "structured_store.db/product_master"
            as_of = row.get("fee_as_of") if page_field == "fee_page" else None
            unit = "%" if field.startswith("return_") or field in {"total_fee", "total_fee_and_cost", "distribution_fee"} else "원" if field == "aum" else "등급" if field == "risk_level" else ""
            rendered = "제공된 자료에서 확인되지 않음" if value is None else f"{value}{unit}"
            if field == "account_type":
                basis = row.get("eligibility")
                if not basis:
                    basis = row.get("description") or row.get("raw_label")
                    page = row.get("meaning_page")
                if basis: rendered += f"; 가입대상 원문: {basis}"
                if page: source = names.get(row["product_code"], "structured_store.db")
            conflict = row.get("_conflicts", {}).get(field)
            if conflict:
                rendered = "표 간 값 충돌로 확정하지 않음: " + "; ".join(
                    f"{v['table']} {v['value']}{unit} (p.{v['page']})" for v in conflict)
            content = f"{row['product_name']} ({row['product_code']})"
            if not product_level and row.get("class_code"): content += f" / {row['class_code']} 클래스"
            if product_level: content += " / 상품 기준"
            content += f": {LABELS[field]} {rendered}"
            if field.startswith("return_") or unit == "%" or field == "aum":
                content += f"; 기준일 {as_of or '확인되지 않음'}"
            evidence.append(Evidence(evidence_id=f"DATA-{row_index}-{field}", kind="structured",
                content=content, source=source, page=page, product_code=row["product_code"],
                class_code=row.get("class_code") if not product_level else None,
                data={"metric": field, "value": value, "unit": unit, "as_of": as_of,
                      "product_name": row["product_name"], "verified": True,
                      "conflicting_values": conflict or [],
                      "eligibility_basis": row.get("eligibility") if field == "account_type" else None}))
    result = {"count": len({r["product_code"] for r in matched}), "total_count": all_count,
              "class_count": len(matched), "rows": [
                  {k: r.get(k) for k in ["product_code", "product_name", "class_code", *fields]} for r in matched],
              "uncertain_product_count": len({r["product_code"] for r in uncertain}),
              "uncertainty_reason": "계좌 가입대상 미확인 또는 동일 지표의 표 간 값 충돌로 조건 충족을 확정하지 않음" if uncertain else "",
              "comparison_basis": "클래스별 수치를 나란히 제공하며 기준일 미확인 값을 동시점 성과 순위로 해석하지 않음"}
    return result, evidence


def render_result(result, evidence):
    lines = [f"자료에서 조건을 확인한 상품 {result['count']}개 (클래스/상품 행 {result['class_count']}건)."]
    if result.get("uncertain_product_count"):
        lines.append(f"별도로 {result['uncertain_product_count']}개 상품은 조건 충족이 불확실해 확정 목록에서 제외했습니다. " + result["uncertainty_reason"])
    if not result["count"]:
        lines.append("이 결과는 상품의 실제 부존재가 아니라, 제공된 데이터에서 조건 충족을 확인하지 못했다는 뜻입니다.")
    for ev in evidence:
        cite = ev.source + (f", p.{ev.page}" if ev.page is not None else "")
        lines.append(f"- {ev.content} (출처: {cite})")
    lines.append(result["comparison_basis"])
    if any(e.data.get("metric") == "risk_level" for e in evidence):
        lines.append("위험등급은 상품 기준이며, 총보수·수익률은 표시된 클래스 기준입니다.")
    if any(e.data.get("metric", "").startswith("return_") for e in evidence):
        lines.append("과거 수익률은 미래 성과를 보장하지 않습니다.")
    return "\n".join(lines)
