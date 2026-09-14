from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from time import monotonic
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ApiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question_id: str
    question: str
    retrieved_context: str
    think_trace: str
    answer: str


class ResponseCache:
    """프로세스 내부의 작은 LRU. 실패 응답은 호출부에서 넣지 않는다."""

    def __init__(self, max_size: int = 256, ttl_seconds: float = 300, version_provider=None):
        self.max_size = max_size
        self._items: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        self._lock = RLock()
        self.ttl_seconds = ttl_seconds
        self._times = {}
        self._version_provider = version_provider or _data_version
        self._version = self._version_provider()

    def get(self, question_id: str, question: str) -> dict[str, str] | None:
        key = (question_id, question)
        with self._lock:
            version = self._version_provider()
            if version != self._version:
                self._items.clear()
                self._times.clear()
                self._version = version
            if monotonic() - self._times.get(key, 0) > self.ttl_seconds:
                self._items.pop(key, None)
                self._times.pop(key, None)
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return dict(value)

    def put(self, value: dict[str, str]) -> None:
        key = (value["question_id"], value["question"])
        with self._lock:
            self._items[key] = dict(value)
            self._times[key] = monotonic()
            self._items.move_to_end(key)
            while len(self._items) > self.max_size:
                evicted, _ = self._items.popitem(last=False)
                self._times.pop(evicted, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._times.clear()


def _data_version():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "data/integrated/structured_store.db", root / "agent_v2/prompts.py",
             root / "data/integrated/chunks.jsonl"]
    return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in paths)


def validate_api_response(payload: dict) -> dict[str, str]:
    return ApiResponse.model_validate(payload).model_dump()
