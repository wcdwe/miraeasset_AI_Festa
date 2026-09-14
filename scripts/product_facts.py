"""상품 하나에 대한 정량 질문을 구조화 DB에서 바로 답한다.

지금까지 구조화 DB(class_fees/class_returns/fund_aum)를 쓰는 경로는
"상품 2개 이상 비교" 하나뿐이었다. 그래서 "이 펀드 총보수 얼마야?" 같은
가장 흔한 질문이 텍스트 청크 검색으로 빠지고, 정확히 뽑아 둔 숫자를
못 쓰고 있었다. 이 모듈이 그 경로를 만든다.

원칙:
- 값은 DB에서 그대로 가져온다(해석하지 않는다).
- 어느 클래스·어느 기준일의 값인지 항상 같이 낸다. 보수·수익률은 시점과
  클래스에 따라 달라지는 값이라 그것 없이 숫자만 말하면 틀린 답이 된다.
- 없는 값은 없다고 말한다(추정하지 않는다).
"""

import json
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")
CLASS_FEES_JSON_PATH = os.path.join(
    REPO_ROOT, "data", "staging", "suhyeon", "class_fees.json"
)

# 보수 세부 내역(운용보수/신탁보수/일반사무관리회사보수 등)은 상세표
# 보강으로만 채워지고, build_product_facts_db.load_class_fees의 결정에
# 따라 SQL 스키마에는 안 들어간다(6축 숫자 비교엔 안 쓰이는 항목이라
# JSON에만 남겨 둔 것) - 그래서 필요할 때 class_fees.json에서 직접
# 읽는다. 사람이 알아볼 이름표도 여기서 붙인다.
_BREAKDOWN_LABELS = {
    "management_fee": "운용보수(집합투자업자보수)",
    "trustee_fee": "신탁보수(신탁업자보수)",
    "admin_fee": "일반사무관리회사보수",
    "other_expense": "기타비용",
    "transaction_cost": "매매·중개수수료 등 거래비용",
}
_BREAKDOWN_ORDER = ["management_fee", "trustee_fee", "admin_fee",
                    "other_expense", "transaction_cost"]

_FEE_BREAKDOWN_CACHE = None


def _load_fee_breakdown(path=CLASS_FEES_JSON_PATH):
    """(product_code, class_code) -> {label: 값 문자열} 사전.

    같은 라벨이 서로 다른 값으로 두 번 나오면(KR5116501001 실측:
    management_fee가 0.2와 0.02로 두 번 찍힘 - 추출이 그 문서에서 애매
    했다는 뜻) 그 라벨은 통째로 버린다. 어느 쪽이 맞는지 모르면서 하나를
    골라 답하면 근거 없이 숫자를 지어내는 것과 같다. 라벨을 아예 못 읽은
    항목(label=None, 106건)도 같은 이유로 안 쓴다."""
    global _FEE_BREAKDOWN_CACHE
    if _FEE_BREAKDOWN_CACHE is not None:
        return _FEE_BREAKDOWN_CACHE
    out = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
        for r in records:
            values, bad = {}, set()
            for item in r.get("fee_breakdown") or []:
                label, v = item.get("label"), item.get("value")
                if label not in _BREAKDOWN_LABELS or v is None:
                    continue
                if label in values and values[label] != v:
                    bad.add(label)
                else:
                    values[label] = v
            for label in bad:
                values.pop(label, None)
            if values:
                out[(r["product_code"], r["class_code"])] = values
    _FEE_BREAKDOWN_CACHE = out
    return out

# 답변에 펼쳐 보일 클래스 줄 수. 열몇 개를 다 늘어놓으면 고객이 못 읽는다.
MAX_CLASS_LINES = 5

# 질문이 무엇을 묻는지 - 겹치면 여러 개를 다 담는다(한 질문에 보수와
# 수익률을 같이 묻는 경우가 흔하다).
INTENT_KEYWORDS = {
    "fee": ("총보수", "보수", "수수료", "비용", "판매보수", "얼마나 떼", "비싸", "싸"),
    # "총보수"(fee)와 겹쳐서 항상 같이 걸린다 - 의도적이다. "운용보수
    # 얼마야?"에 총보수 범위만 답하고 정작 물어본 세부 항목(운용보수
    # 그 자체)은 답 어디에도 없던 게 갭이었다(220문항 테스트셋
    # 38~41/48번). fee 블록은 그대로 두고 이 블록이 세부 내역만 더 낸다.
    "fee_breakdown": ("운용보수", "신탁보수", "사무관리", "기타비용",
                      "거래비용", "총보수비용", "보수비용", "비용비율"),
    "return": ("수익률", "성과", "얼마나 벌", "수익", "실적", "올랐", "떨어졌"),
    # "작년에 얼마 벌었어?" - 연평균(누적)과 다른 값이라 따로 낸다.
    "yearly": ("작년", "재작년", "해마다", "연도별", "년도별", "매년", "해별",
               "올해", "지난해", "년에는", "년 수익", "연간 수익"),
    # 바닥글자 "위험"만으로 걸면 "위험요인"/"환율변동위험"/"신용위험"처럼
    # 투자설명서 본문(RAG)에만 있는 질적 위험 설명 질문까지 "위험등급
    # 몇 등급"으로 잘못 답하게 된다(실측: "주요 위험요인은 뭐야?"가
    # 위험등급 1등급이라고만 답함 - 220문항 테스트셋 20/64/192-198번과
    # 정면으로 겹치는 함정). "등급"이 붙어야 진짜 등급을 묻는 질문이다.
    "risk": ("위험등급", "등급"),
    "aum": ("설정액", "순자산", "규모", "자산총액", "얼마나 큰"),
    "cost_projection": ("비용예시", "1,000만원", "1000만원", "천만원", "투자하면"),
    # "해지"는 계좌 해지도 펀드 환매도 가리킬 수 있고 "정해지다"류 동사
    # 활용과도 글자가 겹쳐, 부분 문자열로는 셋을 구별할 수 없다. 세 가지를
    # 각자 다른 FactType으로 나눈다 - 펀드 환매수수료 조회(fund_redemption)는
    # "환매"라는 명확한 전문용어로만 걸고, "해지" 자체는 여기 목록에 넣지
    # 않는다(detect_intents가 형태소로 직접 판별한다). 주문취소는 anchor.py
    # 의 제도 라우팅 신호("매수\s*취소")와 같은 어휘를 쓴다.
    "fund_redemption": ("환매",),
    "account_termination": ("중도해지", "계약해지", "팔면", "빼면", "인출"),
    "order_cancellation": ("매수취소", "주문취소"),
    # "몇 시까지 신청하면", "돈 언제 들어와요" 같은 질문
    "timing": ("언제", "며칠", "몇 시", "시까지", "기준가", "지급", "들어와",
               "입금", "영업일", "청구하면", "신청하면"),
    # "매수"만 넣으면 "환매수수료"의 가운데 글자에 걸린다("환매수수료"
    # -> 환+매수+수료). 붙는 말까지 넣어 구분한다. "연금저축용"/"퇴직연금용"/
    # "어떤 클래스가 있"는 220문항 테스트셋 50~53번("OO의 연금저축용
    # 클래스는 뭐야?" 등) - 예전엔 아무 의도도 못 알아봐서(당시엔 조용히
    # fee로 넘겨짚거나, detect_intents 수정 이후엔 RAG로) 실제로 이미
    # 있는 class_meaning.account_type 정보를 못 썼다.
    "eligibility": ("가입할", "가입 가능", "가입자격", "살 수 있", "매수할", "매수 가능",
                    "담을 수", "연금저축계좌로", "IRP로", "IRP에서",
                    "연금저축용", "퇴직연금용", "어떤 클래스", "무슨 클래스",
                    "클래스가 있", "클래스는 뭐"),
    # "자산유형이 뭐야?"/"주식형이야 채권형이야?"/"상품코드가 뭐야?"
    # (220문항 테스트셋 2/3/8번) - product_master의 분류·위험등급은 이미
    # lines 맨 위 머리글에 항상 나가는데(intents와 무관하게), 정작 이
    # 질문들 자체를 못 알아봐서 단일 상품 경로를 안 타고 semantic_search로
    # 새 버려 상관없는 청크가 답으로 나갔었다. 새 코드 없이 머리글만으로
    # 이미 답이 되므로 아래 route 화이트리스트에만 추가한다.
    "identity": ("자산유형", "무슨 유형", "어떤 유형", "주식형이야", "채권형이야",
                 "혼합형이야", "상품코드", "펀드코드", "종목코드"),
}


# 한국어의 "~해지다"(정해지다·가능해지다·필요해지다)는 형용사·동사에 붙어
# "~하게 되다"를 뜻하는 흔한 구문인데, 글자만 보면 계약 "해지"와 구별이 안
# 된다. 실측: "퇴직금이 정해지는 방식"이 환매(redemption) 질문으로 분류돼,
# 제도 질문에 환매 설명이 없다는 이유로 답변이 계속 반려됐다.
#
# 처음엔 정규식으로 걸렀다 - 뒤에 오는 어미(-는/-다/-면...)를 열거했더니
# 어미는 열린 집합이라 넣어도 끝이 없었고("정해지나요"의 -나에서 또 샜다),
# 조건을 뒤집어 진짜 해지를 뜻하는 복합어만 예외로 둬도 그 목록이 계속
# 자랄 수밖에 없었다. 낱말 매칭으로는 문법을 흉내만 낼 뿐이라 근본적인
# 한계가 있다. 형태소 분석(Kiwi)으로 "해지"가 명사인지(계약을 해지하다)
# 동사 활용의 일부인지(정해지다) 직접 판별한다 - 문법 정보가 있으면
# 목록을 넓힐 필요가 없다.
_kiwi = None


def _get_kiwi():
    global _kiwi
    if _kiwi is None:
        from kiwipiepy import Kiwi
        _kiwi = Kiwi()
    return _kiwi


def _has_termination_noun(question: str) -> bool:
    return any(
        token.form == "해지" and token.tag.startswith("N")
        for token in _get_kiwi().tokenize(question or "")
    )


def detect_intents(question):
    """질문에서 알아본 의도 목록. 하나도 못 알아보면 빈 리스트.

    예전엔 못 알아보면 ["fee", "return"]로 조용히 넘겨짚었다. 그러면
    api/server.py가 "이 질문은 구조화 DB로 답할 수 있다"고 잘못 판단해서,
    "투자목적이 뭐야?"/"운용사가 어디야?"/"위험요인이 뭐야?"/"원금보장
    상품이야?"처럼 실제로는 투자설명서 본문(RAG)에만 있는 질문에도
    총보수·수익률 숫자를 답으로 내보냈다(실측: 8개 질문으로 재현 확인,
    전부 총보수 안내로 잘못 답함). 여기서 못 알아보면 빈 리스트를 그대로
    돌려줘야, server.py가 "구조화 DB로 답할 의도가 없다"고 보고 RAG로
    넘긴다. product_facts() 자신은 intents가 비어 오면 여전히 fee+return을
    기본값으로 쓴다(CLI로 이 모듈만 단독 호출할 때의 편의 기본값) - 그건
    이 함수가 아니라 product_facts()의 몫이라 그대로 둔다."""
    # "해지" 낱말은 계좌 해지도, "정해지다"류 동사 활용도 다 가리킬 수 있어
    # 부분 문자열로는 셋을 구별할 수 없다. 그래서 이 낱말 하나만 목록에
    # 넣지 않고 형태소(Kiwi)로 직접 판별한다 - "부분 문자열로 걸러 놓고
    # 형태소로 다시 확인"하는 이중 설계 대신, 애매한 낱말은 처음부터
    # 형태소가 유일한 판정 근거다. 나머지(중도해지·팔면·빼면·인출 등)는
    # 그 자체로 뜻이 뚜렷한 어구라 부분 문자열로 충분하다.
    q = (question or "").replace(" ", "")
    intents = [key for key, keywords in INTENT_KEYWORDS.items()
              if any(w.replace(" ", "") in q for w in keywords)]
    if "account_termination" not in intents and _has_termination_noun(question):
        intents.append("account_termination")
    return intents


def _classes_for(conn, code, class_code=None):
    sql = ("SELECT * FROM class_fees WHERE product_code = ?"
           + (" AND class_code = ?" if class_code else "")
           + " ORDER BY class_code")
    params = [code] + ([class_code] if class_code else [])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _returns_for(conn, code, class_code=None):
    sql = ("SELECT * FROM class_returns WHERE product_code = ? "
           "AND row_kind = 'class_return'"
           + (" AND class_code = ?" if class_code else "")
           + " ORDER BY class_code")
    params = [code] + ([class_code] if class_code else [])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _meaning_for(conn, code):
    """클래스 코드 -> 뜻. 코드를 그대로 답에 쓰면 고객이 못 알아본다."""
    return {r["class_code"]: dict(r) for r in conn.execute(
        "SELECT * FROM class_meaning WHERE product_code = ?", (code,))}


def _charges_for(conn, code):
    return {r["class_code"]: dict(r) for r in conn.execute(
        "SELECT * FROM class_charges WHERE product_code = ?", (code,))}


def _label(code, meaning):
    """답변에 쓸 클래스 이름. 뜻을 알면 말로, 모르면 코드 그대로."""
    m = meaning.get(code)
    if m and m.get("description"):
        # 종류형이 아닌 펀드는 클래스 코드 자리에 "투자신탁"처럼 형태
        # 이름이 들어가 있다(KR5123365001). 그걸 코드처럼 괄호에 달면
        # "클래스 구분 없는 단일 펀드 (투자신탁)"이 되어 되레 클래스가
        # 있는 것처럼 보인다. 이름표에 수수료방식도 판매경로도 없는
        # 행이 그 경우다.
        if not m.get("channel") and not m.get("fee_type"):
            return m["description"]
        return f"{m['description']} ({code})"
    return f"{code} 클래스"


def _fee_lines(fees, meaning):
    """보수를 '범위 + 조건별'로 적는다.

    대표 클래스를 정해 숫자 하나만 말할 수는 없다. 상품 100개의 클래스
    구성이 70가지로 제각각이라 어떤 코드를 대표로 잡아도 최소 30개
    상품에는 그게 없고, 한 펀드 안에서 총보수가 최대 1.5%p(0.7% <-> 2.2%)
    까지 벌어져서 아무 클래스나 집으면 틀린 답이 된다.

    그래서 일반 고객이 가입할 수 있는 클래스의 범위를 먼저 말하고,
    조건별로 펼친다. 기관·고액·랩 전용은 살 수가 없으므로 뺀다 - 안 빼면
    교보악사 Tomorrow장기우량처럼 싼 순서 넷이 전부 전용 클래스인 상품에서
    "제일 싼 게 0.1195%"라고 살 수도 없는 걸 안내하게 된다."""
    priced = [f for f in fees if f.get("total_fee") is not None]
    if not priced:
        return ["  보수: 총보수 값을 찾지 못했습니다."], []

    retail, restricted, unknown = [], [], []
    for f in priced:
        m = meaning.get(f["class_code"])
        if m is None:
            unknown.append(f)
        elif m.get("retail"):
            retail.append(f)
        else:
            restricted.append(f)

    shown = sorted(retail or unknown, key=lambda f: f["total_fee"])
    # 표기만 다른 같은 클래스가 두 줄로 나오는 걸 막는다(C-E / CE).
    # 코드는 다르지만 뜻도 값도 같으면 고객에겐 같은 것이다.
    deduped, seen = [], set()
    for f in shown:
        m = meaning.get(f["class_code"]) or {}
        key = (m.get("description"), f["total_fee"], f.get("distribution_fee"))
        if m.get("description") and key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    shown = deduped
    lo, hi = shown[0]["total_fee"], shown[-1]["total_fee"]

    lines = []
    if lo == hi:
        lines.append(f"  [총보수] 연 {lo}%")
    else:
        lines.append(f"  [총보수] 연 {lo}% ~ {hi}% — 가입 방법에 따라 다릅니다")

    # 클래스가 열몇 개씩 되는 상품이 흔한데 전부 나열하면 고객이 못 읽는다.
    # 연금 계좌로 살 수 있는 클래스를 앞세우고, 나머지는 제일 싼 것과
    # 제일 비싼 것만 보인다(범위가 어디서 오는지는 보여야 하므로).
    # 다만 범위의 양 끝은 반드시 보인다. 위에서 "연 1.09% ~ 2.07%"라고
    # 말해 놓고 2.07%짜리 줄이 없으면 그 숫자가 어디서 나왔는지 알 수 없다.
    pension = [f for f in shown
               if (meaning.get(f["class_code"]) or {}).get("account_type")]
    picked = pension or shown
    if len(picked) > MAX_CLASS_LINES:
        picked = picked[:MAX_CLASS_LINES - 1] + [picked[-1]]
    for edge in (shown[-1], shown[0]):
        if edge not in picked:
            picked = [edge] + picked if edge is shown[0] else picked + [edge]
    # "총보수 얼마?"처럼 클래스를 안 짚은 질문은 대개 가장 기본형(온라인·
    # 연금 같은 조건 없이 그냥 "A"/"C")을 궁금해하는 것이다(KR5147430065
    # 실측: PROD-08 검증 실패 - 연금 계좌 클래스만 앞세우다 보니 정작
    # 가장 기본적인 "A" 클래스(0.443%)가 범위 설명에만 녹아들고 숫자로는
    # 안 나왔다). 연금 클래스에 밀려도 기본형 A/C는 항상 보인다.
    base = next((f for f in shown if f["class_code"] in ("A", "C")), None)
    if base is not None and base not in picked:
        picked = picked + [base]
    picked = sorted(picked, key=lambda f: f["total_fee"])

    for f in picked:
        bits = [f"    - {_label(f['class_code'], meaning)}: {f['total_fee']}%"]
        if f.get("distribution_fee") is not None:
            bits.append(f"판매보수 {f['distribution_fee']}%")
        if f.get("sales_commission_desc") and f["sales_commission_desc"] != "-":
            bits.append(f"판매수수료 {f['sales_commission_desc']}")
        lines.append(", ".join(bits))
    rest = len(shown) - len(picked)
    if rest > 0:
        lines.append(f"    (이 외 {rest}개 클래스가 있으며 모두 위 범위 안입니다)")

    if restricted:
        names = ", ".join(sorted({
            meaning[f["class_code"]]["description"] for f in restricted}))
        lines.append(f"    ※ 이 펀드에는 {names} 클래스도 있으나 일반 개인 "
                     "고객은 가입할 수 없어 위 범위에서 제외했습니다.")
    if retail and unknown:
        lines.append(f"    ※ 가입 조건을 확인하지 못한 클래스 "
                     f"{', '.join(f['class_code'] for f in unknown)}는 제외했습니다.")
    elif unknown and not retail:
        lines.append("    ※ 클래스별 가입 조건을 문서에서 확인하지 못했습니다.")
    return lines, shown


# verify_data.check_returns_range와 같은 기준(-100%~500%)을 그대로
# 쓴다. 다만 이 범위만으로는 못 잡는 오류도 있다 - KR5131420025의
# class C는 연도별 수익률표(최근2년차 3,260.76%)뿐 아니라 연평균
# 수익률표(최근2년 469.56%, 최근3년 226.05%, 최근5년 99.93%, 설정후
# 45.30%)까지 두 표 모두에서 형제 클래스들(전부 한 자릿수)과 동떨어진
# 값이다. 원본 PDF의 글자 좌표까지 직접 확인했지만 추출 버그가 아니라
# 원본 문서 자체의 오류였다(같은 행의 비교지수도 다른 클래스들의
# 비교지수와 다르다 - 문서 제작 과정에서 이 클래스 행 전체가 다른
# 버전에서 잘못 옮겨진 것으로 보인다). 추출 버그가 아니므로 임의로
# 고치지 않는다(근거에 없는 숫자로 "정정"하면 그것도 지어내는 것과
# 같다) - 대신 값은 원문 그대로 내보내되, 상식 밖이면 그렇다고 밝혀서
# 사실처럼 보이지 않게 한다. 나머지 4개 항목(최근5년/최근3년의 일부
# 등)은 범위 안에 들어와 있어도, 같은 행 전체가 이미 의심스러우므로
# 이 클래스는 통째로 확인 대상으로 둔다.
SANE_RETURN_RANGE = (-100.0, 500.0)

# 사람이 원본 PDF까지 직접 확인해서 "추출 버그가 아니라 문서 자체의
# 오류"라고 판정한 (상품코드, 클래스코드). 새로 추가할 때는 반드시
# PDF 원문 좌표 확인 후 왜 추출 버그가 아닌지 근거를 남긴다 - 확인
# 없이 이 목록에 넣으면 진짜 추출 버그를 "원본 문제"로 덮어버릴 위험이
# 있다.
_KNOWN_SOURCE_ERRORS = {
    ("KR5131420025", "C"),
}


def _is_known_source_error(product_code, class_code):
    return (product_code, class_code) in _KNOWN_SOURCE_ERRORS


# 확인된 원본 오류 클래스(_KNOWN_SOURCE_ERRORS)는 한 줄에 값이 여러 개
# 나오는데, 숫자마다 이 문구를 반복하면 못 읽는 답이 된다(실측: 값
# 5개짜리 한 줄이 문구 5번 반복으로 화면을 채움) - 그 클래스를 보여줄
# 때 한 번만 달아 준다(_yearly/_return 블록 참고). 여기서는 범위 밖
# 숫자에만 개별로 꼬리말을 붙인다.
KNOWN_SOURCE_ERROR_NOTE = ("(참고: 이 클래스는 문서 자체에 오류로 보이는 "
                           "값이 섞여 있어 참고용으로만 보시기 바랍니다)")


def _return_caveat(v):
    """수익률 값이 상식 밖이면 붙일 꼬리말(개별 숫자용). 정상이면 빈 문자열."""
    if v is None:
        return ""
    lo, hi = SANE_RETURN_RANGE
    if lo <= v <= hi:
        return ""
    return "(문서 원문 그대로의 수치이나 상식적인 범위를 크게 벗어나 확인이 필요합니다)"


def _benchmark_for(conn, code, near_page=None):
    """이 상품의 비교지수 행. 한 상품에 클래스 그룹(개인연금/퇴직연금
    등)마다 별도 비교지수 행이 여러 개 있는 문서가 있다(같은 페이지에
    묶여 나온다 - 그룹을 가르는 별도 칸은 없다). class_code로 어느
    그룹인지 알 길이 없으므로, 지금 보여주는 클래스 행이 있던 페이지에
    가장 가까운 비교지수 행을 고른다 - 아무거나 하나 집으면 엉뚱한
    그룹의 비교지수가 붙을 수 있다."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM class_returns WHERE product_code = ? AND row_kind = 'benchmark'",
        (code,))]
    if not rows:
        return None
    if near_page is None or len(rows) == 1:
        return rows[0]
    return min(rows, key=lambda r: abs((r.get("page") or 0) - near_page))


def product_facts(code, class_code=None, intents=None, db_path=DEFAULT_DB_PATH):
    """상품 하나의 사실을 모아 (사람이 읽을 요약, 근거 목록)로 돌려준다."""
    intents = intents or ["fee", "return"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        prod = conn.execute(
            "SELECT * FROM product_master WHERE product_code = ?", (code,)
        ).fetchone()
        if not prod:
            return f"[{code}] 상품 정보를 찾지 못했습니다.", []
        prod = dict(prod)

        lines = [f"■ {prod.get('product_name') or code} ({code})"]
        ev = []
        head = []
        if prod.get("asset_type"):
            head.append(f"분류 {prod['asset_type']}")
        if prod.get("risk_level") is not None:
            head.append(f"위험등급 {prod['risk_level']}등급")
        if head:
            lines.append("  " + " | ".join(head))

        if "risk" in intents and prod.get("risk_level") is not None:
            ev.append({"table": "product_master", "product_code": code})

        meaning = _meaning_for(conn, code)
        charges = _charges_for(conn, code)

        fee_shown = []
        if "fee" in intents or "cost_projection" in intents:
            fees = _classes_for(conn, code, class_code)
            if not fees:
                # class_meaning(클래스 이름표)엔 있는데 class_fees(보수
                # 숫자표)엔 없는 클래스가 드물게 있을 수 있다. (한때
                # KR5194450018의 I클래스가 이 경우였는데, 조사해 보니
                # 환매수수료 안내문에만 이름이 실린 다른 펀드용 표준
                # 클래스 목록의 잔재였다 - 이 상품엔 애초에 없는 클래스라
                # extract_class_meaning.py에서 제외하도록 고쳤다. 그래도
                # 코퍼스가 넓어 "클래스는 있는데 숫자만 없는" 진짜 경우가
                # 또 나올 수 있으므로 이 구분 로직 자체는 남겨 둔다.)
                # "클래스 자체가 없다"와 "클래스는 있는데 보수 숫자가
                # 없다"는 다른 사실이라 구분해서 알려준다 - 뭉뚱그리면
                # 후자를 전자로 오해해 "그런 클래스는 없다"고 답하게 된다.
                if class_code and class_code in meaning:
                    lines.append(
                        f"  보수: {class_code} 클래스는 존재하지만 보수 정보를 "
                        "문서에서 확인하지 못했습니다.")
                else:
                    lines.append("  보수: 해당 클래스 정보를 찾지 못했습니다.")
            else:
                fee_lines, fee_shown = _fee_lines(fees, meaning)
                lines.extend(fee_lines)
                as_of = next((f.get("as_of") for f in fees if f.get("as_of")), None)
                if as_of:
                    lines.append(f"    (작성 기준일 {as_of})")
                for f in fee_shown:
                    ev.append({"table": "class_fees", "product_code": code,
                               "class_code": f["class_code"], "page": f.get("page")})

        if "fee_breakdown" in intents:
            # "fee" 의도가 이미 뽑아 둔 클래스 목록을 그대로 쓴다 - "운용보수"도
            # "보수"를 포함해서 항상 "fee"와 같이 걸리므로 보통 비어 있지
            # 않다. 혹시 비어 있으면(단독으로만 걸린 드문 경우) 새로 조회한다.
            bd_classes = fee_shown or [
                f for f in _classes_for(conn, code, class_code)
                if (meaning.get(f["class_code"]) or {}).get("retail")]
            breakdown = _load_fee_breakdown()
            per_class = []
            for f in bd_classes:
                vals = breakdown.get((code, f["class_code"]))
                if vals:
                    per_class.append((f["class_code"], vals))
                    ev.append({"table": "class_fees.fee_breakdown",
                               "product_code": code, "class_code": f["class_code"]})
            if not per_class:
                lines.append("  [보수 세부 내역] 운용보수·신탁보수 등 세부 항목은 "
                             "문서에서 확인하지 못했습니다.")
            else:
                # 운용보수/신탁보수/일반사무관리회사보수는 클래스별로 갈리는
                # 판매 방식이 아니라 펀드 전체에서 걷는 몫이라 클래스마다
                # 달라질 이유가 없다(실측: 조회된 클래스 전부 동일). 값
                # 조합이 하나뿐이면 클래스마다 반복해서 늘어놓지 않고
                # 한 번만 말한다. 실제로 클래스별로 갈리는 문서가 있으면
                # (조합이 둘 이상) 그때만 클래스별로 나눠 보인다.
                combos = {tuple(sorted(v.items())) for _cc, v in per_class}
                if len(combos) == 1:
                    vals = per_class[0][1]
                    parts = [f"{_BREAKDOWN_LABELS[k]} {vals[k]}%"
                             for k in _BREAKDOWN_ORDER if k in vals]
                    lines.append("  [보수 세부 내역] (모든 클래스 공통)")
                    lines.append("    - " + ", ".join(parts))
                else:
                    lines.append("  [보수 세부 내역] (클래스별로 다릅니다)")
                    for cc, vals in per_class[:MAX_CLASS_LINES]:
                        parts = [f"{_BREAKDOWN_LABELS[k]} {vals[k]}%"
                                 for k in _BREAKDOWN_ORDER if k in vals]
                        lines.append(f"    - {_label(cc, meaning)}: "
                                     + ", ".join(parts))

            # 총보수비용(total_fee_and_cost)은 총보수(total_fee)와 다른
            # 숫자다(기타비용까지 합친 값) - 헷갈리기 쉬운 자리라 이름을
            # 명시하고 총보수와 나란히 안 섞이게 별도 줄로 낸다.
            tfc = [(f["class_code"], f.get("total_fee_and_cost")) for f in bd_classes
                   if f.get("total_fee_and_cost") is not None]
            if tfc:
                lines.append("  [총보수비용] (총보수에 기타비용 등을 더한 값 - "
                             "총보수와 같은 숫자가 아닙니다)")
                for cc, v in tfc[:MAX_CLASS_LINES]:
                    lines.append(f"    - {_label(cc, meaning)}: {v}%")
                    ev.append({"table": "class_fees", "product_code": code,
                               "class_code": cc})

        if "fund_redemption" in intents:
            # 값을 문장 그대로 담아 둔 이유가 여기서 드러난다. "90일미만
            # 이익금의 30%"만 말하면 틀린 답이 되는 경우가 있다(뒤에
            # "다만 ... 부과하지 않음"이 붙는다).
            got = [(cc, c) for cc, c in sorted(charges.items())
                   if c.get("redemption_fee")]
            note = conn.execute(
                "SELECT redemption_note FROM product_charges WHERE product_code = ?",
                (code,)).fetchone()
            if not got and note and note["redemption_note"]:
                # 클래스별 표가 없어도 펀드 전체에 대해 적어 둔 문장이 있으면
                # 그게 답이다. "모릅니다"로 답할 이유가 없다.
                lines.append(f"  [환매수수료] {note['redemption_note']}.")
                ev.append({"table": "product_charges", "product_code": code})
            elif not got:
                lines.append("  [환매수수료] 문서에서 확인하지 못했습니다.")
            elif len({c["redemption_fee"] for _cc, c in got}) == 1:
                lines.append(f"  [환매수수료] {got[0][1]['redemption_fee']}")
                ev.append({"table": "class_charges", "product_code": code,
                           "page": got[0][1].get("page")})
            else:
                lines.append("  [환매수수료] 클래스에 따라 다릅니다")
                for cc, c in got:
                    lines.append(f"    - {_label(cc, meaning)}: {c['redemption_fee']}")
                    ev.append({"table": "class_charges", "product_code": code,
                               "class_code": cc, "page": c.get("page")})

        if "yearly" in intents:
            # 연평균은 여러 해를 묶은 값이라 "작년 성과"와 다르다. 해마다의
            # 값은 따로 실려 있고, 몇 년 몇 월 구간인지도 같이 말해야 한다
            # (문서마다 회계연도 시작이 다르다).
            yr = list(conn.execute(
                "SELECT * FROM yearly_returns WHERE product_code = ? "
                "AND row_kind = 'class_return'"
                + (" AND class_code = ?" if class_code else "")
                + " ORDER BY class_code, year_rank",
                [code] + ([class_code] if class_code else [])))
            if not yr:
                lines.append("  [연도별 수익률] 문서에서 확인하지 못했습니다.")
            else:
                by_class = {}
                for r in yr:
                    by_class.setdefault(r["class_code"], []).append(r)
                # 클래스가 여럿이면 다 늘어놓지 않는다. 연금 계좌용을
                # 앞세우고 하나만 보인다 - 해마다 값은 클래스별로 소수점
                # 아래만 다르다.
                pick = next((c for c in by_class
                             if (meaning.get(c) or {}).get("account_type")
                             and (meaning.get(c) or {}).get("retail")),
                            next(iter(by_class)))
                lines.append(f"  [연도별 수익률] {_label(pick, meaning)} 기준")
                if _is_known_source_error(code, pick):
                    lines.append(f"    {KNOWN_SOURCE_ERROR_NOTE}")
                for r in by_class[pick]:
                    caveat = _return_caveat(r["return_pct"])
                    lines.append(f"    - 최근 {r['year_rank']}년차"
                                 f"({r['period']}): {r['return_pct']}%{caveat}")
                    ev.append({"table": "yearly_returns", "product_code": code,
                               "class_code": pick, "page": r["page"]})
                if len(by_class) > 1:
                    lines.append(f"    (다른 클래스 {len(by_class) - 1}개도 "
                                 "있으며 소수점 아래에서만 차이가 납니다)")

        if "timing" in intents:
            rules = {r["kind"]: dict(r) for r in conn.execute(
                "SELECT * FROM trade_rules WHERE product_code = ?", (code,))}
            if not rules:
                lines.append("  [매입·환매 기준가격] 문서에서 확인하지 못했습니다.")
            for kind, title in (("매입기준가", "매입 시 기준가격"),
                                ("환매기준가", "환매 시 기준가격·지급시기")):
                r = rules.get(kind)
                if not r:
                    continue
                lines.append(f"  [{title}] {r['text']}")
                ev.append({"table": "trade_rules", "product_code": code,
                           "kind": kind, "page": r.get("page")})

        if "eligibility" in intents:
            got = [(cc, c) for cc, c in sorted(charges.items())
                   if c.get("eligibility")]
            if not got:
                # 가입자격 열은 문서 27개에만 있다. 없으면 이름표의 계좌
                # 종류로 답한다 - "연금저축 · 온라인"이면 연금저축 계좌로
                # 살 수 있다는 뜻이고, 그게 질문에 대한 답이다.
                by_account = {}
                for cc, m in sorted(meaning.items()):
                    if m.get("account_type") and m.get("retail"):
                        by_account.setdefault(
                            m["description"].split(" · ")[0], []).append(cc)
                if by_account:
                    lines.append("  [가입 가능한 계좌]")
                    for account, ccs in by_account.items():
                        lines.append(f"    - {account}: {', '.join(ccs)} 클래스")
                        ev.append({"table": "class_meaning", "product_code": code,
                                   "class_code": ccs[0]})
                    if not any(a for a in by_account
                               if "연금" in a):
                        lines.append("    ※ 연금 계좌 전용 클래스는 없습니다.")
                else:
                    lines.append("  [가입자격] 이 펀드에는 연금저축·퇴직연금 전용 "
                                 "클래스가 문서에 표시되어 있지 않습니다.")
            else:
                lines.append("  [가입자격]")
                for cc, c in got:
                    lines.append(f"    - {_label(cc, meaning)}: {c['eligibility']}")
                    ev.append({"table": "class_charges", "product_code": code,
                               "class_code": cc, "page": c.get("page")})

        if "return" in intents:
            rets = _returns_for(conn, code, class_code)
            if not rets:
                lines.append("  수익률: 해당 클래스 정보를 찾지 못했습니다.")
            else:
                lines.append(f"  [수익률(연평균, %)] 클래스 {len(rets)}개")
                for r in rets:
                    got = [(lbl, r.get(col)) for lbl, col in
                           (("1년", "return_1y"), ("2년", "return_2y"),
                            ("3년", "return_3y"), ("5년", "return_5y"),
                            ("설정후", "return_since_inception"))
                           if r.get(col) not in (None, "")]
                    if not got:
                        continue
                    txt = ", ".join(f"{lbl} {v}{_return_caveat(v)}" for lbl, v in got)
                    note = (f" {KNOWN_SOURCE_ERROR_NOTE}"
                            if _is_known_source_error(code, r["class_code"]) else "")
                    lines.append(f"    - {r['class_code']}: {txt}{note}")
                    ev.append({"table": "class_returns", "product_code": code,
                               "class_code": r["class_code"], "page": r.get("page")})
                bm = _benchmark_for(conn, code, near_page=rets[0].get("page"))
                if bm:
                    got = [(lbl, bm.get(col)) for lbl, col in
                           (("1년", "return_1y"), ("3년", "return_3y"),
                            ("설정후", "return_since_inception"))
                           if bm.get(col) not in (None, "")]
                    if got:
                        lines.append("    - 비교지수: "
                                     + ", ".join(f"{lbl} {v}" for lbl, v in got))
                        ev.append({"table": "class_returns", "product_code": code,
                                   "row_kind": "benchmark", "page": bm.get("page")})

        if "aum" in intents:
            a = conn.execute(
                "SELECT * FROM fund_aum WHERE product_code = ?", (code,)).fetchone()
            if a:
                a = dict(a)
                lines.append(f"  [규모] 순자산 {a.get('net_asset_latest')} "
                             f"{a.get('unit') or ''}")
                ev.append({"table": "fund_aum", "product_code": code,
                           "page": a.get("page")})
            else:
                lines.append("  규모: 정보를 찾지 못했습니다.")

        # "이 수익률/AUM은 언제 기준이야?" 질문에 답하려면 fee 의도가 아닐
        # 때도 자료 기준일이 나가야 한다(220문항 테스트셋 179~184번, 기준일
        # 범주 - "fee" 의도일 때만 작성기준일을 붙이던 위 코드는 총보수를
        # 안 물은 질문(수익률/AUM/위험등급만 물은 질문)에서는 기준일 자체가
        # 통째로 빠졌다). 투자설명서 한 건에 작성기준일은 하나뿐이라
        # class_fees 아무 행에서나 가져와도 같다 - 위에서 이미 fee로 못
        # 찾았을 때만 새로 조회한다.
        if "fee" not in intents and "cost_projection" not in intents:
            doc_as_of = conn.execute(
                "SELECT as_of FROM class_fees WHERE product_code = ? "
                "AND as_of IS NOT NULL LIMIT 1", (code,)).fetchone()
            if doc_as_of and doc_as_of["as_of"]:
                lines.append(f"  (자료 기준일: {doc_as_of['as_of']})")

        return "\n".join(lines), ev
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from product_lookup import find_products, find_class_code
    q = " ".join(sys.argv[1:])
    hits = find_products(q)
    if not hits:
        print("상품을 찾지 못했습니다.")
    else:
        cc = find_class_code(q)
        ints = detect_intents(q)
        print(f"(의도: {ints} / 클래스: {cc})\n")
        for code, name, _ in hits[:2]:
            s, ev = product_facts(code, cc, ints)
            print(s)
            print("  근거:", ev[:3], "...\n")
