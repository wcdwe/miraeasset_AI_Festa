"""Failed Planner JSON never invokes a looser condition parser."""
from .schemas import PlanStep, QueryPlan


def build_rule_plan(question, anchor=None):
    from .structured_request import compile_structured
    plan = compile_structured(question, anchor)
    if plan is not None:
        return plan
    # 정형 컴파일은 "위험등급 4 이하" 같은 조건형만 다룬다. 제도·세제 설명처럼
    # 문서로 답해야 하는 질문은 여기서 None이 되고, 그러면 호출부가 계획 없음
    # 으로 판단해 요청 전체를 503으로 떨어뜨렸다(실측: "DC와 DB ... 어떻게
    # 다른가요?"가 Planner JSON 실패와 겹치면 답변 자체가 안 나감).
    # LLM 계획이 망가졌다는 것이 문서 근거를 못 찾는다는 뜻은 아니므로,
    # 최소한의 문서 검색 계획은 Python이 결정적으로 세운다. 검색 범위
    # (product/institution)는 Plan Merger가 Anchor를 보고 채운다.
    return QueryPlan(
        intents=["문서 근거 조회"],
        tools=["RAG"],
        plan=[PlanStep(step=1, tool="RAG", purpose="질문에 필요한 문서 근거 검색",
                       inputs={"query": question})],
    )
