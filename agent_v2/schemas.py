from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class FilterOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    CONTAINS = "contains"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class ProductMention(BaseModel):
    text: str
    role: Literal["single", "comparison_left", "comparison_right", "filter_target"] = "single"
    resolution_required: bool = True


class QueryFilter(BaseModel):
    field: str
    operator: FilterOperator
    value: Any = None
    source_text: str

    @model_validator(mode="after")
    def validate_null_operator(self):
        if self.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            self.value = None
        elif self.operator == FilterOperator.IN and not isinstance(self.value, list):
            raise ValueError("in 조건은 배열 값이 필요합니다")
        elif self.field in {"risk_level", "total_fee", "total_fee_and_cost", "aum", "distribution_fee"} or self.field.startswith("return_"):
            import math
            if self.operator == FilterOperator.CONTAINS:
                raise ValueError("숫자 지표에는 contains를 사용할 수 없습니다")
            values = self.value if self.operator == FilterOperator.IN else [self.value]
            if any(v is None for v in values): raise ValueError("null 비교에는 is_null/is_not_null을 사용해야 합니다")
            if any(isinstance(v, bool) for v in values): raise ValueError("숫자 조건에 bool 사용 불가")
            parsed = [float(v) for v in values]
            if not all(math.isfinite(v) for v in parsed): raise ValueError("유한한 숫자만 허용됩니다")
            self.value = parsed if self.operator == FilterOperator.IN else parsed[0]
        return self


class PlanStep(BaseModel):
    step: int = Field(ge=1)
    tool: Literal["RESOLVE", "FACT", "FILTER", "COMPARE", "RAG", "TAX", "POLICY"]
    purpose: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)


class MissingInformation(BaseModel):
    for_personalization: list[str] = Field(default_factory=list)
    from_evidence: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    intents: list[str] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    product_mentions: list[ProductMention] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    sort: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)
    return_all: bool = False
    missing: MissingInformation = Field(default_factory=MissingInformation)
    gap_types: list[str] = Field(default_factory=list)
    answerable_now: bool = True
    follow_ups: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    completeness: Literal["single_answer", "all_matches", "all_steps"] = "single_answer"
    plan: list[PlanStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_execution_order(self):
        steps = [item.step for item in self.plan]
        if len(steps) != len(set(steps)):
            raise ValueError("plan.step은 중복될 수 없습니다")
        known = set(steps)
        for item in self.plan:
            if any(dep not in known or dep >= item.step for dep in item.depends_on):
                raise ValueError("depends_on은 존재하는 이전 단계만 참조해야 합니다")
        plan_tools = {item.tool for item in self.plan}
        if self.plan and not self.tools:
            raise ValueError("plan이 있으면 tools를 비워 둘 수 없습니다")
        if self.plan and plan_tools != set(self.tools):
            raise ValueError("tools에 plan에서 사용하는 도구가 모두 포함되어야 합니다")
        if steps != sorted(steps): raise ValueError("plan은 step 순서로 정렬되어야 합니다")
        return self


class ProductCandidate(BaseModel):
    product_code: str
    product_name: str
    score: int = 0


class ProductResolution(BaseModel):
    status: Literal["exact", "alias", "ambiguous", "not_found", "not_applicable"]
    raw_text: str
    candidates: list[ProductCandidate] = Field(default_factory=list)
    reason: str


class AnchorLocked(BaseModel):
    products: list[ProductCandidate] = Field(default_factory=list)
    product_status: Literal[
        "none", "exact", "unambiguous", "multiple", "ambiguous", "not_found"
    ] = "none"
    class_codes: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)
    account_types: list[str] = Field(default_factory=list)
    return_all: bool | None = None
    limit: int | None = Field(default=None, ge=1)
    sort: list[dict[str, Any]] = Field(default_factory=list)


class AnchorHint(BaseModel):
    values: list[str] = Field(default_factory=list)
    confidence: Literal["low", "high"] = "low"


class AnchorHints(BaseModel):
    fact_types: AnchorHint = Field(default_factory=AnchorHint)
    source_types: AnchorHint = Field(default_factory=AnchorHint)
    safety_flags: AnchorHint = Field(default_factory=AnchorHint)


class QueryAnchor(BaseModel):
    """Python 확정 조건과 의미 힌트를 분리한 Planner 입력 계약."""
    locked: AnchorLocked = Field(default_factory=AnchorLocked)
    hints: AnchorHints = Field(default_factory=AnchorHints)
    allowed_source_types: list[Literal["product", "institution", "structured"]] = Field(
        default_factory=lambda: ["product", "institution", "structured"]
    )
    forbidden_source_types: list[Literal["product", "institution", "structured"]] = Field(
        default_factory=list
    )

    @property
    def products(self):
        return self.locked.products

    @property
    def product_status(self):
        return self.locked.product_status

    @property
    def confirmed_fact_types(self):
        return self.hints.fact_types.values

    @property
    def filters(self):
        return self.locked.filters

    @property
    def periods(self):
        return self.locked.periods

    @property
    def account_types(self):
        return self.locked.account_types

    @property
    def return_all(self):
        return self.locked.return_all

    @property
    def safety_flags(self):
        return self.hints.safety_flags.values


class PreRouteDecision(BaseModel):
    route: Literal[
        "FAST_POLICY", "FAST_FACT", "FAST_FILTER", "FAST_COMPARE", "AGENT",
    ]
    reasons: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    template_id: str | None = None
    needs_query_llm: bool = False
    needs_answer_llm: bool = False
    needs_validator_llm: bool = False


class RiskDecision(BaseModel):
    level: Literal["LOW", "HIGH"]
    reasons: list[str] = Field(default_factory=list)
    requires_llm_validation: bool = False


class Evidence(BaseModel):
    evidence_id: str
    kind: Literal["resolution", "structured", "document", "calculation", "policy"]
    content: str
    source: str
    product_code: str | None = None
    class_code: str | None = None
    page: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    status: Literal["PASS", "FAIL", "PARTIAL"]
    tool_results: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ContextBundle(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    omitted_evidence_ids: list[str] = Field(default_factory=list)
    char_count: int = 0
    truncated: bool = False
    missing_task_ids: list[str] = Field(default_factory=list)


class ValidationErrorItem(BaseModel):
    criterion: str
    problem: str
    correction: str
    claim_id: str | None = None
    evidence_id: str | None = None


class MissingEvidenceQuery(BaseModel):
    source_type: Literal["institution", "product", "structured"]
    product_code: str | None = None
    fact_type: str = ""
    query: str = ""
    required_fact: str = ""


class ValidationResult(BaseModel):
    status: Literal["PASS", "FAIL"]
    retry_action: Literal[
        "NONE", "RESOLVE_PRODUCT", "REQUERY_DATA", "RECALCULATE",
        "RETRIEVE_MORE", "REGENERATE", "SAFE_FALLBACK",
    ]
    errors: list[ValidationErrorItem] = Field(default_factory=list)
    missing_evidence_queries: list[MissingEvidenceQuery] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_consistency(self):
        if self.status == "PASS" and (
            self.errors or self.missing_evidence_queries or self.retry_action != "NONE"
        ):
            raise ValueError("PASS는 오류가 없어야 하고 retry_action은 NONE이어야 합니다")
        if self.status == "FAIL" and not self.errors:
            raise ValueError("FAIL은 하나 이상의 오류가 필요합니다")
        if self.retry_action == "RETRIEVE_MORE" and not self.missing_evidence_queries:
            raise ValueError("RETRIEVE_MORE는 missing_evidence_queries가 필요합니다")
        return self
