from __future__ import annotations

from typing import Callable
import json
import hashlib
import logging

from .answer_generator import GenerationOutcome, generate_answer
from .context_builder import build_context
from .executor import execute_plan
from .query_analyzer import AnalysisOutcome, analyze
from .schemas import QueryPlan, ToolExecutionResult
from .validation_gate import RepairResult, run_validation_gate
from .rule_planner import build_rule_plan
from .anchor import extract_anchor
from .plan_merger import merge_anchor_plan
from .schemas import QueryAnchor


Analyzer = Callable[..., AnalysisOutcome]
Executor = Callable[[str, QueryPlan], ToolExecutionResult]
Generator = Callable[..., GenerationOutcome]


def _trace(plan: QueryPlan, execution: ToolExecutionResult, generation: str, gate) -> str:
    tools = ", ".join(plan.tools) or "없음"
    py_status = gate.python_validation.status
    llm_status = gate.llm_validation.status if gate.llm_validation else "생략"
    error_summary = "; ".join(
        f"{item.criterion}: {item.problem}"
        for item in gate.python_validation.errors[:3]
    ) or "없음"
    return (
        f"1. QueryPlan 검증 완료: intents={plan.intents}\n"
        f"2. 도구 실행: {tools}, status={execution.status}\n"
        f"3. 근거 조립: {len(execution.evidence)}건\n"
        f"4. 답변 생성: {generation}\n"
        f"5. Python 근거·안전 검증: {py_status}\n"
        f"6. 고위험 검증 LLM: {llm_status}\n"
        f"7. 재처리 횟수: {gate.retry_count}, 최종={gate.status}\n"
        f"   - Python 검증 오류: {error_summary}"
        + "\n8. 최초/재검증 이력: " + json.dumps(gate.history, ensure_ascii=False)
    )


# 실행 오류 원문에는 pydantic 검증 덤프("1 validation error for QueryFilter
# ... https://errors.pydantic.dev/..."), 내부 도구 이름("Task 1 (POLICY):
# 승인된 정책 템플릿과 일치하지 않음") 같은 내부 사정이 섞여 있다. 이걸
# 그대로 answer로 내보내면 고객이 알 수 없는 문자열을 보게 되고 내부 구조도
# 드러난다. 채점자용 상세는 think_trace에 그대로 남기고, answer에는 사용자가
# 스스로 조치할 수 있는 사유만 옮긴다.
_USER_FACING_REASONS = (
    ("납입액", "세액공제를 계산하려면 납입액이 필요합니다."),
    ("소득 정보", "적용 공제율을 정하려면 총급여 또는 종합소득금액이 필요합니다."),
    ("단일 계좌 유형", "계좌를 IRP와 연금저축 중 하나로 특정해 주시면 계산해 드릴 수 있습니다."),
    ("기본 세액공제뿐", "요청하신 세목은 정형 계산 범위 밖이라 문서 근거로만 확인할 수 있습니다."),
    ("특례", "특례 조건은 정형 계산 범위 밖이라 문서 근거로만 확인할 수 있습니다."),
    ("본문 근거를 찾지 못함", "질문에 해당하는 문서 근거를 찾지 못했습니다."),
    ("상품 식별", "질문에 적힌 상품을 특정하지 못했습니다. 정확한 상품명이나 상품코드를 알려주세요."),
    ("상품명이 없음", "어떤 상품을 말씀하시는지 상품명이나 상품코드를 알려주세요."),
    ("COMPARE 대상", "비교하려면 상품을 두 개 이상 특정해 주세요."),
)


def _user_facing_reason(errors: list[str]) -> str:
    messages: list[str] = []
    for error in errors:
        for marker, message in _USER_FACING_REASONS:
            if marker in error and message not in messages:
                messages.append(message)
    return " ".join(messages)


def try_agent_payload(
    question_id: str,
    question: str,
    *,
    analyzer: Analyzer = analyze,
    executor: Executor = execute_plan,
    generator: Generator = generate_answer,
    llm_validator=None,
    anchor: QueryAnchor | None = None,
) -> dict | None:
    """Plan, execute and validate. None signals failure, never legacy RAG fallback."""
    anchor = anchor or extract_anchor(question)
    # 실제 Planner에는 Anchor를 함께 전달한다. 테스트·외부 주입 분석기는
    # 기존 단일 인자 계약을 유지해 교체 가능성을 보존한다.
    analysis = analyzer(question, anchor=anchor) if analyzer is analyze else analyzer(question)
    plan = analysis.plan
    plan_origin = analysis.status
    if plan is None:
        plan = build_rule_plan(question, anchor)
        plan_origin = f"{analysis.status}; Python 규칙 QueryPlan fallback"
    if plan is None:
        return None
    try:
        plan = merge_anchor_plan(anchor, plan, question)
    except ValueError:
        logging.getLogger(__name__).warning("Anchor/Planner contract rejected")
        return None
    execution = executor(question, plan)
    if not execution.evidence:
        return {"question_id": str(question_id), "question": question,
                "retrieved_context": "확인된 근거 없음",
                "answer": ("요청한 조건을 확인하는 데 필요한 근거 또는 실행 입력이 부족합니다. "
                           "상품이 없다는 뜻은 아닙니다. "
                           + _user_facing_reason(execution.errors)).strip(),
                "think_trace": f"계획={plan_origin}; 도구실행={execution.status}; errors={execution.errors}; LLM 생성 생략",
                "route": "agent_insufficient"}
    if execution.status == "PASS" and set(plan.tools).issubset({"FACT", "FILTER", "COMPARE"}):
        from .product_repository import render_result
        parts = []
        for key, value in execution.tool_results.items():
            if ":" not in key: continue
            task_id = key.split(":")[1]
            facts = [e for e in execution.evidence if e.data.get("task_id") == task_id and "metric" in e.data]
            parts.append(render_result(value, facts))
        body = "\n\n".join(parts)
        if body:
            return {"question_id": str(question_id), "question": question,
                    "retrieved_context": body, "answer": body, "route": "agent_structured",
                    "think_trace": f"계획={plan_origin}; 정형 Task 입력 실행; 동일 클래스 조건; 생성/검증 LLM 불필요"}
    context = build_context(plan, execution)
    if not context.evidence_ids:
        return {"question_id": str(question_id), "question": question,
                "retrieved_context": "근거가 존재하지만 현재 입력 예산에 완전한 근거를 담지 못함",
                "answer": "관련 자료는 검색됐지만 답변을 검증하는 데 필요한 근거를 현재 처리 범위에 담지 못했습니다. "
                          "자료가 없거나 조건에 맞는 상품이 없다는 뜻은 아닙니다.",
                "think_trace": f"Context budget exceeded; omitted={len(context.omitted_evidence_ids)}; 생성 LLM 미호출",
                "route": "context_insufficient"}
    generated = generator(question, plan, context)
    if not generated.answer:
        # 생성이 실패해도(키 없음·호출 실패·제한 시간 소진) 요청 전체를
        # 실패시키지 않는다. 근거는 이미 확보돼 있으므로, 지어낸 문장 대신
        # 검증 게이트와 같은 방식으로 찾은 근거를 그대로 돌려준다.
        from .validation_gate import safe_answer
        return {"question_id": str(question_id), "question": question,
                "retrieved_context": str(context.text),
                "answer": safe_answer(execution.evidence),
                "think_trace": f"계획={plan_origin}; 도구실행={execution.status}; "
                               f"답변 생성 실패({generated.status}); 확보된 근거만 반환",
                "route": "generation_unavailable"}

    def repair(action, errors, *, validation=None, previous_answer=None):
        current_execution = execution
        current_context = context
        if action in {"RESOLVE_PRODUCT", "REQUERY_DATA", "RECALCULATE", "RETRIEVE_MORE"}:
            retry_plan = plan.model_copy(deep=True)
            queries = validation.missing_evidence_queries if validation else []
            affected = {e.evidence_id.split("-")[0][1:] for e in errors if e.evidence_id and e.evidence_id.startswith("T")}
            if action == "RETRIEVE_MORE" and queries:
                for step in retry_plan.plan:
                    for query in queries:
                        if step.tool == "RAG" and (not query.product_code or query.product_code in step.inputs.get("product_codes", [])):
                            step.inputs["query"] = query.query or query.required_fact or step.purpose
                            step.inputs["fact_types"] = [query.fact_type] if query.fact_type else step.inputs.get("fact_types", [])
                            affected.add(str(step.step))
            if not affected:
                targets = {"RESOLVE_PRODUCT": {"RESOLVE"}, "REQUERY_DATA": {"FACT", "FILTER", "COMPARE"},
                           "RECALCULATE": {"TAX"}, "RETRIEVE_MORE": {"RAG"}}[action]
                affected = {str(s.step) for s in retry_plan.plan if s.tool in targets}
            if not affected:
                return None
            # A resolver repair cannot safely replay downstream work without a
            # newly validated identity. Do not guess a new product.
            if action == "RESOLVE_PRODUCT": return None
            retry_plan.plan = [s for s in retry_plan.plan if str(s.step) in affected]
            for step in retry_plan.plan: step.depends_on = []
            retry_plan.tools = list(dict.fromkeys(s.tool for s in retry_plan.plan))
            updated = executor(question, retry_plan)
            retained = [e for e in execution.evidence if str(e.data.get("task_id")) not in affected]
            current_execution = ToolExecutionResult(status=updated.status,
                evidence=[*retained, *updated.evidence], errors=updated.errors,
                tool_results={**execution.tool_results, **updated.tool_results})
            current_context = build_context(plan, current_execution)
        kwargs = {"errors": errors}
        if generator is generate_answer: kwargs["previous_answer"] = previous_answer
        retry = generator(question, plan, current_context, **kwargs)
        if not retry.answer:
            return None
        return RepairResult(retry.answer, current_execution.evidence, current_context)

    gate_kwargs = {"repair_handler": repair}
    if llm_validator is not None:
        gate_kwargs["llm_validator"] = llm_validator
    gate = run_validation_gate(
        question, generated.answer, plan, execution.evidence, context,
        answer_source="LLM", **gate_kwargs,
    )
    logging.getLogger("agent_v2.audit").info(json.dumps({
        "question_hash": hashlib.sha256(question.encode()).hexdigest()[:16],
        "plan_origin": plan_origin, "tasks": execution.tool_results.get("task_states", {}),
        "validation_history": gate.history, "retry_count": gate.retry_count,
        "status": gate.status,
    }, ensure_ascii=False))
    return {
        "question_id": str(question_id),
        "question": str(question),
        "retrieved_context": str(gate.context.text),
        "think_trace": f"0. 계획 출처: {plan_origin}\n" + _trace(
            plan, execution, generated.status, gate
        ),
        "answer": str(gate.answer),
        "route": "agent_v2",
    }
