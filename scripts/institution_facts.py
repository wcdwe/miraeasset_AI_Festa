"""제도(DB/DC/IRP/연금저축) 비교 질문을 원자적 사실 DB에서 바로 답한다.

institution 코퍼스 58개 문서는 대부분 청크 검색(RAG)에 맡겨도 되지만,
"DB와 DC 운용주체 차이", "IRP와 연금저축 차이"처럼 평가에서 답이 하나로
정해져야 하는 비교 질문은 RAG 순위가 흔들리면(TF-IDF 코퍼스가 바뀔 때마다
순위가 재계산된다 - search.py의 DC/DB 검색 버그가 실제로 이걸 한 번
망가뜨렸다) 통째로 오답이 나올 수 있다. 이 모듈은 그런 위험이 아예 없게,
문서에서 직접 확인한 사실만 (subject, predicate, value) 원자 단위로 담아
둔 institution_facts.json에서 값을 그대로 꺼내 답한다.

원칙(product_facts.py와 동일):
- 값은 JSON에서 그대로 가져온다(해석하지 않는다).
- 근거 문서·페이지·원문 문장을 항상 같이 낸다.
- 모르는 조합(주제를 못 알아보거나 사실이 없음)은 빈 결과를 그대로
  돌려준다 - 함부로 짐작해서 채우지 않는다.

이 모듈은 institution 코퍼스 전체를 구조화하지 않는다. 제도의 배경 설명,
사례, 장문 유의사항은 여전히 RAG(chunk 검색)가 맡는다 - 여기 담는 건
"틀리면 안 되는" 소수의 핵심 사실(운용주체/부담금/손실부담/가입대상/
중도인출/이전전환/세액공제/위험자산한도/수령요건)뿐이다.

사용법(CLI, 수동 점검용):
    python3 scripts/institution_facts.py --query "DB와 DC 운용주체 차이"
"""

import argparse
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTS_JSON_PATH = os.path.join(REPO_ROOT, "institution_facts.json")

# 질문에 쓰이는 여러 표현 -> 이 파일의 subject 코드.
SUBJECT_ALIASES = {
    "DB": ["db제도", "db형", "확정급여형퇴직연금제도", "확정급여형퇴직연금", "확정급여형", "db"],
    "DC": ["dc제도", "dc형", "확정기여형퇴직연금제도", "확정기여형퇴직연금", "확정기여형", "dc"],
    "IRP": ["개인형퇴직연금제도", "개인형퇴직연금", "irp"],
    "연금저축": ["연금저축계좌", "연금저축펀드", "연금저축신탁", "연금저축"],
    # 일반 "연금저축"과는 subject를 분리한다 - institution_facts.json의
    # pension_requirement 사실은 2000.12.31 이전에 개설된 (구)개인연금저축
    # 기준이라, "구형"/"개인연금저축"을 콕 집어 물었을 때만 이 사실이
    # 나와야 한다("연금저축"만으로는 못 걸리게 이 항목을 SUBJECT_ALIASES
    # 뒤쪽에 둔다 - "연금저축"도 "개인연금저축" 안에 부분열로 들어 있어
    # 둘 다 걸릴 수 있지만, facts_for가 실제 있는 predicate만 돌려주므로
    # 문제 없다).
    "개인연금저축(구형)": ["개인연금저축", "구형연금저축", "연금저축구형"],
}

# 질문에 쓰이는 여러 표현 -> 이 파일의 predicate 코드. 길게 겹치는 표현이
# 먼저 걸리도록, 값(리스트)마다 구체적인 낱말을 앞에 둔다.
PREDICATE_ALIASES = {
    "investment_decision_maker": ["운용주체", "운용은누가", "누가운용", "적립금운용", "운용을누가"],
    "contributor": ["부담금은누가", "누가부담금", "부담금을누가", "부담금", "기여금", "누가납입", "납입은누가"],
    "benefit_determination": ["급여결정", "급여가정해지는", "급여가확정", "사전에확정", "미리정해지는"],
    "loss_bearer": ["손실부담", "손실은누가", "손실을누가", "위험부담", "운용위험은누가", "운용위험을누가"],
    "mandatory_irp_transfer": ["의무이전", "irp로이전", "irp로의무"],
    "eligibility": ["가입대상", "가입조건", "가입할수있", "누가가입", "가입자격"],
    "early_withdrawal": ["중도인출", "중간정산"],
    # 일부 인출 가능 여부(전액해지만 되는지)는 중도인출 "사유" 충족
    # 여부와 다른 사실이라 predicate를 따로 둔다(합쳐서 한 레코드에
    # 담았더니 "법정사유 있으면 되는데 전액해지만 된다"는 게 앞뒤가
    # 충돌하는 것처럼 읽혔다 - institution_facts.json 참고).
    "withdrawal_method": ["일부인출", "전액해지", "전체해지", "일부만인출"],
    # DB/DC의 "제도전환"(퇴직금제도<->DB<->DC)과 IRP/연금저축의
    # "계좌이체"는 서로 다른 개념이라 predicate를 분리했다
    # (plan_conversion/transfer). 같은 낱말("전환"/"이전")을 양쪽에 다
    # 걸어 둬도 안전하다 - facts_for가 실제 그 subject에 있는
    # predicate만 돌려주므로, DB 질문엔 plan_conversion만, IRP 질문엔
    # transfer만 자연히 걸린다.
    "plan_conversion": ["제도전환", "전환"],
    "transfer": ["이전", "전환", "이체"],
    "risky_asset_limit": ["위험자산", "위험자산한도", "위험자산비중", "위험자산투자"],
    "contribution_limit": ["납입한도", "부담금한도", "얼마까지납입"],
    # "세액공제"만으로는 세액공제율(16.5%/13.2%, 소득에 따라 갈림 -
    # tax_calculator.py가 이미 계산한다)을 묻는 질문까지 가로챈다
    # ("연금저축 세액공제율은 몇 퍼센트예요?" 실측). "한도"가 같이 있을
    # 때만 이 predicate로 본다.
    # "얼마까지"는 한도를 묻는 말이라 "세액공제한도"와 같은 뜻이다(공식
    # 참고질의 "세액공제 얼마까지 되나요?"가 여기 안 걸려 정형 사실을 두고도
    # 문서 검색으로 새고, LLM이 납입한도 1,800만원을 세액공제 한도로 잘못
    # 답했다). 위 주석대로 공제율 질문("세액공제율은 몇 퍼센트")은 여전히
    # 안 걸린다 - 이 별칭들 중 어느 것도 그 문장에 없다.
    "tax_credit_limit": ["세액공제한도", "세액공제얼마까지", "세액공제를얼마까지",
                         "공제한도", "얼마까지세액공제", "얼마까지공제"],
    "pension_requirement": ["연금수령요건", "연금수령조건", "수령요건", "연금개시요건"],
    "definition": ["무엇인가요", "뭔가요", "뭐야", "란무엇", "이란"],
}

_SUBJECT_FULL_NAME = {
    "DB": "확정급여형",
    "DC": "확정기여형",
    "IRP": "개인형퇴직연금",
    "연금저축": "연금저축",
}

_PREDICATE_LABELS = {
    "definition": "정의",
    "investment_decision_maker": "운용주체",
    "contributor": "부담금 납입주체",
    "benefit_determination": "급여 결정방식",
    "loss_bearer": "운용손실 부담주체",
    "eligibility": "가입대상",
    "early_withdrawal": "중도인출",
    "withdrawal_method": "인출 방식",
    "plan_conversion": "제도전환",
    "transfer": "이전·전환",
    "mandatory_irp_transfer": "IRP 의무이전",
    "risky_asset_limit": "위험자산 투자한도",
    "contribution_limit": "납입한도",
    "tax_credit_limit": "세액공제 한도",
    "pension_requirement": "연금수령 요건",
}

COMPARISON_MARKERS = ["차이", "다른가요", "다르나요", "다릅니까", "비교", "차이점", "달라요"]

_FACTS_CACHE = None


def _load_facts(path=FACTS_JSON_PATH):
    global _FACTS_CACHE
    if _FACTS_CACHE is not None:
        return _FACTS_CACHE
    if not os.path.exists(path):
        _FACTS_CACHE = []
        return _FACTS_CACHE
    with open(path, "r", encoding="utf-8") as f:
        _FACTS_CACHE = json.load(f)
    return _FACTS_CACHE


def detect_subjects(question):
    """질문에서 언급된 제도(DB/DC/IRP/연금저축) 코드 목록. 언급 순서를 유지한다."""
    q = (question or "").lower().replace(" ", "")
    found = []
    for subject, aliases in SUBJECT_ALIASES.items():
        if subject in found:
            continue
        for alias in aliases:
            if alias in q:
                found.append(subject)
                break
    return found


def detect_predicates(question):
    """질문에서 알아본 predicate 목록. 하나도 못 알아보면 빈 리스트.

    product_facts.detect_intents와 같은 이유로, 못 알아보면 절대 기본값을
    조용히 채우지 않는다 - 그러면 상관없는 질문에도 이 경로가 걸려서
    엉뚱한 답을 낸다."""
    q = (question or "").lower().replace(" ", "")
    found = []
    for predicate, aliases in PREDICATE_ALIASES.items():
        if any(a in q for a in aliases):
            found.append(predicate)
    return found


def is_comparison_question(question):
    q = question or ""
    return any(m in q for m in COMPARISON_MARKERS)


def facts_for(subject, predicates=None, facts=None):
    """(subject, predicate 목록)에 해당하는 사실 레코드 목록."""
    facts = facts if facts is not None else _load_facts()
    rows = [r for r in facts if r.get("subject") == subject]
    if predicates:
        rows = [r for r in rows if r.get("predicate") in predicates]
    return rows


def _format_value(r):
    v = r.get("value")
    unit = r.get("unit")
    if unit:
        return f"{v}{unit if unit != 'percent' else '%'}"
    return v


def _format_fact_line(r):
    label = _PREDICATE_LABELS.get(r["predicate"], r["predicate"])
    line = f"- {label}: {_format_value(r)}"
    if r.get("condition"):
        line += f" ({r['condition']})"
    if r.get("source_as_of"):
        line += f" [{r['source_as_of']} 기준 문서]"
    return line


def institution_facts_answer(question, facts=None):
    """(요약 문자열 또는 None, 근거 목록).

    질문에서 subject를 하나도 못 알아보면 None을 돌려준다 - 이 경로가
    아니라 RAG로 넘어가야 한다는 뜻이다."""
    facts = facts if facts is not None else _load_facts()
    subjects = detect_subjects(question)
    if not subjects:
        return None, []

    predicates = detect_predicates(question)
    comparison = is_comparison_question(question) and len(subjects) >= 2

    evidence = []
    lines = []

    if comparison:
        # 비교 질문: 알아본 predicate만(없으면 흔히 비교에 쓰이는 predicate
        # 전부) 각 subject별로 나란히 보여준다.
        #
        # 한쪽 subject에만 사실이 있으면 답을 반토막으로 내보내지 않는다
        # (예: "IRP와 연금저축의 차이"에서 연금저축 쪽에 운용주체 사실이
        # 없다고 IRP만 보여주면, 마치 연금저축은 알 길이 없다는 듯 잘못
        # 읽힌다 - 이럴 땐 아예 RAG로 넘겨서 원문으로 답하는 게 낫다).
        # 그래서 모든 subject가 최소 하나는 겹치는 predicate로 답할 수
        # 있을 때만 이 경로를 쓴다.
        preds = predicates or [
            "definition", "investment_decision_maker", "contributor",
            "benefit_determination", "loss_bearer", "eligibility",
            "early_withdrawal", "transfer",
        ]
        per_subject = {s: facts_for(s, preds, facts) for s in subjects}
        if not all(per_subject.values()):
            return None, []
        for subject in subjects:
            full = _SUBJECT_FULL_NAME.get(subject)
            header = f"[{subject}({full})]" if full and full != subject else f"[{subject}]"
            sub_lines = [header]
            for r in per_subject[subject]:
                sub_lines.append(_format_fact_line(r))
                evidence.append({
                    "subject": subject, "predicate": r["predicate"],
                    "source_doc": r.get("source_doc"), "page": r.get("page"),
                    "evidence": r.get("evidence"),
                })
            lines.append("\n".join(sub_lines))
        return "\n\n".join(lines), evidence

    # 단일/복수 subject, 특정 predicate(들)에 대한 사실 조회.
    #
    # predicate를 하나도 못 알아봤으면 여기서 답하지 않고 그대로 None을
    # 돌려준다(product_facts.detect_intents와 같은 원칙). "IRP가 뭐야?"는
    # "뭐야"가 definition 별칭에 이미 걸려 있어 이 경로로도 문제없이
    # 답한다 - 예전엔 여기서 "정의를 기본값으로 보여주자"고 따로
    # 채워 넣었는데, 그러면 "연금저축을 중도해지하면 세금이 어떻게
    # 되나요?"처럼 subject만 언급되고 predicate는 못 알아본 질문에도
    # (세금 얘기인데) 엉뚱하게 "정의"만 답하고 끝나 버렸다(실측).
    if not predicates:
        return None, []

    any_row = False
    for subject in subjects:
        rows = facts_for(subject, predicates, facts)
        if not rows:
            continue
        # subject가 하나뿐이어도 답에 그 이름을 남긴다 - 예전엔 여러
        # subject를 물었을 때만 헤더를 붙였는데, 그러면 "IRP 계좌는 누가
        # 가입할 수 있나요?"처럼 subject 하나짜리 질문의 답이 "- 가입대상:
        # ..."으로만 나가 질문에서 되물은 대상(IRP)이 사라진다(실측,
        # INST-08 - 값은 맞는데 "무엇에 대한" 값인지 답 자체엔 없었다).
        full = _SUBJECT_FULL_NAME.get(subject)
        sub_lines = [f"[{subject}({full})]" if full and full != subject else f"[{subject}]"]
        for r in rows:
            sub_lines.append(_format_fact_line(r))
            evidence.append({
                "subject": subject, "predicate": r["predicate"],
                "source_doc": r.get("source_doc"), "page": r.get("page"),
                "evidence": r.get("evidence"),
            })
        lines.append("\n".join(sub_lines))
        any_row = True

    if not any_row:
        return None, []
    return "\n\n".join(lines), evidence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    args = ap.parse_args()
    summary, evidence = institution_facts_answer(args.query)
    if summary is None:
        print("(구조화 사실을 못 찾음 - RAG로 넘어가야 함)")
        return
    print(summary)
    print()
    print("근거:")
    for e in evidence:
        print(f"  {e['subject']}.{e['predicate']} <- {e['source_doc']} p.{e['page']}: {e['evidence']}")


if __name__ == "__main__":
    main()
