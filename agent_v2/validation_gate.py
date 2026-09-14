from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from .grounding_validator import ADVISORY_CRITERIA, validate_grounding
from .pre_router import assess_risk
from .schemas import ContextBundle, Evidence, QueryPlan, RiskDecision, ValidationResult
from .validator_llm import validate_with_llm


@dataclass(frozen=True)
class RepairResult:
    answer: str
    evidence: list[Evidence]
    context: ContextBundle


@dataclass(frozen=True)
class GateOutcome:
    answer: str
    status: str
    risk: RiskDecision
    evidence: list[Evidence]
    context: ContextBundle
    python_validation: ValidationResult
    llm_validation: ValidationResult | None
    retry_count: int
    used_safe_fallback: bool
    history: tuple[dict, ...] = ()


RepairHandler = Callable[..., RepairResult | None]
LlmValidator = Callable[[str, str, QueryPlan, list[Evidence], ContextBundle], ValidationResult]


MAX_FALLBACK_DOCUMENTS = 3
FALLBACK_EXCERPT_CHARS = 300
# 재생성은 한 번에 다 고쳐지지 않고 한 번마다 남은 지적이 줄어든다(실측:
# 1차에 항목 3개 미인용 -> 2차에 1개 -> 3차 PASS). 기회가 한 번뿐이면 거의
# 다 고친 답변도 통째로 버려져 근거 원문만 나갔다. 3회까지 시도해 봤지만
# PASS 여부는 시도 횟수가 아니라 매 호출의 생성 품질에 좌우돼(실측: 3회
# 설정에서 표본 6개 중 1개만 PASS - 2회일 때와 다르지 않은 노이즈 범위),
# 시간만 늘리고 확실한 이득은 없어 2로 되돌렸다.
MAX_REPAIR_ATTEMPTS = 2


def safe_answer(evidence: list[Evidence]) -> str:
    # Do not present unvalidated generated prose or truncated raw RAG as verified.
    # 검증된 계산 결과(TAX)는 LLM이 지어낸 문장이 아니라 Python이 규칙대로
    # 구한 값이라, 생성 답변을 버리는 상황에서도 그대로 내보낼 수 있는
    # 가장 확실한 근거다. 예전엔 metric·policy만 실어서 세액공제 계산이
    # 끝났는데도 "확정할 수 없습니다"만 나갔다.
    usable = [e for e in evidence if e.data.get("verified") and
              (e.data.get("metric") or e.kind in {"policy", "calculation"})]
    lines = ["답변 전체의 근거 연결을 충분히 검증하지 못했습니다."]
    if usable:
        lines.append("현재 구조화 자료에서 직접 확인되는 항목은 다음과 같습니다.")
        for e in usable:
            citation = e.source + (f", p.{e.page}" if e.page is not None else "")
            lines.append(f"- {e.content} (출처: {citation})")
        return "\n".join(lines)

    lines.append("검증되지 않은 문장을 사실로 안내하지 않겠습니다.")
    # 생성된 문장은 버리되 검색된 문서 원문까지 같이 버리면 사용자에게는
    # 아무 단서도 없는 거절만 남는다. 원문은 LLM이 지어낸 말이 아니라
    # 실제로 찾은 자료이므로, "확정된 답이 아니라 참고 원문"이라고 분명히
    # 구분해 출처와 함께 보여준다.
    documents = [e for e in evidence if e.kind == "document" and (e.content or "").strip()]
    if not documents:
        lines.append("제공된 근거만으로 요청한 결론을 확정할 수 없습니다.")
        return "\n".join(lines)
    lines.append("다만 질문과 관련해 검색된 문서 원문은 아래와 같습니다. "
                 "해석·결론이 아니라 원문 그대로이니 직접 확인해 주세요.")
    for item in documents[:MAX_FALLBACK_DOCUMENTS]:
        citation = item.source + (f", p.{item.page}" if item.page is not None else "")
        excerpt = " ".join((item.content or "").split())[:FALLBACK_EXCERPT_CHARS]
        lines.append(f"- {excerpt} (출처: {citation})")
    return "\n".join(lines)


def run_validation_gate(question, answer, plan, evidence, context, *,
                        answer_source="LLM", repair_handler=None,
                        llm_validator=validate_with_llm) -> GateOutcome:
    guarded_intents = [*plan.intents, *( ["세제"] if "TAX" in plan.tools else [] )]
    risk = assess_risk(guarded_intents, plan.safety_flags, answer_source)
    current_answer, current_evidence, current_context = answer, evidence, context
    history, retry_count = [], 0
    llm_result = None
    # Python can verify identifiers/numbers, not entailment of free-form prose.
    semantic_required = answer_source == "LLM" and any(e.kind == "document" for e in evidence)
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        # Validation must use exactly the evidence supplied to this generation.
        visible = [e for e in current_evidence if e.evidence_id in current_context.evidence_ids]
        py_result = validate_grounding(question, current_answer, plan, visible, current_context)
        history.append({"stage": "python", "attempt": attempt,
                        **py_result.model_dump(mode="json")})
        result = py_result
        if py_result.status == "PASS" and (risk.requires_llm_validation or semantic_required):
            llm_result = llm_validator(question, current_answer, plan, visible, current_context)
            history.append({"stage": "semantic", "attempt": attempt,
                            **llm_result.model_dump(mode="json")})
            result = llm_result
        if result.status == "PASS":
            return GateOutcome(current_answer, "PASS", risk, current_evidence,
                current_context, py_result, llm_result, retry_count, False, tuple(history))
        if attempt >= MAX_REPAIR_ATTEMPTS or not repair_handler or result.retry_action == "SAFE_FALLBACK":
            break
        retry_count = attempt + 1
        # New handlers get full error/query payload and the actual previous text.
        # Keep injected two-argument test/external handlers backwards compatible.
        import inspect
        if "validation" in inspect.signature(repair_handler).parameters:
            repaired = repair_handler(result.retry_action, result.errors,
                                      validation=result, previous_answer=current_answer)
        else:
            repaired = repair_handler(result.retry_action, result.errors)
        if repaired is None:
            history.append({"stage": "repair", "attempt": 1, "status": "UNAVAILABLE"})
            break
        current_answer, current_evidence, current_context = repaired.answer, repaired.evidence, repaired.context

    # 재생성 기회를 다 썼는데 보완용 지적만 남았다면, 그것 때문에 답변을
    # 버리지 않는다. 질문의 일부를 덜 다룬 답변이, 아무것도 답하지 않는
    # 거절보다 낫다. 근거·수치·안전 지적이 하나라도 남아 있으면 여기 오지
    # 않고 아래 SAFE_FALLBACK으로 간다.
    unresolved = (llm_result or py_result).errors
    if unresolved and all(item.criterion in ADVISORY_CRITERIA for item in unresolved):
        history.append({"stage": "advisory_only", "attempt": retry_count,
                        "criteria": sorted({item.criterion for item in unresolved})})
        return GateOutcome(current_answer, "PASS", risk, current_evidence,
            current_context, py_result, llm_result, retry_count, False, tuple(history))
    return GateOutcome(safe_answer(current_evidence), "SAFE_FALLBACK", risk,
        current_evidence, current_context, py_result, llm_result, retry_count, True, tuple(history))
