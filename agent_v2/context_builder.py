from __future__ import annotations

from .schemas import ContextBundle, Evidence, QueryPlan, ToolExecutionResult


DEFAULT_CHAR_BUDGET = 8000


def _priority(evidence: Evidence) -> tuple[int, float]:
    order = {"calculation": 0, "structured": 1, "resolution": 2, "document": 3, "policy": 4}
    return order.get(evidence.kind, 9), -float(evidence.data.get("score") or 0)


def _render(evidence: Evidence) -> str:
    fields = [f"source={evidence.source}"]
    if evidence.page is not None:
        fields.append(f"page={evidence.page}")
    fields.append(f"kind={evidence.kind}")
    if evidence.product_code:
        fields.append(f"product_code={evidence.product_code}")
    if evidence.class_code:
        fields.append(f"class_code={evidence.class_code}")
    return f"[EVIDENCE {evidence.evidence_id}] {'; '.join(fields)}\n{evidence.content}"


def build_context(plan: QueryPlan, execution: ToolExecutionResult,
                  char_budget: int = DEFAULT_CHAR_BUDGET) -> ContextBundle:
    """근거를 중복 없이 우선순위대로 조립하고 예산 초과를 명시한다."""
    unique: dict[tuple, Evidence] = {}
    for item in execution.evidence:
        key = (
            item.kind, item.source, item.product_code, item.class_code,
            item.page, item.content.strip(),
        )
        unique.setdefault(key, item)

    parts: list[str] = []
    used: list[str] = []
    omitted: list[str] = []
    header = (f"[PLAN] required_facts={plan.required_facts}; completeness={plan.completeness}; "
              f"execution={execution.status}; errors={execution.errors}\n")
    if len(header) > char_budget:
        header = "[PLAN] Context budget insufficient\n"[:char_budget]
    size = len(header)
    # Round robin across tasks/products prevents one long product from consuming
    # every other task's evidence budget. Stable retrieval order breaks ties.
    groups = {}
    for item in sorted(unique.values(), key=_priority):
        groups.setdefault((item.data.get("task_id"), item.product_code), []).append(item)
    ordered = []
    while any(groups.values()):
        for group in groups.values():
            if group: ordered.append(group.pop(0))
    for item in ordered:
        rendered = _render(item)
        extra = len(rendered) + (6 if parts else 0)
        if size + extra > char_budget:
            omitted.append(item.evidence_id)
            continue
        # Keep whole facts; oversized chunks are recorded as omitted, not cut
        # mid-sentence or allowed to silently exceed the input budget.
        parts.append(rendered)
        used.append(item.evidence_id)
        size += extra

    text = header + "\n---\n".join(parts)
    expected_tasks = {str(e.data["task_id"]) for e in execution.evidence if e.data.get("task_id") is not None}
    visible_tasks = {str(e.data["task_id"]) for e in execution.evidence if e.evidence_id in used and e.data.get("task_id") is not None}
    return ContextBundle(
        text=text,
        evidence_ids=used,
        omitted_evidence_ids=omitted,
        char_count=len(text),
        truncated=bool(omitted),
        missing_task_ids=sorted(expected_tasks - visible_tasks),
    )
