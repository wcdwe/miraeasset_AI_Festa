"""근거를 사람 말로 옮기는 단계.

무엇을 LLM에 맡기고 무엇을 안 맡기나
------------------------------------
어려운 일은 이미 앞에서 다 했다. 어떤 상품인지 찾고, 어느 클래스의
숫자인지 고르고, 일반 고객이 못 사는 클래스를 빼고, 근거 페이지를 다는
것은 구조화 DB 쪽 몫이다. 여기서 LLM이 하는 일은 그 결과를 읽기 좋은
말로 옮기는 것뿐이다.

이렇게 나눈 이유는 평가 기준이 정확성·근거 완전성·근거 기반(지어내지
않기)이기 때문이다. 숫자를 LLM이 고르게 하면 이 세 가지가 전부 LLM의
운에 걸린다. 숫자를 우리가 고르고 LLM은 문장만 만들면, 틀릴 수 있는
자리가 문장 하나로 줄어든다.

그래도 문장 만들다가 숫자를 흘릴 수 있어서, 답이 나온 뒤에 한 번 더 센다
(check_numbers). 근거에 없는 숫자가 답에 있으면 그 답은 버리고 근거를
그대로 내보낸다. 이 프로젝트가 데이터에 대해 해 온 것과 같은 원칙이다 -
값으로 검산할 수 있는 기준을 두고, 검산이 안 되면 안 담는다.

실행:
    python3 scripts/answer_llm.py --demo       # 프롬프트가 어떻게 생겼는지
    python3 scripts/answer_llm.py --check      # 숫자 검산기만 시험
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hcx import HcxError, chat, is_configured  # noqa: E402

SYSTEM_PROMPT = """\
너는 연금 상품·제도 안내 도우미다. 아래 규칙을 어기면 안 된다.

1. 오직 <근거>에 있는 내용만으로 답한다. <근거>에 없는 사실, 숫자,
   상품명, 제도 내용은 어떤 경우에도 쓰지 않는다. 아는 것 같아도 쓰지 않는다.
2. 숫자는 <근거>에 적힌 그대로 옮긴다. 반올림하거나 단위를 바꾸거나
   더하고 빼서 새 숫자를 만들지 않는다.
3. 답변 안에 근거 위치를 반드시 밝힌다. <근거>에 [문서 p.쪽] 표시가 있으면
   그대로 인용하고, 상품 조회 결과면 상품코드와 작성기준일을 밝힌다.
4. <근거>로 답할 수 없으면 "가지고 있는 자료로는 확인할 수 없습니다"라고
   말하고, 무엇이 있으면 답할 수 있는지 한 줄로 알려 준다. 추측하지 않는다.
5. 어떤 상품이 오를지, 어떤 상품을 사야 하는지는 말하지 않는다. 수익률
   예측, 특정 상품 추천, 투자 권유는 하지 않는다. 대신 자료에 있는 사실
   (보수, 과거 수익률, 위험등급)을 알려 주고 판단은 고객 몫으로 남긴다.
6. 질문에 조건이 빠져 있어 답이 달라지는 경우(가입 계좌 종류, 가입 경로
   등)에는 절대 되묻지 않는다. 이 답변은 한 번 나가면 그걸로 끝이고
   사용자가 다시 답할 기회가 없다 - 질문만 던지고 끝나면 그 턴은 통째로
   버려진 것과 같다. <근거>에서 가장 무난한 경우(예: 조건 없는 일반
   가입, 대표적으로 쓰이는 클래스)를 골라 그 조건으로 답하고, "이 조건
   기준이며 조건에 따라 달라질 수 있다"는 점만 짧게 덧붙인다.
7. 과거 수익률을 말할 때는 그것이 미래를 보장하지 않는다는 점을 한 번
   덧붙인다.

말투는 존댓말로 간결하게. 표 대신 짧은 문장과 목록을 쓴다. 5문장 안팎.
"""

# 답에 있어도 근거와 대조할 필요가 없는 숫자. 순서를 매기는 말이나
# 연금 제도 설명에 늘 따라붙는 표현이라 근거에서 못 찾아도 지어낸 게 아니다.
SAFE_NUMBER_CONTEXTS = ("첫째", "둘째", "셋째", "1)", "2)", "3)")
RE_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# "1)"/"2)"/"3)" 고정 목록만 봐서는 4 이상은 못 거른다. 절차를 여러 페이지
# 근거를 모아 1~N으로 통짜로 다시 매기면(원문은 페이지마다 번호가 이어지는데
# - p.6은 1~3, p.7은 4~6, p.8은 7 - 답은 처음부터 다시 센다) 번호 자체가
# 그 근거 페이지에 없는 숫자가 된다(실측: "6. 매수수량 입력"의 원문 번호는
# 7). 이건 지어낸 사실이 아니라 답을 읽기 좋게 다시 매긴 목록 표시일 뿐이다.
# 그 줄에서 맨 앞에 오는 "숫자+./)"만 목록 번호로 본다 - 같은 줄 뒤쪽의
# "(출처: doc7, p.8)"의 7·8까지 목록 번호로 오인하면 안 된다(실측: 한 줄
# 전체를 검사 범위로 잡아 줄 안의 모든 숫자가 걸린 적이 있음).
def _is_list_marker_number(answer: str, match: re.Match) -> bool:
    line_start = answer.rfind("\n", 0, match.start()) + 1
    if answer[line_start:match.start()].strip():
        return False  # 이 숫자 앞에 같은 줄의 다른 글자가 있으면 목록 번호가 아니다
    return bool(re.match(r"[.)]\s", answer[match.end():match.end() + 2]))


def _num_forms(tok):
    """같은 수를 문서가 여러 모양으로 적는다. 0.30 / 0.3 / 0.300.
    쉼표도 문서마다 있고 없다(1,789 / 1789). 대조할 때 이걸 맞춘다."""
    plain = tok.replace(",", "")
    forms = {tok, plain}
    try:
        f = float(plain)
    except ValueError:
        return forms
    if f == int(f):
        forms.add(str(int(f)))
    # 뒤에 붙은 0을 뗀 모양도 같은 수다(0.4300 -> 0.43)
    if "." in plain:
        forms.add(plain.rstrip("0").rstrip("."))
    return {x for x in forms if x}


def check_numbers(answer, context):
    """답에 있는데 근거에 없는 숫자를 돌려준다. 비어 있으면 통과."""
    hay = context.replace(",", "") + "\n" + context
    bad = []
    for m in RE_NUMBER.finditer(answer or ""):
        tok = m.group(0)
        window = answer[max(0, m.start() - 3): m.end() + 2]
        if any(s in window for s in SAFE_NUMBER_CONTEXTS):
            continue
        if _is_list_marker_number(answer, m):
            continue
        if not any(f in hay for f in _num_forms(tok)):
            bad.append(tok)
    return bad


# 시스템 프롬프트 규칙 6번(되묻지 않는다)을 LLM이 놓칠 수 있어서,
# 프롬프트만 믿지 않고 답이 실제로 되묻는 모양인지 규칙으로도 한 번 더
# 본다. 이 평가는 단일 턴이라 되물으면 그 턴은 사용자가 답할 기회 없이
# 그대로 끝난다 - 근거 없는 숫자를 거르는 check_numbers와 같은 이유로,
# 프롬프트 위반은 걸러서 버리고 조회 결과로 대신한다.
RE_ENDS_WITH_QUESTION = re.compile(r"[?？]\s*$")
# "궁금한 점 있으면 말씀해주세요"류 일반적인 맺음말과 구분하려면 구체적인
# 정보를 조건으로 되묻는 표현이어야 한다("~하시면"/"~해 주시면"으로 조건을
# 걸고 그 뒤에 알려달라/안내한다가 오는 꼴, 또는 "어느/어떤 X"로 항목을
# 콕 집어 되묻는 꼴). "?"로 끝나는지는 안 본다 - "말씀해주시면 안내드리
# 겠습니다"처럼 서술문 모양으로 되묻는 문장도 실제로 나온다.
RE_ASKS_BACK = re.compile(
    r"(하시면|해\s*주시면)[^.!?？\n]{0,20}(알려|말씀|안내)"
    r"|어느\s*[가-힣]{1,6}(로|을|를|인지)"
    r"|어떤\s*[가-힣]{1,6}(로|을|를|인지)"
)


def check_asks_back(answer):
    """답이 실제 정보 없이 되묻기만 하면 True."""
    text = (answer or "").strip()
    if not text:
        return False
    return bool(RE_ASKS_BACK.search(text))


# 규칙 5(추천/권유 금지)도 프롬프트만 믿지 않고 한 번 더 본다. "사세요"류
# 명령형과 "가장 좋다/추천한다"류 단정 평가 표현을 같이 잡는다 - 이 둘을
# 따로 두면 "이 상품이 가장 좋습니다"(명령형 아님)나 "매수하세요"(단정
# 표현 아님) 중 하나만 걸린다.
RE_BUY_IMPERATIVE = re.compile(r"사세요|매수하세요|가입하세요|투자하세요|담으세요")
# "추천드립니다"·"가장 좋습니다"는 무엇을 두고 하는 말이냐에 따라 다르다.
# 낱말만 보고 걸면 "국세청에 문의하시는 것을 추천드립니다" 같은 안내까지
# 상품 권유로 잡혀 멀쩡한 답변이 반려된다(실측). 막아야 하는 것은 특정
# 상품을 고르라는 말이므로, 같은 문장에 상품을 가리키는 말이 있을 때만 본다.
# 반면 "매수하세요"류 명령형은 그 자체로 상품 매매를 뜻해 맥락을 따지지 않는다.
RE_SOFT_RECOMMENDATION = re.compile(
    r"(추천(합니다|드립니다|해요)|권합니다|권해드립니다)"
    r"|((가장|제일)\s*(좋|낫|유리)[가-힣]{0,3}(습니다|아요|어요|다)?)"
)
RE_PRODUCT_CONTEXT = re.compile(r"상품|펀드|클래스|투자신탁|ETF|종목|KR[0-9A-Z]{10}")


def check_recommendation(answer):
    """답이 특정 상품 매수/가입을 권하거나 단정적으로 낫다고 하면 True."""
    text = answer or ""
    if RE_BUY_IMPERATIVE.search(text):
        return True
    return any(
        RE_SOFT_RECOMMENDATION.search(sentence) and RE_PRODUCT_CONTEXT.search(sentence)
        for sentence in re.split(r"[.!?\n]", text)
    )


# 근거에 없는 상품코드를 답이 지어내 언급하면(숫자가 아니라 코드라
# check_numbers로는 안 걸린다) 그 자체로 근거 이탈이다.
# 한글 뒤에는 \b가 안 먹는다(\b는 \w 경계인데, 한글도 \w라 "코드를"처럼
# 코드 바로 뒤에 조사가 붙으면 경계로 안 잡힌다) - 뒤쪽은 영숫자가 더
# 이어지지만 않으면 되므로 부정형 전방탐색으로 막는다.
RE_KR_CODE = re.compile(r"KR[0-9A-Za-z]{10}(?![0-9A-Za-z])")


def check_claims(answer, context):
    """답에 나온 상품코드(KR...) 중 근거에 없는 것을 돌려준다. 비어 있으면 통과."""
    codes = set(RE_KR_CODE.findall(answer or ""))
    return sorted(c for c in codes if c not in (context or ""))


# 질문이 물은 항목(보수/수익률/위험등급/설정액/비용예시 등)이 답에서
# 아예 안 다뤄졌는지 본다. product_facts.detect_intents와 같은 의도
# 분류를 재사용해 질문 쪽과 답변 쪽에 일관되게 적용한다 - 질문 분류에
# 쓰는 낱말과 답이 그 의도를 다뤘다고 볼 낱말은 다르므로(질문은 "총보수
# 얼마"처럼 캐묻는 말투, 답은 "총보수는 0.43%" 같은 서술형이라 낱말
# 자체는 겹친다) 여기서는 INTENT_KEYWORDS를 답 쪽에도 그대로 재사용한다
# - 의도를 물은 낱말이 답에도 나오는 게 정상이기 때문이다(질문 "총보수
# 얼마야" -> 답 "총보수는...").
def check_question_coverage(question, answer):
    """질문에서 감지된 의도 중 답에서 전혀 안 다뤄진 것을 돌려준다."""
    try:
        from product_facts import INTENT_KEYWORDS, detect_intents
    except ImportError:
        return []
    intents = detect_intents(question)
    ans = (answer or "").replace(" ", "")
    missing = []
    for intent in intents:
        kws = INTENT_KEYWORDS.get(intent, ())
        if kws and not any(w.replace(" ", "") in ans for w in kws):
            missing.append(intent)
    return missing


def verify_answer(question, answer, context):
    """생성된 답을 검증기(다섯 항목)로 훑는다. 실패 항목 설명 목록을 돌려준다.

    check_numbers만으로는 숫자 하나만 본다 - 상품코드를 지어내거나,
    추천성 문장을 넣거나, 질문이 물은 항목을 통째로 빠뜨려도 못 잡는다.
    이 다섯 항목이 설계에서 말한 검증기(숫자·계산 재검증/주장-근거 연결
    검사/질문 요구사항 누락 검사/단정 추천 검사/문서에 없는 주장 검사)에
    대응한다."""
    problems = []
    bad_numbers = check_numbers(answer, context)
    if bad_numbers:
        problems.append(f"근거에 없는 숫자 사용: {bad_numbers[:5]}")
    bad_codes = check_claims(answer, context)
    if bad_codes:
        problems.append(f"근거에 없는 상품코드 언급: {bad_codes[:5]}")
    if check_recommendation(answer):
        problems.append("추천/단정 표현 사용(규칙 5 위반)")
    if check_asks_back(answer):
        problems.append("정보 없이 되묻기만 함(단일 턴이라 답을 못 받음)")
    missing = check_question_coverage(question, answer)
    if missing:
        problems.append(f"질문이 물은 항목 미반영: {missing}")
    return problems


def build_messages(question, context):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"<근거>\n{context}\n</근거>\n\n<질문>\n{question}\n</질문>\n\n"
            "위 <근거>만 써서 <질문>에 답하라."},
    ]


def build_retry_messages(question, context, prev_answer, problems):
    """검증 실패 뒤 한 번 더 시도할 때 쓰는 메시지. 무엇이 왜 틀렸는지
    구체적으로 짚어 줘야 같은 실수를 반복하지 않는다 - 그냥 "다시 써라"만
    시키면 새 답도 같은 자리에서 또 어긋날 수 있다."""
    messages = build_messages(question, context)
    messages.append({"role": "assistant", "content": prev_answer})
    messages.append({"role": "user", "content":
        "방금 답이 아래 문제로 반려됐다. <근거>만 다시 확인해서 문제를 "
        "고친 답을 새로 써라. 고칠 수 없으면(근거 자체에 없는 내용이라면) "
        "규칙 4대로 확인할 수 없다고 답하라.\n\n"
        "문제:\n" + "\n".join(f"- {p}" for p in problems)})
    return messages


def generate(question, context, max_tokens=900):
    """(답변 글자, 어떻게 만들었는지 한 줄).

    LLM을 못 쓰거나 검증기를 (재시도까지) 통과 못 하면 None을 돌려준다.
    부르는 쪽이 근거를 그대로 내보내면 된다 - 틀린 문장보다 투박한 근거가
    낫다.

    검증기(verify_answer)에 걸리면 바로 버리지 않고 문제를 구체적으로
    짚어 한 번 다시 시켜 본다(설계의 "실패 -> 답변 1회 재생성"). 그래도
    걸리면 그때 버린다 - 한 번은 봐주되 두 번은 안 봐준다."""
    if not is_configured():
        return None, "HCX 키가 없어 LLM 생성을 건너뛰고 조회 결과를 그대로 내보냄"
    if not context or context.strip() in ("", "(검색된 근거 문서 없음)"):
        return None, "근거가 비어 있어 LLM을 부르지 않음"
    try:
        text = chat(build_messages(question, context), max_tokens=max_tokens)
    except HcxError as e:
        return None, f"HCX 호출 실패({e}) - 조회 결과를 그대로 내보냄"

    problems = verify_answer(question, text, context)
    if not problems:
        return text, "HCX가 조회 결과를 문장으로 옮김(검증기 5항목 통과)"

    try:
        retry_text = chat(build_retry_messages(question, context, text, problems),
                          max_tokens=max_tokens)
    except HcxError as e:
        return None, (f"1차 답 검증 실패({problems[:2]}) 후 재생성 호출도 "
                      f"실패({e}) - 조회 결과를 그대로 내보냄")

    retry_problems = verify_answer(question, retry_text, context)
    if not retry_problems:
        return retry_text, (f"1차 답 검증 실패({problems[:2]})로 1회 재생성함 - "
                            "재생성 답은 검증기 통과")
    return None, (f"1차 답 검증 실패({problems[:2]}), 재생성 답도 검증 "
                  f"실패({retry_problems[:2]}) - 조회 결과를 그대로 내보냄")


def _demo():
    ctx = ("■ 미래에셋솔로몬단기국공채증권자투자신탁1호(채권) (KR5153420063)\n"
           "  [총보수] 연 0.32% ~ 0.65% — 가입 방법에 따라 다릅니다\n"
           "    - 퇴직연금(DC/IRP) · 온라인 (C-P2e): 0.32%, 판매보수 0.12%\n"
           "    - 창구 (A): 0.65%, 판매보수 0.45%\n"
           "    (작성 기준일 2025-02-07)")
    for m in build_messages("이 펀드 총보수 얼마야?", ctx):
        print(f"--- {m['role']}\n{m['content']}\n")


def _check_demo():
    ctx = "총보수 0.4300%, 판매보수 0.300%, 비용예시 1년 44천원 (2025-07-07)"
    cases = [
        ("총보수는 0.43%입니다.", []),
        ("총보수는 0.4300%이고 판매보수는 0.3%입니다.", []),
        ("총보수는 0.55%입니다.", ["0.55"]),
        ("1년 비용은 44천원, 3년은 138천원입니다.", ["138"]),
    ]
    ok = True
    for ans, want in cases:
        got = check_numbers(ans, ctx)
        mark = "OK " if got == want else "!! "
        ok = ok and got == want
        print(f"{mark}{ans!r}\n     근거에 없는 숫자: {got} (기대 {want})")

    print()
    rec_cases = [
        ("이 상품을 매수하세요.", True),
        ("A클래스가 가장 좋습니다.", True),
        ("총보수는 A클래스가 가장 낮습니다.", False),
        ("총보수는 0.43%입니다.", False),
    ]
    for ans, want in rec_cases:
        got = check_recommendation(ans)
        mark = "OK " if got == want else "!! "
        ok = ok and got == want
        print(f"{mark}[추천표현] {ans!r} -> {got} (기대 {want})")

    print()
    claim_ctx = "미래에셋솔로몬단기국공채증권자투자신탁1호(채권) (KR5153420063) 총보수 0.43%"
    claim_cases = [
        ("KR5153420063의 총보수는 0.43%입니다.", []),
        ("KR5153420063와 KR0000000000을 비교하면...", ["KR0000000000"]),
    ]
    for ans, want in claim_cases:
        got = check_claims(ans, claim_ctx)
        mark = "OK " if got == want else "!! "
        ok = ok and got == want
        print(f"{mark}[근거외코드] {ans!r} -> {got} (기대 {want})")
    return 0 if ok else 1


def _mock_demo():
    """HCX 응답을 가짜로 넣어 답변 경로를 통째로 돌려 본다.

    망이 막힌 곳에서는 진짜 호출을 못 하는데, 그렇다고 LLM 갈래를 한 번도
    안 지나가 보고 배포할 수는 없다. 답을 만드는 부분만 가짜로 바꾸고
    나머지(경로 판단 -> 조회 -> 검산 -> think_trace)는 진짜로 돌린다."""
    import answer_llm as me
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from api.server import answer_payload
    import hcx

    q = "미래에셋솔로몬단기국공채 총보수 얼마야?"
    fakes = {
        "정상 응답": (
            "미래에셋솔로몬단기국공채증권자투자신탁1호(채권)(KR5153420063)의 총보수는 "
            "가입 방법에 따라 연 0.32%에서 0.65%까지 다릅니다. 퇴직연금(DC/IRP) 계좌로 "
            "온라인 가입하시면 0.32%로 가장 낮습니다. 작성기준일은 2025-02-07입니다."),
        "숫자를 지어낸 응답": (
            "총보수는 연 0.32%~0.65%이며, 10년간 보유하면 약 71만원의 비용이 "
            "발생합니다. 업계 평균인 0.48%보다 낮은 편입니다."),
    }
    real_chat, real_cfg = me.chat, me.is_configured
    ok = True
    try:
        for name, text in fakes.items():
            hcx.reset_breaker()
            me.chat = lambda *a, **k: text
            me.is_configured = lambda: True
            p = answer_payload("MOCK", q)
            how = p["think_trace"].strip().splitlines()[-1]
            used_llm = "검증기" in how and "통과" in how
            want_llm = name == "정상 응답"
            ok = ok and used_llm == want_llm
            print(f"{'OK ' if used_llm == want_llm else '!! '}{name}")
            print(f"     {how}")
            print(f"     답변 첫 줄: {p['answer'].splitlines()[0][:80]}")
    finally:
        me.chat, me.is_configured = real_chat, real_cfg
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="프롬프트 모양 보기")
    ap.add_argument("--check", action="store_true", help="숫자 검산기 시험")
    ap.add_argument("--mock", action="store_true",
                    help="가짜 HCX 응답으로 답변 경로 전체를 돌려 보기(망 없이)")
    args = ap.parse_args()
    rc = 0
    if args.demo:
        _demo()
    if args.check:
        rc |= _check_demo()
    if args.mock:
        rc |= _mock_demo()
    if not (args.demo or args.check or args.mock):
        ap.print_help()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
