"""투자자가 직접 부담하는 수수료와 클래스별 가입자격을 뽑는다.

투자설명서에는 "가. 투자자에게 직접 부과되는 수수료" 표가 있고, 여기에
지금까지 우리가 안 갖고 있던 것들이 한꺼번에 들어 있다.

    구분              가입자격            선취판매수수료   후취판매수수료  환매수수료  전환수수료
    종류A             제한 없음           납입금액의 1.0% 이내   -          -         -
    종류A-e           온라인 투자자        납입금액의 0.5% 이내   -          -         -
    종류C2(보수체감)   1년이상 종류C1가입자   -                 -          -         -

세 가지가 새로 생긴다.

1. 환매수수료. 지금 class_fees에는 칸 자체가 없었다. 100개 문서 전부
   '환매수수료'라는 말이 나오지만 대부분은 잡음이다 - 연혁표의 "환매수수료
   삭제", 용어집의 정의, 투자위험 설명. 실제 값은 이 표에만 있다.

2. 가입자격. class_meaning은 이름표의 속성(기관/고액/랩)으로 "일반 고객이
   살 수 있나"를 판정하는데, 이 표는 문서가 직접 "제한 없음" / "온라인
   투자자" / "1년이상 종류C1가입자"라고 적어 둔다. 추론이 아니라 원문이다.

3. 클래스 전환 관계. "1년이상 종류C1가입자"처럼 보수체감 클래스가 어떤
   조건으로 넘어가는지가 여기 적혀 있다.

값을 숫자로 바꾸지 않고 문장을 통째로 담는 이유:

    수수료미징구-오프라인-퇴직연금(C) | 90일미만 이익금의 30%.
        다만, 2013년1월17일 이후 환매 청구하는 경우에는 환매수수료를 부과하지 않음

숫자만 뽑으면 "90일 안에 팔면 이익금의 30%를 뗀다"가 되는데 실제로는 아무도
안 뗀다. 조건문을 값 하나로 줄이면 틀린 답이 된다.

실행:
    python3 scripts/extract_class_charges.py
    python3 scripts/extract_class_charges.py --check
"""

import argparse
import glob
import json
import os
import re
import sqlite3

import pdfplumber

import pdf_words
from extract_class_meaning import _parse, _squash
from extract_class_returns import cluster_lines

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "class_charges.json")
OUTPUT_PRODUCT_JSON = os.path.join(REPO_ROOT, "product_charges.json")
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")

# 열 이름 -> 우리가 쓸 이름. 헤더가 두 줄로 쪼개져 있어서(구분/가입자격/수수료율
# 밑에 선취판매수수료/후취판매수수료/환매수수료/전환수수료) 열마다 위아래를
# 이어 붙인 뒤 찾는다.
COLUMNS = (
    ("eligibility", ("가입자격",)),
    ("front_load_fee", ("선취판매", "선취")),
    ("back_load_fee", ("후취판매", "후취")),
    ("redemption_fee", ("환매수수료", "환매")),
    ("switch_fee", ("전환수수료", "전환")),
)

# 좌표 재구성 파서도, 「종류|가입자격」 2칸 표 폴백도 못 잡는 극소수
# 케이스를 PDF 원문 대조로 확인해 그대로 못박아 둔다(KR5118420036
# S-P(퇴직) - 실측: 39쪽 "S-P" 코드 조각과 41쪽 뒤 "(퇴직)" 조각
# 사이에 가입자격 설명 문장이 여러 줄 끼어 있고, 그 값 칸("- -")이
# "(퇴직)" 조각이 code로 합쳐지기도 전에 지나가 버려 pending_bare
# 병합 로직으로도 못 붙잡는다). 선취/후취판매수수료는 원문이 "-"라
# _clean()의 대시 규칙대로 "없음", 환매수수료는 34쪽 "이 투자신탁은
# 수익증권을 환매시 환매수수료를 부과하지 않습니다"라는 펀드 전체
# 문장이 있어 형제 클래스(S, S-P)와 동일하게 "없음"이다.
_KNOWN_MISSING_ROWS = {
    ("KR5118420036", "S-P(퇴직)"): {
        "eligibility": None,
        "front_load_fee": "없음",
        "back_load_fee": "없음",
        "redemption_fee": "없음",
        "switch_fee": None,
        "page": 39,
    },
}

MAX_HEADER_ROWS = 5
# 헤더 칸은 이름표라 짧다. 이보다 길면 본문 문장으로 본다.
MAX_HEADER_CELL = 14
# "-"와 "없음"은 빈칸이 아니라 "이 수수료는 없다"는 답이다. 버리면
# "환매수수료 없습니다"라고 말할 수 있는 걸 "모릅니다"로 답하게 된다.
# 문서가 없다고 적은 것과 우리가 모르는 것은 다르다.
NONE_MARKS = {"-", "–", "—", "−", "없음", "해당없음", "해당사항없음", "미부과"}

# 헤더 글자가 데이터 칸으로 흘러드는 표가 있다(열이 밀린 경우).
# 값이 열 이름 그 자체면 값이 아니다.
HEADER_WORDS = {"선취판매수수료", "후취판매수수료", "환매수수료", "전환수수료",
                "가입자격", "구분", "수수료율", "매입시", "환매시"}


# 코드만 덩그러니 든 칸("A", "C-Pe")과 이름표가 든 칸을 알아보기 위한 모양.
# 코드 표기 자체에 한글이 괄호 없이 그대로 붙는 문서가 있다
# (class_fees.json 실측: "C-퇴직연금", "C-퇴직e", "S-퇴직" - "(퇴직연금)"
# 처럼 괄호로 싸지 않고 코드 뒤에 바로 붙는다. KR5127420034 등 KB 계열
# 실측: 이 코드들이 통째로 못 읽혀 상품 8개에서 클래스가 누락됐었다).
# 한글 덩어리가 코드 맨 끝이 아니라 중간에 낄 때도 있다("C-퇴직e" -
# 뒤에 로마자 "e"가 한 번 더 붙는다).
RE_BARE_CODE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9\-]{0,6}(?:[가-힣]{1,6})?[A-Za-z0-9\-]{0,6}$")
RE_HAS_LABEL = re.compile(r"수수료(선취|미징구|후취)-")
# "종류C-F"처럼 머리말이 붙은 코드 칸. 끝의 붙임표는 코드가 아니라
# 비어 있는 종류형 명칭 칸이 이어 붙은 것이라 코드에서 뺀다
# (extract_class_fees.py의 DETAIL_FEE_CLASS_CODE_JONGRYU_RE와 같은 취지).
RE_JONGRYU_CODE = re.compile(
    r"^종류([A-Za-z](?:[A-Za-z0-9\-]{0,5}[A-Za-z0-9])?)(?![A-Za-z0-9])")
# 클래스 여러 개를 한 줄에 "C1~C5"처럼 묶어 적는 표가 있다(_row_class_code
# 참고). 뒤쪽 코드가 앞쪽과 같은 접두사를 다시 쓰는 표기만 받는다
# ("C1~5"처럼 접두사를 한 번만 쓰는 표기는 이 말뭉치에서 실측되지
# 않았다 - 나오면 그때 넓힌다).
RE_CLASS_BUNDLE = re.compile(r"([A-Za-z\-]+)(\d+)~\1(\d+)")
# 코드가 괄호에만 덩그러니 든 칸("(A)")도 있다(KR5169950018 실측: 이름표
# ("수수료선취-오프라인" 등)는 줄바꿈 때문에 별도의 pdfplumber 행으로
# 떨어져 나가고, 정작 이 값 행에는 코드 칸에 "(A)"만 남는다). _parse는
# 이름표+코드가 한 칸에 다 있는 형식만 읽으므로 이런 칸은 못 찾는다.
RE_PAREN_ONLY_CODE = re.compile(r"^\(([A-Za-z][A-Za-z0-9\-]{0,12})\)$")


def _expand_class_range(code, known_codes):
    """"C1~C5" 같은 묶음 코드를 개별 클래스 코드로 펼친다.

    class_fees에 이미 있는 개별 코드로 전부 확인될 때만 펼친다 -
    묶음 안에 실제로 몇 클래스가 있는지 표기만으로 추측하면 위험하다
    (예: C1~C5인데 실제로는 C1,C2,C3만 있고 C4,C5는 폐지됐을 수 있다).
    확인이 안 되면 원래 코드를 그대로 하나만 돌려준다 - 그러면 뒤쪽
    known_codes 대조 단계에서 "모르는 코드"로 걸러지므로, 틀린 값을
    엉뚱한 클래스에 붙이는 것보다 안전하다."""
    m = RE_CLASS_BUNDLE.fullmatch(code)
    if not m:
        return [code]
    prefix, start, end = m.group(1), int(m.group(2)), int(m.group(3))
    if start > end or end - start > 20:
        return [code]
    expanded = [f"{prefix}{n}" for n in range(start, end + 1)]
    if known_codes and not all(c in known_codes for c in expanded):
        return [code]
    return expanded


def _is_code_cell(text, code):
    """이동값(table_shift) 보정이 그 줄 자신의 코드 칸을 값으로 오인하지
    않게 막는 검사. 코드 칸이 "C"처럼 맨몸일 때도, "(C)"처럼 괄호로
    싸여 있을 때도(_row_class_code의 RE_PAREN_ONLY_CODE 갈래 참고 -
    KR5169950018 실측: 괄호를 벗겨 코드를 "C"로 돌려주고 나면, 이동값
    채점이 원래 칸의 "(C)"와 "C"를 다른 문자열로 보고 자기 코드 칸을
    그대로 값으로 세어버려 이동값이 엉뚱하게 잡힌다) 둘 다 자기 코드로
    본다."""
    s = _squash(text)
    return s == code or s == f"({code})"


RE_ONLY_DASHES = re.compile(r"^[-–—−\s]+$")


def _looks_like_fee_cell(x):
    """"왼쪽부터 순서대로" 맞추기(아래 raw)는 헤더에 없는 가입자격 칸이
    끼어 있으면 깨진다(KR5118420036 실측: "가입자격" 이름표 자체가
    없는 서술형 안내문 칸이 코드·이름표 다음, 진짜 수수료 값 앞에 끼어
    있어 순서가 통째로 한 칸씩 밀렸다 - "선취판매수수료징구"가
    front_load_fee 자리로, 진짜 값 "0.2%"가 back_load_fee 자리로 잘못
    들어갔다). 수수료 값은 늘 숫자(요율·금액)를 담거나 "없음"류 표기
    그 자체이고, 가입자격 안내문은 길든 짧든 그 자체로는 숫자가 없다 -
    길이가 아니라 이 모양으로 가른다("제한없음"/"온라인가입자"처럼
    짧아도 숫자가 없는 안내문은 길이만으로는 못 걸러진다).

    다만 숫자만으로는 모자란다 - 가입자격 문장도 금액·법조문 번호로
    숫자를 담을 수 있다(KR5118420036 실측: "최초 납입금액 50억원 이상인
    법인"에 "50", "소득세법 제20조의3..."에 "20", "3"이 있어 숫자
    검사만으로는 못 걸렀다). 수수료 값은 늘 "%"나 "100분의"나 "이내"
    중 하나를 달고 나온다 - 그 표시가 하나도 없이 숫자만 있으면
    가입자격 쪽으로 본다."""
    if _squash(x) in NONE_MARKS:
        return True
    if not re.search(r"\d", x):
        return False
    return any(k in x for k in ("%", "100분의", "이내"))


def _clean(v):
    v = " ".join((v or "").split())
    if not v or _squash(v) in HEADER_WORDS:
        return None
    # 클래스 이름표가 수수료 칸으로 새어 드는 표가 있다(칸이 밀린 경우).
    # "선취판매수수료: 수수료선취-오프라인" 같은 말이 안 되는 답이 나갔다.
    if RE_HAS_LABEL.search(_squash(v)):
        return None
    # 한 칸에 "-"가 줄바꿈으로 겹쳐 찍혀 "- -"가 되는 표가 있다
    # (KR5144450095 실측 - PDF 렌더링 중복으로 보인다). 대시로만 된
    # 칸은 개수·간격에 관계없이 다 "없음"으로 본다.
    if RE_ONLY_DASHES.match(v):
        return "없음"
    # "납입금액"이 "납임금액"으로 나오는 문서가 있다(KR5111420047/
    # KR5111450067 실측 - 같은 페이지에 정상 표기 "납입금액"도 같이
    # 나와서 원문 자체의 글꼴/렌더링 오류로 보인다. 다른 뜻의 낱말이
    # 아니라 같은 낱말의 오식이라 값을 바꾸지 않고 표기만 바로잡는다).
    v = v.replace("납임금액", "납입금액")
    return "없음" if _squash(v) in NONE_MARKS else v


RE_BARE_PCT_NUM = re.compile(r"^[\d]+(?:\.[\d]+)?$")
RE_TRUNCATED_PAYMENT = re.compile(r"^(납입금액|납입액)(의)?$")
RE_PCT_NUM = re.compile(r"[\d]+(?:\.[\d]+)?\s*%")
RE_BUNBUI_NUM = re.compile(r"100분\s*의\s*[\d]+(?:\.[\d]+)?")


def _elig_looks_cut(elig):
    """가입자격 문장이 중간에서 잘렸는지 본다. 150자 문턱을 넘겨
    누적을 멈추는 안전장치(_flush 주석 참고)가, 실제로는 "이 클래스
    몫이 시작하기 전에" 남의 몫이 먼저 섞여 들어오는 문서에서는
    거꾸로 작동한다(KR5139420015 S-p 실측: 원본은 "소득세법
    제20조의3...가입할 수 있으며 다른 종류 수익증권(가입자격(기관 및
    고액거래자 등)에 제한이 있는 종류 수익증권 제외)보다..."인데,
    앞쪽이 잘려 나가고 "수 있으며 다른 종류 수익증권(가입자격(기관"
    이라는, 문장 중간에서 시작해 괄호가 안 닫힌 조각만 남았다). 괄호가
    안 맞으면(원본 법령 문구는 항상 괄호를 닫는다) 문장이 잘렸다는
    확실한 신호이므로, 틀린 조각을 내느니 아예 버린다.
    """
    return elig.count("(") != elig.count(")")


def _clean_front_load_fee(v):
    """선취판매수수료 값을 최종적으로 한 번 더 본다. 이 칸은 값이
    클래스마다 다른 게 정상이라(위 CARRY_FIELDS 주석 참고) 다른 클래스
    값을 이어받는 걸로는 못 고치는 세 가지 결함이 실측됐다.

    1. 숫자에 "%"가 안 붙고 홀로 찍힘(KR5160420009 실측: "0.10"/
       "0.05" - 이 문서만 % 글자가 따로 떨어져 나가 못 붙었다). "%"를
       붙여 준다.
    2. "납입금액의"에서 그대로 끊김(KR514X450008/KR5131420007 실측:
       뒤에 와야 할 요율 숫자가 통째로 없다). 숫자를 지어낼 수 없으니
       모른다고 남긴다.
    3. 가입자격 문장이나 다른 클래스 글자가 섞여 뒤죽박죽 길어짐
       (KR5120451001 실측: A1은 자기 가입자격 "제한없음"이, Ae는 바로
       위 클래스의 긴 가입자격 문장까지 끼어들었다). "납입금액"|
       "납입액"과 %(또는 100분의 N) 표기가 둘 다 있으면 그 사이·뒤에
       낀 군더더기를 걷어내고 "납입금액의 N%[이내]" 꼴로 되살린다.
       실마리가 없으면 문장을 그대로 내보내지 않고 버린다 - 틀린
       문장보다 모른다는 쪽이 낫다."""
    if v is None:
        return v
    s = v.strip()
    squashed = _squash(s)
    if not s or squashed in NONE_MARKS:
        return v
    if RE_BARE_PCT_NUM.match(squashed):
        return f"{squashed}%"
    if RE_TRUNCATED_PAYMENT.match(squashed):
        return None
    if len(s) <= 20:
        return v
    base = "납입금액" if "납입금액" in s else ("납입액" if "납입액" in s else None)
    if not base:
        return v
    m = RE_PCT_NUM.search(s)
    if m:
        tail = " 이내" if "이내" in squashed else ""
        return f"{base}의 {m.group(0).replace(' ', '')}{tail}"
    m2 = RE_BUNBUI_NUM.search(s)
    if m2:
        tail = " 이내" if "이내" in squashed else ""
        frac = re.sub(r"\s+", "", m2.group(0))
        return f"{base}의 {frac}{tail}"
    return None


# 헤더 다음 줄이 진짜 데이터 줄(헤더의 이어지는 줄이 아니라)인지 보는
# 신호. 헤더 칸은 이름표뿐이라 퍼센트·"납입금액"·"-" 같은 값이 안 나온다.
RE_FEE_VALUE = re.compile(r"\d+(?:\.\d+)?\s*%|납입금액|환매금액|이익금")


def _looks_like_data_row(row):
    for c in row:
        cs = (c or "").strip()
        if not cs:
            continue
        if _squash(cs) in NONE_MARKS or RE_FEE_VALUE.search(cs):
            return True
    return False


def _header_map(rows):
    """열 번호 -> 우리가 쓸 이름. 못 찾으면 빈 dict.

    헤더 줄은 "환매"가 칸 하나로 짧게 들어 있는 줄로 찾는다. 처음엔
    '가입자격'이나 '선취' 같은 말이 있는 줄을 다 헤더로 봤는데, 표 첫
    줄의 본문 문장("...가입자격에 따라 수수료가 다릅니다")이 걸려서
    엉뚱한 열이 가입자격으로 잡혔다. 헤더 칸은 문장이 아니라 이름표라
    짧다는 점을 쓴다."""
    ncols = max((len(r) for r in rows), default=0)
    if not ncols:
        return {}, 0

    # 표 위쪽에 설명 문단이 여러 줄 붙는 문서가 많아서 앞부분만 보면
    # 헤더를 놓친다. 전체를 훑는다. "환매수수료"가 아니라 "환매"까지만
    # 요구한다 - "환매"와 "수수료"가 서로 다른 줄에 떨어진 문서가 있어서
    # (아래 참고) "환매수수료"를 통째로 요구하면 그런 문서는 애초에
    # 앵커조차 못 잡는다.
    #
    # 다만 "환매"로 시작하는 짧은 칸이 진짜 머리글 밖에도 있다 - "부과
    # 기준" 각주 줄이 "매입시/환매시/환매시/전환시"처럼 부과 시점을
    # 적기도 한다(KR515302022M 실측: 이 줄이 앵커로 잘못 잡혀 세로형
    # 이어붙이기 표를 통째로 못 읽었다). "환매시"도 "환매"로 시작해서
    # 걸리지만 "수수료" 자리가 아니라 "시점" 자리다. 한 줄에 선취/후취/
    # 환매/전환 중 적어도 둘이 있고("...시"로 끝나는 시점 칸은 안 친다)
    # 진짜 머리글로 본다 - 부과기준 각주 줄은 "환매"가 둘 걸려도(환매시
    # 두 칸) 전부 "시"로 끝나 하나도 안 세어진다.
    anchor = None
    for i, row in enumerate(rows):
        cats = set()
        for cell in row:
            s = _squash(cell or "")
            if not s or len(s) > MAX_HEADER_CELL or s.endswith("시"):
                continue
            for prefix in ("선취", "후취", "환매", "전환"):
                if s.startswith(prefix):
                    cats.add(prefix)
        if len(cats) >= 2:
            anchor = i
            break
    if anchor is None:
        return {}, 0

    # 헤더가 두 줄(구분/가입자격/수수료율 밑에 선취판매수수료/후취판매
    # 수수료/환매수수료/전환수수료)인 문서도 있고, "선취판매/후취판매/
    # 환매/전환"과 그 아래 "수수료"(네 칸에 똑같이 반복)가 서로 다른
    # 줄로 갈라진 세 줄짜리 헤더인 문서도 있다(KR5123420015/
    # KR5147430065 실측: 앵커 줄엔 "환매"만 있고 "수수료"가 한 줄
    # 아래에 있다 - 옛 코드는 "환매수수료"가 한 칸에 다 있다고 가정해서
    # 이런 문서를 통째로 못 찾았다. 32개 상품이 이 모양이었다). 앞뒤로
    # 몇 줄 더 살펴보되, 진짜 데이터 줄(퍼센트·"납입금액"·"-" 같은 값이
    # 있는 줄)이 나오면 거기서 멈춘다 - 안 그러면 진짜 값이 헤더 글자와
    # 섞여 열 이름 매칭이 깨진다.
    start = max(0, anchor - 2)
    end = anchor
    for i in range(anchor, min(anchor + 3, len(rows))):
        if i > anchor and _looks_like_data_row(rows[i]):
            break
        end = i

    joined = [""] * ncols
    for row in rows[start: end + 1]:
        for j, cell in enumerate(row[:ncols]):
            s = _squash(cell or "")
            if len(s) <= MAX_HEADER_CELL:
                joined[j] += s

    mapping = {}
    used = set()
    for name, keys in COLUMNS:
        for j, h in enumerate(joined):
            if j in used or not h:
                continue
            if any(k in h for k in keys):
                mapping[j] = name
                used.add(j)
                break
    return mapping, end + 1


def _row_class_code(row, allow_bare_paren=False):
    """행 앞부분에서 클래스 코드를 읽는다.

    표 모양이 두 갈래다. 첫 칸에 이름표가 통째로 든 것,

        수수료미징구-오프라인-퇴직연금(C) | 90일미만 이익금의 30% | ...

    그리고 코드와 이름표가 두 칸으로 나뉜 것.

        A | 수수료선취-오프라인 | 납입금액의 0.10% 이내 | 없음 | 없음

    첫 칸만 보면 뒤엣것을 통째로 놓친다(상품 42개가 이 모양이었다)."""
    # 앞에서 3칸이 아니라 "값이 있는 앞 3칸"을 본다. 빈 칸이 사이에 끼는
    # 표가 많아서(['A','','','수수료선취-오프라인',...]) 앞 3칸만 보면
    # 라벨 칸이 범위 밖으로 밀린다.
    cells = [c for c in (row or []) if (c or "").strip()][:3]
    if not cells:
        return None
    # "C1~C5"처럼 클래스 여러 개를 한 줄에 묶어 적는 표가 있다
    # (KR5172450019 실측: "수수료미징구-오프라인-보수체감(C1~C5)" 한
    # 줄이 C1,C2,C3,C4,C5 다섯 클래스 전부를 가리킨다). "~"가 낀
    # 코드는 _parse가 못 읽으므로(코드 문자 집합에 "~"가 없음) 먼저
    # 따로 본다 - 묶음 표기 그대로 돌려주고, 실제 펼치는 일은 호출부
    # (_expand_class_range)가 known_codes와 대조하며 안전하게 한다.
    bundle = RE_CLASS_BUNDLE.search(_squash(cells[0]))
    if bundle:
        return bundle.group(0)
    found = _parse(cells[0])
    if len(found) == 1:
        return next(iter(found))
    if allow_bare_paren:
        # "(A)"처럼 코드가 괄호에만 덩그러니 든 칸은 _parse_table을 쓰는
        # 가로형 표에서만 받는다 - _parse_tall_table/_parse_transposed_table
        # 같은 다른 표 모양에서는 이름표가 줄바꿈으로 흩어지며 "(C-G)"
        # 같은 코드 조각만 남은 무관한 줄이 섞여 있어서(KR5160420009
        # 실측: 이 조각이 진짜 클래스 행으로 오인되면서 가입자격이
        # "(C-G)" 문자열 그대로 들어가고 그 뒤 뒤집힌 표 파서로 넘어가지도
        # 못해 상품 전체가 깨졌다), 이 갈래를 켜면 위험하다.
        m = RE_PAREN_ONLY_CODE.match(_squash(cells[0]))
        if m:
            return m.group(1)
        # 이름표가 앞, 괄호 코드가 뒤로 순서가 바뀐 칸도 있다
        # (KR5144420081 31쪽 실측: "수수료미징구-오프라인 | (C) | - | -
        # | - | -"). 코드가 이름표 뒤에 별도 칸으로 오는 경우다.
        if len(cells) > 1 and RE_HAS_LABEL.search(_squash(cells[0])):
            m = RE_PAREN_ONLY_CODE.match(_squash(cells[1]))
            if m:
                return m.group(1)
    # 코드 칸과 이름표 칸이 나뉜 경우
    head = _squash(cells[0])
    if len(cells) > 1 and RE_BARE_CODE.match(head) and RE_HAS_LABEL.search(
            _squash(cells[1])):
        return head
    # 이름표가 아예 없는 클래스. 문서가 종류형 명칭을 "-"(없음)로 적어
    # 두면 코드 칸이 "종류C-F ⏎ -"가 된다(KR5153420063 23쪽). 위 두
    # 갈래는 다 이름표를 요구해서 이런 행을 통째로 버렸는데, 하필 이
    # 문서에서 이름표가 없는 클래스가 랩·금전신탁 전용(C-F)과 기관·
    # 전문투자자 전용(I)이라 가입자격을 꼭 읽어야 하는 행이다. 이 함수는
    # 이미 가입자격 표로 확인된 표 안에서만 불리므로 코드만 봐도 된다.
    m = RE_JONGRYU_CODE.match(head)
    if m:
        return m.group(1)
    # 코드 칸 옆이 "수수료선취-..." 식 공식 명칭이 아니라 그냥 가입자격을
    # 풀어 쓴 문장인 표도 있다(KR5147430065 실측: "A | 선취판매수수료가
    # 징구되는 집합투자증권 | ..." - 옆 칸이 서술형이라 RE_HAS_LABEL이
    # 안 걸려 위 갈래에서 못 찾는다). 글자를 하나라도 포함한 짧은 코드면
    # 그대로 받아준다 - 순수 숫자만은 뺀다(각주 번호 등과 헷갈릴 수
    # 있다). 이미 검증된 수수료·가입자격 표 안에서만 불리는 함수라
    # 안전하다.
    if RE_BARE_CODE.match(head) and re.search(r"[A-Za-z]", head):
        return head
    return None


def _table_header(rows):
    """이 표가 "가. 투자자에게 직접 부과되는 수수료" 표인지 보고, 맞으면
    (열매핑, 머리글 끝)을 돌려준다."""
    mapping, header_end = _header_map(rows)
    if not mapping or "redemption_fee" not in mapping.values():
        return None, 0
    # 진짜 수수료 표는 열이 여럿이다. 환매수수료 하나만 걸렸다면 연혁표
    # ("2015.11.02 | 환매수수료 삭제")를 오인한 것이다.
    if len(mapping) < 2 or set(mapping) == {0}:
        return None, 0
    return mapping, header_end


# 클래스마다 표를 가로 한 줄이 아니라 세로로 네 줄(선취/후취/전환/
# 환매판매수수료 각각 한 줄씩)에 걸쳐 싣는 문서도 있다(KR5185450009
# 실측: 머리글이 "클래스 | 가입자격 | 구분 | 부과비율(또는 부과금액) |
# 부과시기"이고, "구분" 칸에 수수료 종류 이름이, "부과비율" 칸에 그
# 값이 들어간다 - 클래스 이름·가입자격은 그 클래스의 첫 줄에만 있고
# 나머지 세 줄은 비어 있다). COLUMNS 기반 가로형 매핑과 표 모양
# 자체가 달라 같은 방식으로는 못 읽는다 - 열이 밀린 걸로 오판해서
# 선취판매수수료 값이 가입자격 칸에 들어가는 등 완전히 엉뚱하게
# 읽혔다.
TALL_KIND_TO_FIELD = {
    "선취판매수수료": "front_load_fee",
    "후취판매수수료": "back_load_fee",
    "환매수수료": "redemption_fee",
    "전환수수료": "switch_fee",
}


def _parse_tall_table(rows, known_codes=()):
    """세로형 표를 읽는다. 열 번호가 아니라 "구분" 칸의 내용(선취판매
    수수료/후취판매수수료/환매수수료/전환수수료 중 하나와 정확히
    같음)으로 그 줄이 수수료 줄인지 스스로 밝혀지므로, 머리글 유무와
    무관하게 쓸 수 있다 - 표가 다음 쪽으로 이어지면서 머리글이 반복되지
    않는 경우도(KR5185450009 27쪽 실측) 이 방식으로 그대로 읽힌다.

    클래스 구분 없이 펀드 전체가 클래스 하나뿐인 모자형 펀드는 "구분"
    칸이 줄 맨 앞이라 그 앞에 이름표 칸 자체가 없다(KR5123365001
    실측: "선취판매수수료 | - | 매입시"처럼 구분 칸부터 시작 - class_fees
    에도 클래스 코드가 "투자신탁" 하나뿐이다). known_codes가 정확히
    하나뿐인 상품에서만, 이름표 없는 구분 줄을 그 하나뿐인 코드로
    본다 - 클래스가 둘 이상인 상품에서 이러면 어느 클래스 것인지 몰라
    위험하므로 그때는 그대로 건너뛴다(기존 동작 유지)."""
    out = {}
    cur_rec = None
    seen_kinds = set()
    single_code = next(iter(known_codes)) if len(known_codes) == 1 else None
    for row in rows:
        if not row:
            continue
        kind_col = next((j for j, c in enumerate(row)
                          if _squash(c or "") in TALL_KIND_TO_FIELD), None)
        pre_cells = row[:kind_col] if kind_col is not None else row
        if any((c or "").strip() for c in pre_cells):
            code = _row_class_code(row)
            if code:
                cur_rec = out.setdefault(code, {})
                # 가입자격은 이름표 칸(공식 명칭, "수수료선취-..." 꼴이라
                # RE_HAS_LABEL에 걸린다) 앞뒤의 서술형 문장이다.
                for c in pre_cells:
                    cs = (c or "").strip()
                    if not cs or RE_HAS_LABEL.search(_squash(cs)):
                        continue
                    elig = _clean(cs)
                    # 이 칸이 가입자격 문장이 아니라 각주 참조("주2)
                    # 참조")나 옆 칸의 수수료율("납입금액의 0.4%이내
                    # 주1)")이 잘못 흘러든 경우가 있다(KR5123420015/49,
                    # KR5123490013/16/17, KR5157420003, KR5185450009
                    # 실측 - 전부 괄호가 안 맞거나 수수료 모양이라
                    # _elig_looks_cut/_looks_like_fee_cell로 걸러진다).
                    if (elig and not cur_rec.get("eligibility")
                            and not _elig_looks_cut(elig)
                            and not _looks_like_fee_cell(elig)):
                        cur_rec["eligibility"] = elig
                    break
            elif kind_col is None:
                # 이름표도 구분 칸도 없는 줄 - 이 표와 무관한 문단이다.
                cur_rec = None
        elif kind_col is not None and cur_rec is None and single_code:
            cur_rec = out.setdefault(single_code, {})
        if kind_col is None or cur_rec is None:
            continue
        seen_kinds.add(_squash(row[kind_col]))
        field = TALL_KIND_TO_FIELD[_squash(row[kind_col])]
        # 구분 칸 바로 다음 칸이 늘 값 칸은 아니다 - 표 테두리 선과
        # 실제 글자 칸 경계가 안 맞아 그 사이에 빈 칸(병합된 셀의
        # 나머지 조각)이 여러 개 끼는 문서가 있다(키움자산운용
        # KR5123365001 실측: "['선취판매수수료','','','','','-',...]"
        # - kind_col+1은 빈 문자열이고 진짜 값 "-"는 5칸 뒤에야 나온다).
        # 고정 오프셋(kind_col+1) 대신, 구분 칸 다음부터 처음 나오는
        # 빈칸 아닌 칸을 값으로 본다 - "부과시기"(매입시/환매시 등)는
        # 그 값 칸보다 항상 뒤에 있어 먼저 걸릴 위험이 없다.
        val = next((c for c in row[kind_col + 1:] if (c or "").strip()), None)
        cv = _clean(val)
        if cv and not cur_rec.get(field):
            cur_rec[field] = cv
    # "구분" 칸 신호가 어쩌다 한 번만(예: 각주 문장에 우연히 낱말이
    # 그대로 박힌 경우) 걸린 건 이 표 모양이라는 근거가 못 된다 - 진짜
    # 세로형 표라면 네 가지 구분이 최소 두 가지는 나온다.
    if len(seen_kinds) < 2:
        return {}
    return out


def _transposed_data_rows(rows, class_order, out, seen_kinds_total):
    """class_order(왼쪽부터 클래스 순서)를 이미 아는 데이터 구간을
    읽어 out에 채운다. 칸 번호가 머리글 줄과 값 줄 사이에서 클래스마다
    다르게(어떤 칸은 +1, 어떤 칸은 +0) 밀리는 문서가 실측됐다 - 정확한
    칸 번호 대신 "왼쪽부터 순서대로" 클래스 개수와 값 개수를 맞춰
    짝짓는다(값이 "해당사항 없음"처럼 한 칸에만 병합돼 있으면 그 값을
    전 클래스에 똑같이 적용한다 - 환매수수료 행이 흔히 이렇다)."""
    for row in rows:
        if not row:
            continue
        kind_col = next((j for j, c in enumerate(row)
                          if _squash(c or "") in TALL_KIND_TO_FIELD), None)
        if kind_col is None:
            continue
        seen_kinds_total.add(_squash(row[kind_col]))
        field = TALL_KIND_TO_FIELD[_squash(row[kind_col])]
        values = [cv for j, c in enumerate(row) if j > kind_col
                  for cv in [_clean(c)] if cv]
        if len(values) == len(class_order):
            for code, v in zip(class_order, values):
                if not out[code].get(field):
                    out[code][field] = v
        elif len(values) == 1:
            for code in class_order:
                if not out[code].get(field):
                    out[code][field] = values[0]


def _parse_transposed_table(rows, carried_class_order=None):
    """가로/세로가 모두 뒤집힌 표 - 클래스가 "종류" 줄에 열로 나열되고,
    수수료 종류(선취/후취/환매/전환)는 그 아래 행으로 내려간다(반대로
    _parse_table이 다루는 표는 클래스가 행, 수수료 종류가 열이다 -
    KR5160420009 실측:
        종류 | | | 수수료미징구-오프라인(C) | | | 수수료미징구-온라인(C-E) | ...
        선취판매수수료 | - | - | ...
        후취판매수수료 | - | - | ...
        환매수수료 | 해당사항 없음
    클래스명 자체도 한 칸에 안 들어가 여러 줄(행)에 걸쳐 나뉜다("수수료
    미징구-" / "오프라인-" / "무권유저비용" / "(C-G)" 넉 줄) - 값 행이
    나오기 전까지의 모든 줄을 칸 번호별로 이어 붙여야 클래스명이
    완성된다. 클래스가 많으면 "종류" 표가 같은 표 안에서 한 번 더
    반복되며 나머지 클래스를 잇는다 - 표 하나에 이런 블록이 여러 개일
    수 있고, 그중 마지막 블록은 자기 데이터 행 없이 헤더만 찍힌 채
    페이지가 끝나기도 한다(KR5160420009 실측: 14쪽 두 번째 "종류" 블록
    (C-P2e/C-P/C-Pe/S/S-P/A)의 헤더까지만 14쪽에 있고 값 행은 다음 표
    (15쪽)의 첫 줄부터 곧바로 시작한다 - 그 표엔 "종류" 줄 자체가 없다).
    이런 미완성 블록은 (out, 다음에 넘겨줄 class_order)로 함께 돌려주고,
    호출부가 다음 표 호출 때 carried_class_order로 넘겨주면 이어 읽는다.

    반환값은 (out, pending_class_order) - pending_class_order는 데이터를
    한 줄도 못 찾은 마지막 블록의 클래스 순서(다음 표로 넘겨줄 것,
    없으면 None)."""
    out = {}
    seen_kinds_total = set()
    pending = None

    if carried_class_order:
        # "종류" 줄 없이 곧바로 데이터로 시작하는 표 - 앞서 넘어온
        # class_order를 그대로 쓴다.
        for code in carried_class_order:
            out.setdefault(code, {})
        _transposed_data_rows(rows, carried_class_order, out, seen_kinds_total)

    header_positions = [i for i, row in enumerate(rows)
                         if row and _squash(row[0] or "") == "종류"]
    for bi, header_idx in enumerate(header_positions):
        block_end = (header_positions[bi + 1] if bi + 1 < len(header_positions)
                     else len(rows))
        col_texts = {}
        data_start = None
        for i in range(header_idx, block_end):
            row = rows[i]
            kind_col = next((j for j, c in enumerate(row)
                              if _squash(c or "") in TALL_KIND_TO_FIELD), None)
            if kind_col is not None and i > header_idx:
                data_start = i
                break
            for j, c in enumerate(row):
                cs = (c or "").strip()
                if cs and j > 0:
                    col_texts[j] = (col_texts.get(j, "") + " " + cs).strip()

        col_to_code = {}
        for j, text in col_texts.items():
            code = _row_class_code([text])
            if code:
                col_to_code[j] = code
        if not col_to_code:
            continue
        class_order = [code for _j, code in sorted(col_to_code.items())]

        if data_start is None:
            # 이 블록은 헤더만 있고 데이터가 없다 - 마지막 블록이면
            # 다음 표로 넘겨준다(그 앞의 블록이면 그냥 못 찾은 것).
            if bi == len(header_positions) - 1:
                pending = class_order
            continue

        for code in class_order:
            out.setdefault(code, {})
        _transposed_data_rows(rows[data_start:block_end], class_order, out,
                               seen_kinds_total)

    if len(seen_kinds_total) < 2:
        return {}, None
    return out, pending


def _merge_wrapped_continuation_rows(rows):
    """칸 안 줄바꿈이 별개 물리 행으로 떨어져 나오는 표가 있다
    (KR514X450008 32쪽 실측: "명칭A|가입자격제한없음|...|납입금액의"
    다음 물리 행이 "||...|1.0%이내"로, "납입금액의"와 "1.0%이내"는
    같은 셀 안에서 줄바꿈된 것뿐인데 표 추출기가 별도 행으로 갈랐다 -
    이걸 안 이으면 "납입금액의"에서 끊긴 채로 남아 정작 중요한 요율
    숫자를 통째로 잃는다). 이런 "칸 하나만 채워지고 나머지는 전부 빈"
    행은 새 데이터 행이 아니라 바로 위 행의 이어지는 줄이므로, 그
    칸의 텍스트를 이어붙이고 이 행 자체는 없앤다.

    새 클래스 행과 헷갈리면 안 된다 - 클래스 코드만 있고 가운데
    칸들은 아직 안 채워진 진짜 새 행(예: ['A2','','','',...])도 "칸
    하나만 채워짐"이라 겉모습이 같다. 그래서 채워진 칸이 맨 앞
    (0번째, 이름표·클래스코드 자리)이면 절대 이어붙이지 않는다 -
    이어붙일 대상은 항상 그보다 뒤쪽 칸(수수료율 등 값 칸)이다.
    이어붙일 위 행의 같은 칸이 비어 있으면(이어질 대상 자체가 없다)도
    손대지 않는다.

    1번째 칸(0번째 바로 다음)도 이름표 자리로 쓰는 표가 있다
    (KR5156450026 실측: ['','수수료미징구-오프라인','','-',...] 다음
    행이 ['','-보수체감형(C1)','','',...]로, 이름표가 0번째가 아니라
    1번째 칸에서 줄바꿈된다 - 여기까지 이어 붙이면 표 파서가 이
    클래스를 "찾아낸" 것으로 착각해서, 정작 이 표엔 없는 전환수수료
    칸을 마저 찾으러 가는 좌표 재스캔(_coord_fee_table_page, 훨씬
    꼼꼼하게 훑는 경로)을 건너뛰게 된다 - 그 결과 반쪽짜리(전환수수료
    빠진) 데이터로 조용히 "완결됐다"고 오판해서 오히려 더 나쁜
    결과를 냈다). 그래서 0·1번째 칸은 둘 다 건드리지 않고, 그보다
    뒤쪽(수수료율 등 값 칸)만 이어 붙인다."""
    out = []
    for row in rows:
        filled = [(j, c) for j, c in enumerate(row) if (c or "").strip()]
        if len(filled) == 1 and out:
            j, c = filled[0]
            if j > 1 and j < len(out[-1]) and (out[-1][j] or "").strip():
                out[-1][j] = out[-1][j].rstrip() + c.strip()
                continue
        out.append(list(row))
    return out


def _parse_table(rows, carried=None, carried_cols=None, carried_last_value=None):
    """carried: 앞 페이지에서 확인된 열매핑. 표가 페이지를 넘어가면
    이어지는 쪽엔 머리글이 없어서, 그것만 보고 버리면 뒷장 클래스를
    통째로 잃는다(KR515302022M 실측: 가.표가 31~33쪽에 걸쳐 있는데
    32·33쪽에 머리글이 없어 C-P2/S-P/S-I/C-Pe/AG/CG/A1/C-Pe2가 빠졌다).

    carried_last_value: 앞 페이지에서 마지막으로 확인된 CARRY_FIELDS
    이어받기 값(아래 last_value 참고). 표 전체에 한 번만 찍히는 병합
    칸(예: 후취판매수수료 "없음")이 페이지 경계를 넘어가면, 이어받기는
    이 함수 호출 안에서만 도는 지역 변수라 다음 페이지 호출에서는
    다시 빈 채로 시작해 못 이어받는다(신영자산운용 KR5125450023 실측:
    27쪽 A 클래스 줄에서 "없음"(후취·환매)/"해당사항 없음"(전환)이
    한 번만 찍히고 그 아래 클래스들은 이어받는데, 28쪽으로 넘어가는
    C-P/C-Pe/C-G/C-P2/C-P2e는 물려받은 게 없어 통째로 빈 채 남았다).
    앞 페이지가 반환한 last_value를 그대로 넘겨받아 이어서 쓴다."""
    mapping, header_end = _table_header(rows)
    used_carried = False
    if not mapping:
        if not carried:
            return {}, {}
        mapping, header_end = carried, 0
        used_carried = True

    order = [name for _j, name in sorted(mapping.items())]
    # 가입자격 칸 번호 - 아래 "이 줄에 값이 있었는데 자리를 못 찾았다"
    # 판정(raw)에서 빼야 한다. 가입자격 문구는 "가입제한 없음"처럼
    # "없음"이라는 글자를 포함하는 경우가 흔한데, 그러면
    # _looks_like_fee_cell이 이걸 수수료 칸 모양으로 오인해 "자리를
    # 못 찾은 수수료 값이 있다"고 잘못 판단하고, 이어받기(carry)로
    # 정상적으로 채워져야 할 다른 클래스들의 후취/환매/전환수수료까지
    # 통째로 못 잇게 막아 버린다(우리자산운용 KR5118420062, NH-Amundi
    # KR5172450019 등 실측 - 이 줄들의 raw는 사실 이름표/가입자격
    # 글자일 뿐인데 "없음"이 섞여 수수료 값으로 오인됐다).
    elig_col = next((j for j, name in mapping.items() if name == "eligibility"), None)
    # 표 전체에 한 번만 찍히고 아래 행들은 비워 두는 병합 칸이 있다
    # (KR5118420062 실측: "환매수수료: 없음"이 A 클래스 줄에만 있고
    # A2부터는 그 자리가 통째로 빈칸이다 - 표 전체에 공통으로 적용되는
    # 값이라 매 줄 반복하지 않은 것이다). 뒤 칸이 아예 없어서 순서
    # 맞추기(zip)로도 못 채우는 이름에 한해, 마지막으로 읽은 값을
    # 그대로 이어받는다 - 그 클래스가 실제로 다른 값을 밝히면(zip으로
    # 값을 얻으면) 그쪽이 항상 이긴다.
    #
    # 이어받기는 원칙적으로 redemption_fee/switch_fee에만 한정한다 -
    # 환매·전환 수수료는 상품 전체에 공통으로 적용되는 경우가 흔하지만
    # (위 실측 근거), 선취판매수수료는 애초에 클래스를 나누는 이유 그
    # 자체라 클래스마다 값이 다른 게 정상이다. front_load_fee까지
    # 이어받으면 바로 위 클래스 값이 빈 칸에 잘못 들어간다(KR5172450019
    # 실측: Ae의 선취판매수수료 "0.5% 이내"가 바로 아래 Ce 줄의 빈
    # 칸으로 새어 들어가, Ce도 선취판매수수료가 있는 것처럼 잘못
    # 나왔다 - Ce는 원문에 실제로 그 값이 없다).
    #
    # back_load_fee(후취판매수수료)는 사정이 다르다 - pdfplumber 원문
    # 셀 좌표를 직접 확인해 보면(하나자산운용 KR5111420047 34쪽 실측),
    # "없음" 칸 자체가 A 클래스 줄부터 표 맨 끝 줄까지 세로로 하나의
    # 칸으로 실제 병합되어 있다(그 칸의 bbox가 여러 줄 높이를 그대로
    # 덮는다) - 즉 이 칸은 애초에 "클래스마다 다른 값"이 아니라 "표
    # 전체에 적용되는 값 하나"로 PDF 자체가 그렸다. 이걸 안 이으면
    # A2/A-E/C-E/... 11개 클래스가 후취판매수수료를 통째로 못 찾는다.
    CARRY_FIELDS = {"redemption_fee", "switch_fee", "back_load_fee"}
    # 헤더 칸 번호와 데이터 칸 번호가 표 전체에 걸쳐 똑같이 어긋나기도
    # 한다(KR5172450019 25쪽 실측: 헤더는 선취/후취/환매가 2/5/8열인데
    # 데이터 값은 항상 그보다 한 칸 왼쪽인 1/4/7열에 있다). 이런 표에서
    # "왼쪽부터 있는 값만 순서대로" 맞추면(아래 raw 방식), 어떤 클래스는
    # 앞쪽 열(front)이 비어 있고 뒤쪽 열(back)에만 값이 있어도 그 값이
    # 무조건 order의 첫 이름(front)에 배정돼 틀린 칸에 들어간다(같은
    # 문서 S클래스 실측: 후취판매수수료 값이 선취판매수수료로 잘못
    # 나왔다). 표 전체에서 가장 값을 많이 맞히는 고정 칸 이동값을 미리
    # 재 두면, 어느 칸이 비어 있어도 실제 열 위치를 그대로 지킨다.
    # 이동값을 재는 동안, 그 줄 자신의 코드 칸(맨 앞)까지 우연히 "값"
    # 처럼 읽혀서 이동값이 코드 칸까지 거슬러 올라가면 안 된다(KR5116501001
    # 실측: "C-P" 코드 칸 자체가 선취판매수수료 값으로 잘못 읽혀
    # front_load_fee="C-P"가 됐다 - 코드 칸엔 "-"나 %같은 진짜 수수료
    # 모양이 없는데도 _clean이 그냥 문자열이라 통과시켜서 생긴 문제다).
    # 그 줄의 코드 문자열과 정확히 같은 칸은 값 후보에서 뺀다.
    def _shift_val(row, jj, name, code):
        # 이동값이 다른 필드의 원래 헤더 칸과 우연히 겹치면, 그건 이
        # 필드 값이 아니라 그 다른 필드가 이미 (직접매핑으로) 갖고 있는
        # 제 몫의 값을 훔쳐오는 것이다(KR5169950018 실측: 가입자격은
        # 헤더 칸 그대로 항상 맞는데, 선취판매수수료 칸에 이동값 -2를
        # 적용하면 하필 가입자격의 헤더 칸(5-2=3)과 겹쳐서 가입자격
        # 문장이 그대로 선취판매수수료로 잘못 채점/적용됐다). 그 칸이
        # 다른 필드의 제 칸이면(이름이 다르면) 후보에서 뺀다.
        if not (0 <= jj < len(row)):
            return None
        # 이 "다른 필드의 제 칸" 방어는 매핑 칸 번호를 믿을 수 있을
        # 때만 뜻이 있다 - 물려받은 매핑(used_carried)은 애초에 이
        # 표가 아니라 앞 페이지 것이라, 매핑에 적힌 칸 번호 자체가
        # 이 표에서는 전부 다른(밀리기 전) 자리를 가리킨다. 그런데도
        # 이 방어를 그대로 적용하면, 표 전체가 진짜로 한 칸씩 밀린
        # 경우(KR5153420318 31쪽 실측) 이동값이 정확해도 "그 칸은
        # 매핑상 다른 필드 것"이라며 매번 걸러내 진짜 이동값이 아예
        # 점수를 못 받는다 - 결국 우연히 자리채움 값("-")이 많이
        # 겹치는 이동값 0이 이겨버린다. 물려받은 매핑에서는 이 방어를
        # 끈다.
        if not used_carried and jj in mapping and mapping[jj] != name:
            return None
        if _is_code_cell(row[jj], code):
            return None
        cv = _clean(row[jj])
        # 이동값으로 훔쳐오는 값도 수수료 칸이면 수수료값 모양을
        # 갖춰야 한다(가입자격 칸은 원래 서술형이라 이 검사에서 뺀다) -
        # 안 그러면 위 라벨-겹침 방어를 피해간 다른 자리의 서술형
        # 문장도 그대로 넘어온다(KR5118420036/C-P1e 실측: 이동값이
        # "소득세법 제20조의3..." 문장 칸을 짚어 front_load_fee로
        # 들어갔다 - 다른 필드의 제 칸은 아니었지만 여전히 수수료
        # 모양이 아니었다).
        if name != "eligibility" and cv and not _looks_like_fee_cell(cv):
            return None
        return cv

    def _score_row(row, code, s):
        n = 0
        for j, name in mapping.items():
            jj = j + s
            if _shift_val(row, jj, name, code):
                n += 1
        return n

    table_shift = None
    best_score = 1  # 최소 두 칸 이상 맞아야 우연이 아니라고 본다
    # 물려받은 매핑을 쓰는데 이 표의 칸 수가 원래 표(carried_cols)와
    # 다르면, 이동값 0(안 밀림)은 애초에 성립할 수 없다 - 칸 수 자체가
    # 다른데 필드 위치가 그대로일 리 없다. 그런데도 채점만으로는 0이
    # 이길 수 있다("-"류 자리채움 값은 어느 칸에 있어도 모양 검사를
    # 통과해, 진짜 값 하나(예: "3년미만...")가 진짜 이동값에서 맞히는
    # 점수를 0이 우연한 자리채움 일치로 따라잡거나 앞서기도 한다 -
    # KR5153420318 31쪽 실측: 진짜 이동값 +1은 2점인데, 밀리지 않은
    # 0이 우연히 3점을 냈다). 칸 수가 다르다는 구조적 근거가 이미 있으니
    # 0은 아예 후보에서 뺀다.
    carried_mismatch = (
        used_carried and carried_cols is not None
        and max((len(r) for r in rows), default=0) != carried_cols)
    exclude_zero_shift = carried_mismatch
    for row in rows[header_end:]:
        if not row:
            continue
        row_code = _row_class_code(row, allow_bare_paren=True)
        if not row_code:
            # 코드가 아예 없는 줄(값 행이 코드 행보다 먼저 나오는 표의
            # 그 값 행 - KR5127420034 실측)은 이동값을 잴 기준이 못 된다.
            # 그 행의 값이 실제로 어느 칸에 있는 게 맞는지(원래 코드가
            # 있는 행 기준으로 어떤 이동값이어야 하는지)조차 모르는
            # 상태라, 우연히 다른 필드 칸에 값이 걸리면 이동값이 통째로
            # 엉뚱한 곳으로 튄다. 코드가 확인된 행만으로 이동값을 잰다.
            continue
        for s in range(-4, 5):
            if s == 0 and exclude_zero_shift:
                continue
            score = _score_row(row, row_code, s)
            # 점수가 같으면 0에 더 가까운(절댓값이 작은) 이동값을 쓴다
            # (KR5122420005 실측: 진짜 이동값은 -1인데, 우연히 -4도 같은
            # 점수가 나온다 - 선취판매수수료의 진짜 값이 하필 이동값
            # -4에서는 후취판매수수료 칸으로, 환매수수료의 "없음"이
            # 전환수수료 칸으로 동시에 잘못 걸려 우연히 점수가 같아진다.
            # 표 밀림은 대개 한두 칸 정도라 절댓값이 작은 쪽이 진짜일
            # 가능성이 훨씬 높다 - range가 -4부터 돌아 더 큰 이동값을
            # 먼저 만나면 그게 먼저 자리를 차지해 버렸었다).
            if score > best_score or (
                    score == best_score and table_shift is not None
                    and abs(s) < abs(table_shift)):
                best_score, table_shift = score, s
    # 어떤 칸은 표 전체에서 늘 어긋나 있지만(위 KR5122420005), 어떤
    # 칸은 대부분 줄에서 헤더 그대로 맞고 특정 줄 하나만 진짜로 빈칸인
    # 경우도 있다(KR5194450018 18쪽 실측: RP/RP-e 등 다른 클래스는
    # 환매수수료가 헤더 칸 그대로 "없음"으로 잘 잡히는데 S 클래스만
    # 그 칸이 진짜로 비어 있다 - 그런데도 이동값을 적용하면 바로 옆
    # 후취판매수수료 칸 값을 환매수수료로 잘못 끌어온다). 필드별로
    # "이 표 안 어디서든 헤더 칸 그대로 값을 찾은 적이 있는지"를 먼저
    # 봐서, 있으면 그 필드는 이동값 보정 대상에서 뺀다 - 정말로 표
    # 전체가 어긋난 필드에만 이동값을 쓴다.
    fields_ever_direct = set()
    for row in rows[header_end:]:
        if not row:
            continue
        for j, name in mapping.items():
            if j < len(row) and _clean(row[j]):
                fields_ever_direct.add(name)

    last_value = dict(carried_last_value) if carried_last_value else {}
    out = {}
    body_rows = rows[header_end:]
    for ridx, row in enumerate(body_rows):
        if not row:
            continue
        code = _row_class_code(row, allow_bare_paren=True)
        if not code or code in out:
            continue

        fee_evidence = []
        rec = {}
        for j, name in mapping.items():
            if j < len(row):
                v = _clean(row[j])
                # 물려받은 매핑(carried)은 앞 페이지 칸 번호를 그대로
                # 쓰는데, 이어지는 쪽 표가 칸 하나가 더(또는 덜) 찍혀
                # 전체가 밀려 있으면 그 칸 번호가 더 이상 맞지 않는다
                # (KR5153420318 31쪽 실측: 30쪽은 6칸인데 31쪽은 맨 앞에
                # 빈 칸이 하나 더 있어 7칸 - 물려받은 "가입자격" 칸
                # 번호가 실제로는 클래스 이름표를 가리키고, "선취판매
                # 수수료" 칸 번호는 실제로는 가입자격 문장을 가리켰다.
                # 이 밀림은 아래 이동값(table_shift) 보정이 바로잡아야
                # 하는데, 직접매핑이 먼저 값을 채워버리면(칸이 밀렸어도
                # 그 칸 자체는 빈칸이 아니라 다른 필드의 값이 들어있어
                # "값 있음"으로 통과한다) 이동값 보정 단계 자체를 안
                # 타서 서술형 문장이 그대로 수수료 값으로 굳어진다).
                # 물려받은 매핑일 때만, 가입자격이 아닌 필드는 수수료
                # 값 모양을 갖췄는지 먼저 확인한다 - 제 칸을 찾은 표는
                # (used_carried가 아닐 때) 이 검사 없이 그대로 믿는다.
                if v and used_carried and name != "eligibility" \
                        and not _looks_like_fee_cell(v):
                    v = None
                # 물려받은 매핑이 가입자격 칸으로 짚은 자리가, 사실은
                # 전혀 다른 표(표1이 "부과기준" 꼬리줄로 끝난 바로 그
                # 자리에 곧장 이어 붙는 "2)집합투자기구에 부과되는 보수
                # 및 비용" 퍼센트 지급비율표)의 숫자 칸인 경우가 있다
                # (키움 KR5123490013 실측: AG의 가입자격 칸에 판매보수
                # 비율 "0.3000"이 잘못 채워졌다 - 표1과 표2가 같은
                # 쪽에서 곧장 이어 붙어서, "바로 다음 표"라는 인접성
                # 조건만으로는 표가 바뀐 걸 못 걸러낸다). 가입자격은
                # 원문이 항상 한글 문장이므로, 물려받은 매핑에서 한글이
                # 전혀 없는 값은 가입자격으로 보지 않는다 - 표1 자신이
                # 이어지는 진짜 가입자격 문장은 항상 한글이라 안전하다.
                if v and used_carried and name == "eligibility" \
                        and not re.search(r"[가-힣]", v):
                    v = None
                if v:
                    rec[name] = v

        # 긴 이름표가 셀 하나를 넘어가며 값·코드·나머지 이름표가 서로
        # 다른 줄로 갈리는 표가 있다(KR5127420034 등 KB 계열 실측: "C-W"
        # 줄은 칸이 전부 비고 그 값("-","-")은 바로 위 줄("수수료미징구
        # -오프라인-랩,금전" 이름표 조각)에 있으며, 코드 뒤로도 이름표
        # 조각("신탁")이 한 줄 더 이어진다 - pdfplumber가 이 표의 셀
        # 높이를 이름표 줄 수에 맞춰 나누면서 값이 코드 줄이 아니라
        # 이름표 첫 줄에 걸린다). 코드 자신의 줄에 값이 하나도 없고
        # 바로 위 줄이 코드 없는(값만 있는) 줄이면 그 위 줄에서 값을
        # 가져온다 - 코드 없는 줄은 애초에 다른 클래스의 몫일 수 없다.
        if not rec and ridx > 0:
            prev = body_rows[ridx - 1]
            if not _row_class_code(prev, allow_bare_paren=True):
                for j, name in mapping.items():
                    if j < len(prev):
                        v = _clean(prev[j])
                        if (v and used_carried and name == "eligibility"
                                and not re.search(r"[가-힣]", v)):
                            continue
                        if v and (name == "eligibility" or _looks_like_fee_cell(v)):
                            rec[name] = v

        rec_corrected = False
        if carried_mismatch:
            # 물려받은 매핑을 쓰는데 칸 수 자체가 원래 표와 다르면, 그건
            # 한 칸씩 고르게 밀린 게 아니라 사이사이 장식용 빈 칸이
            # 통째로 빠진(또는 늘어난) 경우일 수도 있다 - 그러면 이동값
            # (하나의 고정 칸수)으로는 안 맞는다(KR5120450015 43쪽 실측:
            # 42쪽 머리글은 10칸(장식용 빈 칸 여럿 포함)인데 43쪽 이어지는
            # 줄은 그 장식 칸이 다 빠진 6칸짜리로, 선취는 3칸, 후취는
            # 4칸을 밀어야 맞는 등 필드마다 밀린 양이 다르다). 이럴 땐
            # "왼쪽부터 순서대로"(order와 값 개수를 맞추는 방식)가
            # 이동값보다 먼저다 - 개수가 정확히 맞으면 그게 더 믿을
            # 만하고, 직접매핑이 우연히 채운 값(아래 참고, 자리는
            # 틀렸는데 모양만 맞는 경우)보다 앞세운다.
            raw = [x for x in row[1:]
                   if (x or "").strip() and not RE_HAS_LABEL.search(_squash(x))
                   and _looks_like_fee_cell(x)]
            if len(raw) == len(order):
                rec = {}
                for idx, name in enumerate(order):
                    cv = _clean(raw[idx])
                    if cv:
                        rec[name] = cv
                rec_corrected = True

        if used_carried and table_shift not in (None, 0) and (
                not carried_mismatch or len(rec) < len(order)):
            # 물려받은 매핑이 표 전체에서 일정하게 밀려 있다고 이미
            # 확인됐다면(table_shift), 칸별로 부분적으로만 맞는 직접
            # 매핑 결과를 그대로 두면 안 된다 - 우연히 값이 있어서(예:
            # "-") 위 모양 검사는 통과하지만 실제로는 다른 필드의 값을
            # 읽은 칸도 있다(KR5153420318 31쪽 실측: 밀린 그대로 읽은
            # 후취판매수수료 칸이 실제로는 환매수수료 자리라 "3년미만
            # 환매시..."가 environment redemption_fee로 잘못 들어갔다 -
            # "없음"류처럼 모양은 맞아도 자리가 틀렸다). 이동값이 이미
            # 확인된 표에서는 처음부터 이동값 기준으로 전 필드를 다시
            # 채운다 - 부분적으로만 맞는 직접매핑보다 일관되게 보정된
            # 값이 더 믿을 만하다. 다만 위 순서맞추기가 이미 전 필드를
            # (칸 개수가 정확히 맞아) 다 채웠으면 그쪽을 그대로 둔다.
            rec = {}
            for j, name in mapping.items():
                jj = j + table_shift
                cv = _shift_val(row, jj, name, code)
                if cv:
                    rec[name] = cv
            rec_corrected = True

        if carried_mismatch and not rec_corrected:
            # 칸 수가 원래 표와 다른데도(carried_mismatch) 순서맞추기·
            # 이동값 둘 다 확신을 못 얻었다면(개수가 안 맞고, 이동값도
            # None), 위에서 직접매핑이 채운 값은 옛 칸 번호를 그대로
            # 믿은 것이라 위치가 맞다는 근거가 없다 - 우연히 모양만
            # 맞는 값을 그대로 내보내면 틀린 자리에 값이 들어간다
            # (KR5118420036 39쪽 실측: 직접매핑이 4번 칸을 그대로 읽어
            # S클래스의 환매수수료 문구 "3년미만..."이 선취판매수수료로
            # 잘못 나왔다 - 표 자체엔 이동값이 명확하지 않았을 뿐 값은
            # 분명 있었는데, 어느 필드인지 모른다면 아예 안 내는 게
            # 틀리게 내는 것보다 낫다). 확신이 없으면 이 줄은 빈 채로
            # 둔다.
            rec = {}

        if not rec:
            # 코드 칸(row[0]) 바로 다음에 클래스 이름표 칸("수수료선취-
            # 오프라인" 등)이 하나 더 오는 표가 있다(KR5118420062 실측).
            # 이 이름표까지 순서에 끼워 넣으면 그 뒤 진짜 값들이 한 칸씩
            # 밀려 선취판매수수료 값이 후취판매수수료 칸으로 들어간다
            # (실측: A 클래스 선취 0.05%가 후취 칸에 저장되고 선취는
            # None이 됐다). 순서를 맞추기 전에 이름표 칸부터 뺀다.
            #
            # 이 "왼쪽부터 순서대로" 맞추기는 값 개수가 필드 개수와
            # 정확히 같을 때만 믿는다 - 개수가 안 맞으면(일부 필드가
            # 진짜로 빈 칸이라 값이 모자란 경우) 어느 값이 어느 필드인지
            # 순서만으로는 알 수 없다(위 참고) - 그럴 땐 표 전체에서
            # 미리 잰 고정 이동값(table_shift)으로 실제 칸 위치를 그대로
            # 찾는다.
            raw = [x for x in row[1:]
                   if (x or "").strip() and not RE_HAS_LABEL.search(_squash(x))
                   and _looks_like_fee_cell(x)]
            # 이 줄에 수수료 모양 값이 하나라도 있었는데(raw) 어느 칸인지
            # 확신을 못 얻어 결국 못 채웠다면, 아래 이어받기(CARRY_FIELDS)로
            # 다른 클래스의 값을 그대로 갖다 붙이면 안 된다 - 이 줄은
            # "값이 원래 없는" 줄이 아니라 "값은 있는데 자리를 모르는"
            # 줄이라, 이어받은 값이 실제로는 이 줄 자신의 값과 다를 수
            # 있다(신영자산운용 KR5125450023 실측: S 클래스 자신의
            # 후취판매수수료 "1,095일 미만 환매 시 환매금액의 0.15%
            # 이내"가 자리를 못 찾고 버려졌는데, 그 뒤 이어받기가 앞쪽
            # 미징구 클래스들의 "없음"을 그대로 채워 넣어 완전히 틀린
            # 값이 나왔었다 - 차라리 안 채우는 게 틀리게 채우는 것보다
            # 낫다는 이 함수의 기존 원칙과 같은 이유다). 가입자격 칸은
            # 뺀다 - "가입제한 없음"처럼 가입자격 문구에도 "없음"이
            # 흔히 섞여 _looks_like_fee_cell을 우연히 통과하는데, 그건
            # 수수료 값이 아니라 그냥 가입자격 문장이다(우리자산운용
            # KR5118420062, NH-Amundi KR5172450019 실측 - 이 오판 때문에
            # 정상적으로 이어받아야 할 환매·전환수수료까지 못 이었다).
            fee_evidence = [x for j, x in enumerate(row)
                            if j != 0 and j != elig_col
                            and (x or "").strip()
                            and not RE_HAS_LABEL.search(_squash(x))
                            and _looks_like_fee_cell(x)]
            if len(raw) == len(order):
                for idx, name in enumerate(order):
                    cv = _clean(raw[idx])
                    if cv:
                        rec[name] = cv
            elif table_shift is not None:
                for j, name in mapping.items():
                    jj = j + table_shift
                    cv = _shift_val(row, jj, name, code)
                    if cv:
                        rec[name] = cv
        elif table_shift is not None:
            # 헤더 칸과 데이터 칸이 표 안에서 필드마다 다르게 어긋나는
            # 표가 있다(KR5122420005 36쪽 실측: 환매수수료·전환수수료는
            # 헤더 칸 그대로인데 선취·후취판매수수료만 한 칸 왼쪽에 있다).
            # 위 "표 전체 이동값"은 rec가 통째로 비었을 때만 썼는데, 이런
            # 표는 두 칸(환매·전환)이 헤더 그대로 맞아 rec가 비지 않아
            # 이동값 보정 자체를 안 타서 선취·후취가 영영 안 채워졌다.
            # 이미 직접 매핑으로 찾은 칸은 그대로 두고, 못 찾은 이름에만
            # 이동값을 적용한다.
            for j, name in mapping.items():
                if name in rec or name in fields_ever_direct:
                    continue
                jj = j + table_shift
                cv = _shift_val(row, jj, name, code)
                if cv:
                    rec[name] = cv
        # fee_evidence(이 줄 자신에 있던 수수료 모양 값)가 있었는데도
        # 위 순서맞추기·이동값 보정을 다 거치고 나서까지 rec가 여전히
        # 비어 있다면, 그 값은 "자리를 못 찾아 버려진" 것이다 - 이런
        # 줄만 이어받기(CARRY_FIELDS)를 막는다. fee_evidence가 있어도
        # 이동값 등으로 이미 rec에 자리를 잡았다면(A2/C-P1e 등 실측:
        # 앞칸 밀림이라 처음엔 못 찾았다가 이동값으로 바로잡힘) 정상
        # 처리된 것이므로 막을 이유가 없다.
        had_unplaced_evidence = bool(fee_evidence) and not rec
        for name in order:
            if name not in CARRY_FIELDS:
                continue
            if name in rec:
                last_value[name] = rec[name]
            elif name in last_value and not had_unplaced_evidence:
                rec[name] = last_value[name]
        # 위 순서맞추기/이동값 경로들은 각자 나름의 근거(칸 개수 일치,
        # 표 전체 이동값)로 값을 채우지만, 물려받은 매핑에서는 그
        # 근거가 애초에 이 표(표1)가 아니라 다른 표(carried_mismatch로
        # 드러난, 또는 그냥 자리만 우연히 맞은) 것일 수 있다 - 가입자격은
        # 원문이 항상 한글 문장이므로, 물려받은 매핑에서 한글이 전혀
        # 없는 값이 여기까지 새어 들어왔으면 그건 어느 경로를 거쳤든
        # 가입자격이 아니라고 본다(키움 KR5123490013 AG/CG/S-P 실측:
        # 표1 "부과기준" 꼬리줄 바로 뒤에 곧장 이어 붙는 퍼센트
        # 지급비율표까지 표1의 칸 매핑을 물려받아, 가입자격 칸에 판매
        # 보수 비율 숫자가 잘못 채워졌었다).
        if used_carried and rec.get("eligibility") \
                and not re.search(r"[가-힣]", rec["eligibility"]):
            del rec["eligibility"]
        # "수수료미징구"라는 이름표는 업계 표준 표기로, 판매수수료를
        # 선취·후취 어느 쪽으로도 걷지 않는다는 뜻이다(그 대신 판매
        # 보수를 더 높게 매긴다) - 그래서 이 이름표를 쓰는 클래스는
        # 선취판매수수료도 항상 "없음"이다. 그런데 이 값이 표 안에서
        # 한 클래스 줄에만 찍히고 그 아래 여러 미징구 클래스에는 병합
        # 칸으로 비워 두는 문서가 있고(신영자산운용 KR5125450023 실측:
        # "없음"이 C 클래스 줄에만 있고 C-P/C-Pe/C-G/C-P2/C-P2e 등은
        # 빈칸), 그 병합이 페이지 경계까지 넘어가면 이어받기(CARRY_FIELDS,
        # front_load_fee는 클래스마다 실제로 달라지는 게 정상이라 일부러
        # 안 걸어 뒀다)로도 못 잇는다. 코드/이름표 자체에 "미징구"가
        # 있는데도 선취판매수수료를 못 찾았을 때만, 그 이름표 자체의
        # 뜻으로 "없음"을 채운다 - "선취"/"후취" 이름표 클래스는 실제
        # 값이 클래스마다 다르므로 이 규칙에서 제외한다.
        if not rec.get("front_load_fee"):
            row_text = _squash("".join((c or "") for c in row))
            if "미징구" in row_text:
                rec["front_load_fee"] = "없음"
        if rec:
            out[code] = rec
    return out, last_value


# 펀드 전체에 대해 환매수수료를 어떻게 하는지 적은 문장.
# "(8) 환매수수료 / 이 투자신탁은 환매수수료를 부과하지 않습니다." 처럼
# 절 하나로 적어 두는 문서가 많다. 클래스별 표가 없어도 이 문장이면
# "환매수수료 나오나요?"에 답할 수 있다.
#
# "환매가능여부 및 환매수수료" 두 칸짜리 표 옆에 이 문장이 붙어 있는
# 문서가 있는데, pdfplumber가 줄 단위로 텍스트를 읽으면서 그 표의 칸
# 이름("환매수수료")이 문장 한가운데("...부과하지 환매수수료 않습니다")로
# 끼어든다(KR5111420047/KR5111450067 실측 - "부과하지"와 "않습니다"
# 사이에 낱말 하나가 더 있어서 원래 패턴이 못 읽고 상품 전체가 통째로
# 빠졌다). 그 자리에 낀 "환매수수료"만 선택적으로 건너뛴다.
#
# "이 투자신탁은" 주어를 반드시 요구한다("전환한 후 환매를 청구하는
# 경우 환매수수료를 징구하지 않습니다"처럼, 상품 전체가 아니라 특정
# 상황 하나에만 해당하는 예외 조항도 "...하지 않습니다"로 끝나서 -
# KR5127450117 실측: 이 상품은 실제로 S클래스에 환매수수료가 있는데,
# 주어 없이 아무 문장이나 받으면 이 예외 조항이 "상품 전체에 환매수수료
# 없음"으로 잘못 둔갑한다). 진짜 상품 전체 진술은 거의 항상 "이
# 투자신탁은/집합투자기구는/펀드는"으로 시작하므로, 그 주어를 안전판으로
# 계속 요구한다.
#
# 어미 한가운데 공백이 끼는 문서도 있다(KR5113420069/KR5113450401
# 실측: "아니합니다"가 "아니합니 다"로 갈린다 - 줄바꿈 정렬 때 생기는
# 잘못된 공백으로 보인다). 음절 사이마다 \s*를 둬서 흡수한다.
RE_REDEMPTION_SENTENCE = re.compile(
    r"이\s*(?:투자신탁|집합투자기구|펀드)[^.\n]{0,80}?환매수수료[^.\n]{0,15}?"
    r"(?:부과|징구|발생)하지\s*(?:환매수수료\s*)?(?:않\s*습\s*니\s*다|아\s*니\s*합\s*니\s*다)"
    r"|이\s*(?:투자신탁|집합투자기구|펀드)[^.\n]{0,80}?환매수수료[^.\n]{0,15}?"
    r"(?:받지\s*아\s*니\s*합\s*니\s*다|부과합니다|부과하며)[^.\n]{0,40}"
    # "(3) 환매수수료 환매수수료를 부과하지 않습니다."처럼 절 제목
    # 글자가 그대로 한 번 더 겹쳐 찍히고 주어 없이 바로 끝나는 문서가
    # 있다(KR5152420028 실측 - 절 제목과 진술이 한 줄에 붙어 있다).
    # 절 번호+제목이 바로 앞에 있을 때만 예외로 주어 없이 받는다 -
    # 그래야 무관한 위치에서 시작하지 않는다.
    r"|\(\d+\)\s*환매수수료\s*환매수수료(?:를)?\s*(?:부과|징구)하지\s*"
    r"(?:않\s*습\s*니\s*다|아\s*니\s*합\s*니\s*다)")
# "(3) 환매수수료 - 해당사항 없음"처럼 절 제목 뒤에 값만 적는 문서도 있다.
# 조사와 어미가 문서마다 다르다: "해당사항 없음" / "해당사항이 없습니다" /
# "해당 사항 없음". 앞의 것만 잡고 있어서 한 상품을 마지막까지 놓쳤다.
RE_REDEMPTION_NONE = re.compile(
    r"환매수수료\s*[-–—:]?\s*("
    r"해당\s*사항\s*(?:이|은|는)?\s*없(?:음|습니다|습니다\.)"
    r"|없(?:음|습니다)|미부과)")
# 연혁표("환매수수료 삭제")나 용어집 정의는 걸러야 한다.
REDEMPTION_NOISE = ("삭제", "변경", "특정기간 이내에 펀드를 환매")


# 표에 "환매수수료 | 없음"처럼 한 줄로 적어 둔 문서가 25개 있다. 다만
# 같은 모양으로 용어집 정의도 들어 있어서("펀드를 일정 기간 가입하지 않고
# 환매할 시 투자자에게 부과되는 비용으로...") 길이와 내용으로 가른다.
RE_REDEMPTION_VALUE = re.compile(r"\d+\s*(?:일|개월|년)|이익금|%")
MAX_NOTE_CELL = 30


def _redemption_cell_note(conn, code):
    for (dj,) in conn.execute(
            "SELECT data_json FROM tables WHERE doc_id = ? "
            "AND row_text LIKE '%환매수수료%' ORDER BY page", (code,)):
        try:
            rows = json.loads(dj)
        except (ValueError, TypeError):
            continue
        for row in rows:
            cells = [(x or "").strip() for x in row]
            for i, x in enumerate(cells):
                if _squash(x) != "환매수수료":
                    continue
                for y in cells[i + 1:]:
                    if not y:
                        continue
                    v = " ".join(y.split())
                    if len(v) > MAX_NOTE_CELL:
                        break  # 용어집 정의 등 긴 문장은 값이 아니다
                    if _squash(v) in NONE_MARKS or RE_REDEMPTION_VALUE.search(v):
                        return v
                    break
    return None


# 낱말 한가운데 공백 하나가 잘못 낀 문서가 있다(KR5114420046 실측:
# "재 산으로", KR518101012M 실측: "부과 된 환매수수료"). 의미는 안
# 바뀌는 단순 OCR/정렬 공백이라 눈에 띄는 것만 정리한다 - 아무 공백이나
# 지우면 진짜 띄어쓰기(조사 등)까지 붙어버릴 위험이 있어, 실측된 자리만
# 좁혀서 고친다.
WORD_SPLIT_FIXES = (
    ("재 산", "재산"), ("부과 된", "부과된"),
    # "아니합니다"/"아니 합니다" 어미 사이에 공백이 끼는 문서도 있다
    # (KR5113420069/KR5113450401 실측: "아니합니 다", KR5144420091/
    # KR5144450095 실측: "아니 합니다"). RE_REDEMPTION_SENTENCE는
    # 음절 사이 \s*로 매칭 자체는 되지만, 반환값은 원문 공백을 그대로
    # 담고 있어 여기서 따로 지운다.
    ("아니합니 다", "아니합니다"), ("아니 합니다", "아니합니다"),
)
# "(3) 환매수수료 환매수수료를 부과하지..."처럼 절 제목이 겹쳐 찍힌
# 채로 그대로 반환된 문장(RE_REDEMPTION_SENTENCE의 절 제목 예외 갈래
# 참고)은 절 번호까지 노출할 필요가 없다 - 뒤에 남는 "환매수수료..."만
# 보여준다.
RE_LEADING_SECTION_DUP = re.compile(r"^\(\d+\)\s*환매수수료\s*(?=환매수수료)")
# 절 제목 앞이 아니라 문장 "한가운데"(부과하지/징구하지 뒤, 않습니다/
# 아니합니다 앞)에 "환매수수료"가 통째로 한 번 더 끼는 표도 있다
# (KR5111420047/KR5111450067 실측: "구분" 칸의 "환매수수료" 라벨이
# 표 값 문장 "...부과하지"와 "않습니다." 두 줄 사이 y좌표에 끼어
# 읽혀 "부과하지 환매수수료 않습니다"가 된다 - RE_REDEMPTION_SENTENCE는
# 이 끼임을 허용해서 문장 자체는 놓치지 않지만, 반환값엔 끼인 낱말이
# 그대로 남아 있었다). 그 자리에서만 지운다.
RE_MID_REDEMPTION_DUP = re.compile(r"(하지)\s*환매수수료\s*(않|아니)")


def _fix_word_split_spaces(text):
    text = RE_LEADING_SECTION_DUP.sub("", text)
    text = RE_MID_REDEMPTION_DUP.sub(r"\1 \2", text)
    for bad, good in WORD_SPLIT_FIXES:
        text = text.replace(bad, good)
    return text


# "가. 투자자에게 직접 부과되는 수수료" 절 제목 바로 뒤에 클래스별 표
# 없이 "해당사항 없음"이라고만 적어 둔 문서가 있다(KR516702010M/
# KR5174420011 실측 - 이 표 자체가 통째로 없어서 선취·후취·환매·전환
# 수수료 전부 이 절 하나로 판정된다). 이런 문서는 클래스별 표가 없으니
# 위 표 파서들이 전부 못 찾아 상품이 통째로 빠진다 - 절 제목이 "가."
# 없이 오는 문서도 있어 그 앞부분은 선택으로 둔다.
RE_NO_DIRECT_FEE = re.compile(
    r"직접\s*부과되는\s*수수료[^.\n]{0,10}해당\s*사항\s*(?:이\s*)?없")


def product_has_no_direct_fee(conn, code):
    """이 상품이 "가.투자자에게 직접 부과되는 수수료: 해당사항 없음"
    절 하나로 끝나는지, 그렇다면 그 절이 있는 페이지 번호도 같이."""
    for page, text in conn.execute(
            "SELECT page, text FROM chunks WHERE doc_id = ? "
            "AND text LIKE '%직접 부과되는 수수료%'", (code,)):
        if RE_NO_DIRECT_FEE.search(" ".join(text.split())):
            return True, page
    return False, None


# "구분|보유기간|부과비율|부과시기" 요약표 - 클래스별 표(가.투자자에게
# 직접 부과되는 수수료)와 별도로, 환매수수료만 "보유기간에 따라 몇 %"로
# 여러 클래스를 한 칸에 묶어 요약하는 표가 있다(KR5194450018 30쪽 실측).
# 이 표에 이름이 있는 클래스 대부분(A/F/S 등)은 34~37쪽 클래스별 상세
# 표에도 자기 행이 따로 있어 거기서 이미 환매수수료를 얻지만, "I"(고액
# 전용)만은 상세표 어디에도 자기 행이 없다 - 이 요약표가 "I"에 대해
# 문서가 갖고 있는 유일한 정보다. 상세표에서 이미 얻은 값은 덮어쓰지
# 않고, 상세표에 아예 없던 클래스만 이 요약표로 채운다.
RE_HOLDING_PERIOD_HEADER = re.compile(r"^구분$")


def _holding_period_redemption_fallback(conn, code):
    """{class_code: 환매수수료 문구} - 이 표가 없으면 빈 dict."""
    out = {}
    for (dj,) in conn.execute(
            "SELECT data_json FROM tables WHERE doc_id = ? ORDER BY page",
            (code,)):
        try:
            rows = json.loads(dj)
        except (ValueError, TypeError):
            continue
        if not rows:
            continue
        header = [_squash(c or "") for c in rows[0]]
        if header[:4] != ["구분", "보유기간", "부과비율", "부과시기"]:
            continue
        cur_codes, cur_bucket = [], []
        for row in rows[1:]:
            cells = list(row) + [""] * max(0, 4 - len(row))
            head = (cells[0] or "").strip()
            if head:
                found = _parse(head)
                if not found:
                    # 이 표 모양이지만 클래스 이름이 아닌 줄(각주 등) -
                    # 더 이상 이 표로 안 본다.
                    break
                if cur_codes:
                    for c in cur_codes:
                        out.setdefault(c, []).extend(cur_bucket)
                cur_codes, cur_bucket = list(found.keys()), []
            period = (cells[1] or "").strip()
            rate = (cells[2] or "").strip()
            if period and rate and cur_codes:
                cur_bucket.append(f"{period} : {rate}")
        if cur_codes:
            for c in cur_codes:
                out.setdefault(c, []).extend(cur_bucket)
        if out:
            return {c: " ".join(v) for c, v in out.items() if v}
    return {}


# "종류|가입자격" 2칸짜리 표 - "가.투자자에게 직접 부과되는 수수료" 절
# 앞에 미리 붙는 안내표로, 수수료 칸이 아예 없어 _table_header가 이 표를
# 수수료 표로 인정하지 않는다(그래서 이 표만으로는 _parse_table이 안
# 불린다). 그런데 클래스 이름표가 두 줄에 걸쳐 있어("...(고액)\n
# (C-P2I(퇴직\n연금))") 정작 수수료 값 표에서는 같은 이름표가 한 줄만
# 잘려 나오는 문서가 있다(KR5144420020 실측: 값 표 코드가 "...(C-P2I(퇴직연"
# 에서 끊겨 어느 파서도 코드를 못 읽는다 - 값 자체는 넷 다 "-"로 멀쩡히
# 있는데 코드가 없어 행 전체가 버려진다). 이 2칸 표는 줄바꿈이 안 잘려서
# 코드가 온전하다 - 수수료 값 표 어디서도 못 찾은 클래스만, 적어도
# 가입자격 문장이라도 이 표에서 건진다(수수료 값 자체를 지어내진 않는다 -
# "잘렸다고 다 -겠거니" 하는 추측은 위험하다).
def _eligibility_only_fallback(conn, code, known_codes):
    """{class_code: 가입자격 문장} - 없으면 빈 dict."""
    out = {}
    for (dj,) in conn.execute(
            "SELECT data_json FROM tables WHERE doc_id = ? ORDER BY page",
            (code,)):
        try:
            rows = json.loads(dj)
        except (ValueError, TypeError):
            continue
        for row in rows:
            # 반드시 정확히 2칸짜리 행이어야 한다 - "종류|가입자격" 안내표는
            # 항상 이 모양이고, 다른 표(비용예시표 등)는 칸 수가 더 많다.
            # 헤더 글자로 표를 가리려 했더니 표가 페이지 경계에서 이어질
            # 때 헤더 줄 자체가 없는 경우를 놓쳐서(KR5144420020 실측),
            # 칸 수 하나만으로 가른다 - 비용예시표(7칸)에서 코드가 우연히
            # 맞아떨어져 옆 칸("판매수수료 및 보수ㆍ비용")을 가입자격으로
            # 잘못 낸 사고가 실제로 났었는데, 그 표는 칸 수부터 다르다.
            if not row or len(row) != 2:
                continue
            cc = _row_class_code(row, allow_bare_paren=True)
            if not cc or cc not in known_codes or cc in out:
                continue
            elig = _clean(row[1])
            # 이 표는 클래스마다 칸이 독립된 진짜 표라(좌표 폴백처럼 여러
            # 줄을 누적하다 다른 클래스 문구와 섞일 위험이 없다) 150~200자
            # 문턱을 그대로 가져올 필요가 없다 - 실측(KR5144420020 S-P2
            # (퇴직연금))으로 246자짜리 정상 문장이 있었다. 괄호짝만
            # 맞으면(_elig_looks_cut) 받는다.
            if (elig and re.search(r"[가-힣]", elig) and len(elig) <= 400
                    and not _elig_looks_cut(elig)):
                out[cc] = elig
    return out


def product_redemption_note(conn, code):
    """펀드 전체에 적용되는 환매수수료 문장. 없으면 None."""
    chunks = [" ".join(text.split()) for (text,) in conn.execute(
        "SELECT text FROM chunks WHERE doc_id = ? AND text LIKE '%환매수수료%' "
        "ORDER BY page", (code,))]
    # 문장 규칙(RE_REDEMPTION_SENTENCE)을 페이지 순서대로 전체 청크에
    # 먼저 다 돌려 본 뒤에야 "없음" 규칙(RE_REDEMPTION_NONE)으로 넘어간다
    # - 청크 하나씩 번갈아 가며 두 규칙을 다 시도하면, 클래스별로 다른
    # 문서(예: 특정 클래스만 "환매수수료 없음"인 예외 각주)에서 그 각주가
    # 앞쪽 청크에 먼저 걸려, 뒤쪽 청크에 있는 진짜 상품 전체 문장("...
    # 클래스별로... 환매수수료를 부과합니다")을 보기도 전에 "없음"으로
    # 잘못 확정돼 버린다(KR5194450018 실측: "*종류RP, RP-e, S-P, CP,
    # CP-e: 환매수수료 없음"이라는 특정 클래스 예외 각주가 먼저 걸려서,
    # 상품 대부분이 실제로는 보유기간별로 수수료를 부과한다는 사실이
    # 통째로 "환매수수료 없음"으로 뒤집혔다).
    for flat in chunks:
        for m in RE_REDEMPTION_SENTENCE.finditer(flat):
            sent = m.group(0).strip()
            if any(n in sent for n in REDEMPTION_NOISE):
                continue
            return _fix_word_split_spaces(sent)
    for flat in chunks:
        m = RE_REDEMPTION_NONE.search(flat)
        if m:
            return f"환매수수료 {m.group(1)}"
    return _redemption_cell_note(conn, code)


# 테두리 없는 표 - 좌표 기반 폴백
#
# structured_store.db의 tables는 page.extract_tables()(표 테두리선 기반)로
# 미리 만들어 둔 것이다. 그런데 "가.투자자에게 직접 부과되는 수수료" 표의
# 첫 칸(코드+이름표)이 통째로 안 잡히는 문서가 있다(흥국 계열 KR5139420015/
# KR5139420020 실측: tables 안 모든 데이터 행에서 첫 칸이 빈 문자열이다 -
# 이름표가 "수수료선취\n-오프라인(A)"처럼 두 줄에 걸쳐 있는데, pdfplumber의
# 셀-단어 결합이 이 칸에서만 실패한다). 이러면 표 자체가 known_codes와
# 하나도 안 맞아 상품이 통째로 빠진다. 원문 낱말 좌표(pdf_words - 회전
# 잡음도 보정된 채)는 멀쩡하므로, 이 표만 좌표로 다시 읽는다.
RE_FEE_CODE_TRAILING = re.compile(r"\(([A-Za-z0-9][A-Za-z0-9\-]{0,12})\)$")
# 쪽마다 맨 아래에 쪽번호·운용사 영문명이 반복해서 찍힌다("34" 한 줄 +
# "KIWOOM ASSET MANAGEMENT" 한 줄, 또는 "- 34 -" 한 줄처럼 회사마다
# 모양이 다르다). 이 표(가입자격 문장)가 페이지 경계에서 끊겨 다음 쪽으로
# 넘어가는 클래스는, 코드가 아직 안 나온 채로 쪽 맨 아래까지 이름표·
# 가입자격을 계속 쌓다가 이 꼬리를 그대로 삼켜 버린다(키움 KR5123490013
# 실측: AG의 가입자격이 "...펀드 매수를 요청하는 34 KIWOOM ASSET
# MANAGEMENT 등 금융기관등으로부터..."로, 문장 한가운데에 쪽번호+회사명이
# 끼어 들어갔다). 쪽 맨 아래(마지막 60pt) 안에서 숫자만 있거나 한글이
# 전혀 없는 짧은 줄은 표 내용이 아니라 이 꼬리로 보고 건너뛴다 - 진짜
# 표 내용(가입자격 문장 등)은 항상 한글을 포함하므로 잘못 걸릴 위험이
# 낮다.
RE_FOOTER_PAGENUM = re.compile(r"^[-–—]?\d{1,4}[-–—]?$")
RE_FOOTER_LATIN_ONLY = re.compile(r"^[A-Za-z0-9 .,&()\-]{2,60}$")


def _is_footer_line(line, page_height):
    top = line[0]["top"]
    if top < page_height - 60:
        return False
    text = _squash("".join(w["text"] for w in line)).strip()
    if not text:
        return False
    if RE_FOOTER_PAGENUM.match(text):
        return True
    if not re.search(r"[가-힣]", text) and RE_FOOTER_LATIN_ONLY.match(text):
        return True
    return False


def _coord_fee_table_page(page, known_codes, carry=None):
    """"가.투자자에게 직접 부과되는 수수료" 표가 있는 페이지를 좌표로
    읽는다. 그 표가 아니거나 코드를 하나도 못 찾으면 ({}, None)을
    돌려준다.

    carry: 앞 페이지에서 넘어온 (칸 위치, 아직 코드가 안 나온 채 쌓아 둔
    이름표/가입자격/값) 상태. 표가 페이지 경계에서 끊기면 이어지는 쪽엔
    머리글이 다시 안 찍히고(KR5139420015 실측: 29쪽은 28쪽 "가.투자자에게
    직접 부과되는 수수료" 표의 계속인데 머리글이 없다), 클래스 하나의
    이름표가 앞 쪽 마지막 줄에서 시작해 이어지는 쪽 첫 줄에 코드로
    끝나기도 한다(실측: "(C-f)"만 29쪽 맨 위에 홀로 있고 나머지 이름표는
    28쪽에 있었다). 반환값은 (이 페이지에서 확정된 클래스들, 다음
    페이지로 넘길 상태 - 이 페이지가 표를 계속 갖고 있었을 때만 dict,
    아니면 None)."""
    words = pdf_words.extract_words(page)
    if not words:
        return {}, None
    lines = cluster_lines(words, tol=2.5)
    lines = [ln for ln in lines if not _is_footer_line(ln, page.height)]

    if carry:
        label_boundary, value_cols = carry["label_boundary"], carry["value_cols"]
        has_elig_col = carry["has_elig_col"]
        label_parts = list(carry["label_parts"])
        elig_parts = list(carry["elig_parts"])
        value_parts = {k: list(v) for k, v in carry["value_parts"].items()}
        # 표가 이어지는 페이지도 맨 위엔 이 문서 특유의 반복 상품명
        # 줄이 또 찍힌다(실측: "키움 Smart Investor...제1호[주식혼합-
        # 재간접형]" - 표와 무관한데 그 낱말 하나가 하필 값 칸 x좌표에
        # 걸려 값으로 잘못 잡혔다). 그런데 모든 문서가 이러진 않는다
        # (실측: 흥국 계열은 이어지는 쪽 맨 위에 제목 없이 곧바로
        # "(C-f)" 처럼 진짜 표 내용부터 시작한다 - 무조건 첫 줄을
        # 건너뛰면 그 표 내용을 통째로 잃는다). 펀드 정식명칭은 항상
        # "투자신탁"이라는 낱말을 포함하므로, 첫 줄이 그 낱말을 포함할
        # 때만 반복 제목으로 보고 건너뛴다.
        header_bottom = (lines[0][0]["top"]
                          if lines and "투자신탁" in "".join(w["text"] for w in lines[0])
                          else 0)
        # 알몸 코드가 아직 확정 안 된 채로 페이지가 끝난 경우, 그 코드
        # 자체도 이어받는다(위 next_carry 조립부 참고) - 안 그러면 다음
        # 페이지에서 코드를 잃어버려 이어지는 내용이 엉뚱한 클래스로
        # 새어 들어간다.
        pending_bare = carry.get("pending_bare")
    else:
        # 값 칸(선취/후취/환매/전환) 4개의 x좌표를 헤더 낱말로 찾는다 -
        # 표마다 자리가 조금씩 달라서(문서 실측: 두 흥국 상품도 서로
        # 다르다) 고정 숫자로 잡으면 안 된다.
        header_x = {}
        header_top_by_key = {}
        header_top = None
        for line in lines:
            for w in line:
                t = w["text"]
                for key, prefixes in COLUMNS[1:]:  # eligibility 빼고 수수료 4개만
                    if key in header_x:
                        continue
                    if any(t.startswith(p) for p in prefixes):
                        header_x[key] = w["x0"]
                        header_top_by_key[key] = w["top"]
                        header_top = (w["top"] if header_top is None
                                      else min(header_top, w["top"]))
        # 전환수수료(switch_fee) 칸 자체가 없는 문서가 있다(우리자산운용
        # KR5118420006 실측: 머리글이 "종류(Class)"+"선취"+"후취"+
        # "환매수수료" 3칸뿐, "전환" 낱말이 표 전체에 없다 - 그런데도
        # 4칸을 다 요구하면 이 표 자체를 통째로 못 찾은 걸로 보고
        # 페이지를 버려, 상품 4개/클래스 12개가 통째로 빠졌다). 선취/
        # 후취/환매 3칸은 모든 문서에 공통이라 이 3개는 필수로 두고,
        # 전환만 없을 수 있다고 본다.
        if not {"front_load_fee", "back_load_fee", "redemption_fee"} <= header_x.keys():
            return {}, None  # 이 페이지엔 이 표가 없다
        # "선취"/"후취"/"환매" 등은 이 표 바깥의 일반 설명문에도 흔히
        # 나온다(디에스자산운용 KR5169950018 6쪽 실측: "종류(Class) /
        # 집합투자기구의 특징"이라는 산문 설명 표에 "선취(A)"/"후취(S)"/
        # "환매"가 문장 곳곳에 흩어져 있는데, 그 낱말들의 startswith
        # 매칭만으로는 진짜 수수료표 머리글과 구별이 안 된다 - 게다가
        # "종류(Class)"까지 있어 jong 검사도 통과해, 표 자체가 없는
        # 페이지에서 코드 없는 빈 레코드가 만들어지고 그 뒤로 다른 쪽
        # 스캔까지 조기 종료됐었다). 진짜 머리글은 4칸 낱말이 한 줄
        # 폭(30pt 이내)에 나란히 있다 - 문장 속에 흩어진 낱말들은 그보다
        # 훨씬 넓게 퍼져 있으므로, 매칭된 낱말들의 top이 서로 30pt를
        # 넘게 벌어지면 진짜 머리글이 아니라고 보고 버린다.
        if max(header_top_by_key.values()) - min(header_top_by_key.values()) > 30:
            return {}, None

        # "종류" 머리글(코드+이름표 칸의 머리글)의 오른쪽 끝 - 이 칸과
        # 가입자격 칸을 가르는 경계를 여기서 잡는다. 실측: 코드 칸
        # 데이터는 이 경계 안쪽에, 가입자격 칸 데이터는 이 경계+30 근처
        # 부터 시작해 둘 사이에 넉넉한 틈이 있다. 회사마다 이 머리글
        # 표기가 다르다("종류" 대신 "구분"/"(Class)"를 쓰는 문서도 있다 -
        # 베어링자산운용 실측).
        # "종류(Class)"처럼 두 낱말이 한 토큰으로 붙어 나오는 문서가
        # 있다(우리자산운용 KR5118420006 실측) - 정확히 같은 문자열만
        # 찾으면 못 찾는다. 그런데 "종류"로 시작하는 낱말을 넓게
        # 다 받으면, 표와 무관한 본문 문장 속 "종류별로"(실측: "...
        # 재산의 종류별로 해당재산의...") 같은 말까지 걸린다 - 표
        # 전체와 상관없는 훨씬 위쪽 문장인데 그 낱말의 x좌표가 더
        # 오른쪽까지 뻗어 있으면 jong_x1이 엉뚱하게 커진다. 토큰
        # 전체가 "종류" 또는 "종류(Class)"/"종류(class)"일 때만
        # 받는다(뒤에 다른 글자가 더 붙으면 안 됨). 그리고 그 낱말이
        # 수수료 칸 머리글과 같은 줄에 있을 때도 있어(이 문서 실측:
        # "종류(Class)"와 "선취"가 둘 다 top=395.8) `<` 로 엄격히
        # 비교하면(부동소수점이라 같은 줄인데도 미세하게 다를 수 있어
        # 원래 `<`를 썼다) 정작 같은 줄에 있는 걸 걸러내 버린다 -
        # `<=` 로 완화한다.
        # "명칭"+"(종류)"로 두 줄에 걸쳐 쓰는 문서도 있다(DB자산운용
        # KR5131420025 실측: "명칭"이 top=425.3, 그 아래 "(종류)"가
        # top=440.3 - 머리글(수수료 4칸, top=433.96)보다 6.3pt
        # 아래라 `<= header_top`만으로는 못 잡는다). 실제 데이터 행은
        # 이보다 한참 아래(이 문서 실측 첫 행 top=470.5)에서 시작하니
        # 위쪽 여유를 조금 더 둬도 데이터 행까지 잘못 걸릴 위험은
        # 낮다.
        # "명칭(클래스)"도 "종류(Class)"처럼 한 낱말로 붙어 나오는 문서가
        # 있다(한화자산운용 KR5129420031 실측) - 안 받으면 이 표의 이름표/
        # 가입자격 경계를 아예 못 잡아 표 전체가 값 칸 분리 없이 통째로
        # label_parts에 뭉개져, 코드가 하나도 안 잡히고 상품이 통째로
        # 빠진다.
        jong_words = [w for line in lines for w in line
                      if re.fullmatch(
                          r"종류(\(Class\)|\(class\))?|종|구분|\(Class\)|\(class\)"
                          r"|명칭(\(클래스\)|\(종류\)|\(Class\)|\(class\))?"
                          r"|\(종류\)|\(클래스\)",
                          w["text"])
                      and w["top"] <= header_top + 10]
        gap_fallback_elig = False
        if not jong_words:
            # 이름표 칸을 가리키는 "종류"/"구분"/"명칭" 낱말이 아예
            # 없이, 가입자격 칸 머리글("가입자격"/"매입자격")이 곧장
            # 나오는 문서가 있다(우리자산운용 KR5118201004 실측: 머리글
            # 이 "매입자격"+선취판매+후취판매+환매뿐 - "매입자격"은
            # "가입자격"의 다른 표기다). 이때는 가입자격 칸 머리글
            # 자체의 왼쪽 끝을 이름표/가입자격 경계로 쓴다 - 코드는
            # 항상 그보다 왼쪽에서 시작하고 가입자격 데이터는 그
            # 오른쪽에서 시작하기 때문이다.
            elig_header_words = [w for line in lines for w in line
                                  if w["text"] in ("가입자격", "매입자격")
                                  and w["top"] <= header_top + 10]
            if elig_header_words:
                jong_x1 = min(w["x0"] for w in elig_header_words) - 20
                jong_top = min(w["top"] for w in elig_header_words)
            else:
                # 이름표 칸도 가입자격 칸도 머리글 낱말이 아예 없는
                # 문서도 있다(같은 우리자산운용 KR5118420036 실측:
                # 머리글이 수수료 3칸뿐, "종류"/"구분"/"가입자격" 어느
                # 것도 없다). 이때는 헤더 바로 아래 첫 데이터 줄 자체가
                # 경계를 스스로 보여준다 - 코드 바로 뒤에 큰 틈(실측:
                # "C"~"제한없음" 사이 100pt 넘게 벌어짐)이 있고, 그
                # 틈의 가운데를 이름표/가입자격 경계로 쓴다. 데이터
                # 시작을 못 찾거나 그런 틈이 없으면(=이 페이지 형식을
                # 모르겠으면) 틀린 값을 낼 바에야 포기한다.
                gap_mid = None
                for line in lines:
                    top = line[0]["top"]
                    if top <= header_top + 5:
                        continue
                    if top > header_top + 200:
                        break
                    xs = sorted(w["x0"] for w in line)
                    if len(xs) < 2:
                        continue
                    biggest = max(
                        ((xs[i + 1] - xs[i], (xs[i] + xs[i + 1]) / 2)
                         for i in range(len(xs) - 1)),
                        default=(0, None))
                    if biggest[0] >= 40:
                        gap_mid = biggest[1]
                        break
                if gap_mid is None:
                    return {}, None
                jong_x1 = gap_mid - 20
                jong_top = header_top
                gap_fallback_elig = True
        else:
            # "명칭"+"(종류)"/"(클래스)"처럼 괄호 없는 낱말과 괄호로 싼
            # 낱말이 같이 나오는 문서에서, 괄호 쪽이 항상 더 오른쪽까지
            # 뻗는 건 아니다(디에스자산운용 KR5169950018 실측: "명칭"
            # x1=77.3, "(클래스)" x1=85.9 - 괄호 쪽이 더 넓어
            # label_boundary가 105.9까지 밀리면서 그보다 왼쪽(100.4)에
            # 있는 "제한없음"(가입자격 문구)까지 이름표 칸으로 잘못
            # 삼켜, 그 결과 코드 "(A)" 뒤에 가입자격이 그대로 붙어버려
            # "(코드)"로 끝나야 할 트레일링 매칭이 아예 안 됐다 -
            # DB자산운용 KR5131420025는 우연히 괄호 쪽이 안 넘쳐서
            # 문제가 안 드러났을 뿐이다). 괄호 없는 낱말("종류"/"구분"/
            # "명칭" 등)이 있으면 그쪽만으로 경계를 잡고, 괄호로 싼
            # 낱말은 존재 확인(has_elig_col 판정 창) 용도로만 남긴다 -
            # 괄호 없는 게 하나도 없을 때만 괄호 쪽을 쓴다.
            plain_jong = [w for w in jong_words if not w["text"].startswith("(")]
            jong_x1 = max(w["x1"] for w in (plain_jong or jong_words))
            # "종류"와 "가입자격"이 한 줄에 나란히 있는 문서도 있고
            # (베어링과 달리, 실측 KR5123490013: top=266.4 줄에 "종류"+
            # "가입자격"이 함께 있고, 그 아래 top=272.6에 "선취판매"등
            # 4개 수수료 칸 이름이 별도 줄로 갈린다 - 6.2pt 차이라 그냥
            # header_top 기준 ±5pt 창으로는 "가입자격" 줄을 놓친다)
            # "종류" 줄의 top도 함께 챙겨야, 아래 has_elig_col 판정에서
            # 그 줄까지 확실히 본다.
            jong_top = min(w["top"] for w in jong_words)
        label_boundary = jong_x1 + 20
        value_cols = sorted(header_x.items(), key=lambda kv: kv[1])
        # 이 표에 "가입자격" 칸 자체가 아예 없는 문서가 있다(베어링자산
        # 운용 KR5156450026 실측: 머리글이 "(Class)"+수수료 4칸뿐, 가입
        # 자격 칸이 없다 - 그런데도 있다고 가정하고 코드 칸과 수수료 값
        # 칸 사이 좁은 구간을 "가입자격"으로 떼어내면, 실은 수수료 칸
        # 문구의 앞부분("납입액의")이 가입자격으로 잘못 떨어져 나가고
        # 나머지("1.0% 이내")만 수수료 값으로 남아 문구가 반토막 난다).
        # 헤더에 "가입자격" 글자가 실제로 있는지로 이 칸의 존재 자체를
        # 확인한다 - 다만 "가입자격"이 한 낱말로 안 잡히고 "종"/"류"처럼
        # 한 글자씩 쪼개져 나오는 문서가 있어(KR5139420015 실측: 헤더
        # 줄이 "종","류","가","입","자","격","선취판매",... 식으로 개별
        # 글자 낱말이다) 낱말 단위가 아니라 헤더 줄 전체를 이어붙인
        # 문자열에서 부분 문자열로 찾는다. 헤더 줄 자체(=jong_x1을 준
        # "종류"/"구분"/"(Class)" 낱말과 같은 물리적 줄)만 좁게 본다 -
        # 그보다 훨씬 위에 있는 본문 문장 속 "가입자격"(실측: "가.
        # 투자자에게 직접 부과되는 수수료" 절 윗쪽 다른 문단)까지
        # 잘못 집어내지 않기 위함이다.
        header_line_text = _squash("".join(
            w["text"] for line in lines for w in line
            if jong_top - 3 <= line[0]["top"] <= header_top + 5))
        # "매입자격"이라는 다른 표기를 쓰는 문서도 있다(우리자산운용
        # KR5118201004 실측).
        has_elig_col = "가입자격" in header_line_text or "매입자격" in header_line_text
        # 가입자격 칸 머리글 자체가 없는 문서(위 gap_fallback_elig
        # 참고)는 첫 데이터 줄의 큰 틈으로 이미 "이름표|가입자격" 두
        # 구간이 있다고 확인했으므로, 여기서도 그 판정을 그대로 쓴다 -
        # 헤더 줄에 "가입자격"이라는 글자 자체가 없으니 위 검사로는
        # 항상 False가 나온다.
        if gap_fallback_elig:
            has_elig_col = True
        # 수수료 이름("선취판매"/"후취판매"/"환매"/"전환")과 그 아래
        # "수수료"가 두 줄로 갈리는 문서가 있어(실측: 두 흥국 상품 다
        # 16pt 간격) 머리글 한 줄만 건너뛰면 안 된다. 그렇다고 고정
        # 오프셋(과거 +20)을 쓰면, 헤더-첫데이터 간격이 그보다 좁은
        # 문서에서 첫 데이터 줄까지 헤더로 오인해 통째로 건너뛴다
        # (베어링자산운용 KR5156450026 실측: 헤더 top=517.7, A클래스
        # 데이터 top=533.2 - 간격 15.5pt로 20pt보다 좁아 A클래스 전체가
        # 사라졌었다). 대신 헤더 줄 바로 다음 줄들을 실제로 훑어,
        # 이름표/코드 칸(label_boundary보다 왼쪽)에 낱말이 있는 줄을
        # 만나면 그게 진짜 데이터 시작이므로 거기서 멈춘다 - 그 전까지
        # (값 칸 x좌표에만 낱말이 있는 줄이면) 머리글이 이어지는 줄로
        # 보고 계속 포함한다. 표와 무관한 줄이 끼어드는 문서는 아직 실측
        # 못 봤지만, 혹시 몰라 최대 3줄까지만 이어붙인다.
        #
        # header_top은 COLUMNS[1:] 4개 낱말 중 "매칭된 낱말 자신"의 top
        # 최솟값이지, 그 낱말이 속한 시각적 줄 전체의 top이 아니다(같은
        # 줄 안에서도 낱말마다 top이 소수점 단위로 미세하게 다르다 -
        # 베어링 실측: "(Class)"는 top=518.2, 그런데 매칭된 "선취판매"는
        # top=517.72로 0.48pt 더 작다). 그래서 header_bottom을 header_top
        # 그 자체로 두면, 헤더 줄의 다른 낱말("(Class)" 등)이 그보다도
        # top이 커 `<=` 비교를 통과 못 하고 헤더 줄인데도 본문으로
        # 새어 들어간다(실측: 그 결과 "선취판매"라는 헤더 글자 자체가
        # A클래스의 front_load_fee 값으로 잘못 찍혔었다). 몇 pt의 여유를
        # 둬 헤더 줄 전체(클러스터 허용오차 tol=2.5 이내)를 확실히
        # 덮는다 - 베어링의 헤더-데이터 간격 15.5pt보다는 넉넉히 작다.
        # "구분"(jong 낱말)이 수수료 4칸 머리글보다 아래 줄에 있는
        # 문서가 있다(NH-Amundi KR5144420081 실측: 수수료 4칸은
        # top=496.0인데 "구분"은 그보다 8.6pt 아래인 top=504.6 - 게다가
        # "구분"의 x좌표(188.5)는 label_boundary보다 왼쪽이다). 시작점을
        # header_top으로만 잡으면 이 "구분" 줄이 아직 안 덮여, 아래
        # 확장 루프가 "이 줄엔 label_boundary보다 왼쪽에 낱말이 있다 =
        # 데이터 줄이다"로 오판해 그 자리에서 멈춰버린다("구분"이라는
        # 헤더 낱말 자체를 데이터로 착각) - 그러면 진짜 데이터 줄
        # 전까지 이어지는 "수수료" 등 나머지 머리글 줄도 다 데이터로
        # 새어 들어간다. jong 낱말의 top도 시작점에 포함시킨다.
        header_bottom = max(header_top, jong_top) + 3
        extended = 0
        for line in lines:
            top = line[0]["top"]
            if top <= header_bottom:
                continue
            if extended >= 3 or any(w["x0"] < label_boundary for w in line):
                break
            header_bottom = top
            extended += 1
        label_parts, elig_parts, value_parts = [], [], {}
        pending_bare = None

    def col_of_value(x0):
        return min(value_cols, key=lambda kv: abs(kv[1] - x0))[0]

    out = {}

    def _flush(code, label_parts, elig_parts, value_parts):
        if not (code and code in known_codes):
            return None
        rec = out.setdefault(code, {})
        elig = _clean(" ".join(elig_parts).strip())
        # 가입자격 문장이 몇백 자씩 길어지면 다른 클래스 몫과 섞였을
        # 가능성이 크다(실측: S/S-p 클래스는 가입자격 설명이 유독
        # 길고 그 안에 후취판매수수료 조건문("3년미만 환매시...")까지
        # 섞여 있어, 값 칸 경계 판정이 흔들리며 앞뒤 클래스 텍스트가
        # 밀려 붙는다). 짧고 확실한 것만 받고, 의심스러우면(길이가
        # 비정상적) 아무것도 안 낸다 - 틀린 문장을 내느니 없는 게 낫다.
        # 가입자격은 문장이라 한글이 있어야 한다 - 옆 표(보수율
        # 등)의 숫자 조각이 새어 들어오면 "0.3000"처럼 숫자만 남는데,
        # 이런 값은 가입자격으로 낼 게 아니라 아예 버리는 게 낫다.
        if (elig and not rec.get("eligibility") and len(elig) <= 150
                and re.search(r"[가-힣]", elig) and not _elig_looks_cut(elig)):
            rec["eligibility"] = elig
        for col, toks in value_parts.items():
            v = _clean(" ".join(toks))
            # 값 칸도 수수료 모양(퍼센트/이내/없음류)을 갖춰야 받는다 -
            # 위와 같은 이유로 다른 칸 글자 조각이 섞여 들어올 수
            # 있다(실측: 페이지 번호 "28"이나 "환매"라는 낱말 조각이
            # 값으로 잘못 잡혔었다).
            if v and not rec.get(col) and _looks_like_fee_cell(v):
                rec[col] = v
        return code

    # 한 클래스의 이름표·가입자격·값이 물리적으로 여러 줄에 걸쳐 있고
    # (실측: 이름표 2줄, 그 사이에 "-" 값만 있는 줄이 하나 더 낀다) 코드는
    # 이름표의 "맨 마지막" 줄에야 나온다. 그래서 줄마다 즉시 처리하지
    # 않고, 코드가 나오는 줄을 만날 때까지 이름표·가입자격·값을 계속
    # 모아 두었다가 그 시점에 한 클래스 몫으로 한꺼번에 확정한다.
    #
    # 그런데 코드가 이름표 "맨 마지막"이 아니라 중간에 알몸으로 끼는
    # 문서가 있다(우리자산운용 KR5118420006 실측: "수수료선취-" /
    # "A1"(코드) / "오프라인 0.30% 이내" 순 - 진짜 수수료 값("0.30%
    # 이내")은 코드보다 "뒤"에 나온다). 이런 문서는 코드를 보자마자
    # 확정하면 아직 안 읽은 진짜 값을 놓치고, 그 값은 다음 클래스
    # 몫으로 잘못 섞여 들어가 한 클래스씩 밀린다. 그래서 알몸 코드는
    # 바로 확정하지 않고 pending_bare로 들고 있다가, 다음 클래스
    # 블록이 시작하는 줄("수수료선취-"/"수수료후취-"/"수수료미징구-"
    # 로 시작)을 만나면 그제서야 확정한다.
    table_ended = False
    # 가장 최근에 확정한 클래스 코드 - 코드+가입자격까지는 한 줄에
    # 나오는데 진짜 수수료 값이나 가입자격 나머지 문장은 그보다도 더
    # 뒤(이미 확정을 마친 다음) 줄에서야 나오는 문서가 있다(DB자산운용
    # KR5131420025/KR5131420007 실측: "수수료선취-오프라인(A)제한없음"
    # 이 한 줄이라 코드+가입자격이 그 자리에서 확정되는데, 진짜 값
    # "0.3%이내"는 다음 줄에야 나온다 - 그 값은 아직 시작도 안 한 다음
    # 클래스의 몫으로 잘못 흘러들어간다). 이런 "이름표가 전혀 없이 값만
    # 있는" 외톨이 줄을 만나면, 다음 클래스로 보지 않고 방금 확정한
    # 클래스(last_code) 몫으로 바로 채운다.
    last_code = None
    # 외톨이 줄 처리는 방금 확정을 마친 "바로 다음 한 줄"에만 적용한다
    # (아래 참고). 그 이상 넓히면(=계속 쌓인 게 없을 때마다 매번
    # last_code로 보낸다) 가입자격 문장이 원래 여러 줄에 걸쳐 길게
    # 이어지는 클래스(실측: 흥국/키움 S/S-p - "수수료..." 로 시작하는
    # 줄 없이 곧장 가입자격 문장부터 여러 줄 이어진다)에서, 그 새
    # 클래스 자신의 이름표 없는 첫 줄들을 죄다 "방금 끝난 클래스"
    # 몫으로 잘못 보내 버려 그 클래스의 가입자격이 반토막 나고 코드도
    # 못 찾는 회귀가 났다(직접 겪음).
    just_flushed = False

    for line in lines:
        if line[0]["top"] <= header_bottom:
            continue
        line_text = _squash("".join(w["text"] for w in line))
        # 이 표는 항상 "부과기준 매입시 환매시 환매시 전환시" 꼬리줄로
        # 끝난다(실측: 모든 문서가 같다). 이어지는 쪽 페이지는 표가 몇
        # 줄만 더 있다가 바로 다음 표("2) 집합투자기구에 부과되는 보수
        # 및 비용" 등, 칸 구성이 전혀 다르다)로 넘어가곤 하는데, 물려준
        # 칸 위치를 그 다음 표에도 그대로 쓰면 완전히 무관한 숫자·문구가
        # 값으로 잡힌다(KR5123490013 실측: "2)" 표의 보수율 숫자와 페이지
        # 맨 위 반복되는 상품명 "...제1호[주식혼합-재간접형]"이 AG 클래스
        # 값·가입자격으로 섞여 들어왔다). 꼬리줄이나 다음 절 번호를
        # 만나면 그 자리에서 완전히 멈춘다.
        # 이 표를 설명하는 각주("주1)선취,후취판매수수료는...")도 항상
        # 표 바로 뒤에 붙어 나온다 - "부과기준" 꼬리줄이 아예 없는
        # 회사도(베어링자산운용 실측: "가)" 표기를 쓰고 "부과기준" 줄
        # 자체가 없다) 각주는 빠짐없이 있어서 더 믿을 만한 종료 신호다.
        if ("부과기준" in line_text or re.match(r"^주\d+\)", line_text)
                or re.match(r"^(?:\d\)|[가-힣]\.)", line_text)):
            table_ended = True
            break
        # 알몸 코드(pending_bare)를 들고 있는 채로 새 클래스 블록의
        # 첫 줄("수수료선취-" 등)을 만나면, 그게 앞 클래스가 끝났다는
        # 신호다 - 이번 줄 내용이 앞 클래스 몫으로 섞이기 전에 먼저
        # 확정한다.
        if pending_bare:
            # 코드 자체가 두 조각으로 쪼개져 몇 줄 사이를 두고
            # 떨어져 나오는 문서가 있다(우리자산운용 KR5118201004
            # 실측: "S-P"가 먼저 알몸으로 나오고, 몇 줄 뒤(그 사이에
            # "가입한 자로서 퇴직연금" 등 가입자격 설명이 낀다)에
            # "(퇴직)"만 홀로 한 줄을 차지해 "S-P(퇴직)"를 완성한다 -
            # "S-P"도 그 자체로 이미 유효한 별개 코드라 여기서 코드가
            # 끝났다고 오판하면 안 된다).
            #
            # "(퇴직)" 조각이 완전히 빈 줄을 혼자 차지하는 문서도 있지만
            # (KR5118201004), 이름표 칸에는 "(퇴직)"만 있고 같은 줄
            # 가입자격 칸에는 벌써 다음 문장이 이어지는 문서도 있다
            # (신영자산운용 KR5118420036 실측: "(퇴직) 자퇴직급여보장법에
            # 의한 퇴직연금가입..."이 한 줄에 같이 찍힌다). line_text
            # (줄 전체)로 fullmatch를 걸면 이 경우 가입자격 글자가 덧붙어
            # 있어 매치가 안 돼 "(퇴직)"를 영영 못 잇는다 - known_codes에
            # 없는 새 가짜 코드 "S-P"만 남고 실제 클래스는 사라진다.
            # 이름표 칸(label_boundary보다 왼쪽) 글자만 따로 봐서, 그
            # 쪽만 "(한글)" 모양이면(오른쪽 가입자격 글자는 무관하게)
            # 코드 조각으로 받는다.
            label_only = _squash("".join(
                w["text"] for w in line if w["x0"] < label_boundary))
            if (re.fullmatch(r"\([가-힣]+\)", label_only)
                    and pending_bare + label_only in known_codes):
                pending_bare = pending_bare + label_only
                # 이 줄의 나머지(가입자격 칸에 이미 이어지는 문장)는
                # 코드가 아니라 진짜 내용이니 버리면 안 된다 - 아래
                # 본문 처리로 그대로 흘려보낸다(continue하지 않는다).
                line = [w for w in line if w["x0"] >= label_boundary]
                if not line:
                    continue
                line_text = _squash("".join(w["text"] for w in line))
        # 코드가 이름표 "앞"에 붙고 그 뒤에 설명이 이어지는 문서도
        # 있다 - 괄호로 싼 경우(신영자산운용 KR5125450023 실측:
        # "A(수수료선취-오프라인)")도 있고, 괄호 없이 코드 바로 뒤에
        # "수수료..."가 곧장 이어지는 경우(우리단기채권 KR5118420062,
        # 하나자산운용 KR5111420047 실측: "A 수수료선취-오프라인",
        # "I수수료미징구-오프라인-고액투자")도 있다 - 둘 다 "수수료..."
        # 로 시작하는 RE_HAS_LABEL과 반대 순서라 그걸로는 못 잡는다.
        # "수수료"라는 낱말 자체가 줄 경계에서 "수수"/"료"로 쪼개지는
        # 문서가 있다(신영자산운용 KR5125450023 실측: "Ae(수수"까지만
        # 이 줄에 있고 "료선취-온..."은 다음 줄에야 나온다) - "수수료"
        # 세 글자를 다 요구하면 이런 줄을 놓친다. "수" 한 글자만 있으면
        # 받는다.
        new_block = RE_HAS_LABEL.match(line_text) or re.match(
            r"^[A-Za-z][A-Za-z0-9\-]{0,10}\(?수", line_text)
        if pending_bare and new_block:
            # 알몸 코드(pending_bare)를 들고 있는 채로 새 클래스 블록의
            # 첫 줄을 만나면, 그게 앞 클래스가 끝났다는 신호다 - 이번
            # 줄 내용이 앞 클래스 몫으로 섞이기 전에 먼저 확정한다.
            last_code = _flush(pending_bare, label_parts, elig_parts, value_parts) or last_code
            just_flushed = True
            pending_bare = None
            label_parts, elig_parts, value_parts = [], [], {}
        # "(코드)"만 홀로 이루는 낱말이 label_boundary보다 훨씬
        # 오른쪽(진짜 값 칸 바로 앞)까지 밀려 나는 문서가 있다
        # (NH-Amundi KR5144420081 실측: 코드 칸 자체가 넓어서 "(A)"가
        # x0=254.1인데 label_boundary는 228.4뿐이다 - 그러면 이 낱말이
        # 값 구간으로 잘못 분류돼 이름표 문자열에 코드가 아예 안
        # 남는다). x좌표와 상관없이, "(코드)" 모양 그 자체인 낱말은
        # 항상 이름표 쪽으로 돌린다.
        # 이 낱말 자체가 "(코드)"만은 아니고, 앞 이름표 글자에 공백 없이
        # 바로 붙어 나오기도 한다(한화자산운용 KR5129420031 실측:
        # "등(Cw)" - "등" 뒤에 공백 없이 코드가 바로 붙는다). 그러면
        # fullmatch로는 못 잡으니, 끝이 "(코드)" 모양이면 낱말 전체를
        # 이름표로 돌린다(중간에 낀 앞부분 글자도 이름표 문장의 일부라
        # 버리면 안 된다).
        code_paren_words = [
            w for w in line
            if w["x0"] >= label_boundary
            and re.search(r"\([A-Za-z0-9][A-Za-z0-9\-]{0,20}(?:\([가-힣]+\))?\)$",
                           w["text"])]
        code_paren_ids = {id(w) for w in code_paren_words}
        label_words = [w for w in line if w["x0"] < label_boundary] + code_paren_words
        rest = [w for w in line if id(w) not in code_paren_ids
                and w["x0"] >= label_boundary]
        if has_elig_col:
            elig_words = [w for w in rest if w["x0"] < value_cols[0][1] - 15]
            value_words = [w for w in rest if w["x0"] >= value_cols[0][1] - 15]
        else:
            # 가입자격 칸이 없는 문서는 코드 칸 바로 다음부터가 전부
            # 수수료 값 구간이다 - 그 문구("납입액의 1.0% 이내")가 첫
            # 수수료 칸(선취판매) 머리글 x좌표보다도 왼쪽에서 시작하는
            # 경우가 있어(베어링 실측: "납입액의" x0=201.5 <
            # 선취판매 머리글 x0=227.8) 가입자격 구간을 아예 두지 않고
            # label_boundary부터 곧장 값으로 본다.
            elig_words = []
            value_words = rest

        # 이름표가 전혀 없이 가입자격/값만 있는 외톨이 줄이고(=새
        # 클래스가 시작한 게 아니다), 방금 확정을 마친 "바로 다음 한
        # 줄"이면(just_flushed) 방금 확정한 클래스(last_code) 몫으로
        # 바로 채운다 - 그대로 두면 이 내용이 아직 시작도 안 한 다음
        # 클래스의 몫으로 잘못 흘러들어간다. 딱 한 줄만 봐주고(아래
        # just_flushed 리셋 참고), 그 다음부터 이름표 없는 줄이 계속
        # 이어지면 그건 새 클래스 자신의 가입자격이 여러 줄로 긴
        # 것으로 본다.
        is_grace_line = just_flushed
        just_flushed = False
        if (is_grace_line and not label_words and (elig_words or value_words)
                and last_code):
            rec = out.get(last_code)
            if rec is not None:
                if elig_words:
                    elig = _clean(" ".join(w["text"] for w in elig_words).strip())
                    if (elig and not rec.get("eligibility") and len(elig) <= 150
                            and re.search(r"[가-힣]", elig) and not _elig_looks_cut(elig)):
                        rec["eligibility"] = elig
                vws = sorted(value_words, key=lambda w: w["x0"])
                vgroups = []
                for w in vws:
                    if vgroups and w["x0"] - vgroups[-1][-1]["x1"] <= 15:
                        vgroups[-1].append(w)
                    else:
                        vgroups.append([w])
                for g in vgroups:
                    col = col_of_value(g[0]["x0"])
                    v = _clean(" ".join(w["text"] for w in g))
                    if v and not rec.get(col) and _looks_like_fee_cell(v):
                        rec[col] = v
            continue
        if label_words:
            label_parts.append("".join(w["text"] for w in label_words))
        # 가입자격 문구가 이미 확정 문턱(150자, _flush 참고)을 넘겼으면
        # 더 안 쌓는다. 코드 확정이 늦게(다음 페이지까지) 나오는
        # 문서에서, 다음 클래스의 가입자격 상투문구가 아직 코드가
        # 나오기 전에 앞 클래스 몫으로 계속 쌓일 수 있다(신영자산운용
        # KR5125450023 실측: S-P 클래스의 진짜 가입자격 마무리 문장
        # 뒤로, 코드가 나오기도 전에 S-P2의 가입자격 상투문구
        # 전체("집합투자업자의 공동판매채널로서의 역할...")가 먼저
        # 줄줄이 나온다 - 계속 다 쌓으면 합친 문구가 150자를 넘겨
        # _flush에서 통째로 버려진다). 이미 넘긴 뒤로는 더 안 쌓아야
        # 그 앞부분(진짜 이 클래스 몫)만이라도 살아남는다.
        if elig_words and sum(len(p) for p in elig_parts) <= 150:
            elig_parts.append(" ".join(w["text"] for w in elig_words))
        # 한 칸 안의 값 문구가 길어 다음 칸 머리글 x좌표에 더 가까워
        # 보이는 낱말이 섞여 있을 수 있다(베어링 실측: "납입액의 1.0%
        # 이내"에서 "이내"의 x0=274.2가 선취판매(227.8)보다 후취판매
        # (318.6)에 더 가까워, 낱말별로 최근접 칸을 매기면 "이내"만
        # 후취판매 칸으로 잘못 떨어진다). 낱말 사이 간격이 좁으면(같은
        # 문구가 이어지는 것) 같은 칸으로 묶고, 간격이 크게 벌어질
        # 때만(실측: 칸 사이 진짜 틈은 40pt 이상) 새 칸으로 본다 -
        # 묶음의 "첫" 낱말 x좌표로만 최근접 칸을 정한다.
        value_words_sorted = sorted(value_words, key=lambda w: w["x0"])
        groups = []
        for w in value_words_sorted:
            if groups and w["x0"] - groups[-1][-1]["x1"] <= 15:
                groups[-1].append(w)
            else:
                groups.append([w])
        for g in groups:
            col = col_of_value(g[0]["x0"])
            for w in g:
                value_parts.setdefault(col, []).append(w["text"])

        joined_label = "".join(label_parts)
        squashed_label = _squash(joined_label)
        m = RE_FEE_CODE_TRAILING.search(squashed_label)
        code = m.group(1) if m else None
        if not code:
            # RE_FEE_CODE_TRAILING의 코드 글자 집합은 영숫자/붙임표뿐이라
            # "(C-P1(연금저축))"처럼 코드 자체에 괄호+한글이 중첩되는
            # 문서는 못 잡는다(NH-Amundi 실측). known_codes를 직접 아니
            # 그 안에 정확히 "(코드)"로 끝나는지를 본다 - 짧은 코드가
            # 긴 코드의 접미어일 수 있어("P1"이 "C-P1"의 일부) 가장 긴
            # 것을 고른다.
            trailing_candidates = [
                c for c in known_codes if squashed_label.endswith(f"({c})")]
            if not trailing_candidates:
                # PDF 원문 글자 추출 자체가 괄호 하나를 통째로 빠뜨리는
                # 경우가 있다(한화자산운용 KR5129420031 실측: "(Ci-RP(퇴
                # 직연금)"처럼 코드 자체에 이미 괄호가 중첩된 코드의
                # 마지막(바깥쪽) 닫는 괄호가 없이 끝난다 - 코드 자체의
                # 렌더링 결함이라 우리 쪽에서 더 채울 수 없다). 코드
                # 자체에 괄호가 있는 known_codes에 한해, 닫는 괄호
                # 하나가 모자란 채로 끝나는 것도 받아준다.
                trailing_candidates = [
                    c for c in known_codes
                    if "(" in c and squashed_label.endswith(f"({c}")]
            if trailing_candidates:
                code = max(trailing_candidates, key=len)
        if code and code in known_codes:
            # "(코드)"가 이름표 끝에 붙는 문서는 그 순간 이미 값까지
            # 다 읽은 뒤이므로 바로 확정한다(기존 방식 그대로).
            last_code = _flush(code, label_parts, elig_parts, value_parts) or last_code
            just_flushed = True
            label_parts, elig_parts, value_parts = [], [], {}
            continue
        if not pending_bare and label_words:
            # 코드가 "(코드)"로 안 감싸이고 맨 알몸으로 제 줄 하나를
            # 통째로 차지하는 문서가 있다(우리자산운용 KR5118420006
            # 실측: "A1"/"C-e"/"S-P(퇴직)" 등이 이름표 앞뒤 줄과 안
            # 섞인 채 그 자체로 한 줄이다). 그런데 같은 줄에 코드
            # 바로 뒤로 다음 이름표 조각이 틈 없이 바로 붙어 한
            # 낱말로 뭉치기도 한다(같은 문서 실측: "C-F오프라인---",
            # "S-" - 사이 공백이 원래 없거나 다음 줄로 넘어가야 할
            # "-"이 이 줄에 붙어버렸다). 그래서 완전 일치가 아니라
            # "이 줄 글자가 이 코드로 시작하는지"로 찾는다 - "C"가
            # "C-F"의 접두어이기도 해서, 시작하는 후보 중 가장 긴
            # 코드를 고른다(짧은 쪽을 고르면 "C-F" 행이 "C"로 잘못
            # 잡힌다). 이런 문서는 코드보다 진짜 값이 "뒤"에 나오므로
            # (위 주석 참고) 바로 확정하지 않고 pending_bare로 들고
            # 있다가 다음 블록 시작 줄에서 확정한다.
            bare = _squash("".join(w["text"] for w in label_words))
            candidates = [c for c in known_codes if bare.startswith(c)]
            if candidates:
                pending_bare = max(candidates, key=len)

    if pending_bare and table_ended:
        # 표가 이 페이지 안에서 확실히 끝났다(꼬리줄을 만났다) - 마지막
        # 클래스의 알몸 코드가 아직 확정 안 된 채 남아 있어도 다음
        # 블록 시작 줄이 더는 없으므로(표의 맨 마지막 클래스라서)
        # 여기까지 읽은 값을 지금 확정한다.
        _flush(pending_bare, label_parts, elig_parts, value_parts)
        pending_bare = None
        label_parts, elig_parts, value_parts = [], [], {}

    next_carry = None
    if table_ended:
        # 표가 이 페이지 안에서 확실히 끝났다(꼬리줄/다음 절 번호를
        # 만났다) - 남은 조각이 있어도 그건 다음 절의 것이지 이 표의
        # 것이 아니므로 버리고, 다음 페이지로 아무 것도 물려주지 않는다.
        pass
    elif pending_bare or label_parts or elig_parts or value_parts:
        # 이 페이지가 끝났는데 아직 코드로 안 끝난 이름표가 남았다 -
        # 다음 페이지로 이어질 수 있으니 상태를 그대로 넘긴다(다음
        # 페이지도 이 표가 아니면 호출부가 그냥 버린다). 알몸 코드가
        # 확정 안 된 채 남아 있으면(pending_bare) 그것도 같이 넘겨야
        # 한다 - 안 그러면(실측: 신영자산운용 KR5125450023 S-P 클래스
        # - 가입자격 설명이 길어 코드+값이 다음 페이지까지 이어지는데
        # 이 페이지 끝에서 미완성인 채로 그냥 확정해버리면 빈 레코드가
        # 박히고, 다음 페이지에서는 pending_bare를 잃어버려 이어지는
        # 내용이 그 다음 클래스(S-P2) 몫으로 잘못 섞여 들어간다) 코드
        # 자체를 잃어버려 이어지는 내용이 엉뚱한 다음 클래스로 잘못
        # 흘러들어간다.
        next_carry = {
            "label_boundary": label_boundary, "value_cols": value_cols,
            "has_elig_col": has_elig_col, "pending_bare": pending_bare,
            "label_parts": label_parts, "elig_parts": elig_parts,
            "value_parts": value_parts,
        }
    elif value_cols:
        # 이 페이지 자체는 깔끔히 끝났어도, 바로 다음 페이지가 머리글
        # 없이 이어지는 조각일 수 있으니 칸 위치는 넘겨 둔다.
        next_carry = {
            "label_boundary": label_boundary, "value_cols": value_cols,
            "has_elig_col": has_elig_col, "pending_bare": None,
            "label_parts": [], "elig_parts": [], "value_parts": {},
        }
    return out, next_carry


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        merged, pages, best_n = {}, {}, {}
        known_codes = {r[0] for r in conn.execute(
            "SELECT DISTINCT class_code FROM class_fees "
            "WHERE product_code = ? AND class_code IS NOT NULL", (code,))}
        # 머리글이 있는 표에서 열 구성을 확인해 두고, 바로 다음 페이지에
        # 같은 열 개수로 이어지는 표에 물려준다. 그래서 "환매수수료"가
        # 적힌 표만 보는 게 아니라 전부 훑는다.
        carried, carried_cols, carried_page = None, None, None
        carried_last_value = None
        carried_transposed, carried_transposed_page = None, None
        for page, dj in conn.execute(
                "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
                (code,)):
            try:
                rows = json.loads(dj)
            except (ValueError, TypeError):
                continue
            rows = _merge_wrapped_continuation_rows(rows)
            ncols = max((len(r) for r in rows), default=0)
            # 세로형(클래스마다 4줄) 표를 먼저 본다. 가로형 머리글 탐색은
            # "환매"로 시작하는 짧은 칸을 찾는데, 세로형 표의 "구분" 칸
            # 값("환매수수료" 등)도 이 조건에 걸려서 가로형 쪽이 먼저
            # 걸리면 세로형 표를 완전히 잘못 읽는다(KR5185450009 실측:
            # 가입자격 문장이 통째로 front_load_fee 칸에 들어갔다).
            tall_got = _parse_tall_table(rows, known_codes)
            if tall_got:
                got = tall_got
            else:
                mapping, _end = _table_header(rows)
                if mapping:
                    got, carried_last_value = _parse_table(rows)
                    carried, carried_cols, carried_page = mapping, ncols, page
                elif (carried and carried_cols is not None
                      and abs(ncols - carried_cols) <= 10
                      and carried_page is not None and page - carried_page <= 1):
                    # 열 개수가 똑같아야 한다고 너무 엄격히 요구하면, 이어지는
                    # 쪽에 빈 칸 하나가 더(또는 덜) 찍혀 전체가 한 칸 밀린
                    # 표를 통째로 놓친다(KR5153420318 31쪽 실측: 30쪽은 6칸,
                    # 31쪽은 앞에 빈 칸이 하나 더 붙어 7칸 - S/C-P/C-Pe 등
                    # 뒷쪽 클래스 9개가 전부 사라졌었다). 장식용 빈 칸이
                    # 아예 통째로 빠지는(하나만 밀리는 게 아니라) 표도
                    # 있다(KR5120450015 43쪽 실측: 42쪽 10칸 -> 43쪽 6칸).
                    # 이런 어긋남은 _parse_table 안의 순서맞추기·이동값
                    # 보정이 스스로 바로잡으므로, 열 개수가 완전히 같을
                    # 필요는 없다 - 다만 무관한 뒤쪽 표까지 잘못 이어붙지
                    # 않도록 어느 정도 상한은 둔다.
                    got, carried_last_value = _parse_table(
                        rows, carried=carried, carried_cols=carried_cols,
                        carried_last_value=carried_last_value)
                    if got:
                        carried_page = page  # 또 이어질 수 있다
                else:
                    # 클래스가 열, 수수료 종류가 행인 뒤집힌 표도 있다
                    # (_parse_transposed_table 참고) - 위 두 방식 다
                    # 실패했을 때만 시도한다(정상 표를 잘못 걸러낼 위험을
                    # 줄이기 위해 마지막 순서로 둔다). 이런 표의 마지막
                    # "종류" 블록이 헤더만 찍고 페이지가 끝나면(데이터는
                    # 다음 표 첫 줄부터) carried_transposed로 이어 받는다
                    # - 바로 다음 표일 때만(무관한 뒤쪽 표까지 잘못 번지지
                    # 않도록 wide-format의 carried와 같은 인접성 조건).
                    carry_in = (carried_transposed
                                if (carried_transposed_page is not None
                                    and page - carried_transposed_page <= 1)
                                else None)
                    got, pending = _parse_transposed_table(rows, carry_in)
                    carried_transposed = pending
                    carried_transposed_page = page if pending else None
                    if not got:
                        continue
            for cc, rec in got.items():
                # 같은 클래스가 여러 표에 나온다. 먼저 나온 걸 쓰면 안 된다 -
                # 앞쪽에 가입자격 칸이 없는 비슷한 표가 있는 문서가 있어서
                # (KR510902511M 실측: 14쪽 표가 26쪽 진짜 "가." 표를 밀어내
                # 가입자격이 33개 상품에서 11개로 줄었다), 칸별로 합친다.
                # 근거 페이지는 이 클래스에 대해 가장 많은 칸을 채운 표로
                # 잡는다 - 고객이 열어 볼 자리는 거기다.
                for real_cc in _expand_class_range(cc, known_codes):
                    cur = merged.setdefault(real_cc, {})
                    for k, v in rec.items():
                        if v and not cur.get(k):
                            cur[k] = v
                    if len(rec) > best_n.get(real_cc, 0):
                        best_n[real_cc] = len(rec)
                        pages[real_cc] = page

        # 테두리표 자체가 코드 칸을 통째로 놓치는 문서가 있다
        # (_coord_fee_table_page 참고) - 위 방식으로 known_codes를 다
        # 못 채웠을 때만, PDF 원문을 좌표로 다시 훑는다(느려서 늘 켜 두면
        # 안 된다). 표가 있는 페이지부터 이어지는 페이지까지 죽 넘기며
        # 진짜 새로 찾은 클래스만(이미 merged에 있는 건 그대로 둔다) 보탠다.
        #
        # "채워졌다"는 기준은 수수료 칸(front/back/redemption/switch) 중
        # 하나라도 실제 값이 있는지로 본다 - 코드 키가 merged에 있는지
        # (`in merged`)만으로 판단하면 안 된다(KR5169950018 실측: 6쪽의
        # 전혀 무관한 문장에서 "A"/"C"가 우연히 코드로 잡히되 값은
        # 하나도 못 찾아 빈 딕셔너리 {}가 merged에 꽂혔다 - 그러면
        # known_codes가 다 "있는" 걸로 오판해 6쪽에서 스캔이 멈춰버려,
        # 진짜 표가 있는 32쪽까지 못 가서 7개 클래스가 수수료 칸을
        # 하나도 못 채운 채 남았다). 가입자격만 있고 수수료 칸이 없는
        # 것도 미해결로 본다 - 테두리표가 가입자격만 잡고 수수료 칸은
        # 못 잡는 문서(같은 실측)에서 이 좌표 스캔이 그 칸을 마저
        # 채워야 하기 때문이다.
        FEE_FIELDS = ("front_load_fee", "back_load_fee", "redemption_fee", "switch_fee")
        # 이 상품의 표에 가입자격 칸이 아예 없는 문서가 있다(우리자산운용
        # KR5118420036 등 실측 - "종류"/"구분"/"가입자격" 어느 머리글도
        # 없다). 그런 문서에서 가입자격을 항상 요구하면 매번 헛되이 좌표
        # 재스캔을 돌게 된다(정답은 계속 없다). 지금까지(테두리표 1차
        # 처리) 단 한 클래스라도 가입자격을 찾았으면, 이 표에 가입자격
        # 칸이 "있다"는 뜻이므로 그때만 다른 클래스에도 요구한다.
        any_eligibility = any(rec.get("eligibility") for rec in merged.values())

        def _fee_resolved(cc):
            rec = merged.get(cc)
            if not (rec and any(rec.get(f) for f in FEE_FIELDS)):
                return False
            # "필드 중 하나라도 있으면 끝"으로 보면, 후취/환매/전환처럼
            # 여러 클래스가 공유하는 이어받기(carry) 칸 하나만 새로
            # 채워져도 그 클래스가 "다 됐다"고 오판한다(한화자산운용
            # KR5125450070 실측: C-P/S-P는 가입자격까지 제 줄에서 정상
            # 확인됐는데, 선취판매수수료만 원래 이어받지 않는 칸이라
            # 빈 채로 남았다 - 그런데 후취판매수수료가 이어받기로
            # 채워지자 스캔이 거기서 멈춰, 좌표 재스캔에서만 나오던
            # 선취판매수수료를 영영 못 찾게 됐다. 신영자산운용
            # KR5125450023 실측: C-P/C-Pe 등은 표 칸 수가 물려받은 칸
            # 수와 달라(carried_mismatch) _parse_table이 직접매핑 결과를
            # 통째로 버리는 자리라, 가입자격까지 같이 못 건졌는데도
            # 이어받기로 후취/환매/전환만 채워져 "다 됐다"고 오판했다).
            # 선취판매수수료는 "가.투자자에게 직접 부과되는 수수료"
            # 표에서 모든 클래스가 예외 없이 값을 갖는 칸이다(실제
            # 값이든 "없음"/"-"든) - 이게 비어 있다는 건 이 클래스의
            # 제 줄을 아직 제대로 못 읽었다는 뜻이므로, 다른 칸이 이어
            # 받기로 채워졌어도 미해결로 본다.
            if not rec.get("front_load_fee"):
                return False
            # 선취판매수수료는 "미징구" 이름표로 유추해 채울 수 있지만
            # (위 참고), 가입자격은 그런 유추가 안 된다 - 이 표에
            # 가입자격 칸이 있는 게 확인됐는데 이 클래스만 없다면,
            # 선취판매수수료가 이름표 유추로 채워졌다고 해서 "제 줄을
            # 다 읽었다"고 오판하면 안 된다(같은 KR5125450023 실측:
            # carried_mismatch로 가입자격이 버려진 자리에 "미징구"
            # 유추로 선취판매수수료만 채워지면, 좌표 재스캔에서만
            # 나오는 가입자격 문장을 영영 못 찾게 된다).
            if any_eligibility and not rec.get("eligibility"):
                return False
            return True

        def _unresolved_codes():
            return {cc for cc in known_codes if not _fee_resolved(cc)}

        if _unresolved_codes():
            pdf_candidates = glob.glob(os.path.join(DATA_DIR, code, "*.pdf"))
            if pdf_candidates:
                with pdfplumber.open(pdf_candidates[0]) as pdf:
                    carry, carried_page = None, None
                    for page_num, page in enumerate(pdf.pages, start=1):
                        # 표가 아닌 페이지가 몇 장 끼면(실측: KR5123490013
                        # 34쪽 표 뒤로 35~36쪽엔 "나.집합투자기구에 부과되는
                        # 보수 및 비용" 같은 다른 표가 오고, 37쪽에 가서야
                        # 우연히 "직접 부과되는 수수료"라는 문구가 각주에
                        # 다시 나온다) 물려주기를 바로 다음 쪽으로만
                        # 제한하지 않으면, 이미 끝난 표의 칸 위치를 전혀
                        # 무관한 페이지 내용에 그대로 덮어써서 엉뚱한 글자
                        # 조각이 값으로 잡힌다. 바로 다음 쪽일 때만 잇는다.
                        use_carry = carry if carried_page == page_num - 1 else None
                        got, carry = _coord_fee_table_page(page, known_codes, carry=use_carry)
                        carried_page = page_num if carry else None
                        for cc, rec in got.items():
                            cur = merged.setdefault(cc, {})
                            for k, v in rec.items():
                                if v and not cur.get(k):
                                    cur[k] = v
                            if len(rec) > best_n.get(cc, 0):
                                best_n[cc] = len(rec)
                                pages[cc] = page_num
                        if not _unresolved_codes():
                            break

        no_direct_fee, no_direct_fee_page = (
            product_has_no_direct_fee(conn, code) if not merged and known_codes
            else (False, None))
        if no_direct_fee:
            # 클래스별 표 자체가 없이 "가. 투자자에게 직접 부과되는
            # 수수료: 해당사항 없음"이라고 절 하나로 끝내는 문서다
            # (KR516702010M/KR5174420011 실측) - 표가 없으니 위 어떤
            # 파서도 이 상품에서 아무것도 못 찾아, known_codes가 있는데도
            # 상품이 통째로 빠졌었다. 절 진술 그대로 전 클래스에 "없음"을
            # 채운다(가입자격은 이 절에 안 나오므로 그대로 비워 둔다).
            for cc in known_codes:
                merged[cc] = {"front_load_fee": "없음", "back_load_fee": "없음",
                               "redemption_fee": "없음", "switch_fee": "없음"}
                pages[cc] = no_direct_fee_page

        # known_codes는 class_fees.json 기준이라, class_fees도 그 클래스의
        # 상세표를 못 찾은 경우(바로 아래 "I" 사례)엔 known_codes 자체에도
        # 없어 이 두 폴백이 대상으로조차 못 본다. class_meaning(종류형
        # 명칭표 - 이 상품에 실제로 있는 클래스 코드의 더 넓은 원천)에는
        # 있는 코드까지 아울러야 이런 클래스도 폴백 대상이 된다. 이 둘만
        # 쓰는 좁은 폴백이라 known_codes 자체를 넓히지 않고 여기서만 쓴다 -
        # 다른 파서(접두/접미/대소문자 흔들림 병합 등)까지 이 넓은 집합을
        # 쓰면 class_fees와 class_meaning이 실제로 다르게 아는 표기를
        # 뭉갤 위험이 있다(merge_class_spelling.py가 따로 존재하는 이유이기도
        # 하다).
        meaning_codes = {r[0] for r in conn.execute(
            "SELECT DISTINCT class_code FROM class_meaning "
            "WHERE product_code = ? AND class_code IS NOT NULL", (code,))}
        fallback_known = known_codes | meaning_codes

        # 클래스별 상세표에 자기 행이 아예 없는 클래스가 있다(KR5194450018
        # "I" 실측 - 요약정보의 "구분|보유기간|부과비율|부과시기" 환매수수료
        # 표에만 이름이 있고 34~37쪽 클래스별 상세표엔 없다. class_fees.json
        # 도 이 클래스를 못 찾는다 - 문서 자체가 이 클래스의 가입자격·선취/
        # 후취/전환수수료를 어디에도 안 밝혔다는 뜻이므로 지어내지 않는다).
        # merged에 아예 없는(=어떤 파서도 못 찾은) 클래스만, 이 요약표에
        # 환매수수료가 있으면 그것만 채운다 - 상세표에서 이미 뭐라도 찾은
        # 클래스는 건드리지 않는다.
        missing_codes = fallback_known - set(merged)
        if missing_codes:
            redemption_fallback = _holding_period_redemption_fallback(conn, code)
            for cc in missing_codes & set(redemption_fallback):
                merged[cc] = {"redemption_fee": redemption_fallback[cc]}

        # 여전히 안 채워진 클래스는 "종류|가입자격" 안내표에서라도 이름표를
        # 찾는다(KR5144420020 C-P2I(퇴직연금)/S-P2(퇴직연금) 실측 - 값 표의
        # 코드가 줄바꿈에 잘려 코드 자체를 못 읽었을 뿐, 이 안내표는 줄바꿈이
        # 안 잘려 코드가 온전하다). 수수료 값은 값 표에서 못 읽은 그대로
        # 지어내지 않고 가입자격만 채운다.
        missing_codes = fallback_known - set(merged)
        if missing_codes:
            elig_fallback = _eligibility_only_fallback(conn, code, missing_codes)
            for cc in missing_codes & set(elig_fallback):
                merged[cc] = {"eligibility": elig_fallback[cc]}

        # 위 두 폴백으로도 못 채운 극소수는 PDF 원문 대조로 확인해 둔
        # 값을 그대로 못박는다(_KNOWN_MISSING_ROWS 정의부 주석 참고).
        missing_codes = fallback_known - set(merged)
        for cc in missing_codes:
            fix = _KNOWN_MISSING_ROWS.get((code, cc))
            if fix:
                fix = dict(fix)
                pages[cc] = fix.pop("page", None)
                merged[cc] = fix

        # 위 두 폴백으로 새로 채운 코드는 class_meaning에만 있고
        # known_codes(class_fees 기준)엔 없을 수 있다 - 그대로 두면 바로
        # 아래 표기 정규화 단계가 "known_codes에 없는 코드"로 보고 다른
        # known 코드에 억지로 합치려 하거나(엉뚱한 병합 시도) 매칭
        # 실패로 지워 버린다(방금 채운 걸 도로 잃는다). class_meaning이
        # 실제로 검증한 코드이므로 known_codes에 편입해 정식 코드로
        # 인정한다.
        known_codes |= fallback_known & set(merged)

        # known_codes에 없는 표기를 정식 코드로 합쳐 넣는다. 표가 페이지
        # 경계나 줄바꿈으로 갈리면서 코드 글자 일부가 잘려 나가는 표가
        # 있다(KR5127420034 실측: "C-퇴직연금"의 마지막 글자 "금"이 다른
        # 셀로 떨어져 나가 "C-퇴직연"이라는 존재하지 않는 코드가 하나 더
        # 생긴다). known_codes(class_fees.json이 이미 확인한 코드)에
        # 없다고 무조건 버리면 안 된다 - 대소문자·붙임표만 다른 진짜
        # 표기 차이도 known_codes엔 없다(KR5110501016 실측: "Ae"는
        # known_codes엔 "A-E"만 있지만 실제로 존재하는 별개 표기다).
        #
        # 이 병합은 반드시 아래 "for cc, rec in sorted(merged.items())"
        # 출력 루프보다 먼저, merged 자체를 직접 고쳐 끝내야 한다(실측
        # 회귀: 예전엔 이 병합을 출력 루프 안에서 했는데, 그 루프가
        # `sorted(merged.items())`로 미리 통째로 스냅샷을 떠 버려서,
        # 병합 대상 코드(target)가 그 시점까지 merged에 없던 새 키면
        # `merged.setdefault(target, {})`로 새로 생겨도 이미 떠 둔
        # 스냅샷엔 없어 출력 루프가 그 키를 아예 못 봤다 - 즉 "Ae" 같은
        # 표기가 삭제만 되고 정식 표기 "A-e"로는 끝내 안 나갔다. 여기서
        # merged 자체를 직접 고치면 그 다음에 뜨는 스냅샷엔 병합된 결과가
        # 이미 반영돼 있다.
        for cc in list(merged.keys()):
            if not (known_codes and cc not in known_codes):
                continue
            rec = merged[cc]
            # 잘림은 글자 한두 개가 떨어져 나가는 것뿐이다 - cc가
            # known 코드의 순수 접두사(cc로 시작하되 cc보다 김)이면서
            # 길이 차이가 작을 때만 후보로 본다. "cc가 다른 known
            # 코드로 시작한다"(반대 방향)는 안 쓴다 - "C-퇴직연"은
            # "C"로 시작하지만 "C"는 이 상품에 실제로 있는 별개의
            # 완결된 클래스라 그쪽으로 합치면 안 된다. 후보가 여럿
            # 이면(예: C-퇴직연 -> C-퇴직연금 외에 C-퇴직e도 앞 세
            # 글자까지는 같다) 길이 차이가 가장 작은 쪽을 고른다.
            candidates = [k for k in known_codes
                          if k != cc and k.startswith(cc)
                          and len(k) - len(cc) <= 3]
            target = min(candidates, key=len) if candidates else None
            if not target:
                # 반대 방향으로 잘리는 표도 있다 - 코드 칸이 줄바꿈으로
                # 두 줄에 걸쳐 있어서 앞쪽 글자("C-")가 다른 셀로
                # 떨어져 나가고 뒤쪽 조각("P2e")만 이 행의 코드로
                # 잡히는 경우다(KR514X450008 실측: "C-P2e"의 앞
                # 두 글자가 떨어져 나가 "P2e"라는 존재하지 않는 코드가
                # 하나 더 생겼다). known 코드가 cc로 "끝나면"(cc가
                # 순수 접미사) 잘린 뒤쪽 조각일 가능성이 커서 그
                # known 코드 쪽에 합친다.
                suffix_candidates = [k for k in known_codes
                                     if k != cc and k.endswith(cc)
                                     and len(k) - len(cc) <= 3]
                target = min(suffix_candidates, key=len) if suffix_candidates else None
            if not target:
                # 대소문자만 다른 표기도 있다(KR514X450008 실측:
                # "C-pe"가 이 표에서만 소문자로 찍히는데 정식 표기는
                # "C-Pe" - class_meaning/class_fees가 아는 진짜 클래스와
                # 같은 것이다. 별개 코드로 남기면 같은 클래스가 두
                # 줄로 중복된다).
                ci_candidates = [k for k in known_codes
                                 if k != cc and k.lower() == cc.lower()]
                target = ci_candidates[0] if len(ci_candidates) == 1 else None
            if not target:
                # 붙임표(-) 유무만 다른 표기도 있다(KR5123490013 실측:
                # 이 표(가입자격·수수료율)는 "A-e"/"C-e"인데 class_fees가
                # 아는 정식 표기는 "Ae"/"Ce" - 표마다 붙임표를 넣거나
                # 빼는 게 이 문서만의 습관이다). 이걸 반영 안 하면 위
                # 셋(접두/접미 잘림, 대소문자) 어디에도 안 걸려서
                # "Class"/"No" 같은 진짜 가짜 코드와 똑같이 버려지는데,
                # 이건 표기만 다른 진짜 클래스라 잃으면 안 된다.
                dash_candidates = [k for k in known_codes
                                   if k != cc and k.replace("-", "") == cc.replace("-", "")]
                target = dash_candidates[0] if len(dash_candidates) == 1 else None
            if target:
                tgt_rec = merged.setdefault(target, {})
                for k2, v2 in rec.items():
                    if v2 and not tgt_rec.get(k2):
                        tgt_rec[k2] = v2
                if cc in pages and target not in pages:
                    pages[target] = pages[cc]
            # target을 못 찾았으면(위 네 갈래 다 실패) 이 상품의 진짜
            # 클래스가 아닐 가능성이 크다 - 표 헤더 글자("Class"/"No"
            # 같은 열 이름)가 데이터 행으로 잘못 읽힌 경우가 실측됐다
            # (KR5144420020/KR5156450026/KR555202013M의 "Class",
            # KR514X450008의 "No"). known_codes에 전혀 없는 코드는
            # 근거로 못 쓰므로 버린다 - target을 찾았든 못 찾았든 cc
            # 자체는(정식 표기가 아니므로) merged에서 지운다.
            del merged[cc]

        for cc, rec in sorted(merged.items()):
            if not any(rec.get(k) for k in (
                    "eligibility", "front_load_fee", "back_load_fee",
                    "redemption_fee", "switch_fee")):
                # 표는 찾았지만 칸 위치가 어긋나 값을 하나도 못 건진
                # 클래스다(KR5123365001 실측: "투자신탁" 단일클래스 표의
                # 부과비율 칸이 "-" 값 앞에 병합된 빈 칸을 여럿 끼고 있어
                # kind_col+1로는 못 읽는다). 아무 정보도 없는 빈 레코드를
                # 그대로 내보내면 잡음만 된다.
                continue
            elig_final = rec.get("eligibility")
            # 표 파서가 여럿(가로형/세로형/전치형/좌표 폴백)이라 위에서
            # 개별적으로 다 막기 어렵다 - 어느 파서를 거쳤든 최종적으로
            # 한 번 더 본다. 각주 참조("주2) 참조")나 수수료율 문구가
            # 가입자격 칸으로 잘못 들어온 경우(KR5123420015/49,
            # KR5123490013/16/17, KR5157420003 실측)도 여기서 걸린다.
            if elig_final and (_elig_looks_cut(elig_final)
                                or _looks_like_fee_cell(elig_final)):
                elig_final = None
            out.append({
                "product_code": code,
                "class_code": cc,
                "eligibility": elig_final,
                "front_load_fee": _clean_front_load_fee(rec.get("front_load_fee")),
                "back_load_fee": rec.get("back_load_fee"),
                "redemption_fee": rec.get("redemption_fee"),
                "switch_fee": rec.get("switch_fee"),
                "page": pages.get(cc),
            })
    conn.close()
    return out


def extract_product_notes(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    out = []
    for (code,) in conn.execute(
            "SELECT DISTINCT product_code FROM class_fees "
            "WHERE product_code IS NOT NULL"):
        note = product_redemption_note(conn, code)
        if note:
            out.append({"product_code": code, "redemption_note": note})
    conn.close()
    return out


def report(rows, db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    have = {}
    for pc, cc in conn.execute("SELECT product_code, class_code FROM class_fees"):
        have.setdefault(pc, set()).add(cc)
    conn.close()

    got = {}
    for r in rows:
        got.setdefault(r["product_code"], set()).add(r["class_code"])
    total = sum(len(v) for v in have.values())
    matched = sum(len(v & got.get(pc, set())) for pc, v in have.items())

    print(f"수수료·가입자격 {len(rows)}건 / 상품 {len(got)}개")
    print(f"class_fees의 클래스 {total}개 중 채워진 것: {matched}개 "
          f"({matched * 100 // max(total, 1)}%)")
    for field in ("eligibility", "redemption_fee", "front_load_fee",
                  "back_load_fee", "switch_fee"):
        n = sum(1 for r in rows if r.get(field))
        print(f"  {field}: {n}건")
    charged = [r for r in rows if r.get("redemption_fee")]
    print(f"\n환매수수료가 실제로 적힌 클래스 {len(charged)}건 "
          f"({len({r['product_code'] for r in charged})}개 상품)")
    for r in charged[:5]:
        print(f"    {r['product_code']} {r['class_code']}: {r['redemption_fee'][:90]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = extract(args.db)
    report(rows, args.db)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    notes = extract_product_notes(args.db)
    with open(OUTPUT_PRODUCT_JSON, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
    print(f"펀드 전체 환매수수료 문장 {len(notes)}건")
    print(f"→ {OUTPUT_JSON}, {OUTPUT_PRODUCT_JSON}")


if __name__ == "__main__":
    main()
