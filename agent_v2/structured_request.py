"""Conservative structured compiler. Unconsumed meaning belongs to the Planner."""
from __future__ import annotations

import re
from .schemas import QueryFilter, QueryPlan, PlanStep

FACT_FIELDS = {
    "RISK_GRADE": "risk_level", "TOTAL_FEE": "total_fee",
    "TOTAL_COST": "total_fee_and_cost", "AUM": "aum",
    "PRODUCT_BASIC": "asset_type", "CLASS_ELIGIBILITY": "account_type",
    "SALES_FEE": "distribution_fee",
    **{f"RETURN_{n}Y": f"return_{n}y" for n in (1, 2, 3, 5)},
}
OPS = {"이상": "gte", "이하": "lte", "초과": "gt", "미만": "lt"}
FIELD_PATTERN = (
    r"총\s*보수\s*[·ㆍ]?\s*비용|총\s*보수|위험\s*등급|"
    r"(?:최근\s*)?[1235]\s*년\s*(?:연평균\s*)?수익률|"
    r"AUM|순자산|설정액|자산\s*유형"
)


def field_name(text):
    text = re.sub(r"\s+", "", text).upper()
    if "수익률" in text:
        return f"return_{re.search('[1235]', text).group()}y"
    if "비용" in text:
        return "total_fee_and_cost"
    return {"총보수": "total_fee", "위험등급": "risk_level", "AUM": "aum",
            "순자산": "aum", "설정액": "aum", "자산유형": "asset_type"}.get(text)


def numeric_filters(question):
    result = []
    pattern = rf"({FIELD_PATTERN})\s*(?:이|가|은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|만)?\s*(?:원|%|등급)?\s*(이상|이하|초과|미만)?"
    for m in re.finditer(pattern, question, re.I):
        field = field_name(m[1])
        if field in {None, "asset_type"}:
            continue
        value = float(m[2].replace(",", ""))
        if field == "aum":
            value *= {None: 1, "억": 100_000_000, "백만": 1_000_000, "만": 10_000}[m[3]]
        elif m[3]:
            continue
        result.append(QueryFilter(field=field, operator=OPS.get(m[4], "eq"),
                                  value=value, source_text=m[0]))
    return result


def compile_structured(question, anchor=None):
    # Anchor imports numeric_filters, so resolve lazily to avoid a module cycle.
    if anchor is None:
        from .anchor import extract_anchor
        anchor = extract_anchor(question)
    if anchor.product_status in {"ambiguous", "not_found"}:
        return None
    remaining = question
    filters = numeric_filters(question)
    metrics = []
    for f in filters:
        remaining = remaining.replace(f.source_text, " ")
        metrics.append(f.field)
    for m in re.finditer(FIELD_PATTERN, remaining, re.I):
        metrics.append(field_name(m[0]))
    for m in re.finditer(rf"({FIELD_PATTERN})\s*(?:이|가|은|는)?\s*(?:자료가\s*)?(존재(?:하는)?|있는|없는)", remaining, re.I):
        filters.append(QueryFilter(field=field_name(m[1]),
            operator="is_null" if m[2] == "없는" else "is_not_null", source_text=m[0]))
        remaining = remaining.replace(m[0], " ")
    remaining = re.sub(FIELD_PATTERN, " ", remaining, flags=re.I)
    for pattern, field in ((r"(?<![A-Za-z])(?:IRP|DC|DB)(?![A-Za-z])|연금저축|퇴직연금", "account_type"),
                           (r"채권혼합형|주식혼합형|채권형|주식형|국공채형", "asset_type")):
        for m in re.finditer(pattern, remaining, re.I):
            filters.append(QueryFilter(field=field, operator="eq", value=m[0].upper(), source_text=m[0]))
        remaining = re.sub(pattern, " ", remaining, flags=re.I)
    # Only remove product spans that the common resolver actually confirmed.
    for product in anchor.products:
        remaining = re.sub(re.escape(product.product_code), " ", remaining, flags=re.I)
        name = product.product_name
        # Confirmed abbreviated names may end before the legal fund suffix.
        stem = re.split(r"증권|증권투자|투자신탁|자투자신탁|모투자신탁", name)[0]
        for candidate in sorted({name, stem}, key=len, reverse=True):
            if candidate:
                remaining = remaining.replace(candidate, " ")
        if len(anchor.products) > 1:
            from os.path import commonprefix
            stems = [re.split(r"증권|투자신탁|자투자신탁|모투자신탁", p.product_name)[0] for p in anchor.products]
            prefix = commonprefix(stems)
            if len(prefix) >= 3 and stem[len(prefix):]:
                remaining = remaining.replace(stem[len(prefix):], " ")
    for code in anchor.locked.class_codes:
        remaining = re.sub(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9-])", " ", remaining, flags=re.I)
    sorts = []
    order = re.search(r"낮은\s*순|높은\s*순|가장\s*낮은|가장\s*높은", remaining)
    if order:
        numeric = [m for m in metrics if m not in {"asset_type", "account_type"}]
        if len(set(numeric)) != 1:
            return None
        sorts = [{"field": numeric[0], "direction": "asc" if "낮" in order[0] else "desc"}]
        remaining = remaining.replace(order[0], " ")
    limit = re.search(r"(?:상위|하위|최대)?\s*(\d+)\s*개", remaining)
    if limit:
        remaining = remaining.replace(limit[0], " ")
        if not sorts and ("상위" in limit[0] or "하위" in limit[0]):
            return None  # metric direction must be explicit, especially risk grades
    remaining = re.sub(r"섞지\s*말고|투자\s*가능(?:하고|한)?|자료|최근|클래스|펀드|상품|"
                       r"각각|비교해줘|비교|알려줘|찾아줘|보여줘|설명해줘|정렬해줘|얼마야|얼마인가요|"
                       r"모두|전부|전체|면서|이고|이며|이면서|에서|대한|정보|해줘", " ", remaining)
    # Grammatical particles only; unknown clauses are never silently discarded.
    remaining = re.sub(r"(?:\b[의와과을를은는이가중도]|\b하고\b)|[\s,.?!·%():]+", "", remaining)
    if remaining or not (metrics or filters):
        return None
    codes = [p.product_code for p in anchor.products]
    tool = "COMPARE" if len(codes) > 1 else "FACT" if codes and not filters else "FILTER"
    metrics = list(dict.fromkeys(m for m in metrics if m))
    if not metrics:
        metrics = list(dict.fromkeys(f.field for f in filters))
    return QueryPlan(intents=["상품 비교" if tool == "COMPARE" else "상품 조회"],
        tools=[tool], filters=filters, metrics=metrics,
        required_facts=metrics, periods=[f"{m[7:-1]}년" for m in metrics if re.fullmatch(r"return_[1235]y", m)],
        return_all=bool(re.search(r"모두|전부|전체", question)),
        limit=int(limit[1]) if limit else None, sort=sorts,
        entities={"anchor_product_codes": codes, "anchor_class_codes": anchor.locked.class_codes},
        plan=[PlanStep(step=1, tool=tool, purpose="완전히 해석된 정형 요청", inputs={
            "product_codes": codes, "metrics": metrics,
            "class_codes": anchor.locked.class_codes,
            "filters": [f.model_dump(mode="json") for f in filters]})])
