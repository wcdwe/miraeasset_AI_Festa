from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from scripts.hcx import HcxError, chat, is_configured

from . import json_extract
from .prompts import QUERY_ANALYZER_PROMPT
from .schemas import QueryAnchor, QueryPlan


PLAN_SHAPE = """반환 JSON 필드:
{
 "intents":[], "entities":{},
 "product_mentions":[{"text":"질문 속 표현","role":"single|comparison_left|comparison_right|filter_target","resolution_required":true}],
 "required_facts":[],
 "filters":[{"field":"필드명","operator":"eq|ne|lt|lte|gt|gte|in|contains|is_null|is_not_null","value":null,"source_text":"질문 속 조건"}],
 "metrics":[], "periods":[], "sort":[{"field":"필드명","direction":"asc|desc"}],
 "limit":null, "return_all":false,
 "missing":{"for_personalization":[],"from_evidence":[]},
 "gap_types":[], "answerable_now":true, "follow_ups":[], "safety_flags":[],
 "tools":[], "completeness":"single_answer|all_matches|all_steps",
 "plan":[{"step":1,"tool":"RESOLVE|FACT|FILTER|COMPARE|RAG|TAX|POLICY","purpose":"",
   "inputs":{"query":"","product_codes":[],"class_codes":[],"source_types":[],"fact_types":[],"filters":[],"periods":[],"metrics":[],"tax_inputs":{}},
   "depends_on":[]}]
}
필수 규칙:
- intents는 1개 이상. product_mentions에는 질문에 직접 나온 펀드·상품명만 넣고 IRP·조건 문장은 넣지 않는다.
- required_facts는 객체가 아니라 FactType 문자열 배열이다(예: ["RISK_NARRATIVE"]).
- 모든 filter에는 질문의 연속된 원문인 source_text가 필수다. metrics는 문자열만 넣는다.
- "값 존재" 조건은 filters에 operator=is_not_null로 넣는다. 정렬 표현이 질문에 없으면 sort=[]이다.
- tools에는 plan에서 쓴 도구를 모두 넣고, plan은 필요한 도구별 단계를 빠짐없이 둔다.
- 각 plan.inputs는 해당 도구가 재해석 없이 실행할 수 있도록 구체적으로 채운다.
- 복합 RAG는 필요한 사실별 step으로 나누고 각 inputs.query에 해당 검색문을 넣는다.
예: "IRP에서 투자 가능하고 채권형이며 5년 수익률이 있는 상품 모두"는
intents=["조건검색"], product_mentions=[], filters=[
{field:"account_type",operator:"eq",value:"IRP",source_text:"IRP에서 투자 가능"},
{field:"asset_type",operator:"eq",value:"채권형",source_text:"채권형"},
{field:"return_5y",operator:"is_not_null",value:null,source_text:"5년 수익률이 있는"}],
metrics=["return_5y"], periods=["5년"], return_all=true, tools=["FILTER"], completeness="all_matches"다."""


@dataclass(frozen=True)
class AnalysisOutcome:
    plan: QueryPlan | None
    status: str
    raw: str = ""


_extract_json = json_extract.extract_json_object


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _preserves_question(
    plan: QueryPlan, question: str, anchor: QueryAnchor | None = None,
) -> list[str]:
    """질문에 없는 상품 표현·필터 원문을 분석기가 새로 만들지 못하게 한다."""
    q = _norm(question)
    errors = []
    anchored_names = {
        _norm(value)
        for product in (anchor.products if anchor else [])
        for value in (product.product_name, product.product_code)
    }
    for mention in plan.product_mentions:
        if _norm(mention.text) not in q and _norm(mention.text) not in anchored_names:
            errors.append(f"질문에 없는 상품 표현: {mention.text}")
    return errors


def parse_plan(
    text: str, question: str, anchor: QueryAnchor | None = None,
) -> AnalysisOutcome:
    data = _extract_json(text)
    if data is None:
        return AnalysisOutcome(None, "JSON 객체를 찾지 못해 폐기", text)
    # HCX가 자주 쓰는 동치 표기만 스키마 표준값으로 정규화한다.
    # 의미를 새로 해석하지 않고 직렬화 차이만 흡수한다.
    operator_aliases = {"ge": "gte", "le": "lte", "greater_equal": "gte", "less_equal": "lte"}
    for item in data.get("filters", []) if isinstance(data.get("filters"), list) else []:
        if isinstance(item, dict) and item.get("operator") in operator_aliases:
            item["operator"] = operator_aliases[item["operator"]]
    required = data.get("required_facts")
    if isinstance(required, list):
        data["required_facts"] = [
            item.get("fact_type") if isinstance(item, dict) else item
            for item in required
            if not isinstance(item, dict) or item.get("fact_type")
        ]
    try:
        plan = QueryPlan.model_validate(data)
    except ValidationError as exc:
        details = [
            {"loc": ".".join(map(str, item["loc"])), "msg": item["msg"]}
            for item in exc.errors()[:3]
        ]
        return AnalysisOutcome(
            None,
            f"Pydantic 스키마 불일치로 폐기: {exc.error_count()}건 {details}",
            text,
        )
    if anchor and anchor.product_status in {"exact", "unambiguous", "multiple"}:
        for mention in plan.product_mentions:
            if _norm(mention.text) in {
                _norm(value)
                for product in anchor.products
                for value in (product.product_name, product.product_code)
            }:
                mention.resolution_required = False
    # LLM이 원문 단어를 건너뛰어 만든 비연속 source_text는 실행 조건으로
    # 신뢰하지 않는다. 계획 전체를 버리지 않고 해당 필터만 제거하며,
    # Python locked 조건은 이후 Plan Merger가 다시 강제한다.
    valid_filters, removed_filters = [], []
    normalized_question = _norm(question)
    for item in plan.filters:
        if item.source_text and _norm(item.source_text) not in normalized_question:
            removed_filters.append(item.source_text)
        else:
            valid_filters.append(item)
    plan.filters = valid_filters
    if removed_filters:
        return AnalysisOutcome(None, f"필터 원문 연결 실패; 조건을 삭제한 채 실행하지 않음: {removed_filters}", text)
    preservation_errors = _preserves_question(plan, question, anchor)
    if preservation_errors:
        return AnalysisOutcome(None, f"질문 의미 보존 실패로 폐기: {preservation_errors[:2]}", text)
    if len(plan.product_mentions) >= 2 and not ({"COMPARE", "RAG"} & set(plan.tools)):
        return AnalysisOutcome(None, "복수 상품 계획에 COMPARE 또는 RAG가 없어 폐기", text)
    if not plan.intents:
        return AnalysisOutcome(None, "intent가 비어 있어 실행계획으로 사용할 수 없어 폐기", text)
    return AnalysisOutcome(plan, "HCX QueryPlan 검증 통과", text)


def analyze(
    question: str,
    anchor: QueryAnchor | None = None,
    max_tokens: int = 1000,
) -> AnalysisOutcome:
    if not (question or "").strip():
        return AnalysisOutcome(None, "빈 질문이라 분석 생략")
    if not is_configured():
        return AnalysisOutcome(None, "HCX 키가 없어 규칙 경로 사용")
    anchor_payload = (anchor or QueryAnchor()).model_dump(mode="json")
    user_payload = {
        "question": question,
        "query_anchor": anchor_payload,
    }
    messages = [
        {"role": "system", "content": QUERY_ANALYZER_PROMPT + "\n\n" + PLAN_SHAPE},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    try:
        raw = chat(messages, max_tokens=max_tokens, temperature=0.0, stage="planner")
    except HcxError as exc:
        return AnalysisOutcome(None, f"HCX 분석 실패로 규칙 경로 사용: {exc}")
    return parse_plan(raw, question, anchor)
