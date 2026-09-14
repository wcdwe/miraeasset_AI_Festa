from __future__ import annotations

import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace


# 평가는 제한 시간 안에 응답하지 못하면 답을 못 한 것으로 친다. 호출 한 건의
# 타임아웃만으로는 이걸 지킬 수 없다 - urllib의 timeout은 소켓 읽기 간격이라
# 서버가 응답을 조금씩 흘리면 20초 설정에도 한 호출이 수백 초로 늘어나고
# (실측 404초), 한 요청이 계획·생성·재생성·검증으로 최대 6번 호출한다.
# 그래서 호출마다가 아니라 "요청 전체"에 남은 시간을 두고, 남은 시간을
# 각 호출의 타임아웃 상한으로 넘긴다.
DEFAULT_REQUEST_BUDGET_SECONDS = 240.0
# 예산이 이보다 적게 남으면 새 호출을 시작하지 않는다. 시작해 봐야 응답을
# 받기 전에 시간이 끝나 결과를 못 쓰고 시간만 버린다.
MIN_CALL_BUDGET_SECONDS = 5.0

_deadline: ContextVar[float | None] = ContextVar("agent_v2_deadline", default=None)


def start_request_budget(seconds: float | None = None) -> None:
    if seconds is None:
        seconds = float(os.environ.get(
            "AGENT_REQUEST_BUDGET_SECONDS", DEFAULT_REQUEST_BUDGET_SECONDS))
    _deadline.set(time.monotonic() + seconds if seconds > 0 else None)


def clear_request_budget() -> None:
    _deadline.set(None)


def remaining_budget() -> float | None:
    """남은 초. 예산을 걸지 않았으면 None(무제한)."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


@dataclass(frozen=True)
class LlmUsage:
    calls: int = 0
    failed_calls: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    http_attempts: int = 0
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_usage_responses: int = 0
    calls_by_stage: tuple[tuple[str, int], ...] = ()


_usage: ContextVar[LlmUsage] = ContextVar("agent_v2_llm_usage", default=LlmUsage())


def reset_usage() -> None:
    _usage.set(LlmUsage())


def _estimate_tokens(text: str) -> int:
    # HCX 응답이 토큰 사용량을 제공하지 않는 환경에서도 예산 추세를 볼 수
    # 있도록 보수적인 문자 기반 추정치를 남긴다. 실제 청구 토큰과는 다르다.
    return max(1, (len(text or "") + 2) // 3)


def record_call(messages: list[dict], stage: str = "unspecified") -> None:
    current = _usage.get()
    prompt = "\n".join(str(item.get("content", "")) for item in messages)
    stages = dict(current.calls_by_stage)
    stages[stage] = stages.get(stage, 0) + 1
    _usage.set(replace(
        current,
        calls=current.calls + 1,
        calls_by_stage=tuple(stages.items()),
        estimated_input_tokens=current.estimated_input_tokens + _estimate_tokens(prompt),
    ))


def record_success(output: str) -> None:
    current = _usage.get()
    _usage.set(replace(
        current,
        estimated_output_tokens=current.estimated_output_tokens + _estimate_tokens(output),
    ))


def record_failure() -> None:
    current = _usage.get()
    _usage.set(replace(current, failed_calls=current.failed_calls + 1))


def record_http_attempt():
    current = _usage.get()
    _usage.set(replace(current, http_attempts=current.http_attempts + 1))


def record_actual_usage(payload):
    result = payload.get("result") or {}
    usage = result.get("usage") or payload.get("usage") or {}
    incoming = result.get("inputLength", usage.get("prompt_tokens", usage.get("inputTokens")))
    outgoing = result.get("outputLength", usage.get("completion_tokens", usage.get("outputTokens")))
    if not isinstance(incoming, int) or not isinstance(outgoing, int): return
    current = _usage.get()
    _usage.set(replace(current, actual_input_tokens=current.actual_input_tokens + incoming,
        actual_output_tokens=current.actual_output_tokens + outgoing,
        actual_usage_responses=current.actual_usage_responses + 1))


def usage_snapshot() -> LlmUsage:
    return _usage.get()
