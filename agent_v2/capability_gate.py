"""LLM은 도구를 최종 결정하지 않는다.

Planner는 "계산 의도로 보이나/세금 얘기가 있나"만 확률적으로 판단할 뿐,
"지원 세목인가, 필수 입력이 다 채워졌는가" 같은 실행 가능성 판정은
Python이 한다. 파이프라인은 이렇다.

    Planner -> Pydantic -> Plan Merger -> Capability Gate -> Task Executor

plan_merger.py는 Anchor와 LLM 계획을 합치는 역할만 맡고, "이 도구가 실제로
실행 가능한가"는 여기서 도구별로 따로 검사한다 - 한 파일에 다 넣으면
plan_merger가 도구 수만큼 계속 자라난다.

지금 실제로 REWRITE(다른 도구로 바꿔서 실행해 다른 결과를 낸다)가 필요한
건 TAX뿐이다(실측: 납입액 없는 서술형 세제 질문에 TAX를 골라 매번
실패했다). FACT/FILTER/COMPARE/RAG는 이미 task_executor.py의 실행 시점
검사가 안전하게 처리한다(Task FAIL만 하지 크래시하지 않는다) - 그걸 여기로
옮겨도 결과가 같고 코드만 중복되므로, 실제로 그런 실패가 관측되기 전까지는
자리만 비워 둔다(PASS 고정)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .schemas import PlanStep, QueryAnchor


@dataclass(frozen=True)
class GateResult:
    status: Literal["PASS", "REWRITE", "BLOCK"]
    reason: str = ""
    replacement_tool: str | None = None
    corrected_inputs: dict | None = None


def _validate_tax(step: PlanStep, anchor: QueryAnchor, question: str) -> GateResult:
    from .tax_inputs import calculate
    try:
        calculate(question, {})
        return GateResult(status="PASS")
    except ValueError as exc:
        return GateResult(
            status="REWRITE", reason=str(exc), replacement_tool="RAG",
            corrected_inputs={"source_types": ["institution"]},
        )


def _pass_through(step: PlanStep, anchor: QueryAnchor, question: str) -> GateResult:
    return GateResult(status="PASS")


_VALIDATORS = {
    "TAX": _validate_tax,
    # 실제 실패 사례가 관측되기 전까지는 자리만 확보한다(모듈 docstring 참고).
    "FACT": _pass_through,
    "FILTER": _pass_through,
    "COMPARE": _pass_through,
    "RAG": _pass_through,
}


def validate_step(step: PlanStep, anchor: QueryAnchor, question: str) -> GateResult:
    validator = _VALIDATORS.get(step.tool)
    return validator(step, anchor, question) if validator else GateResult(status="PASS")
