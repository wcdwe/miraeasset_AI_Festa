from __future__ import annotations

from .schemas import PlanStep, ProductMention, QueryAnchor, QueryPlan


def _remove_resolve(plan: QueryPlan) -> None:
    """이미 확정된 상품을 LLM이 다시 식별해 결과를 흔들지 못하게 한다."""
    kept = [step for step in plan.plan if step.tool != "RESOLVE"]
    old_to_new = {step.step: index for index, step in enumerate(kept, 1)}
    plan.plan = [
        step.model_copy(update={
            "step": old_to_new[step.step],
            "depends_on": [old_to_new[d] for d in step.depends_on if d in old_to_new],
        })
        for step in kept
    ]
    plan.tools = [tool for tool in plan.tools if tool != "RESOLVE"]


def merge_anchor_plan(anchor: QueryAnchor, plan: QueryPlan, question: str | None = None) -> QueryPlan:
    """LLM 계획에 Python 확정 제약을 덮어씌운다. 반대 방향은 허용하지 않는다."""
    merged = plan.model_copy(deep=True)
    lockable = anchor.product_status in {"exact", "unambiguous", "multiple"}
    anchor_codes = [item.product_code for item in anchor.products] if lockable else []
    merged.entities["anchor_product_codes"] = anchor_codes
    merged.entities["anchor_product_candidates"] = [
        item.model_dump(mode="json") for item in anchor.products
    ]
    merged.entities["allowed_source_types"] = list(anchor.allowed_source_types)
    merged.entities["forbidden_source_types"] = list(anchor.forbidden_source_types)
    merged.entities["anchor_product_status"] = anchor.product_status
    merged.entities["anchor_class_codes"] = list(anchor.locked.class_codes)
    merged.entities["anchor_account_types"] = list(anchor.account_types)
    merged.entities["anchor_periods"] = list(anchor.periods)

    if anchor_codes:
        _remove_resolve(merged)
        known = {item.text for item in merged.product_mentions}
        for index, product in enumerate(anchor.products):
            if product.product_name not in known:
                role = "single" if len(anchor.products) == 1 else (
                    "comparison_left" if index == 0 else "comparison_right"
                )
                merged.product_mentions.append(ProductMention(
                    text=product.product_name, role=role, resolution_required=False,
                ))

    # All explicit numeric constraints are compiled together. Replace competing
    # Planner versions of these fields instead of adding eq beside the true lte.
    locked_fields = {item.field for item in anchor.filters}
    merged.filters = [item for item in merged.filters if item.field not in locked_fields]
    existing_filters = {(item.field, item.operator.value, item.source_text) for item in merged.filters}
    for item in anchor.filters:
        key = (item.field, item.operator.value, item.source_text)
        if key not in existing_filters:
            merged.filters.append(item.model_copy(deep=True))
    merged.periods = list(dict.fromkeys([*anchor.periods, *merged.periods]))
    merged.required_facts = list(dict.fromkeys([
        *anchor.confirmed_fact_types, *merged.required_facts,
    ]))
    if anchor.return_all is not None:
        merged.return_all = anchor.return_all
    if anchor.return_all is True:
        merged.return_all = True
        merged.limit = None
        merged.completeness = "all_matches"
    elif anchor.return_all is False and anchor.locked.limit is not None:
        merged.limit = anchor.locked.limit
    if anchor.locked.sort:
        merged.sort = list(anchor.locked.sort)
    merged.safety_flags = list(dict.fromkeys([*anchor.safety_flags, *merged.safety_flags]))

    # 계좌가 명시된 상품 적합성·선택 판단에는 문서 위험만으로 답할 수 없다.
    # 실제 해당 계좌용 클래스 존재 여부를 정형 FACT로 반드시 함께 확인한다.
    suitability = any(
        any(term in intent.replace(" ", "") for term in ("추천", "적합", "판단", "선택"))
        for intent in merged.intents
    )
    if anchor_codes and anchor.account_types and suitability and "FACT" not in merged.tools:
        merged.tools.insert(0, "FACT")
        merged.required_facts = list(dict.fromkeys([
            "CLASS_ELIGIBILITY", *merged.required_facts,
        ]))
        merged.plan = [
            PlanStep(
                step=1, tool="FACT", purpose="명시 계좌의 가입 가능 클래스 확인",
                inputs={"product_codes": anchor_codes, "source_types": ["structured"],
                        "fact_types": ["CLASS_ELIGIBILITY"]},
            ),
            *[
                step.model_copy(update={
                    "step": step.step + 1,
                    "depends_on": [dep + 1 for dep in step.depends_on],
                })
                for step in merged.plan
            ],
        ]

    allowed = set(anchor.allowed_source_types) - set(anchor.forbidden_source_types)
    locked_filters = [item.model_dump(mode="json") for item in anchor.filters]
    # Plan Merger는 Anchor와 LLM 계획을 합치는 역할만 맡는다. "이 도구가
    # 실제로 실행 가능한가"는 별도 모듈(capability_gate)의 몫이다 - 도구별
    # 검사를 여기 다 넣으면 이 파일이 도구 수만큼 계속 자라난다.
    if question is not None:
        from . import capability_gate
        import logging
        for step in merged.plan:
            gate = capability_gate.validate_step(step, anchor, question)
            if gate.status == "REWRITE":
                logging.getLogger("agent_v2.audit").info(
                    "capability_gate REWRITE: step=%s %s->%s reason=%s",
                    step.step, step.tool, gate.replacement_tool, gate.reason)
                step.tool = gate.replacement_tool
                step.inputs = {**step.inputs, **(gate.corrected_inputs or {})}
        merged.tools = list(dict.fromkeys(s.tool for s in merged.plan))

    for step in merged.plan:
        inputs = dict(step.inputs)
        requested_codes = inputs.get("product_codes") or []
        if anchor_codes:
            if requested_codes and not set(requested_codes).issubset(anchor_codes):
                raise ValueError("Task 상품코드가 확인된 Anchor 범위를 벗어남")
            inputs["product_codes"] = requested_codes or anchor_codes
        requested_sources = set(inputs.get("source_types") or [])
        if not requested_sources:
            # hints.source_types는 "상품·제도 키워드 둘 다 없어 LLM이 스스로
            # 판단해야 한다"는 의미로 확정 안 된 질문에도 product를 포함한다
            # (anchor.py: 상품도 제도 키워드도 없으면 셋 다 열어 둔다). 이걸
            # LLM 없는 규칙 계획(rule_planner)이 그대로 실행 source_types로
            # 쓰면, 확정된 상품이 하나도 없는데 상품 RAG를 시도하다 죽는다
            # (실측: "작년에 수익률 20%였으면 올해도 비슷하게 벌겠네?" 같은
            # 상품·제도 무관 질문이 매번 "상품 RAG는 먼저 상품코드를 확정해야
            # 함"으로 실패). 확정 상품이 없으면 상품 검색 자체가 애초에 불가능
            # 하므로 hints를 참고하지 않고 institution으로 확정한다.
            requested_sources = {"structured"} if step.tool in {
                "FACT", "FILTER", "COMPARE", "TAX"
            } else ({"product"} if anchor_codes else {"institution"})
        if step.tool == "RAG":
            # RAG의 실행기(task_executor)는 product/institution 문서만 검색한다.
            # "structured"는 FACT/FILTER/COMPARE가 다루는 정형 DB를 가리키는
            # 힌트일 뿐 RAG 도구가 이해하는 source가 아니라서, hints에서 그대로
            # 흘러들어오면 실행 단계에서 무조건 ValueError로 죽는다(RAG source는
            # product/institution만 허용).
            requested_sources -= {"structured"}
        inputs["source_types"] = [source for source in requested_sources if source in allowed]
        inputs["fact_types"] = inputs.get("fact_types") or (
            anchor.confirmed_fact_types if step.tool in {"FACT", "FILTER", "COMPARE"} else []
        )
        inputs["filters"] = [
            *locked_filters,
            *[f for f in (inputs.get("filters") or []) if f.get("field") not in locked_fields],
        ]
        inputs["periods"] = list(dict.fromkeys([
            *anchor.periods, *(inputs.get("periods") or []),
        ]))
        inputs["metrics"] = list(dict.fromkeys([
            *merged.metrics, *(inputs.get("metrics") or []),
        ]))
        step.inputs = inputs
    return QueryPlan.model_validate(merged.model_dump())
