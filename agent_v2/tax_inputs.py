"""Bind money to its stated role, never to its position in the question."""
import re
from scripts.tax_calculator import _parse_krw, tax_credit_amount

MONEY = r"\d[\d,]*(?:\s*억(?:\s*\d[\d,]*\s*만)?|\s*천만|\s*만)?\s*원"


def extract_roles(question):
    roles = {}
    for role, label in (("annual_salary", r"총\s*급여|연봉"),
                        ("comprehensive_income", r"종합\s*소득(?:금액)?")):
        values = [_parse_krw(m[1]) for m in re.finditer(rf"(?:{label})\s*(?:은|는|이|가)?\s*({MONEY})", question)]
        if len(set(values)) > 1: raise ValueError(f"{role}에 서로 다른 금액이 있음")
        if values: roles[role] = values[0]
    # 사람은 "납입"이라는 낱말로만 묻지 않는다 - "400만원 넣었어", "300만원
    # 입금하면"처럼 흔한 표현을 못 읽어 납입액이 있는 질문까지 계산 불가로
    # 떨어졌다(실측). 다만 금액을 이 동사에 "붙어 있을 때만" 결합한다 -
    # 사이에 다른 말이 끼면("500만 원, IRP에 400만 원 넣으면"의 500만 원)
    # 그 금액이 무엇을 뜻하는지 문장이 밝힌 게 아니므로 잡지 않는다.
    # 세금을 내는 "납부"는 납입이 아니므로 넣지 않는다.
    contributions = []
    for pattern in (rf"(?:납입액|납입금액|납입금|납입|불입액|불입금)\s*(?:은|는|이|가)?\s*({MONEY})",
                    rf"({MONEY})\s*(?:을|를)?\s*(?:납입|불입|입금|저축|넣)"):
        contributions.extend(_parse_krw(m[1]) for m in re.finditer(pattern, question))
    if len(set(contributions)) > 1: raise ValueError("여러 계좌 납입액은 계좌별 합산 계획이 필요함")
    if contributions: roles["contribution"] = contributions[0]
    return roles


def calculate(question, inputs):
    if "세액공제" not in question: raise ValueError("현재 연결된 정형 계산 규칙은 기본 세액공제뿐임; 다른 세제는 문서 근거 필요")
    if re.search(r"ISA|이전|이월|결정세액|중도|해지|종합과세", question, re.I):
        raise ValueError("기본 세액공제 계산 범위를 벗어난 특례·조건은 별도 규칙 근거 필요")
    roles = extract_roles(question)
    supplied = inputs.get("tax_inputs") or inputs
    for key in ("contribution", "annual_salary", "comprehensive_income"):
        if key in supplied and supplied[key] is not None:
            if roles.get(key) != supplied[key]:
                raise ValueError(f"{key} 계산 입력을 질문의 명시적 역할·금액과 일치시킬 수 없음")
    if "contribution" not in roles: raise ValueError("납입액 역할이 명확한 금액이 없음")
    if not any(k in roles for k in ("annual_salary", "comprehensive_income")):
        raise ValueError("적용 공제율을 결정할 소득 정보가 없음")
    if "annual_salary" in roles and "comprehensive_income" in roles:
        raise ValueError("두 소득 기준이 함께 주어져 적용 대상 확인이 필요함")
    if re.search(r"-\s*\d|마이너스|음수", question) or any(v < 0 for v in roles.values()):
        raise ValueError("음수 금액은 허용되지 않음")
    irp = bool(re.search(r"(?<![A-Za-z])IRP(?![A-Za-z])", question, re.I))
    pension = "연금저축" in question
    if irp == pension: raise ValueError("단일 계좌 유형이 명확하지 않음; 합산 조건은 별도 처리 필요")
    amount, steps = tax_credit_amount(**roles, account_type="irp" if irp else "pension_savings")
    return {"inputs": roles, "value": amount, "unit": "원", "rules": steps,
            "summary": f"제공된 제도 자료의 기본 규칙 적용 시 세액공제 계산액은 {amount:,}원입니다.\n"
                       + "\n".join(steps) + "\n실제 환급액은 결정세액 등 추가 조건에 따라 달라지며 최신 적용 연도는 별도 확인이 필요합니다."}
