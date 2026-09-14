from __future__ import annotations

import re

from scripts.product_lookup import find_products

from .schemas import PreRouteDecision, RiskDecision
from .schemas import QueryAnchor
from .anchor import extract_anchor
from .structured_request import compile_structured


_RECOMMENDATION = re.compile(r"추천|골라|선택해|어떤\s*상품이\s*(좋|나아)")
_LOSS_INTOLERANCE = re.compile(r"원금\s*손실.*(싫|안\s*돼|없)|손실.*절대|절대.*손실")
_GUARANTEED_RETURN = re.compile(r"무조건.*(수익|벌)|수익.*보장|절대.*(오르|수익)")
_LOW_RISK_HIGH_RETURN = re.compile(
    r"((위험|손실).{0,15}(낮|적|없).{0,25}(수익률|수익).{0,12}(높|최고|가장))"
    r"|((수익률|수익).{0,12}(높|최고|가장).{0,25}(위험|손실).{0,15}(낮|적|없))"
)
_PROFILE = re.compile(r"\d{2,3}\s*세|IRP|DC|DB|연금저축|\d+\s*년|장기|단기|손실.*감수|중위험|고위험|저위험")
_COMPLEX = re.compile(r"비교|차이|각각|동시에|이면서|그리고|까지|모두")
_FILTER_FIELD = re.compile(r"IRP|DC|연금저축|채권형|주식형|위험등급|총보수|수익률|AUM|설정액", re.I)
_FILTER_OPERATION = re.compile(r"모두|전부|상위\s*\d+|이상|이하|초과|미만|존재|있는\s*상품|낮은\s*순|높은\s*순")
_COMPARE = re.compile(r"비교|차이|각각|섞지\s*말")


def pre_route(question: str, anchor: QueryAnchor | None = None) -> PreRouteDecision:
    text = question or ""
    anchor = anchor or extract_anchor(text)
    flags = []
    if _LOSS_INTOLERANCE.search(text):
        flags.append("loss_intolerance")
    if _GUARANTEED_RETURN.search(text):
        flags.append("guaranteed_return")
    if _LOW_RISK_HIGH_RETURN.search(text):
        flags.append("risk_return_conflict")

    recommendation = bool(_RECOMMENDATION.search(text))
    if recommendation and not anchor.products and ("loss_intolerance" in flags or "risk_return_conflict" in flags):
        return PreRouteDecision(
            route="FAST_POLICY",
            reasons=["추천 요청에 원금손실 회피 또는 위험·수익 충돌 조건이 포함됨"],
            safety_flags=flags,
            template_id="conflicting_risk_return",
        )
    # 되물어야 하는 상황은 "어떤 상품이 좋아?"라는 특정 표현이 아니라
    # "추천을 원하는데 판단에 필요한 조건(계좌·기간·손실감내)이 하나도
    # 안 밝혀진 상태" 자체다. 표현으로 좁히면 "좋은 연금 상품 하나 추천해
    # 주세요"처럼 흔한 문장이 이 경로를 못 타고 Agent로 넘어가, POLICY
    # 도구가 승인 템플릿을 못 찾아 내부 오류 문자열이 그대로 답변으로
    # 나갔다(실측). 대상 상품이 이미 특정된 질문은 그 상품 자체를 설명해야
    # 하므로 여기서 되묻지 않는다.
    if recommendation and not anchor.products and not _PROFILE.search(text):
        return PreRouteDecision(
            route="FAST_POLICY",
            reasons=["개인화 추천에 필요한 계좌·기간·손실감내 정보가 없음"],
            safety_flags=[*flags, "insufficient_recommendation_context"],
            template_id="recommendation_missing_profile",
        )

    compiled = compile_structured(text, anchor)
    if compiled is not None and not recommendation:
        return PreRouteDecision(
            route="FAST_" + compiled.tools[0],
            reasons=["전체 질문을 정형 계획으로 해석했고 미해석 조건이 없음"],
        )
    return PreRouteDecision(
        route="AGENT",
        reasons=["Fast Path로 확정할 수 없는 서술형·복합 질문"],
        needs_query_llm=True,
        needs_answer_llm=True,
    )


def assess_risk(intents: list[str], safety_flags: list[str], answer_source: str) -> RiskDecision:
    reasons = []
    normalized_intents = {item.replace(" ", "").lower() for item in intents}
    if any(
        any(term in item for term in ("추천", "세제", "tax", "적합", "판단", "선택"))
        for item in normalized_intents
    ):
        reasons.append("고위험 의도")
    if set(safety_flags) & {
        "principal_guarantee", "loss_intolerance", "guaranteed_return",
        "future_prediction", "risk_return_conflict", "recent_performance_only",
    }:
        reasons.append("금융 안전성 검토 필요")
    requires = bool(reasons) and answer_source == "LLM"
    return RiskDecision(
        level="HIGH" if reasons else "LOW",
        reasons=reasons,
        requires_llm_validation=requires,
    )
