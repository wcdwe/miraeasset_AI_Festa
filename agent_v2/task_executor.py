from __future__ import annotations

from . import product_repository as repository
from .document_path import retrieve_document_hits
from .product_resolver import resolve_product
from .schemas import Evidence, PlanStep, QueryFilter, ToolExecutionResult


# 제도 문서 검색은 후보 점수가 거의 평평해서(실측 상위 8건이 0.0164~0.0154)
# 3건으로 자르면 순위 노이즈로 정답이 잘려 나간다. "연금저축과 IRP 세액공제
# 한도"의 정답 문장이 7위라 버려졌고, LLM은 근거가 없으니 600+900=1,500만원
# 이라고 잘못 더했다. 상품 문서는 상품마다 따로 검색해 이미 여러 건이 쌓이므로
# 그대로 둔다. 넘치는 분량은 context_builder가 예산 안에서 정리한다.
INSTITUTION_HITS = 8
PRODUCT_HITS = 3


def _institution_fact_evidence(question):
    """제도·세제 수치는 institution_facts에 원자적 사실로 정리돼 있는데도
    RAG 문서 검색만 하고 있었다. 그래서 "연금저축과 IRP 세액공제 얼마까지"에
    LLM이 근거를 못 찾고 납입한도(1,800만원)를 세액공제 한도로 잘못 답했다.
    확정된 사실을 검색 결과와 함께 건네 LLM이 추측할 이유를 없앤다."""
    from scripts import institution_facts
    summary, facts = institution_facts.institution_facts_answer(question)
    if not summary or not facts:
        return []
    seen, evidence = set(), []
    for fact in facts:
        key = (fact.get("source_doc"), fact.get("page"), fact.get("evidence"))
        if key in seen:
            continue
        seen.add(key)
        evidence.append(Evidence(
            evidence_id=f"FACT-{len(evidence) + 1}", kind="structured",
            content=f"{fact.get('subject')} {fact.get('predicate')}: {fact.get('evidence')}",
            source=str(fact.get("source_doc")),
            page=int(fact["page"]) if fact.get("page") is not None else None,
            data={"verified": True, "subject": fact.get("subject"),
                  "predicate": fact.get("predicate"), "source": "institution_facts"},
        ))
    return evidence


def execute_tasks(question, plan):
    results, evidence, errors, states = {}, [], [], {}
    codes = list(plan.entities.get("anchor_product_codes") or [])
    steps = plan.plan or [PlanStep(step=i, tool=t, purpose=t) for i, t in enumerate(plan.tools, 1)]
    for step in steps:
        key = str(step.step)
        if any(states.get(str(dep), {}).get("status") != "PASS" for dep in step.depends_on):
            states[key] = {"status": "SKIPPED", "tool": step.tool, "reason": "선행 Task 실패/부분 결과"}
            errors.append(f"Task {key}: 선행 Task 실패로 실행하지 않음")
            continue
        inputs = step.inputs
        scoped_codes = inputs.get("product_codes") or codes
        evs = []
        try:
            if step.tool == "RESOLVE":
                mentions = inputs.get("product_names") or [m.text for m in plan.product_mentions]
                if not mentions: raise ValueError("식별할 상품명이 없음")
                value = []
                for mention in mentions:
                    resolved = resolve_product(mention)
                    value.append(resolved.model_dump(mode="json"))
                    if resolved.status not in {"exact", "alias"}: raise ValueError(f"상품 식별 {resolved.status}: {mention}")
                    codes.extend(c.product_code for c in resolved.candidates if c.product_code not in codes)
            elif step.tool in {"FACT", "FILTER", "COMPARE"}:
                if step.tool == "FACT" and not scoped_codes: raise ValueError("FACT 상품 식별 미완료")
                if step.tool == "COMPARE" and len(scoped_codes) < 2: raise ValueError("COMPARE 대상 2개 이상 필요")
                local = plan.model_copy(deep=True)
                if inputs.get("filters"):
                    supplied = [QueryFilter.model_validate(f) for f in inputs["filters"]]
                    unique = {f.model_dump_json(): f for f in [*local.filters, *supplied]}
                    local.filters = list(unique.values())
                fields = inputs.get("metrics") or inputs.get("fact_types") or plan.metrics or plan.required_facts
                value, evs = repository.query(local, codes=scoped_codes,
                    classes=inputs.get("class_codes") or plan.entities.get("anchor_class_codes"), fields=fields)
                # Even a valid empty query has evidence: preserve filters/count,
                # rather than falling into unrelated institution RAG.
                evs.append(Evidence(evidence_id="QUERY", kind="structured",
                    content=repository.render_result(value, []), source="structured_store.db",
                    data={"query_result": value, "verified": True, "tool": step.tool}))
            elif step.tool == "RAG":
                sources = inputs.get("source_types") or (["product"] if scoped_codes else ["institution"])
                allowed = plan.entities.get("allowed_source_types", ["product", "institution", "structured"])
                if any(s not in allowed for s in sources): raise ValueError("허용된 검색 범위를 벗어남")
                facts = inputs.get("fact_types") or plan.required_facts
                query = inputs.get("query") or step.purpose or question
                hits = []
                for source in sources:
                    if source == "product":
                        if not scoped_codes:
                            # source가 하나뿐이면(호출부가 명시적으로 product만
                            # 요청) 진짜 계약 위반이라 그대로 막는다. 여러
                            # source가 함께 왔다면("확정 상품이 없어 product·
                            # institution 둘 다 후보"인 애매한 질문) product
                            # 하나가 전제조건 미충족이라고 institution 검색까지
                            # 막을 이유가 없다 - 그 소스만 건너뛴다.
                            if len(sources) == 1:
                                raise ValueError("상품 RAG는 먼저 상품코드를 확정해야 함")
                            continue
                        for code in scoped_codes:
                            hits.extend(retrieve_document_hits(
                                query, "product", code, k=PRODUCT_HITS, fact_types=facts))
                    elif source == "institution":
                        evs.extend(_institution_fact_evidence(question))
                        hits.extend(retrieve_document_hits(
                            query, "institution", k=INSTITUTION_HITS, fact_types=facts))
                    else: raise ValueError("RAG source는 product/institution만 허용")
                seen = set()
                for hit in hits:
                    identity = (hit.get("doc_id"), hit.get("page"), hit.get("chunk_id"), hit.get("text"))
                    if identity in seen: continue
                    seen.add(identity)
                    evs.append(Evidence(evidence_id=f"RAG-{len(evs)+1}", kind="document",
                        content=hit.get("text") or "", source=str(hit.get("doc_id")),
                        product_code=hit.get("product_code") or (scoped_codes[0] if len(scoped_codes)==1 and hit.get("doc_type")=="product" else None),
                        page=int(hit["page"]), data={"chunk_id": hit.get("chunk_id"),
                            "fact_types": facts, "score": hit.get("rrf", 0), "doc_type": hit.get("doc_type")}))
                    evs[-1].data["retrieval_backend"] = hit.get("retrieval_backend", "lexical_or_hybrid")
                if not evs: raise ValueError("해당 Task 범위에서 본문 근거를 찾지 못함")
                value = [e.model_dump(mode="json") for e in evs]
            elif step.tool == "TAX":
                from .tax_inputs import calculate
                value = calculate(question, inputs)
                evs = [Evidence(evidence_id="TAX", kind="calculation", content=value["summary"],
                    source="institution/doc41", page=1, data={**value, "verified": True})]
            elif step.tool == "POLICY":
                from .pre_router import pre_route
                from .templates import build_policy_payload
                decision = pre_route(question)
                if decision.route != "FAST_POLICY": raise ValueError("승인된 정책 템플릿과 일치하지 않음")
                value = build_policy_payload("policy", question, decision)["answer"]
                evs = [Evidence(evidence_id="POLICY", kind="policy", content=value, source="approved_policy", data={"verified": True})]
            else:
                raise ValueError(f"지원되지 않은 도구 {step.tool}")
            for ev in evs:
                ev.evidence_id = f"T{key}-{ev.evidence_id}"
                ev.data["task_id"] = key
            evidence.extend(evs)
            results[f"{step.tool}:{key}"] = value
            # Compatibility for consumers of a single Task of each tool.
            results[step.tool] = value
            states[key] = {"status": "PASS", "tool": step.tool,
                           "evidence_ids": [e.evidence_id for e in evs]}
        except (ValueError, KeyError, TypeError) as exc:
            states[key] = {"status": "FAIL", "tool": step.tool, "reason": str(exc)}
            errors.append(f"Task {key} ({step.tool}): {exc}")
    results["task_states"] = states
    return ToolExecutionResult(status="PARTIAL" if errors and evidence else "FAIL" if errors else "PASS",
                               tool_results=results, evidence=evidence, errors=errors)
