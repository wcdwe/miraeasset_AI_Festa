from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.hcx import HcxError, chat, is_configured

from .prompts import ANSWER_GENERATOR_PROMPT
from .schemas import ContextBundle, QueryPlan, ValidationErrorItem


@dataclass(frozen=True)
class GenerationOutcome:
    answer: str | None
    status: str


_RE_EVIDENCE_HEADER = re.compile(
    r"\[EVIDENCE ([^\]]+)\] source=([^;\n]+)(?:; page=(\d+))?")
# LLM은 같은 태그를 [T1-RAG-2] / [EVIDENCE T1-RAG-2] / [근거 T1-RAG-2]처럼
# 제각기 써 온다. 태그 표기를 프롬프트로 통일시키려 하지 말고 읽는 쪽에서
# 흡수한다 - 어차피 실제 출처 문자열은 Python이 채우므로 ID만 알아보면 된다.
_RE_EVIDENCE_TAG = re.compile(
    r"\[(?:EVIDENCE|근거)?\s*([A-Za-z0-9][A-Za-z0-9._-]*)\]", re.IGNORECASE)


# LLM이 태그를 맨몸으로 두지 않고 "([T1-RAG-2], p.15)"처럼 괄호와 페이지를
# 스스로 덧붙여 쓰면, 태그만 바꿔치기한 결과가 "((출처: A, p.15), p.15)"로
# 겹친다(실측). 안쪽 인용만 남기고 LLM이 덧댄 껍데기를 걷어낸다.
_RE_REWRAPPED_CITATION = re.compile(
    r"\(\s*(\(출처:[^()]*\))\s*(?:,\s*p\.?\s*\d+\s*)?\)")


def resolve_evidence_citations(answer: str, context_text: str) -> str:
    """LLM은 [T1-RAG-2] 같은 근거 ID 태그만 쓰고, 실제 "(출처: doc23, p.1)"
    문자열은 여기서 근거 헤더를 그대로 읽어 채운다. LLM이 출처 문자열을
    직접 타이핑하면 0패딩·대소문자를 마음대로 바꿔 쓰는 문제가 있었는데
    (예: doc23 -> DOC00023), ID 태그를 그대로 대괄호째 옮겨쓰는 것만
    요구하면 그 여지가 없어진다."""
    if not answer:
        return answer
    id_map = {
        eid: (source.strip(), page)
        for eid, source, page in _RE_EVIDENCE_HEADER.findall(context_text or "")
    }

    def _sub(match: re.Match) -> str:
        eid = match.group(1)
        info = id_map.get(eid)
        if info is None:
            return match.group(0)
        source, page = info
        return f"(출처: {source}, p.{page})" if page else f"(출처: {source})"

    return _RE_REWRAPPED_CITATION.sub(r"\1", _RE_EVIDENCE_TAG.sub(_sub, answer))


def generate_answer(
    question: str,
    plan: QueryPlan,
    context: ContextBundle,
    errors: list[ValidationErrorItem] | None = None,
    max_tokens: int = 900,
    previous_answer: str | None = None,
) -> GenerationOutcome:
    if not is_configured():
        return GenerationOutcome(None, "HCX 키가 없어 답변 생성 생략")
    if not context.evidence_ids or not context.text.strip():
        return GenerationOutcome(None, "답변에 사용할 근거가 없어 생성 생략")
    correction = ""
    if errors:
        items = "\n".join(
            f"- {item.criterion}: {item.problem} / 수정: {item.correction}"
            for item in errors[:8]
        )
        correction = f"\n\n이전 답변의 아래 오류만 고쳐 새 답변을 작성하라.\n{items}"
        if previous_answer:
            correction += f"\n<이전 답변>\n{previous_answer}\n</이전 답변>"
    user = (
        f"<질문>\n{question}\n</질문>\n"
        f"<실행계획>\n{plan.model_dump_json()}\n</실행계획>\n"
        f"<근거>\n{context.text}\n</근거>"
        f"\n<근거 한계>예산으로 제외된 Task={context.missing_task_ids}; 이 요구는 확인불가로 명시하고 추측하지 마라.</근거 한계>"
        f"{correction}"
    )
    try:
        answer = chat(
            [
                {"role": "system", "content": ANSWER_GENERATOR_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.1,
            stage="repair_generator" if errors else "generator",
        )
    except HcxError as exc:
        return GenerationOutcome(None, f"HCX 답변 생성 실패: {exc}")
    answer = resolve_evidence_citations(answer, context.text)
    return GenerationOutcome(answer, "HCX 근거 기반 답변 생성 완료")
