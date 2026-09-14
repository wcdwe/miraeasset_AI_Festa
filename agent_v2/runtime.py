"""The only production entry: strict fast compiler, otherwise Planner + gate."""
from .anchor import extract_anchor
from .pre_router import pre_route
from .structured_request import compile_structured
from .plan_merger import merge_anchor_plan
from .executor import execute_plan
from .product_repository import render_result
from .templates import build_policy_payload
from .api_contract import validate_api_response


def finish(body):
    validate_api_response({k: v for k, v in body.items() if k != "route"})
    return body


def structured_payload(question_id, question, *, expected_tool=None):
    anchor = extract_anchor(question)
    plan = compile_structured(question, anchor)
    if plan is None or (expected_tool and plan.tools != [expected_tool]): return None
    plan = merge_anchor_plan(anchor, plan, question)
    result = execute_plan(question, plan)
    if result.status != "PASS": return None
    tool = plan.tools[0]
    value = result.tool_results[tool]
    facts = [e for e in result.evidence if "metric" in e.data]
    # Output is built directly from the very rows that satisfied every filter.
    # Assert cardinality and identifiers instead of stamping a fictitious PASS.
    codes = {r["product_code"] for r in value["rows"]}
    if len(codes) != value["count"] or any(e.product_code not in codes for e in facts):
        raise ValueError("정형 출력과 조회 결과의 상품 집합 불일치")
    answer = render_result(value, facts)
    return finish({"question_id": str(question_id), "question": question,
        "retrieved_context": answer, "answer": answer,
        "think_trace": f"FAST_{tool}; 전체 조건 컴파일; 동일 클래스 조건 교집합; "
                       f"상품 {value['count']}개/행 {value['class_count']}건; 집합·개수 검증 PASS; "
                       f"가입대상 미확인 {value['uncertain_product_count']}개; LLM 호출 없음",
        "route": {"FACT": "single_product", "FILTER": "fast_filter", "COMPARE": "comparison"}[tool]})


def answer_payload(question_id, question):
    from .audit import configure
    from .telemetry import start_request_budget
    configure()
    # 평가 제한 시간을 넘기면 답을 못 한 것으로 처리된다. 남은 시간을 이
    # 요청의 모든 LLM 호출이 공유하게 해서, 호출 하나가 늦어져도 요청 전체가
    # 제한 시간을 넘기지 않도록 한다.
    start_request_budget()
    from scripts import input_guard
    from .orchestrator import try_agent_payload
    blocked = input_guard.check(question_id, question)
    if blocked is not None: return finish(blocked)
    anchor = extract_anchor(question)
    if anchor.product_status in {"ambiguous", "not_found"}:
        candidates = "\n".join(f"- {p.product_name} ({p.product_code})" for p in anchor.products)
        message = ("상품명이 여러 상품과 일치하여 한 상품의 정보로 합칠 수 없습니다. 아래 후보는 서로 다른 상품입니다."
                   if anchor.product_status == "ambiguous" else
                   "질문에 적힌 정확한 상품은 제공된 상품 목록에서 확인되지 않습니다. 아래 유사 후보를 해당 상품으로 대신 설명하지 않겠습니다.")
        return finish({"question_id": str(question_id), "question": question,
            "answer": message + ("\n" + candidates if candidates else "") + "\n정확한 상품명 또는 상품코드가 필요합니다.",
            "retrieved_context": candidates or "상품 목록에서 정확한 일치 없음",
            "think_trace": f"RESOLVE {anchor.product_status}; 임의 후보 고정 차단; 후보 식별 정보만 반환",
            "route": "resolution_required"})
    decision = pre_route(question, anchor)
    if decision.route == "FAST_POLICY": return finish(build_policy_payload(question_id, question, decision))
    if decision.route.startswith("FAST_"):
        body = structured_payload(question_id, question)
        if body is not None: return body
    body = try_agent_payload(question_id, question, anchor=anchor)
    if body is not None: return finish(body)
    # Infrastructure/plan failures must not masquerade as 'no matching product'.
    raise RuntimeError("Agent 계획/생성 실행 불가; 기존 RAG 경로로 전환하지 않음")
