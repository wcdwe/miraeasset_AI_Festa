"""연금계좌 세제 규칙·계산기.

이 파일의 세율·한도·기준 숫자는 전부 이 프로젝트가 이미 모아 둔 제도
안내 문서(institution 코퍼스, doc20/21/23/27/38/39/40/41/44/51 등)에서
실제로 확인한 값이다 - LLM이나 사람 기억으로 채우지 않는다(확인 방법:
scripts/search.py의 lexical_search로 "세액공제", "연금소득세", "기타소득세"
등을 검색해 여러 문서에서 같은 숫자가 서로 검증되는지 대조했다).

세법은 자주 바뀌고 예외도 많다(ISA 전환 특례, 부득이한 사유별 한도,
소규모주택연금 특례 등). 이 계산기는 코퍼스에서 명확히, 여러 문서로
교차 확인되는 "기본 규칙"만 다룬다. 조건에 안 맞거나 정보가 모자라면
숫자를 억지로 내지 않고 그 사유를 그대로 밝힌다 - 틀린 계산값을 자신
있게 내놓는 것이 "확인 안 됨"이라 답하는 것보다 훨씬 나쁘다(이 프로젝트
전체가 지켜 온 원칙과 같다).

사용법(CLI, 수동 점검용):
    python3 scripts/tax_calculator.py --check
    python3 scripts/tax_calculator.py --credit 9000000 --salary 60000000
    python3 scripts/tax_calculator.py --pension-rate 72
    python3 scripts/tax_calculator.py --retirement-reduction 15
"""

import argparse
import re

# (하한, 상한 또는 None, 세율, 근거)
PENSION_INCOME_TAX_BRACKETS = (
    (55, 69, 0.055, "만 55~69세 5.5% (근거: doc39 p.1, doc44 p.1)"),
    (70, 79, 0.044, "만 70~79세 4.4% (근거: doc39 p.1)"),
    (80, None, 0.033, "만 80세 이상 3.3% (근거: doc39 p.1)"),
)

# (하한 연차, 상한 연차 또는 None, 감면율, 근거)
# 감면율은 "원래 이연퇴직소득세에서 깎아 주는 비율"이다(70% 감면 = 30%만
# 납부, 표현은 아니고 실제로는 "70%만 납부"가 아니라 "30%만 감면"이 맞다 -
# 아래 표는 실제 코퍼스 표현을 그대로 따른다: 1~10년차 30%감면, 11~20년차
# 40%감면, 21년차부터 50%감면).
RETIREMENT_TAX_REDUCTION = (
    (1, 10, 0.30, "연금실제수령연차 1~10년차 30% 감면 "
                  "(근거: doc21 p.1, doc39 p.1, doc40 p.1, doc51 p.1)"),
    (11, 20, 0.40, "연금실제수령연차 11~20년차 40% 감면 "
                   "(근거: doc21 p.1, doc39 p.1, doc40 p.1, doc51 p.1)"),
    (21, None, 0.50, "연금실제수령연차 21년차 이상 50% 감면 "
                     "(근거: doc21 p.1, doc39 p.1, doc40 p.1, doc51 p.1)"),
)

TAX_CREDIT_RATE_HIGH = 0.165  # doc41 p.1
TAX_CREDIT_RATE_LOW = 0.132  # doc41 p.1
TAX_CREDIT_INCOME_THRESHOLD_SALARY = 55_000_000  # 총급여 5,500만원, doc41 p.1
TAX_CREDIT_INCOME_THRESHOLD_COMPREHENSIVE = 45_000_000  # 종합소득금액 4,500만원, doc41 p.1
TAX_CREDIT_LIMIT_IRP = 9_000_000  # 연금저축 포함 합산 900만원, doc41 p.1 / doc6 p.1
TAX_CREDIT_LIMIT_PENSION_SAVINGS = 6_000_000  # 연금저축 단독 600만원, doc41 p.1 / doc6 p.1
OTHER_INCOME_TAX_RATE = 0.165  # 기타소득세(연금외수령), doc20 p.1 / doc39 p.1 / doc27 p.4
COMPREHENSIVE_TAX_ANNUAL_THRESHOLD = 15_000_000  # 연금소득 종합과세/분리과세 선택 기준, doc39 p.1 / doc37 p.6


def tax_credit_rate(annual_salary=None, comprehensive_income=None):
    """(세액공제율 또는 None, 판단 근거 설명).

    소득 정보가 전혀 없으면 16.5%/13.2% 중 뭘 적용할지 정할 수 없으므로
    None을 돌려준다 - 임의로 하나를 골라 계산하면 틀린 답을 자신 있게
    말하는 셈이다."""
    if annual_salary is None and comprehensive_income is None:
        return None, "총급여 또는 종합소득금액 정보가 없어 세액공제율(16.5%/13.2%)을 판단할 수 없음"
    if annual_salary is not None and annual_salary <= TAX_CREDIT_INCOME_THRESHOLD_SALARY:
        return TAX_CREDIT_RATE_HIGH, (
            f"총급여 {annual_salary:,}원 ≤ 5,500만원 → 16.5% (근거: doc41 p.1)")
    if comprehensive_income is not None and comprehensive_income <= TAX_CREDIT_INCOME_THRESHOLD_COMPREHENSIVE:
        return TAX_CREDIT_RATE_HIGH, (
            f"종합소득금액 {comprehensive_income:,}원 ≤ 4,500만원 → 16.5% (근거: doc41 p.1)")
    basis = f"총급여 {annual_salary:,}원" if annual_salary is not None else f"종합소득금액 {comprehensive_income:,}원"
    return TAX_CREDIT_RATE_LOW, f"{basis}이 기준을 초과 → 13.2% (근거: doc41 p.1)"


def tax_credit_amount(contribution, account_type="irp", annual_salary=None,
                       comprehensive_income=None):
    """(세액공제액 또는 None, 계산 과정 설명 목록).

    account_type: "irp"(연금저축 포함 합산 900만원 한도) 또는
    "pension_savings"(연금저축만, 600만원 한도)."""
    limit = (TAX_CREDIT_LIMIT_IRP if account_type == "irp"
             else TAX_CREDIT_LIMIT_PENSION_SAVINGS)
    eligible = min(contribution, limit)
    rate, rate_reason = tax_credit_rate(annual_salary, comprehensive_income)
    steps = [
        f"세액공제 대상 납입액 = min(납입액 {contribution:,}원, 한도 {limit:,}원) "
        f"= {eligible:,}원 (근거: doc41 p.1{'  / doc6 p.1' if account_type == 'irp' else ''})",
        f"세액공제율 판단: {rate_reason}",
    ]
    if rate is None:
        return None, steps
    credit = round(eligible * rate)
    steps.append(f"세액공제액 = {eligible:,}원 × {rate * 100:.1f}% = {credit:,}원")
    return credit, steps


def pension_income_tax_rate(age):
    """(세율 또는 None, 근거 설명). 정상 연금수령(세액공제받은 원금+운용수익)에 적용."""
    for lo, hi, rate, reason in PENSION_INCOME_TAX_BRACKETS:
        if age >= lo and (hi is None or age <= hi):
            return rate, reason
    return None, f"만 {age}세는 연금수령 개시 가능 연령(만 55세) 미만으로 세율 구간을 판단할 수 없음"


def retirement_tax_reduction_rate(actual_receiving_years):
    """(감면율 또는 None, 근거 설명). 연금계좌 내 이연퇴직소득에만 적용."""
    for lo, hi, rate, reason in RETIREMENT_TAX_REDUCTION:
        if actual_receiving_years >= lo and (hi is None or actual_receiving_years <= hi):
            return rate, reason
    return None, f"연금실제수령연차 {actual_receiving_years}년차 구간을 판단할 수 없음(1년차 이상이어야 함)"


def other_income_tax(amount):
    """(세액, 근거 설명). 연금수령한도를 초과해 연금외수령하는 금액에 적용."""
    tax = round(amount * OTHER_INCOME_TAX_RATE)
    return tax, (f"기타소득세 = {amount:,}원 × {OTHER_INCOME_TAX_RATE * 100:.1f}% = {tax:,}원 "
                 "(근거: doc20 p.1, doc39 p.1, doc27 p.4)")


# ---------------------------------------------------------------------------
# 자연어 질문 -> 계산. answer_payload에서 쓴다.
# ---------------------------------------------------------------------------

# "900만원", "9천만원", "9,000,000원", "9000000원"을 원 단위 정수로 바꾼다.
# 세제 질문에서 금액은 거의 항상 "OOO만원" 꼴이라 "만원" 처리가 핵심이다.
RE_KRW_EOK_MAN = re.compile(r"(\d[\d,]*)\s*억\s*(?:(\d[\d,]*)\s*천)?\s*(?:(\d[\d,]*)\s*만)?\s*원?")
RE_KRW_CHEONMAN = re.compile(r"(\d[\d,]*)\s*천만\s*원?")
RE_KRW_MAN = re.compile(r"(\d[\d,]*)\s*만\s*원")
RE_KRW_PLAIN = re.compile(r"(\d[\d,]*)\s*원")
RE_AGE = re.compile(r"만?\s*(\d{1,3})\s*세")
RE_YEARS = re.compile(r"(\d{1,3})\s*년\s*차")


def _parse_krw(text):
    """질문 글자에서 맨 처음 나오는 원화 금액 하나를 원 단위 정수로. 없으면 None."""
    m = RE_KRW_EOK_MAN.search(text)
    if m and "억" in m.group(0):
        eok = int(m.group(1).replace(",", ""))
        cheon = int(m.group(2).replace(",", "")) if m.group(2) else 0
        man = int(m.group(3).replace(",", "")) if m.group(3) else 0
        return eok * 100_000_000 + cheon * 10_000_000 + man * 10_000
    m = RE_KRW_CHEONMAN.search(text)
    if m:
        return int(m.group(1).replace(",", "")) * 10_000_000
    m = RE_KRW_MAN.search(text)
    if m:
        return int(m.group(1).replace(",", "")) * 10_000
    m = RE_KRW_PLAIN.search(text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _parse_all_krw(text):
    """질문에 금액이 둘(납입액, 소득) 있을 수 있어 전부 뽑는다. 순서 유지."""
    # 각 표현이 서로 겹치지 않게(예: "9,000,000원"이 RE_KRW_PLAIN에도 걸리고
    # RE_KRW_MAN에도 걸리면 안 된다) 가장 구체적인 패턴부터 자리를 표시해
    # 가며 훑는다.
    taken = []  # (start, end) - 이미 다른 패턴이 차지한 자리
    spans = []  # (start, end, val)
    for pat, unit in ((RE_KRW_EOK_MAN, None), (RE_KRW_CHEONMAN, 10_000_000),
                      (RE_KRW_MAN, 10_000), (RE_KRW_PLAIN, 1)):
        for m in pat.finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e in taken):
                continue
            if unit is None:  # 억 단위 패턴
                if "억" not in m.group(0):
                    continue
                eok = int(m.group(1).replace(",", ""))
                cheon = int(m.group(2).replace(",", "")) if m.group(2) else 0
                man = int(m.group(3).replace(",", "")) if m.group(3) else 0
                val = eok * 100_000_000 + cheon * 10_000_000 + man * 10_000
            else:
                val = int(m.group(1).replace(",", "")) * unit
            taken.append((m.start(), m.end()))
            spans.append((m.start(), m.end(), val))
    spans.sort()
    return [v for _s, _e, v in spans]


def answer_from_question(question):
    """(요약 텍스트 또는 None, 근거 목록 또는 None).

    질문에서 계산에 필요한 숫자(금액/나이/연차)를 못 찾으면 None을 돌려준다
    - 부르는 쪽이 일반 검색(RAG) 경로로 넘어가면 된다. 계산기가 아무거나
    끼워 맞춰 억지로 답하면 안 된다."""
    q = question or ""
    ages = [int(x) for x in RE_AGE.findall(q)]
    years = [int(x) for x in RE_YEARS.findall(q)]
    amounts = _parse_all_krw(q)

    if ("세액공제" in q or "공제" in q) and amounts:
        contribution = amounts[0]
        income = amounts[1] if len(amounts) > 1 else None
        account_type = "pension_savings" if (
            "연금저축" in q and "irp" not in q.lower() and "IRP" not in q) else "irp"
        credit, steps = tax_credit_amount(
            contribution, account_type, annual_salary=income)
        if credit is None:
            return (f"세액공제 대상 납입액은 계산했지만, 총급여 또는 종합소득금액 정보가 "
                    f"없어 적용 세율(16.5%/13.2%)을 정할 수 없습니다.\n" + "\n".join(steps)), steps
        return (f"납입액 {contribution:,}원에 대한 예상 세액공제액은 {credit:,}원입니다.\n"
                + "\n".join(steps)), steps

    if ("연금소득세" in q) and ages:
        rate, reason = pension_income_tax_rate(ages[0])
        if rate is None:
            return None, None
        return f"만 {ages[0]}세 기준 연금소득세율은 {rate * 100:.1f}%입니다. ({reason})", [reason]

    if ("퇴직소득세" in q and ("감면" in q or "연차" in q)) and years:
        rate, reason = retirement_tax_reduction_rate(years[0])
        if rate is None:
            return None, None
        return f"연금실제수령연차 {years[0]}년차 기준 퇴직소득세 감면율은 {rate * 100:.0f}%입니다. ({reason})", [reason]

    if "기타소득세" in q and amounts:
        tax, reason = other_income_tax(amounts[0])
        return f"{amounts[0]:,}원에 대한 기타소득세는 {tax:,}원입니다. ({reason})", [reason]

    return None, None


def _check_demo():
    cases = [
        ("세액공제(IRP 900만원, 총급여 6천만원)",
         lambda: tax_credit_amount(9_000_000, "irp", annual_salary=60_000_000),
         1_188_000),
        ("세액공제(연금저축 600만원, 총급여 5천만원)",
         lambda: tax_credit_amount(6_000_000, "pension_savings", annual_salary=50_000_000),
         990_000),
        ("연금소득세율(만 72세)", lambda: pension_income_tax_rate(72)[0], 0.044),
        ("퇴직소득세 감면율(15년차)", lambda: retirement_tax_reduction_rate(15)[0], 0.40),
        ("기타소득세(1,000만원)", lambda: other_income_tax(10_000_000)[0], 1_650_000),
    ]
    ok = True
    for name, fn, want in cases:
        got = fn()
        got_val = got[0] if isinstance(got, tuple) else got
        mark = "OK " if got_val == want else "!! "
        ok = ok and got_val == want
        print(f"{mark}{name}: {got_val} (기대 {want})")

    print()
    parse_cases = [
        ("900만원 넣으면", [9_000_000]),
        ("연금저축 600만원, IRP 300만원 합쳐서 900만원", [6_000_000, 3_000_000, 9_000_000]),
        ("1억 2천만원", [120_000_000]),
        ("9,000,000원", [9_000_000]),
    ]
    for text, want in parse_cases:
        got = _parse_all_krw(text)
        mark = "OK " if got == want else "!! "
        ok = ok and got == want
        print(f"{mark}[금액추출] {text!r} -> {got} (기대 {want})")

    print()
    nl_cases = [
        ("IRP에 900만원 납입했고 총급여 6천만원인데 세액공제 얼마나 받아요?", True),
        ("만 72세인데 연금소득세율이 얼마인가요?", True),
        ("퇴직소득세 감면율이 15년차에는 얼마나 되나요?", True),
        ("기타소득세로 1000만원에 대해 얼마 내나요?", True),
        ("이 펀드 총보수 얼마야?", False),
    ]
    for q, want_answer in nl_cases:
        summary, evidence = answer_from_question(q)
        got_answer = summary is not None
        mark = "OK " if got_answer == want_answer else "!! "
        ok = ok and got_answer == want_answer
        print(f"{mark}[자연어] {q!r} -> {'답변함' if got_answer else '패스(RAG로)'}")
        if summary:
            print(f"     {summary.splitlines()[0]}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="자체 검산 시험")
    ap.add_argument("--credit", type=int, help="세액공제 계산: 납입액(원)")
    ap.add_argument("--account-type", default="irp", choices=("irp", "pension_savings"))
    ap.add_argument("--salary", type=int, help="총급여(원)")
    ap.add_argument("--comprehensive-income", type=int, help="종합소득금액(원)")
    ap.add_argument("--pension-rate", type=int, metavar="AGE", help="연금소득세율 조회: 나이")
    ap.add_argument("--retirement-reduction", type=int, metavar="YEARS",
                    help="퇴직소득세 감면율 조회: 연금실제수령연차")
    ap.add_argument("--other-income-tax", type=int, metavar="AMOUNT",
                    help="기타소득세 계산: 금액(원)")
    args = ap.parse_args()
    rc = 0
    if args.check:
        rc |= _check_demo()
    if args.credit is not None:
        credit, steps = tax_credit_amount(
            args.credit, args.account_type, args.salary, args.comprehensive_income)
        print("\n".join(steps))
        print(f"=> {credit}")
    if args.pension_rate is not None:
        rate, reason = pension_income_tax_rate(args.pension_rate)
        print(f"{reason} => {rate}")
    if args.retirement_reduction is not None:
        rate, reason = retirement_tax_reduction_rate(args.retirement_reduction)
        print(f"{reason} => {rate}")
    if args.other_income_tax is not None:
        tax, reason = other_income_tax(args.other_income_tax)
        print(f"{reason}")
    if not any((args.check, args.credit is not None, args.pension_rate is not None,
                args.retirement_reduction is not None, args.other_income_tax is not None)):
        ap.print_help()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
