from __future__ import annotations

import re

from .product_resolver import _duration, _durations, resolve_product
from .structured_request import numeric_filters

from .schemas import (
    AnchorHint, AnchorHints, AnchorLocked, FilterOperator,
    ProductCandidate, QueryAnchor, QueryFilter,
)


_INSTITUTION = re.compile(
    r"DB|DC|IRP|연금저축|퇴직연금|퇴직금|연금|세액공제|연금소득세|중도인출|"
    r"중도해지|계좌이전|매수\s*취소|장외채권|예금\s*만기|재예치",
    re.I,
)
_INSTITUTION_DETAIL = re.compile(
    r"세제|세금|세액공제|중도인출|중도해지|이전|절차|방법|한도|가입|수령",
    re.I,
)


def _products(question: str) -> tuple[list[ProductCandidate], str]:
    resolved = resolve_product(question)
    products = resolved.candidates
    if resolved.status == "not_found":
        named = bool(products or re.search(r"KR[A-Z0-9]{10}|미래에셋|솔로몬", question, re.I))
        return products, "not_found" if named else "none"
    if resolved.status == "ambiguous":
        compact = re.sub(r"\s+", "", question).lower()
        explicit = []
        from os.path import commonprefix
        stems = [re.split(r"증권|투자신탁|자투자신탁|모투자신탁", p.product_name)[0] for p in products]
        prefix = commonprefix(stems)
        has_full_name = any(s.lower() in compact for s in stems)
        for item in products:
            stem = re.split(r"증권|투자신탁|자투자신탁|모투자신탁", item.product_name)[0]
            suffix = stem[len(prefix):] if len(prefix) >= 3 else ""
            if item.product_code.lower() in compact or (stem and stem.lower() in compact) or (
                has_full_name and len(suffix) >= 3 and suffix.lower() in compact
            ):
                explicit.append(item)
        if len(explicit) >= 2 and len(explicit) == len(products):
            return explicit, "multiple"
        # 공통 이름을 한 번만 쓰고 뒤에 기간만 나열하는 표현("솔로몬 국공채
        # 단기·중장기·장기, 뭐가 달라요?")은 전체 상품명을 적지 않았을 뿐
        # 비교 대상을 분명히 지목한 것이다. 후보가 요청된 기간과 하나씩
        # 짝지어지면 되묻지 않고 그 전부를 비교 대상으로 확정한다.
        durations = _durations(question)
        if len(durations) >= 2 and len(products) == len(durations):
            paired = {_duration(item.product_name) for item in products}
            if paired == set(durations):
                return products, "multiple"
        return products, "ambiguous"
    return products, "exact" if resolved.status == "exact" else "unambiguous"




def _fact_types(question: str) -> list[str]:
    facts = []
    for fact_type, pattern in (
        ("RISK_GRADE", r"위험\s*등급|몇\s*등급"),
        ("TOTAL_COST", r"총\s*보수[·ㆍ\s]*비용"),
        ("TOTAL_FEE", r"총\s*보수(?![·ㆍ\s]*비용)"),
        ("SALES_FEE", r"판매\s*보수"),
        ("MANAGEMENT_FEE", r"운용\s*보수|집합투자업자\s*보수"),
        ("AUM", r"AUM|설정액|순자산|운용\s*규모"),
        ("CLASS_ELIGIBILITY", r"가입\s*(?:가능|자격)|판매\s*클래스|연금저축용\s*클래스|퇴직연금용\s*클래스"),
        ("PRODUCT_BASIC", r"상품\s*코드|자산\s*유형|주식형이야|채권형이야"),
    ):
        if re.search(pattern, question, re.I):
            facts.append(fact_type)
    for match in re.finditer(r"(?:최근\s*)?(1년|2년|3년|5년)\s*(?:연평균\s*)?수익률", question):
        facts.append(f"RETURN_{match.group(1)[:-1]}Y")
    return list(dict.fromkeys(facts))


def _filters(question: str) -> list[QueryFilter]:
    return numeric_filters(question)




def extract_anchor(question: str) -> QueryAnchor:
    text = question or ""
    products, status = _products(text)
    accounts = list(dict.fromkeys(re.findall(r"IRP|DC|DB|연금저축", text, re.I)))
    accounts = [item.upper() if item.lower() != "연금저축" else "연금저축" for item in accounts]
    periods = list(dict.fromkeys(re.findall(r"(?:최근\s*)?(\d+년)\s*(?:연평균\s*)?수익률", text)))
    safety = []
    if re.search(r"원금\s*손실.*(싫|안\s*돼|없)|손실.*절대|절대.*손실", text):
        safety.append("loss_intolerance")
    if re.search(r"무조건.*(수익|벌)|수익.*보장|절대.*(오르|수익)", text):
        safety.append("guaranteed_return")

    if products:
        allowed = ["product", "structured"]
        if _INSTITUTION.search(text) and _INSTITUTION_DETAIL.search(text):
            allowed.insert(1, "institution")
    elif _INSTITUTION.search(text):
        allowed = ["institution", "structured"]
    else:
        allowed = ["product", "institution", "structured"]

    limit_match = re.search(r"(?:상위|하위|최대)\s*(\d+)\s*(?:개|건)", text)
    return_all = True if re.search(r"모두|전부|전체", text) else (
        False if limit_match else None
    )
    class_codes = list(dict.fromkeys(re.findall(
        r"(?<![A-Za-z0-9])(A-e|C-P2e|C-Pe|C-P2|C-e|C-RPe|C-RP|A|C|S)(?![A-Za-z0-9])",
        text, re.I,
    )))
    facts = _fact_types(text)
    return QueryAnchor(
        locked=AnchorLocked(
            products=products, product_status=status, class_codes=class_codes,
            filters=_filters(text), periods=periods, account_types=accounts,
            return_all=return_all,
            limit=int(limit_match.group(1)) if limit_match else None,
        ),
        hints=AnchorHints(
            fact_types=AnchorHint(values=facts, confidence="high" if facts else "low"),
            source_types=AnchorHint(values=allowed, confidence="high" if products else "low"),
            safety_flags=AnchorHint(values=safety, confidence="high" if safety else "low"),
        ),
        allowed_source_types=["product", "institution", "structured"],
        forbidden_source_types=[],
    )
