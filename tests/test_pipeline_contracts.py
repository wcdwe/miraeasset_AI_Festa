"""Offline regression tests for the diagnosed routing and evidence defects."""
import unittest
from unittest.mock import patch
from agent_v2.anchor import extract_anchor
from agent_v2.pre_router import pre_route
from agent_v2.structured_request import compile_structured, numeric_filters
from agent_v2.schemas import QueryPlan, QueryFilter, Evidence, ToolExecutionResult, ValidationResult, PlanStep
from agent_v2.product_repository import query
from agent_v2.plan_merger import merge_anchor_plan
from agent_v2.context_builder import build_context
from agent_v2.task_executor import execute_tasks
from agent_v2.tax_inputs import calculate
from agent_v2.validation_gate import MAX_REPAIR_ATTEMPTS, run_validation_gate, RepairResult
from agent_v2.grounding_validator import validate_grounding
from agent_v2.telemetry import reset_usage, record_actual_usage, record_http_attempt, usage_snapshot


class PipelineContracts(unittest.TestCase):
    def test_ambiguous_anchor_not_locked_by_conjunction(self):
        self.assertEqual(extract_anchor("솔로몬 국공채 펀드의 장점과 단점을 설명해줘").product_status, "ambiguous")

    def test_nonexistent_duration_not_substituted(self):
        self.assertEqual(extract_anchor("미래에셋솔로몬초장기국공채 펀드를 설명해줘").product_status, "not_found")

    def test_mixed_request_goes_to_planner(self):
        self.assertEqual(pre_route("미래에셋장기성장포커스의 위험등급과 투자할 때 조심할 점을 알려줘").route, "AGENT")

    def test_numeric_operators_preserved(self):
        self.assertEqual(numeric_filters("위험등급 3등급 이하")[0].operator.value, "lte")
        self.assertEqual(numeric_filters("총보수·비용 0.5% 미만")[0].field, "total_fee_and_cost")
        self.assertEqual(numeric_filters("AUM 1000억원 이상")[0].value, 100_000_000_000)

    def test_period_does_not_include_investment_horizon(self):
        self.assertEqual(extract_anchor("20년 투자할 건데 3년 수익률 알려줘").periods, ["3년"])

    def test_complete_filter_compile(self):
        p = compile_structured("IRP에서 투자 가능하고 채권형이면서 최근 5년 수익률이 존재하는 상품을 모두 찾아줘.")
        self.assertIsNotNone(p)
        self.assertEqual({f.field for f in p.filters}, {"account_type", "asset_type", "return_5y"})
        self.assertTrue(p.return_all)

    def test_extra_risk_condition_not_dropped(self):
        p = compile_structured("IRP에서 투자 가능하고 채권형이며 5년 수익률이 있는 상품 중 위험등급 1등급 상품을 모두 찾아줘")
        self.assertIsNotNone(p)
        self.assertEqual(next(f.value for f in p.filters if f.field == "risk_level"), 1)

    def test_unrecognized_condition_not_fast(self):
        self.assertIsNone(compile_structured("채권형이고 환헤지가 적용되며 5년 수익률이 있는 상품 모두 찾아줘"))

    def rows(self):
        base = {"product_code": "P", "product_name": "상품 P", "asset_type": "국공채", "risk_level": 3,
                "retail": 1, "aum": 2_000_000, "return_5y": None, "total_fee": None}
        return [{**base, "class_code": "C", "account_type": "일반", "return_5y": 9., "total_fee": .1},
                {**base, "class_code": "C-R", "account_type": "퇴직연금", "eligibility": "IRP 개인형퇴직연금", "total_fee": .5},
                {**base, "product_code": "Q", "product_name": "상품 Q", "class_code": "C-R", "account_type": "퇴직연금",
                 "eligibility": "개인형퇴직연금", "return_5y": 2., "total_fee": .4}]

    def filter_plan(self, *filters, **kwargs):
        return QueryPlan(tools=["FILTER"], metrics=["return_5y", "total_fee"], filters=[QueryFilter(source_text="fixture condition", **f) for f in filters], **kwargs)

    def test_same_class_join(self):
        plan = self.filter_plan(dict(field="account_type", operator="eq", value="IRP"),
                                dict(field="return_5y", operator="is_not_null"))
        result, ev = query(plan, rows=self.rows())
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rows"][0]["product_code"], "Q")
        self.assertEqual(result["rows"][0]["total_fee"], .4)

    def test_unknown_irp_not_assumed(self):
        rows = self.rows()
        rows[-1]["eligibility"] = "퇴직연금 가입자"
        result, _ = query(self.filter_plan(dict(field="account_type", operator="eq", value="IRP"),
                           dict(field="return_5y", operator="is_not_null")), rows=rows)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["uncertain_product_count"], 1)

    def test_asset_negation_is_preserved(self):
        result, _ = query(self.filter_plan(dict(field="asset_type", operator="ne", value="채권형")), rows=self.rows())
        self.assertEqual(result["count"], 0)

    def test_no_default_five_result_limit(self):
        rows = [{**self.rows()[0], "product_code": f"P{i}"} for i in range(12)]
        result, _ = query(self.filter_plan(), rows=rows)
        self.assertEqual(result["count"], 12)

    def test_unknown_fields_fail_not_ignore(self):
        with self.assertRaises(ValueError):
            query(QueryPlan(metrics=["unknown"]), rows=self.rows())

    def test_null_not_zero(self):
        result, _ = query(self.filter_plan(dict(field="return_5y", operator="eq", value=0)), rows=self.rows())
        self.assertEqual(result["count"], 0)

    def test_scope_merger_retains_individual_task(self):
        a = extract_anchor("KR5153420079와 KR5153420105를 비교해줘")
        p = QueryPlan(tools=["RAG"], plan=[PlanStep(step=1, tool="RAG", purpose="위험", inputs={
            "product_codes": ["KR5153420079"], "source_types": ["product"], "fact_types": ["RISK_NARRATIVE"]})])
        self.assertEqual(merge_anchor_plan(a, p).plan[0].inputs["product_codes"], ["KR5153420079"])

    def test_context_strict_budget(self):
        ev = Evidence(evidence_id="long", kind="document", source="d", page=1, content="가"*5000)
        c = build_context(QueryPlan(), ToolExecutionResult(status="PASS", evidence=[ev]), char_budget=500)
        self.assertLessEqual(c.char_count, 500)
        self.assertEqual(c.omitted_evidence_ids, ["long"])

    def test_task_dependencies_do_not_run_on_failure(self):
        p = QueryPlan(tools=["FACT", "RAG"], plan=[
            PlanStep(step=1, tool="FACT", purpose="unresolved"),
            PlanStep(step=2, tool="RAG", purpose="risk", depends_on=[1])])
        with patch("agent_v2.task_executor.retrieve_document_hits") as search:
            result = execute_tasks("question", p)
        search.assert_not_called()
        self.assertEqual(result.tool_results["task_states"]["2"]["status"], "SKIPPED")

    def test_tax_money_order_invariant(self):
        a = calculate("IRP에 900만원 납입했고 총급여는 6000만원입니다. 세액공제액은?", {})
        b = calculate("총급여는 6000만원이고 IRP에 900만원 납입했습니다. 세액공제액은?", {})
        self.assertEqual(a["value"], b["value"])
        self.assertEqual(a["value"], 1_188_000)

    def test_tax_rejects_hallucinated_input(self):
        with self.assertRaises(ValueError):
            calculate("IRP에 900만원 납입했고 총급여 6000만원 세액공제액?", {"annual_salary": 9000000})

    def test_tax_rejects_missing_account(self):
        with self.assertRaises(ValueError): calculate("900만원 납입했고 총급여 6000만원 세액공제액?", {})

    def test_citation_local_number_binding(self):
        evs = [Evidence(evidence_id="A", kind="document", content="상품 A 위험등급 2등급", source="A.pdf", page=1),
               Evidence(evidence_id="B", kind="document", content="상품 B 위험등급 5등급", source="B.pdf", page=2)]
        ctx = build_context(QueryPlan(), ToolExecutionResult(status="PASS", evidence=evs))
        result = validate_grounding("상품 A의 위험등급", "상품 A의 위험등급 5등급 (출처: A.pdf, p.1)", QueryPlan(), evs, ctx)
        self.assertEqual(result.status, "FAIL")

    def test_first_failure_preserved_and_retry_bounded(self):
        ev = Evidence(evidence_id="D", kind="document", content="위험이 있음", source="d.pdf", page=1)
        ctx = build_context(QueryPlan(), ToolExecutionResult(status="PASS", evidence=[ev]))
        fail = ValidationResult(status="FAIL", retry_action="REGENERATE", errors=[{
            "criterion": "근거", "problem": "unsupported", "correction": "remove"}])
        with patch("agent_v2.validation_gate.validate_grounding", return_value=ValidationResult(status="PASS", retry_action="NONE")):
            result = run_validation_gate("설명", "내용", QueryPlan(), [ev], ctx,
                llm_validator=lambda *a: fail,
                repair_handler=lambda *a: RepairResult("수정 내용", [ev], ctx))
        self.assertEqual(result.retry_count, MAX_REPAIR_ATTEMPTS)
        # 시도마다 python·semantic 두 건이 쌓이고, 상한을 넘겨 반복하지 않는다.
        self.assertEqual(len(result.history), 2 * (MAX_REPAIR_ATTEMPTS + 1))
        self.assertEqual(result.history[1]["errors"][0]["problem"], "unsupported")
        self.assertEqual(result.status, "SAFE_FALLBACK")
        self.assertNotIn("검증을 통과한", result.answer)

    def test_actual_tokens_separate_from_estimates(self):
        reset_usage()
        record_http_attempt()
        record_actual_usage({"result": {"inputLength": 123, "outputLength": 45}})
        self.assertEqual(usage_snapshot().actual_input_tokens, 123)
        self.assertEqual(usage_snapshot().http_attempts, 1)

    def test_real_database_aum_won_is_normalized(self):
        from agent_v2.product_repository import class_rows
        rows = class_rows()
        self.assertTrue(rows)
        self.assertTrue(all(r["aum"] is None or r["aum"] >= 0 for r in rows))

    def test_real_fee_conflicts_are_not_arbitrarily_selected(self):
        from agent_v2.product_repository import class_rows
        rows = class_rows()
        conflicts = [(r, f) for r in rows for f in r.get("_conflicts", {})]
        self.assertTrue(conflicts)
        self.assertTrue(all(r[f] is None for r, f in conflicts))

    def test_actual_api_entry_uses_structured_without_hcx(self):
        from api.server import answer_payload
        with patch("agent_v2.orchestrator.try_agent_payload") as agent:
            result = answer_payload("offline-unit", "KR510902511M 위험등급 알려줘")
        agent.assert_not_called()
        self.assertIn("상품 기준", result["answer"])

    def test_planner_failure_never_uses_legacy_rag(self):
        from api.server import answer_payload
        with patch("agent_v2.orchestrator.try_agent_payload", return_value=None), \
             patch("api.server.compose_answer") as legacy:
            with self.assertRaises(RuntimeError):
                answer_payload("offline-unit", "IRP 제도를 설명해줘")
        legacy.assert_not_called()

    def test_cache_data_version_and_expiration(self):
        from agent_v2.api_contract import ResponseCache
        revision = [1]
        cache = ResponseCache(version_provider=lambda: revision[0])
        body = {"question_id": "q", "question": "q", "answer": "a"}
        cache.put(body)
        self.assertIsNotNone(cache.get("q", "q"))
        revision[0] = 2
        self.assertIsNone(cache.get("q", "q"))
        cache = ResponseCache(ttl_seconds=-1)
        cache.put(body)
        self.assertIsNone(cache.get("q", "q"))

    def test_rag_passes_fact_type_and_keeps_distinct_chunks(self):
        p = QueryPlan(tools=["RAG"], required_facts=["RISK_NARRATIVE"],
                      entities={"anchor_product_codes": ["KR510902511M"]})
        hits = [{"doc_id": "d.pdf", "page": 7, "chunk_id": str(n), "text": "근거" + str(n),
                 "product_code": "KR510902511M", "doc_type": "product"} for n in (1, 2)]
        with patch("agent_v2.task_executor.retrieve_document_hits", return_value=hits) as search:
            result = execute_tasks("뭘 조심해야 해", p)
        self.assertEqual(search.call_args.kwargs["fact_types"], ["RISK_NARRATIVE"])
        self.assertEqual(len(result.evidence), 2)

    def test_fact_task_executes_declared_field_not_question_keywords(self):
        p = QueryPlan(tools=["FACT"], entities={"anchor_product_codes": ["KR510902511M"]},
            plan=[PlanStep(step=1, tool="FACT", purpose="등급 확인", inputs={"fact_types": ["RISK_GRADE"]})])
        result = execute_tasks("위험이 큰 편이야?", p)
        self.assertEqual(result.status, "PASS")
        self.assertTrue(any(e.data.get("metric") == "risk_level" for e in result.evidence))

    def test_competing_planner_risk_eq_cannot_narrow_lte(self):
        anchor = extract_anchor("위험등급 3등급 이하인 상품 모두 찾아줘")
        p = QueryPlan(tools=["FILTER"], filters=[QueryFilter(field="risk_level", operator="eq", value=3,
                     source_text="위험등급 3등급 이하")])
        merged = merge_anchor_plan(anchor, p)
        self.assertEqual([f.operator.value for f in merged.filters], ["lte"])

    def test_compare_accepts_elliptical_names_but_no_comma_numeric_crash(self):
        p = compile_structured("미래에셋솔로몬장기국공채와 중장기국공채의 위험등급, 총보수, 최근 5년 수익률을 비교해줘")
        self.assertIsNotNone(p)
        self.assertEqual(p.tools, ["COMPARE"])
        self.assertIn("return_5y", p.metrics)

    def test_bad_filter_source_not_silently_removed(self):
        import json
        from agent_v2.query_analyzer import parse_plan
        raw = {"intents": ["조건검색"], "tools": ["FILTER"], "filters": [{
            "field": "risk_level", "operator": "eq", "value": 3, "source_text": "없는 원문"}]}
        self.assertIsNone(parse_plan(json.dumps(raw), "위험등급 3등급 상품").plan)

    def test_unknown_numeric_value_rejected(self):
        with self.assertRaises(ValueError):
            QueryFilter(field="risk_level", operator="gte", value="NaN", source_text="q")

    def _gate_with_persistent(self, errors):
        ev = Evidence(evidence_id="D", kind="document", content="근거", source="d.pdf", page=1)
        ctx = build_context(QueryPlan(), ToolExecutionResult(status="PASS", evidence=[ev]))
        failure = ValidationResult(status="FAIL", retry_action="REGENERATE", errors=errors)
        with patch("agent_v2.validation_gate.validate_grounding", return_value=failure):
            return run_validation_gate(
                "질문", "생성된 답변", QueryPlan(), [ev], ctx,
                llm_validator=lambda *a: ValidationResult(status="PASS", retry_action="NONE"),
                repair_handler=lambda *a, **k: RepairResult("생성된 답변", [ev], ctx))

    def test_advisory_only_findings_do_not_discard_the_answer(self):
        # 얕은 휴리스틱(요구사항 충족) 하나로 답변 전체를 버리면, 질문의
        # 일부를 덜 다룬 답변 대신 아무것도 답하지 않는 거절이 나간다.
        result = self._gate_with_persistent([{
            "criterion": "요구사항 충족", "problem": "항목 미반영", "correction": "보강"}])
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.answer, "생성된 답변")
        self.assertFalse(result.used_safe_fallback)

    def test_grounding_findings_still_discard_the_answer(self):
        result = self._gate_with_persistent([
            {"criterion": "요구사항 충족", "problem": "항목 미반영", "correction": "보강"},
            {"criterion": "근거 기반 답변", "problem": "근거 없이 단정", "correction": "삭제"}])
        self.assertEqual(result.status, "SAFE_FALLBACK")
        self.assertTrue(result.used_safe_fallback)

    def test_become_verb_is_not_read_as_contract_termination(self):
        # "~해지다"(정해지다·가능해지다)는 "~하게 되다"라는 흔한 구문이지
        # 계약 해지가 아니다. 이걸 해지 질문으로 분류하면 제도 질문에 해지
        # 설명이 없다는 이유로 답변이 영영 반려된다.
        from product_facts import detect_intents
        for question in (
            "DC와 DB, 퇴직금이 정해지는 방식이랑 운용 주체가 어떻게 다른가요?",
            "수익률이 어떻게 정해지나요?",
            "자금이 필요해지면 어떻게 해야 하나요?",
        ):
            self.assertNotIn("account_termination", detect_intents(question), question)

    def test_real_termination_and_withdrawal_still_detected(self):
        from product_facts import detect_intents
        for question in (
            "연금저축을 중간에 해지하면 세금상 불이익이 있어?",
            "중도해지는 어떤 경우에 가능한가요?",
            "부분해지가 가능한가요?",
            "IRP에서 중도인출할 수 있는 경우가 어떤 경우야?",
        ):
            self.assertIn("account_termination", detect_intents(question), question)

    def test_termination_redemption_and_cancellation_are_distinct_facttypes(self):
        # "해지"/"환매"/"매수취소"를 하나로 뭉치면(예전 "redemption") 계좌
        # 해지 질문에 펀드 환매수수료 커버리지를 요구하는 식의 잘못된 짝짓기가
        # 생긴다. 셋을 서로 다른 FactType으로 유지한다.
        from product_facts import detect_intents
        cases = {
            "이 펀드를 환매하면 수수료가 얼마인가요?": "fund_redemption",
            "연금저축을 해지하면 세금상 불이익이 있어?": "account_termination",
            "매수 취소는 어떻게 하나요?": "order_cancellation",
        }
        for question, expected in cases.items():
            intents = detect_intents(question)
            self.assertIn(expected, intents, question)
            for other in {"fund_redemption", "account_termination", "order_cancellation"} - {expected}:
                self.assertNotIn(other, intents, f"{question} -> {intents}")


if __name__ == "__main__": unittest.main()
