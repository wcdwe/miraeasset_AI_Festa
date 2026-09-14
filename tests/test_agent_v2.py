import unittest
import json
import re
from unittest.mock import patch

from fastapi import HTTPException

from agent_v2.pre_router import assess_risk, pre_route
from agent_v2.document_path import (
    _usable,
    retrieve_document_hits,
    try_simple_institution_document,
    try_simple_product_document,
)
from agent_v2.product_resolver import resolve_product
from agent_v2.query_analyzer import parse_plan
from agent_v2.executor import execute_plan
from agent_v2.context_builder import build_context
from agent_v2.grounding_validator import validate_grounding
from agent_v2.schemas import ContextBundle, Evidence, QueryPlan, ValidationResult
from agent_v2.validation_gate import MAX_REPAIR_ATTEMPTS, RepairResult, run_validation_gate
from agent_v2.validator_llm import parse_validation
from agent_v2.answer_generator import GenerationOutcome
from agent_v2.orchestrator import try_agent_payload
from agent_v2.query_analyzer import AnalysisOutcome
from agent_v2.schemas import ToolExecutionResult
from agent_v2.api_contract import ResponseCache, validate_api_response
from agent_v2.rule_planner import build_rule_plan
from agent_v2.telemetry import (
    record_call, record_failure, record_success, reset_usage, usage_snapshot,
)
from agent_v2.structured_path import try_fast_structured
from agent_v2.filter_path import try_fast_filter
from agent_v2.comparison_path import try_fast_compare
from agent_v2.templates import build_policy_payload
from agent_v2.anchor import extract_anchor
from agent_v2.plan_merger import merge_anchor_plan


class ProductResolverTests(unittest.TestCase):
    def test_exact_duration_product(self):
        result = resolve_product("미래에셋솔로몬국공채 중 단기 상품만 알려줘")
        self.assertEqual(result.status, "alias")
        self.assertEqual(result.candidates[0].product_code, "KR5153420063")

    def test_family_is_ambiguous(self):
        result = resolve_product("솔로몬 국공채 펀드가 뭐야?")
        self.assertEqual(result.status, "ambiguous")
        self.assertGreaterEqual(len(result.candidates), 4)

    def test_nonexistent_duration_is_not_found(self):
        result = resolve_product("미래에셋솔로몬초장기국공채 펀드를 설명해줘")
        self.assertEqual(result.status, "not_found")

    def test_two_named_products_are_ambiguous_set(self):
        result = resolve_product("미래에셋솔로몬장기국공채와 중장기국공채를 비교해줘")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual({c.product_code for c in result.candidates}, {"KR5153420079", "KR5153420105"})


class PreRouterTests(unittest.TestCase):
    def test_conflicting_recommendation_uses_policy_template(self):
        decision = pre_route("원금손실은 절대 싫지만 가장 높은 수익률을 원해. 어떤 상품이 좋아?")
        self.assertEqual(decision.route, "FAST_POLICY")
        self.assertEqual(decision.template_id, "conflicting_risk_return")
        payload = build_policy_payload("Q", "질문", decision)
        self.assertIn("원금손실", payload["answer"])
        self.assertIn("계좌 유형", payload["answer"])

    def test_low_risk_high_return_conflict_uses_policy_template(self):
        decision = pre_route("위험은 가장 낮고 수익률은 가장 높은 상품 하나만 추천해줘")
        self.assertEqual(decision.route, "FAST_POLICY")
        self.assertEqual(decision.template_id, "conflicting_risk_return")

    def test_ranking_is_not_misclassified_as_vague_recommendation(self):
        decision = pre_route("채권형 펀드 중에서 수익률이 가장 좋은 것")
        self.assertNotEqual(decision.route, "FAST_POLICY")

    def test_recommendation_without_stated_profile_asks_back(self):
        # "제 나이에 맞는"이라고만 하고 나이를 밝히지 않았다 - 개인화에 필요한
        # 조건이 하나도 없으므로 임의 추천 대신 확인 조건을 되묻는다.
        decision = pre_route("제 나이에 맞는 펀드 하나만 콕 집어 추천해주세요")
        self.assertEqual(decision.route, "FAST_POLICY")
        self.assertEqual(decision.template_id, "recommendation_missing_profile")

    def test_recommendation_with_stated_profile_keeps_agent_path(self):
        decision = pre_route("35세이고 IRP로 20년 이상 투자할 건데 어떤 상품이 좋아?")
        self.assertEqual(decision.route, "AGENT")

    def test_simple_structured_question(self):
        decision = pre_route("미래에셋장기성장포커스 위험등급 알려줘")
        self.assertEqual(decision.route, "FAST_FACT")

    def test_period_ambiguous_product_comparison_uses_planner(self):
        decision = pre_route("하나파워e단기채와 한국투자 크레딧포커스 ESG 수익률 차이가 어때?")
        self.assertEqual(decision.route, "AGENT")

    def test_narrative_product_question_uses_planner(self):
        decision = pre_route("미래에셋장기성장포커스의 주요 투자위험은 무엇인지 설명해줘.")
        self.assertEqual(decision.route, "AGENT")

    def test_explicit_structured_comparison_uses_fast_compare(self):
        decision = pre_route(
            "미래에셋솔로몬장기국공채와 중장기국공채의 위험등급, "
            "총보수, 최근 3년 수익률을 각각 비교해줘"
        )
        self.assertEqual(decision.route, "FAST_COMPARE")

    def test_clear_multi_filter_uses_zero_llm_path(self):
        decision = pre_route(
            "IRP에서 투자 가능하고 채권형이면서 최근 5년 수익률이 존재하는 상품을 모두 찾아줘"
        )
        self.assertEqual(decision.route, "FAST_FILTER")

    def test_high_risk_llm_answer_requires_llm_validation(self):
        risk = assess_risk(["추천"], ["loss_intolerance"], "LLM")
        self.assertTrue(risk.requires_llm_validation)

    def test_approved_template_skips_llm_validation(self):
        risk = assess_risk(["추천"], ["loss_intolerance"], "TEMPLATE")
        self.assertFalse(risk.requires_llm_validation)


class AnchorContractTests(unittest.TestCase):
    def test_product_narrative_locks_product_scope_not_fact_type(self):
        anchor = extract_anchor(
            "미래에셋장기성장포커스의 주요 투자위험은 무엇인지 설명해줘."
        )
        self.assertEqual([p.product_code for p in anchor.products], ["KR510902511M"])
        self.assertEqual(anchor.confirmed_fact_types, [])
        self.assertEqual(anchor.hints.source_types.values, ["product", "structured"])
        self.assertEqual(anchor.allowed_source_types, ["product", "institution", "structured"])

    def test_explicit_numeric_fact_is_confirmed(self):
        anchor = extract_anchor("미래에셋장기성장포커스 위험등급 알려줘")
        self.assertIn("RISK_GRADE", anchor.confirmed_fact_types)

    def test_anchor_repairs_planner_product_omission(self):
        anchor = extract_anchor(
            "미래에셋장기성장포커스의 주요 투자위험은 무엇인지 설명해줘."
        )
        planner_plan = QueryPlan(
            intents=["상품설명"], required_facts=["RISK_NARRATIVE"], tools=["RAG"],
            plan=[{"step": 1, "tool": "RAG", "purpose": "위험 근거 검색"}],
        )
        merged = merge_anchor_plan(anchor, planner_plan)
        self.assertEqual(merged.entities["anchor_product_codes"], ["KR510902511M"])
        self.assertEqual(merged.entities["allowed_source_types"], ["product", "institution", "structured"])
        self.assertEqual(merged.plan[0].inputs["source_types"], ["product"])
        self.assertNotIn("RESOLVE", merged.tools)
        self.assertFalse(merged.product_mentions[0].resolution_required)
        self.assertEqual(merged.plan[0].inputs["product_codes"], ["KR510902511M"])

    def test_product_scope_executor_never_queries_institution(self):
        plan = QueryPlan(
            intents=["상품설명"], required_facts=["RISK_NARRATIVE"], tools=["RAG"],
            entities={
                "anchor_product_codes": ["KR510902511M"],
                "allowed_source_types": ["product"],
            },
            plan=[{"step": 1, "tool": "RAG", "purpose": "상품 위험 검색"}],
        )

        def fake_retrieve(_question, source_type, product_code=None, k=8, fact_types=None):
            self.assertEqual(source_type, "product")
            self.assertEqual(product_code, "KR510902511M")
            return [{
                "doc_id": "KR510902511M", "page": 7, "product_code": product_code,
                "doc_type": "product", "chunk_id": "risk-1", "text": "주요 투자위험 근거",
            }]

        with patch("agent_v2.task_executor.retrieve_document_hits", side_effect=fake_retrieve):
            result = execute_plan("주요 투자위험은?", plan)
        self.assertEqual(result.status, "PASS")
        self.assertTrue(all(ev.product_code == "KR510902511M" for ev in result.evidence))

    def test_planner_receives_question_and_anchor(self):
        from agent_v2 import query_analyzer as planner

        anchor = extract_anchor("미래에셋장기성장포커스 주요 투자위험을 설명해줘")
        raw_plan = json.dumps({
            "intents": ["상품설명"],
            "required_facts": ["RISK_NARRATIVE"],
            "tools": ["RAG"],
            "plan": [{"step": 1, "tool": "RAG", "purpose": "위험 근거 검색"}],
        }, ensure_ascii=False)
        captured = []

        def fake_chat(messages, **_kwargs):
            captured.extend(messages)
            return raw_plan

        with patch("agent_v2.query_analyzer.is_configured", return_value=True), \
             patch("agent_v2.query_analyzer.chat", side_effect=fake_chat):
            outcome = planner.analyze(
                "미래에셋장기성장포커스 주요 투자위험을 설명해줘", anchor=anchor
            )

        self.assertIsNotNone(outcome.plan)
        payload = json.loads(captured[1]["content"])
        self.assertEqual(
            payload["query_anchor"]["locked"]["products"][0]["product_code"],
            "KR510902511M",
        )
        self.assertEqual(
            payload["query_anchor"]["allowed_source_types"], ["product", "institution", "structured"]
        )


class ValidationSchemaTests(unittest.TestCase):
    def test_pass_shape(self):
        result = ValidationResult(status="PASS", retry_action="NONE", errors=[])
        self.assertEqual(result.status, "PASS", result.errors)

    def test_fail_requires_error(self):
        with self.assertRaises(ValueError):
            ValidationResult(status="FAIL", retry_action="REGENERATE", errors=[])


class GroundingValidatorTests(unittest.TestCase):
    def setUp(self):
        self.plan = QueryPlan(intents=["상품설명"], tools=[])
        self.evidence = [Evidence(
            evidence_id="E1", kind="structured",
            content="상품코드 KR1234567890, A-e 클래스 총보수 연 0.32%, 위험등급 3등급",
            source="product_master", product_code="KR1234567890", class_code="A-e",
        )]
        self.context = ContextBundle(text=self.evidence[0].content, evidence_ids=["E1"])

    def test_grounded_answer_passes(self):
        result = validate_grounding(
            "이 상품 A-e 클래스 총보수와 위험등급 알려줘",
            "A-e 클래스 총보수는 연 0.32%이고 위험등급은 3등급입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.status, "PASS", result.errors)

    def test_unsupported_number_fails(self):
        result = validate_grounding(
            "이 상품 총보수 알려줘", "총보수는 연 0.45%입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.retry_action, "REGENERATE")

    def test_unknown_product_code_requires_resolution(self):
        result = validate_grounding(
            "상품 설명", "상품코드는 KR9999999999입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.retry_action, "RESOLVE_PRODUCT")

    def test_unknown_class_requires_resolution(self):
        result = validate_grounding(
            "이 상품 클래스 알려줘", "C-Pe 클래스입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.retry_action, "RESOLVE_PRODUCT")

    def test_total_fee_cannot_be_renamed_total_cost(self):
        result = validate_grounding(
            "이 상품 보수 알려줘", "총보수·비용은 연 0.32%입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("총보수·비용" in e.problem for e in result.errors))

    def test_fake_source_page_fails(self):
        result = validate_grounding(
            "상품 설명", "위험등급은 3등급입니다. (출처: fake.pdf, p.12)",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("출처·페이지" in e.problem for e in result.errors))

    def test_unsupported_principal_guarantee_uses_safe_fallback(self):
        result = validate_grounding(
            "원금 손실 없어?", "이 상품은 원금이 보장됩니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.retry_action, "SAFE_FALLBACK")

    def test_false_no_results_is_rejected_when_filter_has_rows(self):
        plan = QueryPlan(intents=["조건검색"], tools=[])
        evidence = [Evidence(
            evidence_id="FILTER-1", kind="structured", content="상품 A (KR1234567890)",
            source="structured_store.db", product_code="KR1234567890",
        )]
        result = validate_grounding(
            "조건 상품을 모두 찾아줘", "조건에 맞는 상품을 찾을 수 없습니다.",
            plan, evidence, ContextBundle(text=evidence[0].content, evidence_ids=["FILTER-1"]),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("FILTER 결과" in e.problem for e in result.errors))

    def test_invented_general_scope_is_rejected(self):
        result = validate_grounding(
            "상품 비교", "이는 일반 가입 기준입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("가입 범위" in e.problem for e in result.errors))

    def test_truncated_all_matches_retrieves_more(self):
        plan = QueryPlan(
            intents=["조건검색"], return_all=True, completeness="all_matches", tools=[]
        )
        context = ContextBundle(
            text=self.context.text, evidence_ids=["E1"], omitted_evidence_ids=["E2"],
            truncated=True,
        )
        result = validate_grounding(
            "조건에 맞는 상품 모두 알려줘", "조건에 맞는 상품은 모두 1개입니다.",
            plan, self.evidence, context,
        )
        self.assertEqual(result.retry_action, "RETRIEVE_MORE")

    def test_return_period_must_be_named(self):
        plan = QueryPlan(
            intents=["상품설명"], metrics=["return_5y"], periods=["5년"], tools=[]
        )
        evidence = [Evidence(
            evidence_id="R1", kind="structured", content="최근 5년 수익률 12.3%",
            source="class_returns",
        )]
        result = validate_grounding(
            "최근 5년 수익률 알려줘", "수익률은 12.3%입니다.", plan, evidence,
            ContextBundle(text=evidence[0].content, evidence_ids=["R1"]),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("수익률 기간" in e.problem for e in result.errors))

    def test_risk_grade_direction_error_fails(self):
        result = validate_grounding(
            "위험등급 설명", "1등급이 가장 낮은 위험등급입니다.",
            self.plan, self.evidence, self.context,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("위험등급 숫자" in e.problem for e in result.errors))


class ValidationGateTests(unittest.TestCase):
    def setUp(self):
        self.evidence = [Evidence(
            evidence_id="E1", kind="structured", content="위험등급은 3등급입니다.",
            source="product_master",
        )]
        self.context = ContextBundle(text=self.evidence[0].content, evidence_ids=["E1"])

    @staticmethod
    def _pass_validator(*_args):
        return ValidationResult(status="PASS", retry_action="NONE", errors=[])

    @staticmethod
    def _fail_validator(*_args):
        return ValidationResult(
            status="FAIL", retry_action="REGENERATE",
            errors=[{"criterion": "안전성", "problem": "오류", "correction": "수정"}],
        )

    def test_low_risk_pass_skips_llm_validator(self):
        calls = []
        outcome = run_validation_gate(
            "위험등급 알려줘", "위험등급은 3등급입니다.",
            QueryPlan(intents=["상품설명"]), self.evidence, self.context,
            llm_validator=lambda *_args: calls.append(True),
        )
        self.assertEqual(outcome.status, "PASS")
        self.assertEqual(calls, [])

    def test_high_risk_pass_requires_llm_validator(self):
        calls = []

        def validator(*_args):
            calls.append(True)
            return self._pass_validator()

        outcome = run_validation_gate(
            "추천해줘", "위험등급은 3등급입니다.",
            QueryPlan(intents=["조건부추천"]), self.evidence, self.context,
            llm_validator=validator,
        )
        self.assertEqual(outcome.status, "PASS")
        self.assertEqual(len(calls), 1)

    def test_python_failure_is_not_overridden_by_llm(self):
        calls = []
        outcome = run_validation_gate(
            "위험등급 알려줘", "위험등급은 2등급입니다.",
            QueryPlan(intents=["상품설명"]), self.evidence, self.context,
            llm_validator=lambda *_args: calls.append(True),
        )
        self.assertEqual(outcome.status, "SAFE_FALLBACK")
        self.assertEqual(calls, [])

    def test_llm_failure_repairs_once_and_revalidates(self):
        calls = []

        def validator(*_args):
            calls.append(True)
            return self._fail_validator() if len(calls) == 1 else self._pass_validator()

        repairs = []

        def repair(action, _errors):
            repairs.append(action)
            return RepairResult("위험등급은 3등급입니다.", self.evidence, self.context)

        outcome = run_validation_gate(
            "추천해줘", "위험등급은 3등급입니다.",
            QueryPlan(intents=["추천"]), self.evidence, self.context,
            repair_handler=repair, llm_validator=validator,
        )
        self.assertEqual(outcome.status, "PASS")
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(len(calls), 2)

    def test_repeated_validator_failure_uses_safe_answer(self):
        outcome = run_validation_gate(
            "추천해줘", "위험등급은 3등급입니다.",
            QueryPlan(intents=["추천"]), self.evidence, self.context,
            repair_handler=lambda *_args: RepairResult(
                "위험등급은 3등급입니다.", self.evidence, self.context
            ),
            llm_validator=self._fail_validator,
        )
        self.assertEqual(outcome.status, "SAFE_FALLBACK")
        # 계속 실패해도 재생성은 상한에서 멈춘다(무한 재시도 금지).
        self.assertEqual(outcome.retry_count, MAX_REPAIR_ATTEMPTS)
        self.assertTrue(outcome.used_safe_fallback)
        self.assertIn("검증하지 못했습니다", outcome.answer)
        self.assertNotIn("검증을 통과한 범위", outcome.answer)

    def test_repaired_context_is_returned_by_gate(self):
        repaired_evidence = [Evidence(
            evidence_id="E2", kind="structured", content="위험등급은 2등급입니다.",
            source="product_master",
        )]
        repaired_context = ContextBundle(
            text=repaired_evidence[0].content, evidence_ids=["E2"]
        )
        outcome = run_validation_gate(
            "위험등급 알려줘", "위험등급은 1등급입니다.",
            QueryPlan(intents=["상품설명"]), self.evidence, self.context,
            repair_handler=lambda *_args: RepairResult(
                "위험등급은 2등급입니다.", repaired_evidence, repaired_context
            ),
        )
        self.assertEqual(outcome.status, "PASS")
        self.assertEqual(outcome.context.evidence_ids, ["E2"])

    def test_invalid_validator_json_fails_closed(self):
        result = parse_validation("PASS")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.retry_action, "SAFE_FALLBACK")


class OrchestratorTests(unittest.TestCase):
    def _analysis(self, intents=None):
        return AnalysisOutcome(
            QueryPlan(
                intents=intents or ["비교"], required_facts=["위험등급"],
                tools=["COMPARE"],
                plan=[{"step": 1, "tool": "COMPARE", "purpose": "비교", "depends_on": []}],
            ),
            "테스트 계획",
        )

    @staticmethod
    def _execution(_question, _plan):
        evidence = [Evidence(
            evidence_id="C1", kind="structured",
            content="상품 A 위험등급 3등급, 상품 B 위험등급 4등급",
            source="structured_store.db",
        )]
        return ToolExecutionResult(
            status="PASS", tool_results={"COMPARE": "비교 결과"}, evidence=evidence,
        )

    def test_agent_connects_plan_tools_context_generation_and_gate(self):
        body = try_agent_payload(
            "Q-1", "상품 A와 B 위험등급을 비교해줘",
            analyzer=lambda _q: self._analysis(), executor=self._execution,
            generator=lambda *_args, **_kwargs: GenerationOutcome(
                "상품 A는 위험등급 3등급이고 상품 B는 4등급입니다.", "가짜 생성"
            ),
        )
        self.assertIsNotNone(body)
        self.assertEqual(body["route"], "agent_v2")
        self.assertIn("상품 A", body["answer"])
        self.assertIn("Python 근거·안전 검증: PASS", body["think_trace"])
        self.assertTrue(all(isinstance(body[key], str) for key in (
            "question_id", "question", "retrieved_context", "think_trace", "answer"
        )))

    def test_analysis_failure_falls_back_to_python_rule_plan(self):
        # Planner JSON이 깨져도 요청 전체를 실패시키지 않는다. 레거시 RAG
        # 경로로 새지 않고(별도 계약 테스트가 지킨다) Python이 세운 규칙
        # 계획으로 같은 검증 파이프라인을 그대로 태운다.
        body = try_agent_payload(
            "Q-2", "질문",
            analyzer=lambda _q: AnalysisOutcome(None, "분석 실패"),
        )
        self.assertIsNotNone(body)
        self.assertIn("Python 규칙 QueryPlan fallback", body["think_trace"])

    def test_generation_failure_returns_evidence_instead_of_failing(self):
        # 생성이 실패해도(키 없음·호출 실패·제한 시간 소진) 요청 전체를
        # 실패시키지 않는다. 지어낸 문장 대신 확보된 근거를 돌려준다.
        body = try_agent_payload(
            "Q-3", "상품 A와 B 비교",
            analyzer=lambda _q: self._analysis(), executor=self._execution,
            generator=lambda *_args, **_kwargs: GenerationOutcome(None, "생성 실패"),
        )
        self.assertIsNotNone(body)
        self.assertEqual(body["route"], "generation_unavailable")
        self.assertIn("생성 실패", body["think_trace"])
        self.assertIn("검증되지 않은 문장을 사실로 안내하지 않겠습니다", body["answer"])

    def test_high_risk_agent_uses_validator(self):
        calls = []

        def validator(*_args):
            calls.append(True)
            return ValidationResult(status="PASS", retry_action="NONE", errors=[])

        body = try_agent_payload(
            "Q-4", "두 상품 중 조건에 맞는 후보를 추천해줘",
            analyzer=lambda _q: self._analysis(["조건부추천"]), executor=self._execution,
            generator=lambda *_args, **_kwargs: GenerationOutcome(
                "상품 A는 위험등급 3등급이고 상품 B는 4등급입니다.", "가짜 생성"
            ),
            llm_validator=validator,
        )
        self.assertIsNotNone(body)
        self.assertEqual(len(calls), 1)
        self.assertIn("고위험 검증 LLM: PASS", body["think_trace"])


class ApiCompletionTests(unittest.TestCase):
    def setUp(self):
        from api.server import response_cache
        response_cache.clear()

    @staticmethod
    def _payload(question_id="Q", question="질문"):
        return {
            "question_id": question_id,
            "question": question,
            "retrieved_context": "근거",
            "think_trace": "처리 경로",
            "answer": "답변",
            "route": "test",
        }

    def test_api_contract_requires_exact_string_fields(self):
        good = self._payload()
        good.pop("route")
        self.assertEqual(validate_api_response(good)["answer"], "답변")
        bad = dict(good, answer=123)
        with self.assertRaises(ValueError):
            validate_api_response(bad)

    def test_lru_cache_uses_question_id_and_original_question(self):
        cache = ResponseCache(max_size=2)
        value = self._payload()
        value.pop("route")
        cache.put(value)
        self.assertIsNotNone(cache.get("Q", "질문"))
        self.assertIsNone(cache.get("Q", "다른 질문"))

    def test_same_request_is_computed_once_and_cached(self):
        from api.server import answer
        calls = []

        def fake_payload(question_id, question):
            calls.append((question_id, question))
            return self._payload(question_id, question)

        with patch("api.server.answer_payload", side_effect=fake_payload):
            first = answer("Q-cache", "같은 질문")
            second = answer("Q-cache", "같은 질문")
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(first.body), json.loads(second.body))

    def test_internal_exception_becomes_retryable_503(self):
        from api.server import answer
        with patch("api.server.answer_payload", side_effect=RuntimeError("비밀 내부 오류")):
            with self.assertRaises(HTTPException) as caught:
                answer("Q-error", "질문")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("비밀", str(caught.exception.detail))

    def test_usage_is_added_to_think_trace(self):
        from api.server import answer

        def fake_payload(question_id, question):
            record_call([{"role": "user", "content": "테스트 프롬프트"}])
            record_success("테스트 출력")
            return self._payload(question_id, question)

        with patch("api.server.answer_payload", side_effect=fake_payload):
            response = answer("Q-usage", "질문")
        body = json.loads(response.body)
        self.assertIn("호출 1회", body["think_trace"])
        self.assertIn("문자 기반 추정", body["think_trace"])

    def test_telemetry_tracks_failure_without_storing_prompt(self):
        reset_usage()
        record_call([{"role": "user", "content": "민감할 수 있는 원문"}])
        record_failure()
        snapshot = usage_snapshot()
        self.assertEqual(snapshot.calls, 1)
        self.assertEqual(snapshot.failed_calls, 1)
        self.assertFalse(hasattr(snapshot, "prompt"))


class QueryAnalyzerTests(unittest.TestCase):
    def test_valid_plan_is_parsed(self):
        raw = """```json
        {"intents":["조건검색"],"entities":{"account_type":"IRP"},
        "product_mentions":[],"required_facts":["자산유형","5년 수익률"],
        "filters":[{"field":"account_type","operator":"eq","value":"IRP","source_text":"IRP에서 투자 가능"}],
        "metrics":["return_5y"],"periods":["5년"],"sort":[],"limit":null,
        "return_all":true,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["FILTER"],"completeness":"all_matches",
        "plan":[{"step":1,"tool":"FILTER","purpose":"조건 상품 전체 조회","depends_on":[]}]}
        ```"""
        outcome = parse_plan(raw, "IRP에서 투자 가능하고 채권형이며 5년 수익률이 있는 상품")
        self.assertIsNotNone(outcome.plan)
        self.assertTrue(outcome.plan.return_all)

    def test_invented_product_mention_is_rejected(self):
        raw = """{"intents":["상품설명"],"entities":{},
        "product_mentions":[{"text":"없는펀드","role":"single","resolution_required":true}],
        "required_facts":[],"filters":[],"metrics":[],"periods":[],"sort":[],
        "limit":null,"return_all":false,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["RESOLVE"],"completeness":"single_answer",
        "plan":[{"step":1,"tool":"RESOLVE","purpose":"식별","depends_on":[]}]}"""
        outcome = parse_plan(raw, "IRP가 뭐야?")
        self.assertIsNone(outcome.plan)

    def test_forward_dependency_is_rejected(self):
        raw = """{"intents":[],"entities":{},"product_mentions":[],"required_facts":[],
        "filters":[],"metrics":[],"periods":[],"sort":[],"limit":null,"return_all":false,
        "missing":{"for_personalization":[],"from_evidence":[]},"gap_types":[],
        "answerable_now":true,"follow_ups":[],"safety_flags":[],"tools":["FACT","RAG"],
        "completeness":"single_answer","plan":[
        {"step":1,"tool":"FACT","purpose":"조회","depends_on":[2]},
        {"step":2,"tool":"RAG","purpose":"검색","depends_on":[]}]}"""
        outcome = parse_plan(raw, "질문")
        self.assertIsNone(outcome.plan)

    def test_empty_tools_with_plan_is_rejected(self):
        raw = """{"intents":["비교"],"entities":{},"product_mentions":[],
        "required_facts":[],"filters":[],"metrics":[],"periods":[],"sort":[],
        "limit":null,"return_all":false,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":[],"completeness":"single_answer",
        "plan":[{"step":1,"tool":"COMPARE","purpose":"비교","depends_on":[]}]}"""
        self.assertIsNone(parse_plan(raw, "두 상품 비교").plan)

    def test_rule_plan_recovers_clear_filter_when_llm_json_fails(self):
        plan = build_rule_plan(
            "IRP에서 투자 가능하고 채권형이며 최근 5년 수익률이 있는 상품을 모두 찾아줘"
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.tools, ["FILTER"])
        self.assertTrue(plan.return_all)
        self.assertEqual({item.field for item in plan.filters}, {
            "account_type", "asset_type", "return_5y",
        })


class PlanExecutorTests(unittest.TestCase):
    def test_multi_filter_returns_all_matching_products(self):
        raw = """{"intents":["조건검색"],"entities":{"account_type":"IRP"},
        "product_mentions":[],"required_facts":["자산유형","5년 수익률"],
        "filters":[
        {"field":"account_type","operator":"eq","value":"IRP","source_text":"IRP에서 투자 가능"},
        {"field":"asset_type","operator":"eq","value":"채권형","source_text":"채권형"},
        {"field":"return_5y","operator":"is_not_null","value":null,"source_text":"5년 수익률이 존재"}],
        "metrics":["return_5y"],"periods":["5년"],"sort":[],"limit":null,
        "return_all":true,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["FILTER"],"completeness":"all_matches",
        "plan":[{"step":1,"tool":"FILTER","purpose":"전체 조회","depends_on":[]}]}"""
        question = "IRP에서 투자 가능하고 채권형이면서 5년 수익률이 존재하는 상품을 모두 찾아줘"
        plan = parse_plan(raw, question).plan
        result = execute_plan(question, plan)
        self.assertEqual(result.status, "PASS")
        self.assertGreater(result.tool_results["FILTER"]["count"], 0)
        self.assertTrue(all(row["return_5y"] is not None for row in result.tool_results["FILTER"]["rows"]))

    def test_unknown_filter_field_fails_closed(self):
        raw = """{"intents":["조건검색"],"entities":{},"product_mentions":[],
        "required_facts":[],"filters":[{"field":"future_profit","operator":"gte","value":10,
        "source_text":"미래수익 10 이상"}],"metrics":[],"periods":[],"sort":[],
        "limit":null,"return_all":true,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["FILTER"],"completeness":"all_matches",
        "plan":[{"step":1,"tool":"FILTER","purpose":"조회","depends_on":[]}]}"""
        question = "미래수익 10 이상 상품"
        result = execute_plan(question, parse_plan(raw, question).plan)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("지원되지 않은 정형 항목", result.errors[0])

    def test_two_resolved_products_can_be_compared(self):
        raw = """{"intents":["비교"],"entities":{},"product_mentions":[
        {"text":"미래에셋솔로몬장기국공채","role":"comparison_left","resolution_required":true},
        {"text":"중장기국공채","role":"comparison_right","resolution_required":true}],
        "required_facts":["위험등급","총보수"],"filters":[],"metrics":["risk_level","total_fee"],
        "periods":[],"sort":[],"limit":null,"return_all":false,
        "missing":{"for_personalization":[],"from_evidence":[]},"gap_types":[],
        "answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["RESOLVE","COMPARE"],"completeness":"all_matches","plan":[
        {"step":1,"tool":"RESOLVE","purpose":"두 상품 식별","depends_on":[]},
        {"step":2,"tool":"COMPARE","purpose":"동일 기준 비교","depends_on":[1]}]}"""
        question = "미래에셋솔로몬장기국공채와 중장기국공채의 위험등급과 총보수를 비교해줘"
        plan = parse_plan(raw, question).plan
        result = execute_plan(question, plan)
        self.assertEqual(result.status, "PASS", result.errors)
        codes = {r["product_code"] for r in result.tool_results["COMPARE"]["rows"]}
        self.assertEqual(codes, {"KR5153420079", "KR5153420105"})

    def test_rag_is_scoped_per_resolved_product(self):
        raw = """{"intents":["상품설명"],"entities":{},"product_mentions":[
        {"text":"미래에셋장기성장포커스","role":"single","resolution_required":true}],
        "required_facts":["투자전략"],"filters":[],"metrics":[],"periods":[],"sort":[],
        "limit":null,"return_all":false,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["RESOLVE","RAG"],"completeness":"single_answer","plan":[
        {"step":1,"tool":"RESOLVE","purpose":"상품 식별","depends_on":[]},
        {"step":2,"tool":"RAG","purpose":"투자전략 검색","depends_on":[1]}]}"""
        question = "미래에셋장기성장포커스 투자전략을 설명해줘"
        plan = parse_plan(raw, question).plan
        result = execute_plan(question, plan)
        self.assertEqual(result.status, "PASS", result.errors)
        docs = [item for item in result.evidence if item.kind == "document"]
        self.assertTrue(docs)
        self.assertTrue(all(item.product_code == "KR510902511M" for item in docs))

    def test_context_builder_dedupes_and_reports_budget_omissions(self):
        question = "미래에셋장기성장포커스 투자전략을 설명해줘"
        raw = """{"intents":["상품설명"],"entities":{},"product_mentions":[
        {"text":"미래에셋장기성장포커스","role":"single","resolution_required":true}],
        "required_facts":["투자전략"],"filters":[],"metrics":[],"periods":[],"sort":[],
        "limit":null,"return_all":false,"missing":{"for_personalization":[],"from_evidence":[]},
        "gap_types":[],"answerable_now":true,"follow_ups":[],"safety_flags":[],
        "tools":["RESOLVE","RAG"],"completeness":"single_answer","plan":[
        {"step":1,"tool":"RESOLVE","purpose":"식별","depends_on":[]},
        {"step":2,"tool":"RAG","purpose":"검색","depends_on":[1]}]}"""
        plan = parse_plan(raw, question).plan
        result = execute_plan(question, plan)
        bundle = build_context(plan, result, char_budget=900)
        self.assertTrue(bundle.text)
        self.assertTrue(bundle.truncated)
        self.assertTrue(bundle.omitted_evidence_ids)


class FastStructuredTests(unittest.TestCase):
    def test_risk_question_uses_db_without_llm(self):
        result = try_fast_structured("Q", "미래에셋장기성장포커스 위험등급 알려줘")
        self.assertIsNotNone(result)
        self.assertEqual(result["route"], "single_product")
        self.assertIn("LLM 호출 없음", result["think_trace"])
        self.assertIn("위험등급", result["answer"])

    def test_class_fee_question_keeps_class_scope(self):
        result = try_fast_structured("Q", "미래에셋장기성장포커스 A-e 클래스 총보수 얼마야?")
        self.assertIsNotNone(result)
        self.assertIn("A-e", result["answer"])

    def test_ambiguous_family_is_not_arbitrarily_selected(self):
        result = try_fast_structured("Q", "솔로몬 국공채 펀드 총보수 알려줘")
        self.assertIsNone(result)

    def test_product_and_class_scope_are_explicit(self):
        result = try_fast_structured(
            "Q", "미래에셋장기성장포커스 A-e 클래스 총보수와 위험등급 알려줘"
        )
        self.assertIn("위험등급은 상품 기준", result["answer"])


class FastFilterAndCompareTests(unittest.TestCase):
    def test_irp_bond_five_year_filter_lists_every_db_match(self):
        result = try_fast_filter(
            "Q", "IRP에서 투자 가능하고 채권형이면서 최근 5년 수익률이 존재하는 상품을 모두 찾아줘"
        )
        self.assertIsNotNone(result)
        self.assertIn("LLM 호출 없음", result["think_trace"])
        self.assertNotIn("찾지 못했습니다", result["answer"])
        self.assertIn("과거 수익률은 미래", result["answer"])
        count = int(re.search(r"조건을 확인한 상품 (\d+)개", result["answer"]).group(1))
        self.assertGreater(count, 0)
        # Multiple matching classes may be shown; cite actual PDF pages, not a
        # fabricated class_returns document. Product count is unique codes.
        codes = set(re.findall(r"KR[A-Z0-9]{10}", result["answer"]))
        self.assertEqual(len(codes), count)
        self.assertNotIn("(출처: class_returns", result["answer"])

    def test_comparison_skips_llm_and_adds_return_limit(self):
        result = try_fast_compare(
            "Q", "미래에셋솔로몬장기국공채와 중장기국공채의 위험등급, 총보수, 최근 3년 수익률을 비교해줘"
        )
        self.assertIsNotNone(result)
        self.assertIn("LLM 호출 없음", result["think_trace"])
        self.assertIn("과거 수익률은 미래", result["answer"])
        self.assertIn("기준일 확인되지 않음", result["answer"])
        self.assertIn("총보수", result["answer"])


class SimpleDocumentTests(unittest.TestCase):
    def setUp(self):
        # 로컬 .env에 실제 키가 있어도 단위 테스트가 유료 호출을 하지 않는다.
        self.generate_patcher = patch(
            "agent_v2.document_path.generate",
            return_value=(None, "테스트에서 LLM 호출 생략"),
        )
        self.generate_patcher.start()

    def tearDown(self):
        self.generate_patcher.stop()

    def test_cover_and_toc_are_rejected(self):
        self.assertFalse(_usable({"doc_id": "d", "page": 1, "text": "(표지) 투자설명서"}))
        self.assertFalse(_usable({"doc_id": "d", "page": 2, "text": "1. 투자전략"}))

    def test_contact_directory_is_rejected(self):
        self.assertFalse(_usable({
            "doc_id": "d", "page": 53,
            "text": "회사 주소 서울특별시 연락처 02-1234-5678 홈페이지 www.example.com",
        }))

    def test_product_document_path_is_scoped_and_cited(self):
        result = try_simple_product_document("Q", "미래에셋장기성장포커스 투자전략은 뭐야?")
        self.assertIsNotNone(result)
        self.assertEqual(result["route"], "rag")
        self.assertIn("p.", result["answer"])
        self.assertIn("상품 문서 Hybrid RAG", result["think_trace"])
        self.assertLessEqual(result["retrieved_context"].count("\n---\n"), 3)
        self.assertNotIn("p.53", result["retrieved_context"])

    def test_product_answer_normalizes_citation_and_returns_only_used_evidence(self):
        strategy = {
            "doc_type": "product", "doc_id": "DOC-X", "page": 10,
            "chunk_id": "s", "product_code": "KR510902511M",
            "text": "투자전략: 국내 주식에 투자합니다.",
        }
        risk = {
            "doc_type": "product", "doc_id": "DOC-X", "page": 20,
            "chunk_id": "r", "product_code": "KR510902511M",
            "text": "주요 투자위험: 가격 변동으로 원금손실이 발생할 수 있습니다.",
        }
        unused = {
            "doc_type": "product", "doc_id": "DOC-X", "page": 30,
            "chunk_id": "u", "product_code": "KR510902511M",
            "text": "사용하지 않은 부가정보입니다.",
        }
        with patch(
            "agent_v2.document_path.retrieve_document_hits",
            side_effect=[[strategy, unused], [risk, unused]],
        ), patch(
            "agent_v2.document_path.generate",
            return_value=(
                "투자전략은 국내 주식 투자입니다. [product/DOC-X p.10] "
                "주요 위험은 원금손실 가능성입니다. [product/DOC-X p.20]",
                "가짜 생성",
            ),
        ):
            result = try_simple_product_document(
                "Q", "미래에셋장기성장포커스 투자전략과 주요 위험요인을 설명해줘"
            )
        self.assertIn("(출처: DOC-X, p.10)", result["answer"])
        self.assertIn("(출처: DOC-X, p.20)", result["answer"])
        self.assertNotIn("p.30", result["retrieved_context"])

    def test_atomic_institution_fact_skips_llm(self):
        result = try_simple_institution_document("Q", "IRP의 세액공제 한도는 얼마인가요?")
        self.assertIsNotNone(result)
        self.assertEqual(result["route"], "institution_facts")
        self.assertIn("LLM 호출 없음", result["think_trace"])

    def test_procedure_uses_institution_rag_with_citation(self):
        result = try_simple_institution_document("Q", "퇴직연금 장외채권 매수 신청 어떻게 해?")
        self.assertIsNotNone(result)
        self.assertEqual(result["route"], "rag")
        self.assertIn("p.", result["answer"])
        self.assertIn("표지·목차 제외", result["think_trace"])

    def test_buy_cancel_prefers_exact_procedure(self):
        result = try_simple_institution_document("Q", "매수취소 어떻게해?")
        self.assertIsNotNone(result)
        self.assertIn("doc7", result["answer"])
        self.assertIn("연금계좌현황", result["answer"])

    def test_deposit_maturity_is_document_question(self):
        self.assertEqual(
            pre_route("예금 만기상환 자금은 자동 재예치 되는 거 아니었나요?").route,
            "AGENT",
        )

    def test_faq_index_is_skipped_for_deposit_maturity(self):
        result = try_simple_institution_document(
            "Q", "예금 만기상환 자금은 자동 재예치 되는 거 아니었나요?"
        )
        self.assertIsNotNone(result)
        self.assertIn("doc30 p.9", result["answer"])
        self.assertIn("2023년 7월 12일", result["answer"])

    def test_unrelated_question_is_not_intercepted(self):
        self.assertIsNone(try_simple_institution_document("Q", "오늘 날씨가 어때?"))

    def test_early_termination_tax_variants_rank_correct_evidence(self):
        """_coverage()의 행위어(중도해지 등) 가중치가 여러 표현에 걸쳐
        일반화되는지 확인한다. doc30 p.16/doc31 p.10은 둘 다 "DC, IRP
        계약기간 만료 전 중도해지... 기타소득세(16.5%)" 조항을 그대로
        담고 있다(2026-09-06 PDF 원문 대조 확인). 예전엔 이 셋 다 무관한
        ISA 절세 페이지(doc23)가 "연금저축"+"세금이"만 일치해서 1등으로
        올라왔다(실측, INST-05 회귀 방지)."""
        questions = [
            "연금저축을 중도해지하면 세금이 어떻게 되나요?",
            "연금저축펀드를 중도에 해지하면 어떤 세금을 내나요?",
            "연금저축 계약을 중도해지하면 어떤 세금이 부과되나요?",
            # "중도" 없이 "해지"만 쓴 준말 - 한때는 이 낱말이 이 코퍼스에
            # 워낙 흔해(계약 해지 등) 검색 자체가 정답을 못 찾았다.
            # _action_term_expansions()가 "중도"가 전혀 안 보일 때만
            # "중도해지"를 덧붙여 검색·순위 양쪽에 반영하도록 고쳐서
            # 해결했다(과잉 확장 방지: "중도에 해지"처럼 이미 "중도"가
            # 있으면 확장하지 않는다 - 안 그러면 다른 subject의 우연한
            # "중도해지" 일치까지 덩달아 세져서 새 회귀가 났었다).
            "연금저축 해지 시 세금이 부과되나요?",
            # "연금저축"(세제적격, 현행)과 "(구)개인연금저축"(소득공제
            # 방식의 다른 상품)이 둘 다 "중도해지"+과세를 다루는 페이지가
            # 있어서 낱말 겹침만으로는 구분이 안 됐다(실측: 이 질문이
            # 개인연금저축의 이자소득 비과세 조항으로 샜었다) -
            # topic_coverage()에 "질문이 개인연금저축/구형/소득공제를
            # 명시하지 않았는데 후보가 그 상품 얘기면 감점"하는 규칙을
            # 추가해 해결(institution_facts.py가 이 둘을 별도 subject로
            # 분리해 둔 것과 같은 원리).
            "연금저축 중도해지 시 과세는?",
        ]
        for question in questions:
            hits = retrieve_document_hits(question, "institution")
            self.assertTrue(hits, question)
            self.assertIn("기타소득세", hits[0].get("text", ""), question)

    def test_early_termination_tax_without_jungdo_is_resolved(self):
        """팀원의 행위어 확장과 FactType 검색을 병합한 뒤에도 준말을 처리한다."""
        hits = retrieve_document_hits("연금저축 해지 시 세금이 부과되나요?", "institution")
        self.assertIn("기타소득세", hits[0].get("text", ""))

    def test_early_termination_tax_subject_ambiguity_is_resolved(self):
        """연금저축 중도해지 질문은 구체적인 세목 결과가 있는 근거를 우선한다."""
        hits = retrieve_document_hits("연금저축 중도해지 시 과세는?", "institution")
        self.assertIn("기타소득세", hits[0].get("text", ""))


class RagConditionsAnswerTests(unittest.TestCase):
    """api.server의 레거시 rag 경로(scripts.router.route_search +
    generate_answer)가 "어떤 경우에 가능한가" 류 질문에서 사유 목록을
    끝까지 뽑아내는지 확인한다. document_path 쪽과는 별개 코드 경로라
    tests/test_agent_v2.py의 다른 SimpleDocumentTests와는 분리해 둔다."""

    def setUp(self):
        self.generate_patcher = patch("agent_v2.document_path.generate", return_value=(None, "테스트에서 LLM 호출 생략"))
        self.generate_patcher.start()
        self.planner_patcher = patch(
            "agent_v2.query_analyzer.is_configured", return_value=False
        )
        self.planner_patcher.start()

    def tearDown(self):
        self.planner_patcher.stop()
        self.generate_patcher.stop()

    _REASON_GROUPS = [
        ["무주택", "주택구입", "전세보증금", "임차보증금"],
        ["요양", "의료비"],
        ["파산", "개인회생", "재난", "담보대출"],
    ]

    def test_early_withdrawal_conditions_lists_multiple_reasons(self):
        """실측(INST-06): must=["중도인출"] 한 단어만 보면 "IRP로 입금할 수
        있나요? 네, 가능합니다"처럼 실제 사유가 하나도 없는 답도 통과했다.
        대표 사유 범주(주택/요양/파산 계열)가 실제로 답변에 나오는지
        확인한다."""
        # Legacy extraction helper regression, not the production Planner API.
        from agent_v2.document_path import try_simple_institution_document as answer_payload

        questions = [
            "퇴직연금 중도인출은 어떤 경우에 가능한가요?",
            # "사유가 뭐가 있나요"로만 바꾸면 한때 검색 자체가 doc13(제도
            # 변경 안내, 무관)로 샜다 - _split_sentences가 "1." 같은
            # 번호 목록 마침표에서 잘못 쪼개 목록 인식 자체가 깨졌던
            # 게 원인이었다(수정 후 회귀 테스트로 남긴다).
            "퇴직연금 중도인출 사유가 뭐가 있나요?",
        ]
        for question in questions:
            payload = answer_payload("Q", question)
            answer = payload.get("answer", "")
            for group in self._REASON_GROUPS:
                self.assertTrue(
                    any(term in answer for term in group),
                    f"{question!r} 답변에 {group} 중 어느 것도 없음: {answer!r}",
                )


if __name__ == "__main__":
    unittest.main()
