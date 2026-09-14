"""HCX 응답에서 JSON 객체 하나를 최대한 뽑아낸다.

여기서 고치는 건 순수 문법 오류(홑따옴표, trailing comma, 괄호 불일치,
앞뒤에 붙은 설명문)뿐이다. "TAX를 골랐어야 하는데 RAG를 골랐다" 같은
의미 오류는 이 함수의 책임이 아니다 - 문법적으로 멀쩡한 JSON이라
json.loads도, json_repair도 잡아내지 못하고, 그건 Pydantic 스키마
검증과 그 위의 Python 가드(plan_merger 등)가 대신 막는다.
"""
from __future__ import annotations

import json

import json_repair


def extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    candidate = text[start:end + 1] if start >= 0 and end > start else text

    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        pass

    # json_repair는 실패해도 예외 대신 빈 문자열 같은 "JSON이 아닌 값"을
    # 돌려준다(설계상 never-raise). 순수 설명문(중괄호 없음)에도 안전하게
    # 빈 문자열을 돌려줄 뿐 엉뚱한 객체를 지어내지 않는다(확인됨).
    try:
        repaired = json_repair.loads(candidate)
    except Exception:
        return None
    return repaired if isinstance(repaired, dict) else None
