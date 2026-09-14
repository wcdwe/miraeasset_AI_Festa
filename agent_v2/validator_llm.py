from __future__ import annotations

import json

from pydantic import ValidationError

from scripts.hcx import HcxError, chat, is_configured

from . import json_extract
from .prompts import FINAL_VALIDATOR_PROMPT
from .schemas import ContextBundle, Evidence, QueryPlan, ValidationErrorItem, ValidationResult

_extract_json = json_extract.extract_json_object


# HCX는 판정 내용은 맞게 내면서도 포장만 다르게 하는 일이 잦다(실측:
# {"validationResult": {"errors": [], "missingEvidences": []}} - 봉투로 한 번
# 감싸고 camelCase를 쓰며 status·retry_action을 생략). 이걸 스키마 오류로만
# 처리하면 Python 검증까지 통과한 답변이 포장 차이 하나로 통째로 버려진다.
# 직렬화 차이만 흡수하고 판정 내용은 새로 해석하지 않는다.
_ENVELOPE_KEYS = ("validationResult", "validation_result", "ValidationResult", "result")
_FIELD_ALIASES = {
    "retryAction": "retry_action",
    "missingEvidences": "missing_evidence_queries",
    "missingEvidenceQueries": "missing_evidence_queries",
    "missing_evidences": "missing_evidence_queries",
}


_JSON_ONLY_RETRY = (
    "직전 응답은 JSON 객체가 아니었다. 설명·Markdown·코드펜스 없이 아래 키만 가진 "
    "JSON 객체 하나만 출력하라.\n"
    '{"status":"PASS 또는 FAIL","retry_action":"NONE|RESOLVE_PRODUCT|REQUERY_DATA|'
    'RECALCULATE|RETRIEVE_MORE|REGENERATE|SAFE_FALLBACK",'
    '"errors":[{"criterion":"","problem":"","correction":""}],'
    '"missing_evidence_queries":[]}'
)


def _normalize(data: dict) -> dict:
    for key in _ENVELOPE_KEYS:
        inner = data.get(key)
        if isinstance(inner, dict):
            data = inner
            break
    data = {_FIELD_ALIASES.get(key, key): value for key, value in data.items()}
    # 빠진 판정 필드는 ValidationResult가 이미 정의해 둔 등가 관계로만 채운다
    # (PASS면 오류 없음, 오류가 있으면 FAIL). 판정을 새로 만드는 것이 아니다.
    if "status" not in data and isinstance(data.get("errors"), list):
        data["status"] = "FAIL" if data["errors"] else "PASS"
    if "retry_action" not in data:
        # 모델이 후속 조치를 밝히지 않은 FAIL은 가장 보수적인 쪽으로 닫는다.
        data["retry_action"] = "NONE" if data.get("status") == "PASS" else "SAFE_FALLBACK"
    return data


def _parse_or_none(text: str) -> ValidationResult | None:
    """판정을 읽어내지 못하면 None - 호출부가 한 번 더 물어볼 수 있게 한다."""
    data = _extract_json(text)
    if data is None:
        return None
    try:
        return ValidationResult.model_validate(_normalize(data))
    except ValidationError:
        return None


def parse_validation(text: str) -> ValidationResult:
    result = _parse_or_none(text)
    if result is None:
        return _closed_failure("검증 LLM이 판정을 읽을 수 있는 형식으로 반환하지 않음")
    return result


def _closed_failure(problem: str) -> ValidationResult:
    return ValidationResult(
        status="FAIL",
        retry_action="SAFE_FALLBACK",
        errors=[ValidationErrorItem(
            criterion="검증 수행",
            problem=problem,
            correction="검증되지 않은 고위험 답변을 내보내지 말고 근거 있는 안전 답변 사용",
        )],
    )


def validate_with_llm(
    question: str,
    answer: str,
    plan: QueryPlan,
    evidence: list[Evidence],
    context: ContextBundle,
    max_tokens: int = 800,
) -> ValidationResult:
    """고위험 및 문서 서술 답변의 의미를 검증한다. 호출 실패는 fail-closed다."""
    if not is_configured():
        return _closed_failure("HCX 키가 없어 고위험 답변 검증을 수행할 수 없음")
    payload = {
        "question": question,
        "plan": plan.model_dump(mode="json"),
        "python_validation": {"status": "PASS", "errors": []},
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "context_truncated": context.truncated,
        "missing_task_ids": context.missing_task_ids,
        "answer": answer,
    }
    messages = [
        {"role": "system", "content": FINAL_VALIDATOR_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    # 형식이 어긋난 응답은 "답변이 위험하다"는 판정이 아니라 검증기가 제 일을
    # 못 한 것이다. 그대로 닫아 버리면 멀쩡한 답변이 검증기 사정으로 버려지므로
    # 형식을 못박아 한 번 더 묻는다(실측: JSON 대신 영어 산문으로 판정을 쓰고
    # "the validation result is PASS"로 끝내는 응답). 산문에서 판정을 읽어내지는
    # 않는다 - 그렇게 추측하면 FAIL을 PASS로 뒤집을 위험이 있다. 두 번 다 못
    # 읽으면 fail-closed를 유지한다.
    for attempt in range(2):
        turn = messages if attempt == 0 else [*messages, {"role": "user", "content": _JSON_ONLY_RETRY}]
        try:
            raw = chat(turn, max_tokens=max_tokens, temperature=0.0, stage="validator")
        except HcxError as exc:
            return _closed_failure(f"고위험 답변 검증 호출 실패: {exc}")
        result = _parse_or_none(raw)
        if result is not None:
            return result
    return _closed_failure("검증 LLM이 판정을 읽을 수 있는 형식으로 반환하지 않음")
