"""
연금 Agent 과제 - 클래스별 총보수 추출 (좌표 기반 재구성)

products 표 중 "클래스 종류 + 총보수" 수수료표가 pdfplumber extract_tables()로
뽑을 때 셀이 뭉쳐지는(줄바꿈만으로 구분된 텍스트 블록이 되는) 경우가 많다는
걸 확인했다 (194개 표, 83개 문서 - README 참고). extract_tables()의
table_settings(strategy, tolerance)만 조정해서는 열 분리가 안 됐고, 원인은
pdfplumber의 표 셀 경계 인식 실패였다.

그래서 이 스크립트는 표 재인식을 시도하는 대신, 페이지의 각 단어의 실제 좌표
(page.extract_words())를 직접 읽어서:
  1. top(y좌표)이 비슷한 단어들을 "한 줄"로 묶고
  2. 소수 3~4개(총보수/판매보수/동종유형총보수/총보수·비용) + 정수 4개 이상
     (1/2/3/5/10년 비용예시)이 있는 줄을 "데이터 행"으로 판별하고
  3. 그 줄에서 x좌표가 가장 왼쪽인 소수를 총보수로, 그 앞의 텍스트에서
     클래스 코드(괄호 안 알파벳/숫자, 예: A2, C1, Ae, C-E)를 찾는다

KR5120420039(정상 추출된 표)로 방법을 검증(A2=0.3195 등 4개 클래스 전부 일치)
했고, KR5111420047(깨진 표)에도 적용해 원본 이미지와 6개 클래스 전부 일치함을
육안으로 확인했다.

범위: 이번 1차는 "총보수 표"만 대상으로 한다 (수익률/AUM 표는 컬럼 구조가
달라서 별도 스크립트가 필요 - 다음 단계).

사용법:
    python scripts/extract_class_fees.py
    python scripts/extract_class_fees.py --output class_fees.json
"""

import argparse
import glob
import json
import os
import bisect
import re

import pdfplumber

import pdf_words  # noqa: E402  (import만으로 Page.chars 전역 패치가 걸린다 - pdf_words.py 참고)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "class_fees.json")

NUM_RE = re.compile(r"^\d[\d,]*\.?\d*$")
# 일부 운용사 서식(신영자산운용 등)은 총보수 % 값을 "1.18%"처럼 %가 붙은 한
# 토큰으로 낸다(공백 없이 붙어 있어 pdfplumber가 한 단어로 묶음). %가 없는
# 문서와 똑같이 처리하기 위해 optional %를 허용하고, 저장할 때는 벗겨낸다.
DECIMAL_RE = re.compile(r"^\d+\.\d+%?$")
# 보수율 칸에는 소수점 없이 "0"만 놓이기도 한다 - 랩·기관 전용 클래스의
# 판매보수가 0인 건 정상이다. DECIMAL_RE가 소수점을 요구해서 그 칸을 값으로
# 안 봤고, 칸 하나가 비면서 열 맞추기가 어긋나 그 행이 통째로 버려졌다
# (KR5152420028 28쪽 실측: 15줄 중 랩 전용 CW 한 줄만 빠졌다 -
# "수수료미징구-오프라인-랩(CW) | 0 | ... | 0.035").
# DECIMAL_RE 자체는 "이 줄이 데이터 줄인가"를 세는 데도 쓰여서 건드리면
# 안 된다(맨 정수는 펀드코드·비용예시·연도에도 널려 있다). 값 칸이라는 게
# 이미 확인된 자리에서만 이걸 쓴다.
# 가운뎃점 자리에 문서마다 다른 글자를 쓴다 - "·"(U+00B7)/"ㆍ"(U+318D)/
# "∙"(U+2219)/"․"(U+2024)/"•"(U+2022) 외에 "･"(U+FF65, KR5111450067
# 41쪽 실측)/"‧"(U+2027)도 쓰는 문서가 있다. 한 글자로 안 통일하면
# "총보수·비용"류 매칭이 이 문서들에서만 실패해 엉뚱한 칸("합성총보수‧
# 비용")이 대신 선택된다.
DOT_NORMALIZE_TRANS = str.maketrans({
    "ㆍ": "·", "∙": "·", "․": "·", "•": "·", "･": "·", "‧": "·",
})
FEE_VALUE_RE = re.compile(r"^(?:\d+\.\d+|0)%?$")
# 값 칸 안에 각주 번호("주6)")가 값과 같은 셀 사각형에 찍혀 있는 문서가
# 있다(KR515302022M 34쪽 실측: "판매회사보수" 행에서 Ae/Ce 두 칸만 값
# 뒤에 "주6)"가 붙어 "0.2250 주6)"처럼 읽힌다 - 온라인 클래스 판매보수가
# 체감 구조라는 각주를 그 칸에 바로 달아 놓은 것). 이 낱말이 안 지워지면
# 그 칸은 float()가 깨져 "값이 요약표와 어긋난다"로 오판되고, 그 칸 하나
# 때문에 같은 행(판매회사보수)의 나머지 멀쩡한 칸(C2/C3/C4/CI/CF/CW
# 등)까지 필드 전체가 버려졌다. 각주 번호는 라벨 칸(클래스명·항목명)엔
# 나올 일이 없는 표기라 값 칸에서만 지워도 안전하다.
FEE_FOOTNOTE_MARK_RE = re.compile(r"\s*주\d+\)\s*")
DECIMAL_FINDALL_RE = re.compile(r"\d+\.\d+")  # 앵커 없이 텍스트 뭉치 안에서 찾을 때
# 코드 자체가 순수 한글(운용사 직판 채널)인 문서가 있다("수수료미징구-
# 직판(직판)", "수수료미징구-직판-기관(직판f)" - KR5114420016/027
# 실측). 코드는 항상 라틴 문자로 시작한다고 가정했는데 이 셋은 아예
# "직판"이 코드다. 좁게 "직판" 낱말만 예외로 허용한다 - 아무 한글이나
# 코드로 받으면 서술형 문장을 코드로 오인하는 사고가 난다(RE_ATTRS 8→7
# 글자수 상한을 고친 이유와 같은 위험).
CLASS_CODE_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8}|직판[A-Za-z0-9]{0,3})\)")
# "A(수수료선취-오프라인)"처럼 클래스 코드가 괄호 안이 아니라 괄호 바로
# 앞에 붙어 나오는 문서가 있다(괄호 안은 클래스 코드가 아니라 상품유형
# 설명 - KR5125450023/KR5125450070 실측).
CLASS_CODE_PREFIX_RE = re.compile(r"^([A-Za-z]{1,3})\(")
# "(Cp(퇴직연금))"처럼 클래스 코드 뒤에 괄호가 또 하나 중첩돼 부가설명이
# 따라붙는 문서도 있다(코드 자체는 "Cp"/"Cpe"처럼 하이픈 없는 표기 -
# KR5114420027 실측, 원본 표를 사용자가 직접 캡처해서 확인함: 글자가
# 깨진 게 아니라 원래 이렇게 이중 괄호로 표기됨). 여는 괄호 바로 다음에
# 또 여는 괄호가 오면(닫는 괄호 대신) 그 사이를 코드로 본다.
CLASS_CODE_NESTED_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\(")
# "운용전환일"이 고정된 캘린더 날짜가 아니라 목표기준가격 도달 같은 조건이
# 충족돼야 발생하는 문서가 있다(KR5147430065 실측: "목표전환형" 펀드 -
# "목표기준가격(종류A 누적기준가격 1,060원 이상)에 도달한 이후 운용전환").
# total_fee_after_conversion 등 필드만 보면 "후"가 언제/왜인지 알 수 없다.
# 처음엔 이 조건을 문장으로 풀어서 conversion_note에 남겼는데, 이 파일의
# 다른 모든 필드가 원본에서 그대로 뽑은 값(숫자/코드)이지 해석문이 아닌
# 것과 성격이 달라서("답을 미리 써주는" 꼴이 될 위험 - 사용자 지적)
# 숫자만 구조화된 필드로 남기기로 바꿨다. 의미(= 목표가격 도달 시 전환)는
# 필드 이름과 이 주석/README에 문서화해두고, 실제 문장으로 풀어 답하는 건
# 나중에 에이전트 규칙을 만들 때 다룬다.
CONVERSION_TRIGGER_RE = re.compile(r"목표기준가격\([^)]*?([\d,]+)\s*원\s*이상\)")
# "운용전환일 전/후로 수수료가 나뉜다"는 표는 "구분" 칸에 "최초설정일부터
# 운용전환일 전일까지"/"운용전환일부터 해지일까지"라는 문구를 직접 적어
# 둔다(KR5147430065 실측) - 숫자 개수·줄 간격만으로 추측하지 않고 이
# 문구가 실제로 근처에 있는지로 확인한다.
PERIOD_LABEL_RE = re.compile(r"최초설정일|운용전환일|해지일")
# 위 "구분" 칸 문구는 클래스명 칸과 같은 x좌표 구간(왼쪽)에 찍히는 경우가
# 있어("최초설정일부터"가 줄바꿈으로 "최초설정일부"/"터"로 쪼개짐 -
# KR5147430065 실측), evidence의 클래스명을 만들 때 이 문구를 걸러내지
# 않으면 "구분 총보수 수수료 최초설정일부 터 운용전환일 수수료선취- 전일까지
# 오프라인형(A)"처럼 클래스명과 구분 칸 문구가 뒤섞여 보인다(사용자 지적).
# "터"는 "부터"가 줄바꿈으로 쪼개진 조각이라 단어 자체엔 문맥이 없지만,
# 실제 클래스명(수수료선취/미징구/후취-오프라인/온라인...)에는 "터" 한
# 글자짜리 토큰이 나올 일이 없어 안전하게 같이 걸러낸다.
PERIOD_COLUMN_WORD_RE = re.compile(r"최초설정일|운용전환일|해지일|전일까지")
PERIOD_COLUMN_LONE_WORDS = {"터"}
# 클래스명이 줄바꿈될 때, 위/아래로 넓히는 과정에서 표 자체의 칸 이름(헤더)
# 줄까지 같이 끌려 들어오는 경우가 있다(예: KR518101002M 실측 - 표의 첫
# 클래스 행은 위로 넓혀도 "납입금"이 안 나오니 MAX_EXTRA_LINES까지 계속
# 올라가다가 "클래스종류/판매수수료/총보수·비용/1년~10년" 헤더 줄까지
# 포함해버려 evidence 클래스명이 "판매 총보수 수수료 수수료미징구-..."처럼
# 헤더 단어가 섞여 나온다 - 사용자가 KR5147430065에서 이 현상을 지적해
# 다른 문서도 전수 확인해보니 총 26건에서 같은 문제가 있었다). 헤더 줄은
# 실측 문서들에서 전부 "클래스/종류/구분/판매/수수료/판매보수/총보수/
# 보수/비용/동종유형/N년" 같은 정해진 칸 이름 단어로만 이루어져 있고 실제
# 클래스명 글자(수수료선취-오프라인(A) 등)가 섞이는 일이 없어, 그 줄의
# 모든 단어가 이 칸 이름 집합(+숫자)에 속할 때만 "헤더 줄"로 판단한다 -
# 클래스명이 조금이라도 섞인 줄은 걸리지 않도록 보수적으로 잡는다.
HEADER_LABEL_TOKENS = {
    "클래스", "종류", "(클래스)", "구분",
    "판매", "수수료", "판매보수", "판매수수료",
    "총보수", "보수", "비용", "년",
    "총보수·", "총보수ㆍ", "ㆍ비용", "·비용",
    "총보수·비용", "총보수ㆍ비용", "동종유형",
}


def _is_header_row(l):
    non_empty = [w for w in l if w["text"].strip()]
    if not non_empty:
        return False
    for w in non_empty:
        t = w["text"]
        if t in HEADER_LABEL_TOKENS or t.isdigit():
            continue
        return False
    return True


# "후취"(환매 시점에 떼는) 판매수수료 클래스는 "납입금액의 N%이내"가 아니라
# "OO시 환매시: 환매금액의 N%이내"처럼 판매수수료율의 기준을 "환매금액"으로
# 쓴다(KR5114420027 S클래스 실측: "3년 미만 환매시: 환매금액의 100분의 0.15
# 이내" - 이건 별개의 벌칙성 수수료가 아니라 이 클래스의 판매수수료 문구
# 자체다). 처음엔 "환매"가 들어간 줄을 별개의 조건문으로 보고 위/아래 확장의
# 경계로 아예 걷어냈는데, 그러면 정작 판매수수료 문구 자체가 통째로 빠져
# sales_commission_desc가 null이 돼버렸다(사용자가 evidence 이상하다고
# 지적해서 재확인 중 발견) - "납입금액"만 판매수수료 신호로 보던 기존 판정을
# "환매금액"도 같은 뜻으로 인정하도록 넓혀서 고쳤다(아래 사용처 참고).
# 다만 "3년/미/만/환/매/시" 같은 낱글자는 여전히 클래스명도 판매수수료
# 숫자도 아니므로, evidence 클래스명에 섞이지 않도록 이 문구가 있는 줄의
# 단어는 전부 "commission" 역할로 묶어서 보여준다(아래 role 분류 참고).
REDEMPTION_NOTE_RE = re.compile(r"환매")
CLASS_NAME_START_RE = re.compile(r"^수수료(선취|미징구|후취)")
# "환매금액의 N%이내"엔 원래 "OO년 미만 환매시:"라는 조건이 붙어 있다(위
# 실측 - 3년을 채우기 전에 환매하면 벌칙성으로 이 수수료가 붙는다는 뜻).
# 이 조건 없이 "환매금액의 N%이내"만 남기면 무조건 떼는 수수료처럼 보여서
# 뜻이 달라진다(사용자 지적: "3년미만 환매시인데 이건 언급이 없는데
# 있어야하는거 아닌가"). 글자 사이에 공백이 낀 채로도(letter-spacing)
# 찾도록 각 글자 사이에 \s*를 둔다.
REDEMPTION_CONDITION_RE = re.compile(r"(\d+)\s*년\s*미\s*만\s*환\s*매\s*시")


def _is_note_row(l):
    non_empty = [w for w in l if w["text"].strip()]
    if not non_empty:
        return False
    text = "".join(w["text"] for w in non_empty)
    if CLASS_NAME_START_RE.match(text):
        return False
    # "100분의 0.15 이내"처럼 "환매"라는 낱말 없이 판매수수료 비율만 나오는
    # 줄도 있다(위 KR5114420027 S클래스의 같은 문구가 줄바꿈으로 갈라진
    # 다음 줄 - "10"/"0분"/"의"/"0"/".1"/"5"/"이"/"내"). 이 조각들 하나하나는
    # _word_role의 어떤 패턴에도 안 걸려("0분"/"이"/"내" 등은 숫자도 "%"도
    # "이내" 전체 토큰도 아님) 기본값(class_name)으로 새어나간다. "100분의"
    # 패턴 자체가 이미 판매수수료 신호이므로(BUNUI_RE) 같이 잡는다.
    if REDEMPTION_NOTE_RE.search(text) or BUNUI_RE.search(text):
        return True
    # "이내"가 데이터 행을 사이에 두고 앞뒤로 갈라지면서("...이" / [데이터
    # 행] / "내") 뒤쪽 "내"만 뚝 떨어진 별도 줄로 남는 경우가 있다
    # (KR5114420016/KR5114420027 실측 - 클래스명 뒤에 "이 내"가 덧붙어
    # 보였다). 줄에 "이"/"내" 말고 다른 글자가 없으면 그 "이내" 잔여
    # 조각으로 본다.
    return text in ("이", "내", "이내")


# 판매수수료 칸은 숫자가 아니라 정형화된 문구("없음" 또는 "납입금액의 N%[ ]이내")인데,
# "납입금액의"와 "N%이내"가 셀 줄바꿈 때문에 서로 다른 줄(그 사이에 다른 칸 텍스트가
# 끼어든 상태)로 떨어져 있는 경우가 많아 하나의 정규식으로는 못 잡는다. "이내"까지
#3줄로 쪼개지는 경우도 있어("납입금" / "액의 N%" / "이내") 퍼센트 숫자만 여기서
# 찾고, "이내"가 바로 붙어 있을 필요는 없다고 본다 (이 좁은 윈도우 안의 "%"는
# 사실상 판매수수료율 말고는 나올 데가 없다).
SALES_COMMISSION_PCT_RE = re.compile(r"([\d.]+)\s*%")
# "N%" 대신 "100분의 N"(=N/100, 같은 뜻)으로 표기하는 문서가 있다(KR5114420027).
BUNUI_RE = re.compile(r"100\s*분의\s*([\d.]+)")


def cluster_lines(words, tol=2.5):
    words = sorted(words, key=lambda w: w["top"])
    lines = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


# "총보수·비용" 칸의 가운뎃점 연결자가 문서마다 다른 글자로 나온다
# (ㆍ/▪/･/· 뿐 아니라, KR5111450067처럼 임베딩 폰트가 유니코드 사용자
# 영역(PUA) 글자 ""로 대체해 나오는 경우도 실측으로 확인함) - "한글도
# 공백도 아닌 글자 하나 + 비용"으로 넓게 잡되, 단순히 본문 어딘가에 홀로
# 나오는 "비용"이라는 단어(예: 표 위 설명문 "총보수 및 비용")까지 걸리면
# 안 되므로 그 연결자 글자가 반드시 붙어 있어야 한다("비용" 단독 토큰은
# 이 글자 수 요건에 안 걸림).
HAS_COST_COLUMN_RE = re.compile(r"^(?:총보수)?[^가-힣\sA-Za-z0-9]비용$")


def page_has_cost_column_header(words, lines):
    """표에 "총보수ㆍ비용"(총보수+판매보수+동종유형총보수를 더한 결과) 칸이
    아예 없는 문서가 있다(KR5194450018 실측: 헤더가 총보수/판매보수/
    동종유형총보수 3개뿐, "총보수ㆍ비용" 헤더 자체가 없음). 이 경우 데이터
    행도 소수 3개(총보수/판매보수/동종유형총보수)만 나오는데, 기존 로직은
    "소수 3개=총보수/판매보수/총보수ㆍ비용(동종유형총보수 없음)"으로 가정해
    왔던 것과 똑같은 개수라 구분이 안 되고, 세 번째 소수(동종유형총보수)를
    총보수ㆍ비용으로 잘못 읽어 "총보수ㆍ비용 < 총보수" 같은 앞뒤가 안 맞는
    값이 나왔다. 헤더에 "ㆍ비용"류 표기(가운뎃점+비용, "총비용예시"의
    "총비용"과는 다름 - 그쪽은 가운뎃점이 없음)가 있는지로 이 칸의 존재
    여부를 확인한다.

    주의: 페이지 전체에서 찾으면 오탐이 난다 - KR5194450018은 표 헤더엔
    "총보수ㆍ비용"이 없는데도, 표 한참 아래(주석 "(주3)/(주4)" 문단, top
    ~380~450)에서 "총보수·비용비율은"/"총보수·비용" 같은 설명 문구로
    우연히 다시 등장해 실측으로 오탐을 확인했다. 표 헤더는 항상 데이터
    행(소수 3개 이상 있는 첫 줄)보다 위에 있고 주석은 항상 그 아래에
    있으므로, 첫 데이터 행보다 위쪽(top이 더 작은 영역)에서만 찾는다."""
    first_data_top = None
    for line in lines:
        if sum(1 for w in line if DECIMAL_RE.match(w["text"])) >= 3:
            first_data_top = line[0]["top"]
            break
    header_words = words if first_data_top is None else [w for w in words if w["top"] < first_data_top]
    return any(HAS_COST_COLUMN_RE.search(w["text"]) for w in header_words)


def page_cost_projection_years(words, lines):
    """비용예시가 보통 5개년(1/2/3/5/10년)인데, 기간별로 수수료율이 바뀌는
    문서(운용전환일 전/후로 수수료가 달라지는 구조 - KR5147430065 실측)는
    3개년(1/2/3년)뿐인 경우가 있다. 헤더에서 "5년"이 있는지로 판별한다
    (위 has_cost_column과 같은 이유로 첫 데이터 행보다 위쪽에서만 찾는다)."""
    first_data_top = None
    for line in lines:
        if sum(1 for w in line if DECIMAL_RE.match(w["text"])) >= 3:
            first_data_top = line[0]["top"]
            break
    header_words = words if first_data_top is None else [w for w in words if w["top"] < first_data_top]
    header_text = "".join(w["text"] for w in header_words)
    if "5년" in header_text:
        return ["1y", "2y", "3y", "5y", "10y"]
    if "3년" in header_text:
        return ["1y", "2y", "3y"]
    return None


def find_fee_rows_on_page(page, page_num, has_cost_column, next_page_head_lines=None, cost_years=None):
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    lines = cluster_lines(words)
    cost_years = cost_years or ["1y", "2y", "3y", "5y", "10y"]

    def _nearby_has_period_label(center, span=3):
        # "% 있는데 정수가 근처에 없는 줄"이 정말 "운용전환일 전/후 기간
        # 구분" 표 때문인지, 그냥 숫자 개수/줄 간격만으로 추측하지 않고
        # "구분" 칸에 실제로 적히는 문구("최초설정일"/"운용전환일"/
        # "해지일")가 근처에 있는지로 확인한다(사용자 지적: 표지 없이
        # 순전히 개수·간격만 보면 다른 문서에서 우연히 오탐할 위험이 있음
        # - 이 라벨 문구가 있으면 그 위험이 사실상 없어진다).
        lo, hi = max(0, center - span), min(len(lines), center + span + 1)
        nearby = "".join(w["text"] for k in range(lo, hi) for w in lines[k])
        return bool(PERIOD_LABEL_RE.search(nearby))

    rows = []
    for i, line in enumerate(lines):
        # decimals를 NUM_RE로 거른 뒤 다시 추리면 "1.18%"처럼 %가 붙은 토큰이
        # NUM_RE(퍼센트 미허용)에 애초에 안 걸려 통째로 빠진다 - line에서 직접
        # 따로 찾는다.
        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        int_like = [w for w in line if NUM_RE.match(w["text"]) and w not in decimals]

        # 수수료 %(소수)와 비용예시 정수가 아예 다른 줄(y좌표)에 떨어져
        # 있는 문서가 있다(KR5147430065 실측: "운용전환일" 전/후로 클래스당
        # 수수료가 두 번 나오는 구조인데, 첫 번째 시기 줄엔 %만 있고, 그
        # 2줄 아래 별도 줄에 정수만 있음 - "최초설정일부"/"터 운용전환일
        # 0.443% ... 0.443%"/"수수료선취- 납입금액의"/"전일까지 145 192
        # 241"). 이 줄 자체엔 정수가 모자라면(소수는 있는데) 바로 아래
        # 몇 줄 안에서 소수 없이 정수만 있는 줄을 찾아 빌려온다 - 그런
        # 줄이 없으면(대부분의 다른 문서) 원래대로 아무 효과 없다.
        if decimals and len(int_like) < 3 and _nearby_has_period_label(i):
            for k in range(i + 1, min(i + 4, len(lines))):
                cand_line = lines[k]
                if any(DECIMAL_RE.match(w["text"]) for w in cand_line):
                    break
                cand_nums = [w for w in cand_line if NUM_RE.match(w["text"])]
                if len(cand_nums) >= 3:
                    int_like = cand_nums
                    break

        # 판매수수료("납입금액의 N%이내") 문구의 퍼센트 숫자가 데이터 줄
        # 자체에 얹혀 나오는 서식이 있다(KR5123490013 등: "의 0.8% 0.845
        # 0.40 0.75 0.868 ..." - "납입금액"은 윗줄, "의 N%"만 이 줄에 걸침).
        # 이 %값도 DECIMAL_RE에 걸려 decimals 맨 앞에 끼어들면서 실제 4개
        # 컬럼(총보수/판매보수/동종유형총보수/총보수·비용)이 통째로 한 칸씩
        # 밀려 읽힌다(총보수 자리에 판매수수료%가, 판매보수 자리에 실제
        # 총보수가... 실측으로 확인, 마지막 총보수·비용 값은 아예 유실됨).
        # 실제 컬럼은 최대 4개뿐이므로 소수가 5개면 맨 왼쪽은 무조건 이
        # 판매수수료% 이다(모호할 수 없음 - 드롭). 소수가 4개인 경우는
        # 정상적인 "4개 다 실제 컬럼" 케이스와 개수가 같아 구분이 안 되는데,
        # 실측 사례(KR5114420016)에서 이땐 맨 앞 소수 바로 앞/뒤에 "의"
        # (납입금액"의") 또는 "이내"가 같은 줄에 붙어 있어 그걸로 가려낸다.
        # 단, 이 "의"/"이내" 인접 판정만으로는 오탐이 난다(KR5113420069:
        # "취-오프 액의 0.3910 ..."에서 "액의" 바로 뒤가 하필 진짜 총보수
        # 값이었음, 진짜 판매수수료%는 다음 줄 "0.02%"였음 - 실측으로 확인).
        # 판매수수료 스트레이 값은 반드시 "%"가 붙어 있는 반면(퍼센트 값이므로)
        # 총보수 등 실제 컬럼 값은 이 데이터 줄 자체에서는 "%" 없이 나온다는
        # 점으로 추가 필터링한다(맨 왼쪽 소수 자신이 "%"로 끝나야만 스트레이
        # 후보로 본다).
        if len(decimals) == 5 and decimals[0]["text"].endswith("%"):
            decimals = decimals[1:]
        elif len(decimals) == 4 and decimals[0]["text"].endswith("%"):
            idx0 = next((idx for idx, w in enumerate(line) if w is decimals[0]), -1)
            prev_w = line[idx0 - 1] if idx0 > 0 else None
            next_w = line[idx0 + 1] if 0 <= idx0 + 1 < len(line) else None
            if (prev_w and prev_w["text"].endswith("의")) or (next_w and next_w["text"] == "이내"):
                decimals = decimals[1:]

        # 판매수수료 문구가 "%" 대신 "100분의 N"(=N/100, 같은 뜻)으로 나오는
        # 문서가 있다(KR5114420027 실측: "납입금액의 100분의 0.3 이내" - 이
        # "0.3"엔 "%"가 안 붙어서 위의 % 기반 판별에 안 걸리고, 총보수 등
        # 진짜 4개 컬럼 앞에 끼어들어 전부 한 칸씩 밀리는 같은 종류의 문제를
        # 일으켰다). 맨 왼쪽 소수 바로 앞 토큰이 "...분의"로 끝나면(그
        # 사이에 낀 판매수수료 숫자라는 뜻) 마찬가지로 드롭한다.
        if len(decimals) >= 4:
            idx0 = next((idx for idx, w in enumerate(line) if w is decimals[0]), -1)
            prev_w = line[idx0 - 1] if idx0 > 0 else None
            if prev_w and prev_w["text"].endswith("분의"):
                decimals = decimals[1:]

        # 일부 문서(KR5169950018 등)는 네 번째 소수(총보수·비용)의 소수점이
        # 쉼표로 잘못 찍혀 나온다("1.807"이어야 할 값이 "1,807"로 나와서
        # 정수(비용예시)로 오인됨) - 그러면 소수 3개+정수 6개(정상은 4개+5개)
        # 라는 특이한 개수 조합이 되는데, 이때만 좁게 판별해서 정수 목록의
        # 첫 값을 다시 소수로 되돌린다. 총보수(decimals[0])보다 약간 큰
        # 값이어야 한다는 조건까지 걸어(총보수·비용은 총보수+기타비용이라
        # 항상 총보수 이상) 진짜 큰 비용예시 정수(예: "1,937")를 잘못
        # 건드리지 않게 한다.
        # 판매보수(3번째 열)가 소수점 없이 정수 하나로만 나오는 경우가 있다
        # (KR5153420318/KR5153450785 실측: "1" - 원본 PDF 글자 자체가 그렇게
        # 찍혀 있음, 추출 오류 아님 - 같은 줄 다른 숫자들과 폰트/크기 동일함을
        # 확인). 판매보수 칸이 소수 목록에서 통째로 빠지면 그 다음 소수(동종
        # 유형총보수)를 판매보수로 잘못 읽고, 정수였던 "1"은 1년 비용예시
        # 자리로 잘못 흘러들어간다. 총보수(decimals[0])와 그 다음 소수
        # (decimals[1], 정상 문서라면 판매보수 그 자체) 사이 x좌표에 낀 정수
        # 토큰이 정확히 하나 있으면(비용예시 정수들은 훨씬 오른쪽에 있어
        # 안 걸림) 그게 판매보수라고 보고 소수 목록 제자리에 되돌린다.
        if len(decimals) >= 2:
            between = [
                w for w in int_like
                if decimals[0]["x1"] < w["x0"] < decimals[1]["x0"]
            ]
            if len(between) == 1:
                decimals = [decimals[0], between[0]] + decimals[1:]
                int_like = [w for w in int_like if w is not between[0]]

        if len(decimals) == 3 and len(int_like) == 6:
            m = re.match(r"^(\d),(\d{3})$", int_like[0]["text"])
            if m:
                candidate = float(f"{m.group(1)}.{m.group(2)}")
                if candidate >= float(decimals[0]["text"].rstrip("%")):
                    fixed_word = dict(int_like[0])
                    fixed_word["text"] = f"{m.group(1)}.{m.group(2)}"
                    decimals = decimals + [fixed_word]
                    int_like = int_like[1:]

        # 총보수·비용 칸도 없고(has_cost_column=False) 동종유형총보수까지
        # "-"인 문서가 있다(KR5116501001 실측: 판매수수료도 "-", 총보수/
        # 판매보수만 진짜 소수, 동종유형총보수도 "-") - 이러면 소수가 2개
        # (총보수/판매보수)뿐이라 기존 3개 기준에 걸려 이 문서 전체가
        # 통째로 빠지고 있었다("데이터 100건 중 97건만 나온다"고 사용자가
        # 지적해서 발견). 소수 2개까지는 허용하되, 이 행이 진짜 총보수
        # 표의 데이터 행이라는 걸 더 확실히 하기 위해(엉뚱한 텍스트가
        # 우연히 소수 2개+정수 4개를 만족하는 오탐 방지) 총보수 앞쪽에
        # "-"(판매수수료 없음 표시) 단독 토큰이 있을 때만 허용한다 - 이
        # 문서에서 실측으로 확인된 실제 패턴과 동일.
        if len(decimals) == 2:
            has_leading_dash = any(
                w["text"] == "-" and w["x0"] < decimals[0]["x0"] for w in line
            )
            # 위 리딩 대시 기준은 KR5116501001(판매수수료="-", 동종유형
            # 총보수="-")에 맞춰 만든 것인데, 소수 2개짜리 행이 이 패턴
            # 하나만 있는 게 아니었다(KR5194450018 실측: 12개 클래스 중
            # 7개(W/F/S/RP/RP-e/S-P/CP)가 판매수수료="없음"이거나 빈칸,
            # 동종유형총보수는 뒤쪽 "-"이거나 아예 빈칸이라 리딩 대시가
            # 없어서 통째로 빠지고 있었다 - 사용자가 "클래스 없는것들이
            # 너무 많다"고 지적해서 발견). 리딩 대시가 없어도, 바로
            # 위/아래(±2줄) 근처에 클래스명 시작 패턴("수수료선취/
            # 미징구/후취-")이 있으면 진짜 총보수 표의 데이터 행으로
            # 본다(엉뚱한 문장이 우연히 소수 2개+정수 4개를 만족해도
            # 그 근처에 클래스명이 있을 리는 없어 오탐 위험이 낮다).
            nearby_class_name = any(
                CLASS_NAME_START_RE.match("".join(w["text"] for w in lines[k]))
                for k in range(max(0, i - 2), min(len(lines), i + 3))
            )
            if not has_leading_dash and not nearby_class_name:
                continue
        elif len(decimals) == 1:
            # 총보수만 진짜 소수고 판매보수·동종유형총보수가 둘 다 "-"인
            # 행도 있다(KR5194450018 W클래스 실측: "0.765 - - 78 161
            # 247 433 986" - 총보수 뒤에 대시가 두 개 연달아 나옴). 위와
            # 같은 클래스명 인접 여부로 진짜 데이터 행인지 확인한 뒤,
            # 총보수 뒤·비용예시 정수 앞 구간의 "-" 토큰들을 순서대로
            # 판매보수/동종유형총보수 자리로 채운다.
            nearby_class_name = any(
                CLASS_NAME_START_RE.match("".join(w["text"] for w in lines[k]))
                for k in range(max(0, i - 2), min(len(lines), i + 3))
            )
            if not nearby_class_name:
                continue
            right_bound = int_like[0]["x0"] if int_like else float("inf")
            dashes_after = sorted(
                (w for w in line if w["text"] == "-" and decimals[0]["x1"] < w["x0"] < right_bound),
                key=lambda w: w["x0"],
            )
            if not dashes_after:
                continue
            # 실제 대시 단어 객체를 그대로 써야(x0/x1 좌표 포함) 아래
            # 열 배정 로직이 그 좌표를 다시 참조해도 안전하다.
            decimals = [decimals[0]] + dashes_after[:2]
        elif len(decimals) < 1:
            continue
        if len(int_like) < min(4, len(cost_years)):
            # "운용전환일" 전/후로 수수료가 바뀌는 문서(위 참고, KR5147430065)는
            # 전환 후 시기도 소수(%) 4개는 멀쩡히 있는데 비용예시 정수가
            # 원본 자체에 없다(사용자가 원본 표를 캡처해서 확인 - 전환
            # 후 줄엔 정말 %만 있고 1/2/3년 비용예시는 전환 전 줄 것 하나만
            # 공유됨). 이 값도 버리지 말고 바로 앞에서 찾은 행(같은 클래스의
            # 전환 전 값)에 "전환 후" 값으로 덧붙인다 - 페이지 안에서 아주
            # 가까운 줄에 있을 때만(다른 클래스와 헷갈릴 위험 방지).
            if (
                len(decimals) == 4 and rows
                and (i - rows[-1].get("_row_line_idx", -99)) <= 8
                and _nearby_has_period_label(i)
            ):
                rows[-1]["total_fee_after_conversion"] = decimals[0]["text"].rstrip("%")
                rows[-1]["distribution_fee_after_conversion"] = decimals[1]["text"].rstrip("%")
                rows[-1]["peer_avg_fee_after_conversion"] = decimals[2]["text"].rstrip("%")
                rows[-1]["total_fee_and_cost_after_conversion"] = decimals[3]["text"].rstrip("%")
            continue

        # 운용전문인력(운용역) 표 행이 같은 블록에 섞여 있을 수 있다 - 생년
        # (19xx/20xx, 단독 숫자)이나 콤마 있는 큰 수(운용규모)가 라벨 자리에
        # 있으면 그 표로 보고 제외한다(수익률 표에서 실제로 겪은 문제와 동일 -
        # "김혜용 1980 8개 55.78% ..."이 총보수 55.78%인 것처럼 잘못 뽑힘).
        pre_text_words_check = [w for w in line if w["x0"] < decimals[0]["x0"]]
        if any(re.fullmatch(r"(19|20)\d{2}", w["text"]) for w in pre_text_words_check):
            continue
        if any(re.fullmatch(r"\d{1,3},\d{3}", w["text"]) for w in pre_text_words_check):
            continue

        # 열 순서: [클래스종류] [판매수수료] 총보수 판매보수 동종유형총보수 총보수·비용
        #          1년 2년 3년 5년 10년  (동종유형총보수는 '-'로 빠질 수 있어 소수 3개까지 허용)
        has_peer_avg = len(decimals) >= 4
        total_fee, distribution_fee = decimals[0], decimals[1]
        if has_peer_avg:
            peer_avg_fee = decimals[2]
            total_fee_and_cost = decimals[3]
        elif len(decimals) == 2:
            # 총보수·비용 칸도 없고 동종유형총보수도 "-"인 문서(위 참고) -
            # 판매보수 뒤, 비용예시 정수들 앞 구간에 단독 "-"가 있으면
            # 동종유형총보수가 "-"로 확인된 것으로 본다.
            total_fee_and_cost = None
            right_bound = int_like[0]["x0"] if int_like else float("inf")
            dash_between = [
                w for w in line
                if w["text"] == "-" and distribution_fee["x1"] < w["x0"] < right_bound
            ]
            peer_avg_fee = "-" if dash_between else None
        elif not has_cost_column:
            # 이 페이지엔 "총보수ㆍ비용" 칸 자체가 없다(위 has_cost_column
            # 참고) - 소수 3개는 총보수/판매보수/동종유형총보수이고
            # 총보수ㆍ비용은 원본에 없는 정보라 null로 둔다(하이픈으로 확인된
            # 부재가 아니라 애초에 그 칸이 없는 것 - null이 맞다).
            peer_avg_fee = decimals[2]
            total_fee_and_cost = None
        else:
            total_fee_and_cost = decimals[2]
            # 동종유형총보수 칸이 원본에 "-"로 명시돼 있으면(정보가 없다는
            # 걸 실제로 확인한 것) null이 아니라 "-"로 남긴다 - 못 찾은 것과
            # 원본이 실제로 "-"라고 밝힌 건 다른 의미다(사용자가 지적함).
            dash_between = [
                w for w in line
                if w["text"] == "-" and distribution_fee["x1"] < w["x0"] < total_fee_and_cost["x0"]
            ]
            peer_avg_fee = "-" if dash_between else None
        cost_projection = {
            y: int_like[idx]["text"] for idx, y in enumerate(cost_years) if idx < len(int_like)
        }

        pre_text_words = [w for w in line if w["x0"] < total_fee["x0"]]
        class_part1 = " ".join(w["text"] for w in pre_text_words)

        # 클래스 코드와 판매수수료 문구는 이 줄 또는 인접한 줄(줄바꿈으로 나뉜 셀)에
        # 걸쳐 있을 수 있어서, 이 줄 기준 앞뒤로 창을 넓혀서 찾는다.
        #
        # 판매수수료 문구가 "납입금"/"액의 N%"/"이내" 3줄로 나뉘어 데이터 줄
        # 앞뒤로 2줄 넘게 걸치는 경우가 실측으로 확인됐다(KR510902511M A-e:
        # "납입금"(2줄 위)/"액의"(1줄 위)/[데이터]/"0.5%"(1줄 아래)/"이내"
        # (2줄 아래) - 총 5줄). 그렇다고 무작정 고정폭(±2 등)으로 넓히면
        # 바로 옆 클래스 행의 판매수수료를 잘못 가져오는 더 나쁜 문제가
        # 생긴다 - 실측으로 두 가지 경로를 확인했다: (a) 옛날에 확인한 "C
        # 클래스가 A 클래스의 0.10%이내를 잘못 가져옴", (b) 이번에 새로
        # 확인한 KR5114420027 - 이 문서는 클래스 한 줄에 줄바꿈 없이 값이
        # 다 붙어 나오는 서식인데, 그래도 바로 위/아래 줄이 "다른 클래스의
        # 완전한 데이터 행 그 자체"라서(줄바꿈이 아예 없으니 인접 줄=다른
        # 클래스 전체), 그걸 무조건 한 줄까지는 포함하던 기존 로직이 그
        # 다른 클래스의 %값을 그대로 판매수수료로 잘못 집어왔다(A가 C의
        # "0.4500%"를, C가 A의 "0.3000%"를 서로 잘못 가져옴).
        #
        # 그래서 "다른 클래스의 완전한 데이터 행(소수 3개 이상)"은 바로
        # 인접한 한 줄이라도 절대 포함하지 않는다(포함 여부 자체를 먼저
        # 판단) - 그 다음에야, 포함하기로 한 범위 안에서 "납입금"/"이내"을
        # 찾을 때까지, 역시 그런 완전한 데이터 행이나 클래스 코드 괄호
        # "(...)"가 있는 줄(다른 클래스명의 마지막 조각)을 만나기 전까지만
        # 늘려간다.
        def _is_full_data_row(l):
            return sum(1 for w in l if DECIMAL_RE.match(w["text"])) >= 3

        def _has_class_paren(l):
            text = " ".join(w["text"] for w in l)
            if CLASS_CODE_RE.search(text):
                return True
            # 글자를 한 자씩 따로 찍는 서식(letter-spacing)이 있는 문서는
            # "(Ce)"가 "(", "Ce", ")"처럼 별도 단어로 쪼개져 나와 join한
            # 텍스트에 공백이 끼어(예: "( Ce )") 위 정규식이 못 잡는다
            # (KR5114420022 실측: 이 탓에 닫는 괄호 줄을 경계로 못 보고
            # 계속 아래로 넓혀가다 다음 각주 문단까지 클래스명에 끌려
            # 들어왔다). 공백을 다 지운 버전으로도 한 번 더 확인한다.
            if CLASS_CODE_RE.search(text.replace(" ", "")):
                return True
            # "(Cp(퇴직연금))"처럼 코드 뒤에 괄호가 중첩되는 문서(위
            # CLASS_CODE_NESTED_RE 참고)는 여는 괄호가 두 번 나오고 닫는
            # 괄호가 한 번에 다 안 붙어 있어서 위 CLASS_CODE_RE로는(공백을
            # 지워도) 못 잡는다 - 사이에 낀 한글 설명 때문에 "괄호 안이
            # 전부 영숫자"라는 전제가 깨짐(KR5114420027 실측: 이 탓에 Cp의
            # 닫는 괄호 줄을 경계로 못 보고 다음 클래스(Cpe) 이름 시작까지
            # 서로 끌고 들어왔다). 이런 줄도 중첩 코드 정규식으로 확인한다.
            if CLASS_CODE_NESTED_RE.search(text) or CLASS_CODE_NESTED_RE.search(text.replace(" ", "")):
                return True
            # 일부 문서(KR5125450023)는 클래스 코드가 "A(수수료선취-...오프
            # 라인)"처럼 여는 괄호가 클래스 행 자체에, 닫는 괄호만 다음 줄에
            # 따로 떨어져 나온다("오프라인)") - 이 경우 CLASS_CODE_RE(여닫는
            # 괄호가 한 줄에 다 있어야 매치)는 못 잡아서, 여는 괄호 없이
            # 닫는 괄호만 있는 줄도 "다른 클래스명의 마지막 조각"으로 본다.
            return ")" in text and "(" not in text

        MAX_EXTRA_LINES = 3

        def _has_word(l, word):
            # "납입금"은 "납입금액"(전화번호처럼 붙여 나온 문서도 있음)처럼
            # 뒤에 다른 글자가 붙는 경우가 있어(KR5127420083 실측), 정확히
            # 일치할 때만 찾으면 못 잡고 지나쳐 위/아래로 계속 넓혀버린다
            # (결국 표 헤더까지 evidence에 끼어드는 사고로 이어졌다) - 부분
            # 일치로 찾는다.
            if any(word in w["text"] for w in l):
                return True
            # 글자를 한 자씩 따로 찍는 서식(letter-spacing)이 있는 문서는
            # "이내"조차 "이"/"내"처럼 서로 다른 단어로 쪼개져 나와 단어
            # 하나씩만 봐서는 못 찾는다(KR5114420016 실측: 환매수수료
            # 문구의 "이내"가 쪼개져 있어 아래로 넓히는 걸 못 멈추고 다음
            # 각주 문단까지 끌고 옴). 줄 전체를 이어붙인 텍스트로도 한 번
            # 더 확인한다.
            return word in "".join(w["text"] for w in l)

        # 바로 위/아래 한 줄은 줄바꿈된 이 행 자신의 클래스명 조각을 담기
        # 위해 일단 넣어본다(경계에 걸리지만 않으면).
        base_up = (
            [lines[i - 1]]
            if i - 1 >= 0
            and not _is_full_data_row(lines[i - 1])
            and not _has_class_paren(lines[i - 1])
            and not _is_header_row(lines[i - 1])
            else []
        )
        base_down = (
            [lines[i + 1]]
            if i + 1 < len(lines)
            and not _is_full_data_row(lines[i + 1])
            and not _is_header_row(lines[i + 1])
            else []
        )

        # 이 행 자신의 판매수수료가 이미 "없음"/"-"으로 결론 나 있는지는 이
        # 데이터 줄 자체가 아니라 줄바꿈된 자신의 라벨 줄(방금 넣어본 바로
        # 위/아래 한 줄)에 있을 수도 있다(KR5152420028 Ce 실측: "없음"이
        # 데이터 줄이 아니라 바로 위의 라벨 줄 "수수료미징구- 없음"에 있음).
        # 그래서 데이터 줄 + 바로 위/아래 한 줄까지 합쳐서 확인한다.
        own_row_no_commission = any(
            _has_word(wl, "없음") for wl in (base_up + [line] + base_down)
        ) or any(
            w["text"] == "-" and w["x0"] < total_fee["x0"] for w in line
        )

        # "없음"/"-"으로 이미 결론 난 행은 "납입금"을 더 찾아 위/아래로 넓힐
        # 이유가 없다(넓히면 다른 클래스 문구를 잘못 끌고 오는 사고만 남음 -
        # KR510902511M C1 실측). 그리고 바로 위/아래 한 줄조차, 데이터도
        # 없고 괄호도 없어 경계 판정엔 안 걸리지만 사실은 *다른* 클래스의
        # 판매수수료 문구 잔재("이내"/"납입금" 단독)일 수 있어(C-e 실측)
        # 그런 경우 아예 빼버린다.
        if own_row_no_commission:
            if base_up and _has_word(base_up[0], "이내"):
                base_up = []
            if base_down and _has_word(base_down[0], "납입"):
                base_down = []

        # 이 행 자신의 줄(class_part1)에 이미 괄호 닫힌 클래스 코드가 완전히
        # 있으면("수수료미징구-온라인(C-e)"처럼 한 줄에 다 있는 경우), 클래스명은
        # 이미 완성된 것이라 위/아래로 더 이어붙일 필요가 없다. 그런데도 바로
        # 아래 줄을 무조건 넣다 보니, 그게 사실은 *다음* 클래스의 이름 시작
        # 부분("수수료미징구-오프라인-개"처럼 아직 괄호가 안 나온 라벨 앞
        # 조각)인 경우 잘못 이어붙여 버렸다(C-e 실측: "수수료미징구-온라인
        # (C-e) 수수료미징구-오프라인-개"처럼 다음 클래스 이름이 붙어버림).
        # 클래스명은 항상 "수수료선취-"/"수수료미징구-"/"수수료후취-"로
        # 시작하므로, 이미 완성된 행에서 인접 줄이 이 패턴으로 새로 시작하면
        # 다음 클래스의 것으로 보고 뺀다.
        own_class_name_complete = bool(CLASS_CODE_RE.search(class_part1))
        if own_class_name_complete:
            # 글자를 한 자씩 따로 찍는 서식은 "수수료미징구"조차 "수수료미"/
            # "징"/"구"처럼 여러 단어로 쪼개져 나와서, 단어 하나하나를 이
            # 패턴과 비교하면(예전 방식) 매칭되는 단어가 하나도 없어 못
            # 걸러낸다(KR5114420027 Ce 실측: 다음 클래스(C-P)의 시작
            # "수수료미 징 구 -오 프 라인-개"가 안 걸러지고 그대로 끌려
            # 들어왔다). 줄 전체를 이어붙인 텍스트로 비교한다(표 왼쪽 여백
            # 캡션은 클래스명 칸보다 왼쪽(x0<70)이라 먼저 제외).
            down_text = "".join(w["text"] for w in base_down[0] if w["x0"] >= 70) if base_down else ""
            if down_text and CLASS_NAME_START_RE.match(down_text):
                base_down = []
            # "OO년 미만 환매시: 환매금액의..." 같은 조건문 줄은 항상 그
            # *다음* 클래스(아직 코드가 안 나온 쪽)의 것이다 - 이 행은
            # 클래스명+코드가 이미 이 줄에서 끝났으니 판매수수료도 이미
            # 이 줄에 다 있거나("납입금액의 N%이내"), 아예 "없음"이다.
            # 그런데도 바로 아래 조건문 줄을 계속 끌고 오면, 다음 클래스
            # 것인 "환매금액" 기준과 "OO년 미만" 조건이 엉뚱하게 이
            # 행에 붙어버린다(KR5114420016 R-A 실측: "수수료선취-오프라인
            # (R-A)"는 원래 "납입금액의 0.3%이내"인데, 바로 아래 S클래스의
            # "3년 미만 환매시: 환매금액의..." 조건문까지 끌려와서
            # "환매금액의 0.3%이내"로 잘못 나왔다 - 사용자가 "3년미만
            # 환매시 언급이 없는데 있어야 하는거 아니냐"고 물어서 고치다가
            # 발견).
            # 다만 이 행 자신의 줄에 판매수수료가 아직 안 나와 있으면(예:
            # KR5114420027 S클래스 - 이름+코드만 한 줄에 있고 "100분의
            # 0.15 이내"는 바로 아래 줄에 있음) 그 조건문 줄이야말로 이
            # 행의 진짜 판매수수료이므로 빼면 안 된다. "이 줄에 이미
            # 납입금/환매금액/% 판매수수료 신호가 있는지"로 구분한다.
            own_commission_already_on_line = bool(
                SALES_COMMISSION_PCT_RE.search(class_part1)
                or BUNUI_RE.search(class_part1)
                or "납입" in class_part1
                or "환매금액" in class_part1
            )
            if base_down and _is_note_row(base_down[0]) and own_commission_already_on_line:
                base_down = []
            if base_up and any(")" in w["text"] for w in base_up[0]):
                base_up = []

        # 판매수수료가 없다고 이미 결론난 행(own_row_no_commission)도
        # 클래스명 자체는 여러 줄에 걸쳐 나뉠 수 있다(KR5113450111 실측:
        # "수수료미"(2줄 위)/"징구-"(1줄 위)/[없음+데이터]/"개인연금"(1줄
        # 아래)/"(C)"(2줄 아래) - "없음"이 있다고 위/아래 확장을 아예 막아
        # 버리면 "수수료미"를 놓쳐 evidence의 클래스명이 "징구-..."로
        # 잘려 보인다). 그래서 확장 자체는 막지 않되, 그러다가 "이내"
        # (위쪽)/"납입금"(아래쪽)을 만나면 - 이 행 자신은 판매수수료가
        # 없다고 이미 확인됐으니 그건 무조건 다른(이웃) 클래스의 판매수수료
        # 문구 잔재다 - 포함하지 않고 그 자리에서 멈춘다.
        up_lines = list(base_up)
        found_napipgeum = any(_has_word(wl, "납입") for wl in up_lines)
        j = i - 2
        extra = 0
        while up_lines and j >= 0 and extra < MAX_EXTRA_LINES and not found_napipgeum:
            if (
                _is_full_data_row(lines[j])
                or _has_class_paren(lines[j])
                or _is_header_row(lines[j])
            ):
                break
            if own_row_no_commission and _has_word(lines[j], "이내"):
                break
            up_lines.insert(0, lines[j])
            found_napipgeum = _has_word(lines[j], "납입")
            extra += 1
            j -= 1

        down_lines = list(base_down)
        found_ianae = any(_has_word(wl, "이내") for wl in down_lines)
        stop_down = down_lines and _has_class_paren(down_lines[0])
        # 닫는 괄호가 있는 줄을 만나면 보통 "클래스명이 끝났다"는 뜻으로
        # 보고 거기서 멈추는데, 판매수수료 %가 그 닫는 괄호 줄보다도 더
        # 아래에 떨어져 나오는 문서가 있다(KR5185450009 실측: "...
        # 오프라인(A1)" 다음 줄에 "1.0%이내"가 옴 - "%"/"이내"가 있는데도
        # 그 앞줄에 이미 클래스 코드 괄호가 있다는 이유로 못 보고
        # 지나쳤다). 닫는 괄호를 봤어도 바로 다음 줄이 "이내"나 %값처럼
        # 보이면 아직 판매수수료 문구가 이어지는 것으로 보고 한 줄은 더
        # 열어준다.
        if stop_down and (i + 2) < len(lines):
            peek_text = "".join(w["text"] for w in lines[i + 2])
            if "이내" in peek_text or SALES_COMMISSION_PCT_RE.search(peek_text) or BUNUI_RE.search(peek_text):
                stop_down = False
        j = i + 2
        extra = 0
        while (
            down_lines and j < len(lines) and extra < MAX_EXTRA_LINES
            and not found_ianae and not stop_down
        ):
            if _is_full_data_row(lines[j]) or _is_header_row(lines[j]):
                break
            if own_row_no_commission and _has_word(lines[j], "납입"):
                break
            down_lines.append(lines[j])
            found_ianae = _has_word(lines[j], "이내")
            stop_down = _has_class_paren(lines[j])
            extra += 1
            j += 1

        # 클래스명/판매수수료 문구가 페이지 경계를 넘어가는 경우도 있다
        # (KR514X450008 Ae 실측: 데이터 줄 자체가 그 페이지의 마지막
        # 줄이라 "온라인형(Ae)"와 "0.5%이내"가 통째로 다음 페이지 첫
        # 줄로 넘어감). class_code 탐색은 이미 next_page_head_lines로
        # 이런 경우를 봐주고 있었지만(위 참고), evidence/판매수수료
        # 재구성 쪽은 이 페이지 안(`lines`)에서만 찾다 보니 이 행만
        # "클래스명: 수수료선취 –"처럼 끊긴 채로 남고 sales_commission_desc
        # 도 null이 됐다. 이 페이지 끝까지 갔는데도 아직 "이내"를 못
        # 찾았고(다른 클래스의 완전한 경계도 아직 안 만났다면) 다음
        # 페이지 머리글 후보를 이어서 본다.
        if (
            not found_ianae and not stop_down and not own_row_no_commission
            and extra < MAX_EXTRA_LINES and j >= len(lines) and next_page_head_lines
        ):
            for hl in next_page_head_lines:
                if extra >= MAX_EXTRA_LINES or _is_full_data_row(hl) or _is_header_row(hl):
                    break
                down_lines.append(hl)
                found_ianae = _has_word(hl, "이내")
                stop_down = _has_class_paren(hl)
                extra += 1
                if found_ianae or stop_down:
                    break

        # 표 왼쪽 여백(클래스명 칸보다도 왼쪽)에 세로로 찍힌 구간 캡션
        # ("투자비용" 등, x0≈27.8)이 y좌표가 데이터 행과 가까워 같은 줄로
        # 묶이는 경우가 있다. 클래스명 칸은 실측 사례들에서 전부 x0≈77.6
        # 부터 시작해서(수십 개 문서에서 일관됨) 이 캡션과는 확실히 구간이
        # 갈린다 - 처음엔 "그 줄 안에서 유독 멀리 떨어진 단어만" 걸렀는데,
        # 캡션이 클래스명 바로 옆(간격 13pt 정도)에 붙어 나오는 문서도 있어
        # (KR5153420105 실측) 그 조건으로는 놓치는 경우가 있었다. 그렇다고
        # 이 행 자신의 줄 최솟값을 기준으로 자르면(전에 시도) 그 줄에 클래스
        # 명이 없는 행(예: 대시만 있는 행)에서 다음 줄의 진짜 클래스명까지
        # 잘라내는 부작용이 있었다(C1 실측) - 그래서 문서 전체에서 일관되게
        # 관찰된 절대 좌표 기준(70pt)으로 고정한다.
        # 이 캡션의 x0가 문서마다 달라(대부분 27.8 근처인데 KR5185450009는
        # 74.4로 70pt 기준을 살짝 넘어서 안 걸러졌다 - "수수료선취-
        # 온라인(A-e) 투자비용"처럼 클래스명 뒤에 캡션이 그대로 붙어
        # 보였다) 절대좌표만으로는 모든 문서를 다 못 잡는다. "투자비용"은
        # 이 세로 캡션에서만 쓰는 고정 라벨이라(클래스명엔 나올 일이
        # 없음) 좌표와 무관하게 글자 자체로도 걸러낸다.
        def _strip_stray_caption(wl):
            return [w for w in wl if w["x0"] >= 70 and w["text"] != "투자비용"]

        window_lines = [_strip_stray_caption(wl) for wl in (up_lines + [line] + down_lines)]
        window_lines = [wl for wl in window_lines if wl]
        window_text = " ".join(" ".join(w["text"] for w in wl) for wl in window_lines)

        # evidence를 물리적 줄 순서 그대로 이어 붙이면 "클래스종류"/"판매수수료"가
        # 실제로는 서로 다른 칸(컬럼)인데도 마치 한 문장인 것처럼 뒤섞여 보인다
        # (사용자가 원본 표 캡처와 나란히 대조해서 지적함: "납입금 수수료선취-
        # 오프라인(A) 액의 1%"는 원본에서 "클래스종류" 칸과 "판매수수료" 칸이
        # 우연히 같은 y좌표 구간에 걸쳐 있어서 생기는 순서일 뿐, 실제로 섞여
        # 있는 게 아니다 - 그런데 그대로 이어 붙이면 마치 한 칸인 것처럼 보여서
        # 오해를 산다). 게다가 클래스명이 데이터 줄 앞/뒤로 쪼개지면 그 사이에
        # 낀 숫자들 때문에 두 조각이 evidence 안에서 뚝 떨어져 보인다. 그래서
        # 칸(클래스명 vs 판매수수료 vs 숫자데이터)별로 단어를 분리해 각각
        # 이어붙인 뒤, 칸 이름을 붙여 evidence를 구성한다 - 물리적 줄 순서가
        # 아니라 "논리적 칸" 순서로 보여준다.
        COMMISSION_MARKER_WORDS = {"이내", "없음"}
        COMMISSION_PCT_TOKEN_RE = re.compile(r"^[\d.]+%$")

        def _word_role(w):
            if w["x0"] >= total_fee["x0"]:
                return "data"
            # "운용전환일" 전/후로 수수료가 나뉘는 문서는 "구분" 칸(최초설정일부터/
            # 운용전환일부터/해지일까지 등)이 클래스명 칸과 같은 x좌표 구간에
            # 찍혀 있어서, 걸러내지 않으면 클래스명에 "최초설정일부 터
            # 운용전환일"처럼 구분 칸 문구가 섞여 들어간다(KR5147430065 실측,
            # 사용자 지적).
            if PERIOD_COLUMN_WORD_RE.search(w["text"]) or w["text"] in PERIOD_COLUMN_LONE_WORDS:
                return "period"
            # "납입금"/"납입금액"처럼 뒤에 "액"이 붙거나 안 붙거나 하는
            # 표기가 문서마다 달라서(KR5123490017 실측: "납입금액"이 한
            # 토큰) 부분 일치로 잡는다. "납입금"조차 "납입"/"금"으로 다시
            # 쪼개져 나오는 문서도 있어(KR5185450009 실측: "납입"이 데이터
            # 줄 2줄 위에 단독으로 떨어져 있는데, 이 2글자만으론 "납입금"
            # 부분일치에도 안 걸려 기본값(class_name)으로 샜다) "납입"까지만
            # 봐도 충분히 특정된다(클래스명에 이 글자가 나올 일이 없음).
            # "액의"/"금액의"처럼 "...의"로 끝나는 조각도 마찬가지로
            # "납입금액의"가 쪼개진 조각이라 접미사로 잡는다(클래스명은 이런
            # 조사로 끝나지 않아 오탐 위험이 낮다).
            if "납입" in w["text"] or w["text"].endswith("의"):
                return "commission"
            if w["text"] in COMMISSION_MARKER_WORDS or w["text"].endswith("이내"):
                return "commission"
            # 글자를 한 자씩 따로 찍는 서식은 "이내"조차 "이"/"내" 두 단어로
            # 쪼개져 나온다(KR5114420016 S클래스 실측: "0.15%이내"의 "이"/
            # "내"가 따로 떨어진 채 x0가 클래스명 칸 쪽이라 기본값(class_name)
            # 으로 새서 evidence에 "수수료후취-온라인슈퍼(S) 이 내"처럼 붙어
            # 보였다). 클래스명에 "이"/"내" 한 글자짜리 단독 토큰이 나올 일은
            # 없어 안전하게 판매수수료 쪽으로 본다.
            if w["text"] in ("이", "내"):
                return "commission"
            if COMMISSION_PCT_TOKEN_RE.match(w["text"]) or "%" in w["text"]:
                # "0.30%이내"처럼 %값과 "이내"가 공백 없이 한 토큰으로 붙어
                # 나오는 문서가 있다(KR5127420083 실측) - 클래스명 글자에는
                # "%"가 나올 일이 없어 "%"가 있으면 무조건 판매수수료 쪽으로
                # 본다.
                return "commission"
            if w["text"] == "-":
                return "commission"
            if w["text"].endswith("분의") or re.fullmatch(r"[\d.]+", w["text"]):
                return "commission"
            return "class_name"

        class_name_words = []
        commission_words = []
        for wl in window_lines:
            # "환매금액의 N%이내"류 문구가 있는 줄은 그 자체가 판매수수료
            # 문구라 줄 전체를 판매수수료 쪽으로 묶는다 - 그래야 "3년/미/
            # 만/환/매/시" 같은 낱글자가 기본값(class_name)으로 새서
            # evidence 클래스명에 섞이는 걸 막는다(위 REDEMPTION_NOTE_RE
            # 주석 참고).
            note_line = _is_note_row(wl)
            for w in wl:
                role = "commission" if note_line else _word_role(w)
                if role == "class_name":
                    class_name_words.append(w["text"])
                elif role == "commission":
                    commission_words.append(w["text"])
        class_name_full = " ".join(class_name_words) if class_name_words else None
        commission_raw = " ".join(commission_words) if commission_words else None
        # 클래스명/판매수수료만 남기고 총보수 등 숫자 데이터를 통째로 빼버리면
        # (원래는 실수로 빠졌었다 - 사용자가 "판매수수료만 보이는거야?"라고
        # 바로 지적함) total_fee/distribution_fee/peer_avg_fee/
        # total_fee_and_cost/cost_projection_per_10m을 원본과 대조 확인할
        # 방법이 없어진다. 이 행 자신의 줄(line)에서 "숫자데이터"로 분류된
        # 토큰만(클래스명/판매수수료 단어는 이미 위에서 따로 보여주므로
        # 여기서 또 반복하지 않는다) 순서대로 남긴다.
        data_text = " ".join(w["text"] for w in line if _word_role(w) == "data")

        # %가 숫자에 바로 붙는 서식(위 DECIMAL_RE 참고)에서는 총보수/판매보수
        # 값 자체도 "0.145%"처럼 "%"를 달고 있어서, 판매수수료 % 탐색에 이
        # 값들이 같이 걸려 있으면(예: 진짜 판매수수료 "0.1%"보다 총보수
        # "0.145%"가 텍스트상 먼저 나옴) 엉뚱한 숫자를 판매수수료로 오인한다.
        # 이 행 자신의 총보수류 값 토큰은 검색 대상에서 제외한다.
        decimal_ids = {id(w) for w in decimals}
        wide_text = " ".join(
            w["text"] for wl in window_lines for w in wl if id(w) not in decimal_ids
        )
        # "-"는 판매수수료가 "없음"이라는 뜻으로 쓰이기도 하는데, 클래스명 자체에
        # 하이픈("수수료선취-오프라인")이 들어있어 그냥 문자열에 "-"가 있는지만
        # 보면 항상 참이 된다. 그래서 "-"가 단독 토큰(그 칸에 딱 "-"만 있는 경우)
        # 으로 존재하는지를 봐야 한다.
        has_standalone_dash = any(
            w["text"] == "-" for wl in window_lines for w in wl
        )

        # 클래스 코드(괄호 안 텍스트)는 실측 사례들에서 항상 "이 줄" 또는 "다음 줄들"
        # 에서만 나타났다 - "이전 줄"은 위쪽 행(다른 클래스)의 이름 꼬리일 수 있어서
        # 잘못 가져다 쓸 위험이 있다 (KR514X450008에서 확인: 이전 줄의 클래스코드를
        # 엉뚱하게 가져와서 실제로는 다른 클래스인 행에 잘못 붙인 사례). 그래서
        # 클래스 코드는 이 줄 기준 아래쪽으로만 찾고, 이전 줄은 보지 않는다.
        #
        # 닫는 괄호가 데이터 줄 바로 다음 줄이 아니라 그보다 더 아래(클래스명이
        # 3줄 이상으로 나뉘는 경우 - 예: "오프라인- 없음 [데이터]" / "개인연금" /
        # "(C)", KR5113420012 실측)에 있는 경우가 있어서, 다른 클래스의 완전한
        # 데이터 행(경계)을 만나기 전까지는 몇 줄 더 내려가며 찾는다. 또한 일부
        # 문서는 글자 간격이 벌어진 폰트 때문에 괄호 안까지 공백이 끼어 나온다
        # ("( C- P)") - 정규식이 원래 공백을 허용 안 하므로, 못 찾으면 공백을
        # 지운 텍스트로 한 번 더 시도한다.
        def _try_class_code(text):
            m = CLASS_CODE_RE.search(text)
            if m:
                return m.group(1)
            # 공백을 지운 뒤 다시 찾아보는데, 판매수수료 칸 글자("없음")가
            # 클래스명 조각과 닫는 괄호 사이에 끼어 있으면 공백만 지웠을 때
            # 오히려 그 글자와 들러붙어버려 방해가 된다(KR5123420015 실측:
            # "-온라인(C- 없음 e)"를 그냥 공백만 지우면 "...C-없음e)"가 돼
            # 안 걸림) - "없음"을 먼저 빼고 공백을 지운다.
            cleaned = re.sub(r"\s+", "", text.replace("없음", " "))
            m = CLASS_CODE_RE.search(cleaned)
            if m:
                return m.group(1)
            # "(Cp(퇴직연금))"처럼 코드 뒤에 괄호가 중첩되는 경우, 안쪽까지
            # 통째로 코드로 삼아야 class_meaning·상세표와 같은 표기로
            # 이어진다(DETAIL_FEE_CLASS_CODE_NESTED_RE 참고) - 바깥 코드만
            # 자르면("Cp") 상세표가 뽑은 "Cp(퇴직연금)"과 다른 표기가 돼
            # 같은 클래스가 두 행으로 갈라진다(KR5114420027 실측: 요약표는
            # "Cp" 0.35%, 상세표는 "Cp(퇴직연금)" 0.35%로 값은 같은데
            # 코드만 갈려 두 클래스처럼 보였다).
            m = DETAIL_FEE_CLASS_CODE_NESTED_RE.search(cleaned)
            if m:
                return m.group(1)
            m = CLASS_CODE_NESTED_RE.search(cleaned)
            return m.group(1) if m else None

        # 여는 괄호와 닫는 괄호가 서로 다른 줄에 떨어져 있는 경우도 있다
        # (KR5123420039 실측: "(C-"가 데이터 줄+1, "e)"가 데이터 줄+2) -
        # 한 줄씩 따로따로만 보면 못 잡으므로, 줄을 누적해가며 본다(줄 하나
        # 추가할 때마다 매번 다시 시도).
        class_code = None
        accumulated = class_part1
        j = i + 1
        steps = 0
        while class_code is None and j < len(lines) and steps < 4:
            if _is_full_data_row(lines[j]):
                break
            accumulated += " " + " ".join(w["text"] for w in lines[j])
            class_code = _try_class_code(accumulated)
            j += 1
            steps += 1
        if class_code is None:
            # 데이터 줄 자신에서도(다음 줄이 전혀 없을 때) 시도해둔다.
            class_code = _try_class_code(class_part1)
        if class_code is None and next_page_head_lines:
            # 이 페이지 안에서 못 찾았으면, 표가 다음 페이지로 이어지면서
            # 클래스명의 닫는 괄호 조각이 다음 페이지 맨 앞줄로 넘어간 경우도
            # 확인한다(KR514X450008 실측: "온라인형(Ae)"가 다음 페이지 첫
            # 줄에 있어서, 이 페이지 안의 다음 줄(무관한 페이지 푸터)만 보면
            # 놓쳤다).
            next_page_text = " ".join(w["text"] for wl in next_page_head_lines for w in wl)
            class_code = _try_class_code(class_part1 + " " + next_page_text)
        if class_code is None:
            # "A(수수료선취-오프라인)"처럼 클래스 코드가 괄호 안이 아니라
            # 괄호 바로 앞에 붙는 문서도 있다(KR5125450023/KR5125450070
            # 실측 - 괄호 안은 클래스 코드가 아니라 상품유형 설명임). 보통은
            # 이 행 자신의 클래스명 첫 토큰에서 찾는데, 이 행 자신의 줄에는
            # 숫자와 판매수수료 칸의 "없음"만 있고(클래스명 첫 토큰 자리가
            # 아님) 클래스명 전체가 바로 윗줄에 있는 문서도 있다(같은 두
            # 문서에서 판매수수료가 "없음"인 클래스들 실측: 데이터 줄엔
            # "없음"+숫자뿐, "C(수수료미징구"는 바로 위 줄 전체). "없음"은
            # 클래스명이 아니므로 pre_text_words 첫 단어가 있어도 그게
            # "없음"이면 윗줄도 같이 후보로 본다 - 윗줄이 이미 다른
            # 클래스의 완전한 데이터 행이면(경계) 후보에서 뺀다.
            prefix_candidates = []
            if pre_text_words and pre_text_words[0]["text"] != "없음":
                prefix_candidates.append(pre_text_words[0]["text"])
            if i - 1 >= 0 and lines[i - 1] and not _is_full_data_row(lines[i - 1]):
                prefix_candidates.append(lines[i - 1][0]["text"])
            for cand in prefix_candidates:
                m3 = CLASS_CODE_PREFIX_RE.match(cand)
                if m3:
                    class_code = m3.group(1)
                    break

        if class_code is None:
            # 괄호로 감싼 클래스 코드 자체가 원본에 없는 문서도 있다
            # (KR5123365001 실측: 클래스가 애초에 하나뿐이라 "(A)" 같은
            # 코드 없이 "투자신탁"이라는 라벨 하나만 있음). 이럴 땐 코드를
            # 추측해서 지어내는 대신, 이 행 자신의 줄에 실제로 적힌 라벨
            # 글자(판매수수료 칸의 "없음"은 제외)를 그대로 클래스 이름으로
            # 쓴다 - 없는 값을 만들어내는 것보다 원본에 있는 걸 그대로
            # 옮기는 쪽이 "틀린 값 < 없는 값" 원칙에 맞다.
            label_words = [w["text"] for w in pre_text_words if w["text"] != "없음"]
            if label_words:
                class_code = " ".join(label_words)

        # "납입금액의"가 3줄로 쪼개지는 경우도 있다("납입금" / 데이터 줄에 낀
        # "액의 1%" / "이내" - 사이에 클래스명 등 다른 텍스트가 끼어 있어서
        # "납입금액의"를 하나의 이어붙은 문자열로 찾으면 놓친다). "납입금"이라는
        # 조각만으로도 판매수수료 문구라는 걸 충분히 특정할 수 있어 그걸로 판별한다.
        sales_commission_desc = None
        # 글자를 한 자씩 따로 찍는 서식이 있는 문서는 "100분의 0.15"의
        # "100"조차 "10"/"0분의"처럼 서로 다른 단어로 쪼개져 나와(공백이
        # 그 사이에 끼어) 위 두 정규식이 이어붙인 텍스트에서도 못 찾는다
        # (KR5114420027 Ae/S 실측: 판매수수료가 실제로 있는데도
        # sales_commission_desc가 null로 나왔다). 공백을 다 지운 버전으로
        # 한 번 더 시도한다.
        wide_text_nospace = wide_text.replace(" ", "")
        pct_m = SALES_COMMISSION_PCT_RE.search(wide_text) or SALES_COMMISSION_PCT_RE.search(wide_text_nospace)
        bunui_m = BUNUI_RE.search(wide_text) or BUNUI_RE.search(wide_text_nospace)
        # "후취"(환매 시점에 떼는) 클래스는 기준이 "납입금액"이 아니라
        # "환매금액"이다(위 REDEMPTION_NOTE_RE 주석 참고) - 원본 문구 그대로
        # "환매금액의 N%이내"로 남기고, 그 외(선취/일반)는 기존대로
        # "납입금액의 N%이내"로 남긴다.
        # "납입금액"이 "납입"/"금액의"로 쪼개져 나오는 문서도 있다(위
        # _word_role의 "납입" 주석 참고, KR5185450009 실측) - "납입금"
        # 대신 "납입"까지만 봐야 그런 경우도 잡힌다.
        commission_basis = (
            "환매금액"
            if "환매금액" in wide_text or "환매금액" in wide_text_nospace
            else ("납입금액" if "납입" in wide_text else None)
        )
        # "환매금액"을 기준으로 쓰는 후취형은 거의 항상 "OO년 미만 환매시"
        # 조건이 같이 붙어 있다(위 REDEMPTION_CONDITION_RE 주석 참고) -
        # 조건 없이 "환매금액의 N%이내"만 남기면 무조건 떼는 수수료처럼
        # 읽혀서 뜻이 달라지므로, 찾아지면 원본 그대로 앞에 붙인다.
        condition_prefix = ""
        if commission_basis == "환매금액":
            cond_m = REDEMPTION_CONDITION_RE.search(wide_text) or REDEMPTION_CONDITION_RE.search(wide_text_nospace)
            if cond_m:
                condition_prefix = f"{cond_m.group(1)}년 미만 환매시: "
        if commission_basis and pct_m:
            sales_commission_desc = f"{condition_prefix}{commission_basis}의 {pct_m.group(1)}%이내"
        elif commission_basis and bunui_m:
            # "N%" 대신 "100분의 N"(=N/100, 같은 뜻)으로 쓰는 문서가 있다
            # (KR5114420027). 위에서 이 값을 이미 총보수 등 실제 컬럼과
            # 분리해뒀으니, 여기서는 같은 뜻인 "%" 표기로 통일해서 남긴다.
            sales_commission_desc = f"{condition_prefix}{commission_basis}의 {bunui_m.group(1)}%이내"
        elif "없음" in window_text or has_standalone_dash:
            # 원본이 "없음"이라는 글자를 쓰든 그냥 "-"만 찍든 의미는 같아서
            # ("판매수수료가 없다"는 확인된 사실), 출력은 원본에 실제로 보이는
            # 기호인 "-"로 통일한다(사용자 요청).
            sales_commission_desc = "-"

        if isinstance(peer_avg_fee, str):
            peer_avg_fee_text = peer_avg_fee
        elif peer_avg_fee:
            peer_avg_fee_text = peer_avg_fee["text"].rstrip("%")
        else:
            peer_avg_fee_text = None

        # evidence는 "클래스명"을 물리적 줄 순서가 아니라 논리적 칸 이름을
        # 붙여 따로 보여주고, 그 뒤에 판매수수료 문구 + 숫자데이터를 이어
        # 붙인다(사용자 요청 - "판매수수료" 이름표 자체는 빼고, 클래스명/
        # 판매수수료 원문("수수료선취-오프라인(A) 액의 1%")이 숫자 앞에
        # 또 반복되지 않게). sales_commission_desc가 이미 정규화됐으면 그걸
        # 쓰고, 못 찾았으면(null) 원본에서 실제로 걸린 원문 조각
        # (commission_raw)을 대신 보여줘 왜 못 찾았는지 확인할 수 있게
        # 한다. 숫자데이터(data_text)는 total_fee/distribution_fee/
        # peer_avg_fee/total_fee_and_cost/cost_projection_per_10m을 원본과
        # 대조 확인하는 용도다.
        commission_display = sales_commission_desc if sales_commission_desc is not None else (commission_raw or "(확인안됨)")
        evidence = f"클래스명: {class_name_full or '(확인안됨)'} | {commission_display} {data_text}".rstrip()

        rows.append({
            "class_code": class_code,
            "sales_commission_desc": sales_commission_desc,
            "total_fee": total_fee["text"].rstrip("%"),
            "distribution_fee": distribution_fee["text"].rstrip("%"),
            "peer_avg_fee": peer_avg_fee_text,
            "total_fee_and_cost": total_fee_and_cost["text"].rstrip("%") if total_fee_and_cost else None,
            "cost_projection_per_10m": cost_projection,
            # "운용전환일" 전/후로 수수료가 바뀌는 문서에서만 이 키들이
            # 붙는다(아래 참고, KR5147430065) - 그 외 문서(대다수)는 애초에
            # 키 자체가 없다(null로 채운 빈 필드를 모든 행에 다 넣으면
            # 대부분 안 쓰는 필드로 보기 불편하다는 지적을 받아, 해당되는
            # 행에만 조건부로 붙이도록 바꿨다). total_fee 등 위쪽 필드는
            # 전환 "전"(현재 적용 중인) 값이고, *_after_conversion은 전환
            # 이후 예정된 값이다.
            "page": page_num,
            "evidence": evidence,
            "method": "coordinate_reconstruction",
            # 주의: 이 confidence는 "이 행의 모든 필드가 다 맞다"는 뜻이
            # 아니다 - "class_code(클래스 이름표)를 다른 클래스와 헷갈릴
            # 위험 없이 찾았는가"만 본다(class_code를 못 찾으면 어느
            # 클래스 것인지조차 불확실하니 0.5로, 찾았으면 1.0으로). 사용자
            # 지적대로 "다 제대로 뽑아야 1이어야 하는 거 아니냐"는 게 맞는
            # 말이지만, total_fee/판매수수료/클래스명 표기처럼 서로 다른
            # 이유로 틀릴 수 있는 필드들을 하나의 숫자로 합칠 근거가 없어
            # (이번 세션에서 고친 버그들 - sales_commission_desc null,
            # 인접 클래스명 섞임 등 - 이 전부 class_code는 처음부터 1.0
            # 이었던 행에서 나왔다는 게 그 증거) 이 좁은 의미로 한정해서
            # 쓴다. "행 전체가 실제로 맞는지"는 confidence가 아니라
            # extract_class_fees.py 실행 후 매번 돌리는 전수 이상치 검사
            # (1y>500/1y<10/total_fee>10/distribution_fee>total_fee/
            # total_fee_and_cost<total_fee, class_code 중복 등 - README
            # 참고)가 실질적으로 그 역할을 한다.
            "confidence": 1.0 if class_code else 0.5,
            "_row_line_idx": i,
        })

    # "판매수수료" 칸이 "없음" 글자 하나를 여러 클래스 행에 걸쳐 세로로
    # 병합해서 공유하는 문서가 있다(KR5194450018 실측 - 화면 캡처로 직접
    # 확인: C1/C-e/W/F 4개 클래스, RP/RP-e/S-P/CP/CP-e 5개 클래스가 각각
    # "없음" 하나씩을 그룹 세로 중앙에 공유). 개별 행 위/아래 몇 줄만
    # 보는 기존 로직은 그 그룹의 가운데 근처 행(C-e/S-P처럼 "없음"과
    # 가까운 행)만 우연히 맞고, 그룹 양 끝 행(C1/CP-e, RP)은 놓쳐서
    # sales_commission_desc가 null로 남았다(사용자가 "제발 제대로
    # 해줘"라고 지적해서 화면 캡처까지 받아 직접 확인함 - class_returns의
    # 병합 셀 설정일 처리와 같은 종류의 문제). 아직 못 찾은 행에 대해,
    # 그 행과 "없음" 토큰 사이에 이미 다른 진짜 문구("납입금액의..."/
    # "환매금액의..." 등 - "-"로 확정된 행은 같은 그룹일 수 있어 경계로
    # 안 본다)로 확정된 다른 행이 끼어있지 않은 가장 가까운 "없음"을
    # 찾아 같은 병합 셀로 보고 채운다.
    #
    # 다만 이 판정은 "중간에 진짜 문구가 없다"만 보기 때문에, 표에 아무
    # 마커도 안 남기고 진짜로 비어있는 행(우리가 아직 본 적 없는 케이스)이
    # 끼어 있으면 엉뚱하게 먼 "없음"을 끌어다 붙일 위험이 있다. 실측
    # 병합 그룹(KR5194450018)의 최대 거리가 6줄이었던 것에 근거해, 그보다
    # 뚜렷이 먼 "없음"은 같은 병합 셀이라 확신할 수 없다고 보고 채우지
    # 않는다 - 틀린 "-"보다 null로 남겨 이상치 검사에 걸리게 하는 게 낫다
    # ("틀린 값은 없는 값보다 나쁘다").
    # "없음" 판정을 줄 안에 그 글자가 있는지만으로 하면, 표가 아니라
    # 근처의 다른 문장(각주/설명 문구 등)에 우연히 등장한 "없음"까지
    # 병합 셀로 착각할 위험이 있다(사용자 지적). 판매수수료 칸 자체에서
    # 이미 직접 "없음"이 잡힌 행(예: 이 페이지의 C-e처럼 병합 그룹
    # 가운데라 기존 로직으로도 맞은 행)이 있으면 그 x좌표를 이 칸의
    # 실제 위치로 보고, 후보 "없음"도 그 x좌표 근처에 있는 것만
    # 인정한다(class_returns의 최초설정일 병합 셀 판정과 같은 방식 -
    # 표 밖 문장은 x좌표가 이 칸과 다를 수밖에 없어 걸러진다). 이 페이지에
    # 그런 직접-매치 행이 아예 없으면(앵커를 못 구하면) 판정 불가로 보고
    # 안전하게 x좌표 필터 없이 기존 방식(거리 상한만 적용)으로 대체한다.
    MAX_MERGED_CELL_DISTANCE = 8
    unresolved = [r for r in rows if r.get("sales_commission_desc") is None]
    if unresolved:
        row_line_idxs = {r["_row_line_idx"] for r in rows}
        commission_col_x0 = None
        for idx in row_line_idxs:
            for w in lines[idx]:
                if w["text"] == "없음":
                    commission_col_x0 = w["x0"]
                    break
            if commission_col_x0 is not None:
                break

        def _none_word_x0(l):
            return next((w["x0"] for w in l if w["text"] == "없음"), None)

        none_word_lines = [
            idx for idx, l in enumerate(lines)
            if (x0 := _none_word_x0(l)) is not None
            and (commission_col_x0 is None or abs(x0 - commission_col_x0) < 15)
        ]
        real_phrase_positions = [
            r["_row_line_idx"] for r in rows
            if r.get("sales_commission_desc") not in (None, "-")
        ]
        for r in unresolved:
            ri = r["_row_line_idx"]
            best = None
            for ni in none_word_lines:
                if abs(ni - ri) > MAX_MERGED_CELL_DISTANCE:
                    continue
                lo, hi = min(ri, ni), max(ri, ni)
                if any(lo < p < hi for p in real_phrase_positions):
                    continue
                if best is None or abs(ni - ri) < abs(best - ri):
                    best = ni
            if best is not None:
                r["sales_commission_desc"] = "-"

    for r in rows:
        r.pop("_row_line_idx", None)
    return rows


def candidate_pages_for_doc(doc_id, max_page):
    """처음엔 "블롭"(뭉쳐서 깨진) 페이지만 대상으로 삼았는데, 그러면 표가 여러
    페이지에 걸쳐 있을 때(예: 클래스 일부는 정상 추출된 페이지에, 나머지는 깨진
    페이지에) 정상 페이지 쪽 클래스를 통째로 놓치는 버그가 있었다(KR514X450008
    사례로 확인). 좌표 기반 재구성은 이미 정상 추출된 페이지에도 똑같이 정확하게
    동작한다는 걸 검증했으므로(KR5120420039), "클래스"+"총보수"가 언급된 페이지는
    깨졌든 안 깨졌든 전부 대상으로 삼고, 표가 다음 페이지로 이어질 수 있으니
    바로 다음 페이지도 같이 포함한다."""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_tables.json")
    if not os.path.exists(fp):
        return []
    with open(fp, "r", encoding="utf-8") as f:
        tables = json.load(f)

    pages = set()
    for t in tables:
        flat = " ".join(c for row in t["data"] for c in row if c)
        if "클래스" in flat and "총보수" in flat:
            pages.add(t["page"])
            if t["page"] + 1 <= max_page:
                pages.add(t["page"] + 1)
    return sorted(pages)


def conversion_trigger_nav_price(doc_id):
    """total_fee_after_conversion 등이 채워진 행이 있는 문서에서, "운용전환일"이
    고정 날짜가 아니라 이 펀드 자신의 기준가격이 특정 값 이상 오르면 발생하는
    조건부 전환인 경우(목표전환형 펀드), 그 목표 기준가격(원) 숫자만 뽑는다.
    문장으로 풀어 쓰지 않는 이유: 이 파일의 다른 모든 필드는 원본에서 그대로
    뽑은 값이지 해석문이 아니다 - 숫자만 남기고 의미(목표가 도달 시 전환)는
    필드 이름과 README에 문서화한다. 못 찾으면(고정 날짜인 일반적인 경우
    - 이 필드 자체가 만들어지는 문서는 지금 KR5147430065 하나뿐) None."""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_text.json")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        pages = json.load(f)
    full_text = " ".join(p.get("text", "") for p in pages)
    m = CONVERSION_TRIGGER_RE.search(full_text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _cluster_header_labels(lines, header_end_idx, x_tol=8):
    """header_end_idx 위쪽 최대 12줄에서 x좌표로 헤더 라벨 텍스트를
    재구성한다(소수 값 토큰은 제외) - "나.집합투자기구에 부과되는 보수"류
    상세표는 헤더가 여러 줄에 걸쳐 한 글자씩 쌓이는 서식이 많다.

    묶는 기준은 x0가 아니라 글자 뭉치의 가운데(center)다. 헤더 칸 이름은
    가운데 정렬이라 조각마다 글자 수가 다르면 x0가 어긋난다
    (KR510902511M 28페이지 실측: "집합"@141.1 / "투자업자"@131.1 /
    "보수"@141.1 - x0로 묶으면 10pt 차이라 "투자업자"가 떨어져 나가
    라벨이 "집합"으로 잘렸다. 가운데로 맞추면 세 조각이 하나로 묶여
    "집합투자업자보수"가 된다). 칸 간격은 실측상 45pt 안팎이라 이
    허용오차로 옆 칸과 섞일 위험은 없다. 돌려주는 x는 그 칸의 왼쪽
    끝(x0 최솟값)으로, 값 토큰의 x0와 비교하는 호출부와 기준을 맞춘다."""
    header_lines = lines[max(0, header_end_idx - 12) : header_end_idx + 1]
    clusters = []
    for row_i, line in enumerate(header_lines):
        for w in line:
            if DECIMAL_RE.match(w["text"]):
                continue
            center = (w["x0"] + w["x1"]) / 2
            placed = False
            for c in clusters:
                if abs(c["center"] - center) <= x_tol:
                    c["pieces"].append((row_i, w["text"]))
                    c["x0"] = min(c["x0"], w["x0"])
                    placed = True
                    break
            if not placed:
                clusters.append({"center": center, "x0": w["x0"], "pieces": [(row_i, w["text"])]})
    labels = []
    for c in clusters:
        pieces = sorted(c["pieces"])
        labels.append((c["x0"], "".join(t for _, t in pieces)))
    return sorted(labels)


DETAIL_FEE_DASH_RE = re.compile(r"^-$")
# "(C)"처럼 괄호로 안 떨어지고 "종류C"/"종류C-F"로만 나오는 라벨 서식
# (KR510902773M 실측). 뒤에 다른 한글이 바로 안 붙게(예: "종류형") 경계를
# 둔다 - class_returns.py의 CLASS_CODE_JONGRYU_RE와 같은 취지.
# 코드 칸이 "종류C-F ⏎ -"처럼 두 줄인 문서가 있다. 둘째 줄은 종류형
# 명칭인데, 이 문서는 C-F와 I에만 명칭을 안 붙이고 "-"(없음)로 적었다
# (KR5153420063 8/23/24쪽). 줄바꿈을 지우고 이어 붙이면 "종류C-F-"가
# 되어 코드를 "C-F-"로 읽는다 - 없는 클래스가 하나 생기고, 진짜 C-F는
# 이름표·가입자격과 이어지지 않는다. 클래스 코드는 붙임표로 끝나지
# 않으므로 끝의 붙임표는 코드에서 뺀다.
# "종류직판F"(KR5153420105 실측)처럼 코드가 "직판"으로 시작하는 경우도
# 있어 첫 글자가 라틴이라는 가정을 "직판"만 예외로 늦춘다.
DETAIL_FEE_CLASS_CODE_JONGRYU_RE = re.compile(
    r"종류(직판[A-Za-z0-9]{0,3}|[A-Za-z](?:[A-Za-z0-9\-]{0,5}[A-Za-z0-9])?)(?![A-Za-z0-9])")
# 클래스 코드 뒤에 자격 설명이 괄호로 한 번 더 붙는 문서가 있다
# (KR5129420025 실측: "수수료미징구-온라인-개인연금 (C-Pe(연금저축))").
# 괄호가 겹쳐서 CLASS_CODE_RE가 아무것도 못 잡고 그 행을 통째로 버렸는데,
# 하필 연금 클래스들이다(C-P/C-Pe/C-Pu/C-RP/C-RPe 5개). class_meaning이
# 쓰는 표기("C-Pe(연금저축)")와 같게 뽑아야 두 표가 같은 클래스로 이어진다.
DETAIL_FEE_CLASS_CODE_NESTED_RE = re.compile(
    r"\(([A-Za-z0-9\-]{1,8}\([^()]{1,12}\))\)")
# 위 형식에서 바깥쪽 닫는 괄호가 문서에 아예 없는 경우가 있다
# (KR5129420031 실측: "...퇴직연금, 고액(Ci-RP(퇴직연금) " - 안쪽
# "(퇴직연금)"만 닫히고 바깥쪽 "(Ci-RP..."는 안 닫힌 채 셀이 끝난다).
# 보수표와 명칭표 두 곳에서 똑같이 이렇게 적혀 있어서 우리 셀 읽기가
# 아니라 문서 자체의 오타로 보인다. 바깥 괄호가 없다는 이유로 그
# 클래스(총보수 0.257)를 통째로 잃을 수는 없어서, 안쪽 괄호까지만
# 요구하고 바깥쪽은 있어도 되고 없어도 되게 둔다. 정상 형식을 먼저
# 시도하는 게 우선이라(DETAIL_FEE_CLASS_CODE_NESTED_RE가 먼저 옴), 바깥
# 괄호가 있는 문서는 그쪽이 이미 잡아서 여기까지 안 온다.
DETAIL_FEE_CLASS_CODE_NESTED_UNCLOSED_RE = re.compile(
    r"\(([A-Za-z0-9\-]{1,8}\([^()]{1,12}\))")
# 헤더 여러 줄이 데이터 행과 가까워서(표마다 헤더 줄 수가 달라 정확한
# 경계를 못 잡음) 클래스 코드 정규식에 걸리는 흔한 금융 약어들
# (KR5172450019 실측: 헤더의 "(TER)"이 A클래스 행의 코드로 잘못 붙어서
# 이후 모든 클래스가 한 칸씩 밀림). 실제 클래스 코드로 이 값들이 나올
# 일은 없다고 봐도 안전하다.
DETAIL_FEE_CODE_BLOCKLIST = {"TER", "CDSC", "IRP", "Class", "Wrap", "Cost"}


def _is_bad_code(code):
    """블록리스트이거나 숫자로만 된 "코드"는 진짜 코드가 아니다.

    코퍼스 전체에서 클래스 코드는 항상 문자(영문 또는 한글)로 시작하고,
    순수 숫자만인 코드는 없다. 각주 번호("(1) 이익분배금...")나 조항
    번호가 라벨에 섞여 괄호 안 숫자가 코드로 잘못 잡힌 적이 있다
    (KR5131420007 28쪽 실측: "...(1) 이익분배금..."의 "1"이 코드로
    읽혔다)."""
    return code in DETAIL_FEE_CODE_BLOCKLIST or code.isdigit()


def _detail_fee_labels_by_column(lines, first_data_idx, col_x0s):
    """상세표의 각 컬럼(값 x좌표) 위에 있는 헤더 글자들을 모아 칸 이름을
    복원한다. fee_breakdown 항목 이름으로 쓰이므로 "집합"처럼 잘리거나
    옆 칸 이름이 붙으면 안 된다(잘린 이름은 못 쓰고, 틀린 이름은 더 나쁘다).

    처음엔 페이지 전체 헤더를 x좌표로 한 번에 클러스터링하고 컬럼마다
    "가장 가까운 라벨"을 붙였는데, 두 가지로 깨졌다(KR510902511M 28페이지
    실측):
      - 문서 제목("미래에셋장기성장포커스...")이나 여러 칸을 아우르는
        묶음 헤더("지급비용(연간%)")가 가로로 넓어 개별 칸 글자와 같은
        클러스터로 묶였다 - 그 바람에 "집합투자업자보수"가 제목 클러스터에
        흡수되고, 0.72(집합투자업자보수)에 옆 칸 이름 "판매회사보수"가
        붙는 밀림이 생겼다.
      - "총 보수"처럼 한 줄에 띄어 쓴 칸 이름은 세로로 쌓이지 않아 하나로
        안 묶였다.
    그래서 전역 클러스터링 대신 컬럼마다 "이 칸 위에 실제로 겹쳐 있는
    글자"만 모은다. 칸 하나를 넘어 퍼지는 토큰(칸 간격의 1.5배 초과)은
    묶음 헤더/제목으로 보고 제외한다."""
    if len(col_x0s) < 2:
        return [None] * len(col_x0s)
    spacing = min(b - a for a, b in zip(col_x0s, col_x0s[1:]))
    max_width = spacing * 1.5
    # 헤더 영역의 위 경계: 표 제목("나.집합투자기구에 부과되는 보수 및
    # 비용")보다 위로는 안 올라간다. 제목 글자도 폭이 좁아 폭 필터에 안
    # 걸리고 컬럼 위에 겹쳐서, 안 자르면 칸 이름이 "부과되는보수집합투자
    # 업자보수"처럼 제목 조각을 달고 나온다(KR5122420005/KR5172450019 실측).
    start = max(0, first_data_idx - 14)
    for j in range(first_data_idx - 1, start - 1, -1):
        if "부과되는" in "".join(w["text"] for w in lines[j]):
            start = j + 1
            break
    header_lines = lines[start:first_data_idx]

    out = []
    for cx in col_x0s:
        # 칸 이름은 값 위에 가운데 정렬로 찍히므로, 값의 가운데를 기준으로
        # 칸 간격의 절반 안에 "글자 뭉치의 가운데"가 들어올 때만 이 칸의
        # 이름으로 본다(겹침만 보면 옆 칸 이름까지 딸려온다 - 실측으로
        # "집합판매회사투자업자보수"처럼 두 칸 이름이 섞였다). 값 폭은
        # "0.72"류 4~6글자라 10pt를 더해 가운데로 잡는다.
        center = cx + 10
        lo, hi = center - spacing * 0.5, center + spacing * 0.5
        pieces = []
        for row_i, line in enumerate(header_lines):
            for w in line:
                if DECIMAL_RE.match(w["text"]):
                    continue
                if (w["x1"] - w["x0"]) > max_width:
                    continue  # 묶음 헤더("지급비용(연간%)")/문서 제목
                # 값 칸 전체를 아우르는 단위·범위 표기("지급비율",
                # "지급비용", "(연간,%)")는 특정 칸의 이름이 아니다 -
                # 폭이 좁아 위 필터를 통과하므로 글자로도 걸러낸다.
                if w["text"].startswith("지급") or "연간" in w["text"]:
                    continue
                wc = (w["x0"] + w["x1"]) / 2
                if lo <= wc <= hi:
                    pieces.append((row_i, w["x0"], w["text"]))
        text = "".join(t for _, _, t in sorted(pieces))
        out.append(text or None)
    return out


def _find_detail_fee_data_rows(pdf):
    """"나.집합투자기구에 부과되는 보수 및 비용"류 상세표 - 캡션 문구가
    문서마다 달라서(README 참고: "투자실적" 같은 고정 캡션이 없음) 캡션
    대신 데이터 행 자체의 모양으로 찾는다: 순수 소수(%아님, 정수 비용예시도
    아님) + "-"(원본이 명시적으로 비워둔 칸)를 합쳐 7개 이상 한 줄에 있으면
    이 표의 데이터 행으로 본다(앞쪽 요약표는 소수 3~4개 + 정수 비용예시라
    이 조건에 안 걸림). 소수만으로 세면, 컬럼 대부분이 "-"인 클래스(직판/
    기관형 등 부가서비스가 거의 없는 클래스 - KR510902773M의 C-F 실측:
    소수 6개뿐이라 7개 기준에 아예 안 걸려서 데이터 행 취급을 못 받았다)를
    통째로 놓친다 - "-"도 원본이 실제로 채워 넣은 값(칸이 빈 게 아니라
    명시적으로 "없음"이라고 표시한 것)이므로 같이 센다. 페이지별로
    (page_num, 그 페이지의 lines, 데이터 행 인덱스 목록)을 돌려준다."""
    results = []
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        lines = cluster_lines(words)
        data_idx = []
        for j, line in enumerate(lines):
            decimals = [w for w in line if DECIMAL_RE.match(w["text"]) and "%" not in w["text"]]
            dashes = [w for w in line if DETAIL_FEE_DASH_RE.match(w["text"])]
            if len(decimals) + len(dashes) >= 7:
                data_idx.append(j)
        if data_idx:
            results.append((i + 1, lines, data_idx))
    return results


def _detail_fee_row_class_code(lines, row_idx, consumed=None, header_idx=None):
    """데이터 행 자신 또는 앞/뒤 몇 줄에서 클래스 코드를 찾는다(클래스명이
    데이터 행 앞/뒤로 걸쳐 있는 서식이 많음 - class_returns.py에서 이미
    검증된 것과 같은 패턴). consumed: 이미 앞선(위쪽) 행의 라벨로 확정돼
    "소비"된 줄 번호 집합 - 한 클래스의 라벨이 "시작(앞) / 데이터 / 끝(뒤)"
    구조로 자기 데이터 행을 감싸는 서식에서, 다음 클래스가 위로 훑을 때
    바로 이전 클래스의 "끝" 조각을 자기 라벨로 잘못 주워가는 걸 막는다
    (KR5122420005 실측: A-E 행이 바로 위의 "형(A)"(A 자신의 끝 라벨
    조각)를 A-E의 라벨로 착각해 코드가 "A"로 잘못 나옴 - consumed로
    한 번 쓰인 줄은 다음 행 탐색에서 제외해야 고쳐짐). header_idx: 이
    줄보다 위로는 절대 안 넘어간다 - 첫 번째 데이터 행이 헤더 바로
    다음이면, 헤더 자체에 있는 "(TER)"(Total Expense Ratio 약자) 같은
    괄호 문구를 클래스 코드로 잘못 주워서 전체 클래스가 한 칸씩 밀리는
    사고가 났었다(KR5172450019 실측 - A행이 "TER"로 잘못 라벨링되면서
    A/Ae/C1/... 전체 값이 한 행씩 밀려 대응됨). 호출부는 데이터 행을
    위→아래 순서로 처리해야 한다."""
    if consumed is None:
        consumed = set()
    floor = header_idx if header_idx is not None else -1
    line = lines[row_idx]
    decimals = [w for w in line if DECIMAL_RE.match(w["text"]) and "%" not in w["text"]]
    if not decimals:
        return None, None
    pre_text = "".join(w["text"] for w in line if w["x0"] < decimals[0]["x0"])
    # 다른 클래스의 데이터 행(소수 3개 이상)이나 이미 소비된 줄, 헤더 위
    # 영역을 만나면 그 전에서 멈춰 옆 클래스 라벨(또는 헤더 문구)을 잘못
    # 가져오지 않게 한다. prev는 가장 가까운(마지막) 매치, next는 가장
    # 가까운(첫) 매치만 쓴다.
    prev_idx = []
    for k in range(row_idx - 1, max(row_idx - 4, floor), -1):
        if k in consumed or sum(1 for w in lines[k] if DECIMAL_RE.match(w["text"])) >= 3:
            break
        prev_idx.insert(0, k)
    next_idx = []
    for k in range(row_idx + 1, min(row_idx + 4, len(lines))):
        if sum(1 for w in lines[k] if DECIMAL_RE.match(w["text"])) >= 3:
            break
        next_idx.append(k)
    prev_text = "".join("".join(w["text"] for w in lines[k]) for k in prev_idx)
    next_text = "".join("".join(w["text"] for w in lines[k]) for k in next_idx)

    def _nearest_match(regex, text, want_last):
        matches = [m for m in regex.finditer(text) if not _is_bad_code(m.group(1))]
        if not matches:
            return None
        return (matches[-1] if want_last else matches[0]).group(1)

    for regex in (CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
        code = _nearest_match(regex, pre_text, want_last=True)
        if code:
            return code, pre_text
        code = _nearest_match(regex, prev_text, want_last=True)
        if code:
            consumed.update(prev_idx)
            return code, prev_text
        code = _nearest_match(regex, next_text, want_last=False)
        if code:
            consumed.update(next_idx)
            return code, next_text
    return None, prev_text + pre_text + next_text


def _detail_fee_grids(pdf):
    """"나.집합투자기구에 부과되는 보수 및 비용"류 상세표를 셀 격자로
    읽는다. 좌표 방식(줄 묶기 + x 근접 매칭)은 이 표에서도 같은 문제를
    겪었다 - 클래스명이 데이터 행 위/아래로 쪼개져 옆 행 것을 주워오거나
    (consumed 추적 필요), 헤더가 여러 줄로 쌓여 라벨이 잘리거나
    ("집합" 하나만 남음), 값이 "-"라 빠진 칸 때문에 열이 밀렸다.
    셀 경계를 쓰면 이 보정들이 전부 필요 없어진다.

    돌려주는 것: (page_num, header_rows, data_rows, col_x0s)
      - col_x0s: 이 표의 열 왼쪽 x좌표(정렬)
      - data_rows: [{"label": 맨왼쪽칸 텍스트, "cells": {열번호: 텍스트}}]
        (숫자 칸이 5개 이상인 행만 - 헤더/각주 행 제외)
    """
    results = []
    # 칸 이름표만 있고 값 행은 하나도 없는 표(데이터가 전부 다음 페이지로
    # 넘어간 경우 - KR5153420022 실측: 26쪽엔 "구분|집합투자업자보수|...
    # |총보수비용" 머리글 한 줄뿐이고 값 두 줄은 27쪽에 있다)는 data_rows가
    # 없어 이 함수가 그냥 버렸다. 그러면 값은 27쪽에서 멀쩡히 읽혀도 칸
    # 이름을 몰라 total_fee_and_cost 등 이름으로만 찾는 칸을 못 채운다.
    # 이런 표를 바로 다음 페이지로 넘겨 뒀다가, 열 개수가 같은 표가
    # 이어지면 그 앞에 이 머리글을 붙여 함께 돌려준다.
    pending_header = None
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        for t in page.find_tables():
            tbbox = t.bbox
            cells = [c for c in t.cells if c]
            if len(cells) < 12:
                continue
            # 같은 논리적 열인데 헤더 셀과 값 셀의 x가 몇 pt 어긋나 있는
            # 문서가 있다(KR5122420005 실측: "일반사무관리회사보수" 헤더는
            # x=283, 그 값은 x=278 - 그대로 두면 서로 다른 열로 잡혀 열이
            # 14개로 늘고 라벨과 값이 어긋난다). 가까운 x는 한 열로 묶는다.
            raw_x0s = sorted({round(c[0], 1) for c in cells})
            col_x0s = []
            for x in raw_x0s:
                if col_x0s and x - col_x0s[-1] <= 6:
                    continue
                col_x0s.append(x)
            if len(col_x0s) < 7:
                continue

            bands_preview = sorted({(round(c[1], 1), round(c[3], 1))
                                     for c in cells})
            sparse = len(bands_preview) <= 3
            if sparse:
                # 표에 행이 몇 줄 안 남은 페이지(대개 앞 페이지에서
                # 이어지다 끝에 한 줄만 남은 경우)는 칸을 가르는 테두리선이
                # 일부만 그려져, 숫자가 놓인 자리인데도 그 구간을 덮는
                # 셀 자체가 하나도 없다(KR5113420013 46쪽 실측: "총보수"
                # 이후 6개 값(x=297.8~)은 셀이 있는데, 그 앞 4개 값
                # "0.1100 0.1200 0.0300 0.0140"(집합투자업자보수 등,
                # x=133.8~260.6)은 어느 셀에도 안 걸려 통째로 버려진다).
                # 행이 몇 줄 안 남아 이 페이지의 다른 행과 열을 맞출
                # 필요가 없을 때만, 셀로 안 덮인 자리의 숫자 낱말을 찾아
                # col_x0s에 새 열로 끼워 넣는다.
                extra = []
                for w in words:
                    wt = w["text"].replace(" ", "")
                    if not DECIMAL_RE.match(wt):
                        continue
                    # 다른 표에 속한 숫자를 이 표의 새 열로 끼워 넣으면
                    # 안 된다 - 이 표 테두리 밖 낱말은 애초에 대상이
                    # 아니다.
                    if not (tbbox[1] - 2 <= w["top"] and w["bottom"] <= tbbox[3] + 2):
                        continue
                    mid_x = (w["x0"] + w["x1"]) / 2
                    mid_y = (w["top"] + w["bottom"]) / 2
                    covered = any(
                        x0 - 1 <= mid_x <= x1 + 1 and ctop - 1 <= mid_y <= cbottom + 1
                        for (x0, ctop, x1, cbottom) in cells)
                    if not covered:
                        extra.append(w["x0"])
                for x in extra:
                    x = round(x, 1)
                    if not any(abs(x - c) <= 6 for c in col_x0s):
                        col_x0s.append(x)
                col_x0s.sort()

            def col_of(x0):
                return min(range(len(col_x0s)),
                           key=lambda k: abs(col_x0s[k] - x0))

            bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in cells})
            rows = []
            for top, bottom in bands:
                row_cells = [c for c in cells
                             if abs(c[1] - top) < 1 and abs(c[3] - bottom) < 1]
                if not row_cells:
                    continue
                entry = {}
                for (x0, ctop, x1, cbottom) in row_cells:
                    # 단어가 셀 경계를 살짝 넘는 경우가 있어(좁은 클래스명
                    # 칸에서 "…형(A)"의 꼬리가 잘려 클래스 코드를 통째로
                    # 놓쳤다 - KR5122420005 실측) 완전 포함이 아니라 단어
                    # 중심이 셀 안에 드는지로 담는다.
                    ws = [w for w in words
                          if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                          and ctop - 1 <= (w["top"] + w["bottom"]) / 2 <= cbottom + 1]
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    txt = FEE_FOOTNOTE_MARK_RE.sub(" ", txt).strip()
                    if txt:
                        entry[col_of(x0)] = txt
                # 이 자리를 덮는 셀 자체가 없어 위 row_cells 훑기에서 빠진
                # 숫자 낱말을 직접 찾아 채운다(KR5144420020 32쪽 실측: 이미
                # 열 구성이 확실한 다열 표에서도 딱 한 행만 셀 일부가
                # 빠져 총보수 등 6칸을 통째로 잃었다 - col_x0s는 이미
                # 있으니 이미 채워진 칸은 안 건드리고, 셀이 안 잡힌 칸만
                # 메운다). 다만 이 밴드가 유난히 높으면(보통 한 줄은
                # 20pt 안팎) 여러 실제 행이 셀 경계 없이 한 밴드로 뭉친
                # 것이다(KR5118420062 30쪽 실측: "집합투자업자보수"부터
                # "일반사무관리회사보수"까지 4개 항목이 top=329.7~422.2
                # 하나의 밴드로 뭉쳐 있다 - 그 안 아무 칸에나 값을
                # 채우면 실제로는 서로 다른 항목의 값이 한 행에 섞여
                # "판매회사보수" 등 엉뚱한 필드로 읽힌다). 그런 밴드는
                # 이미 잡힌 칸만 쓰고 더 채우지 않는다.
                # 이미 칸이 넉넉히 잡힌 행까지 채우면, 같은 값이 인접한
                # 두 칸에 겹쳐 들어가는 등(열 하나가 col_x0s에서 두 자리로
                # 잡히는 문서가 있어서) 값 개수가 코드 개수보다 많아져
                # 오히려 짝짓기가 깨진다. 절반도 안 잡힌, 확실히 모자란
                # 행에서만 메운다.
                if bottom - top > 40 or len(entry) >= max(3, len(col_x0s) // 2):
                    if entry:
                        rows.append({"top": top, "bottom": bottom, "cells": entry})
                    continue
                for w in words:
                    wt = w["text"].replace(" ", "")
                    if not DECIMAL_RE.match(wt):
                        continue
                    # 이 페이지의 다른 표(예: "가.투자자에게 직접
                    # 부과되는 수수료" 표)에 속한 숫자까지 주워 오면 안
                    # 된다(KR510902511M 실측: 14쪽 "가" 표의 행에 무관한
                    # "나" 표 스타일 숫자가 이렇게 섞여 들어가 엉뚱한
                    # 열이 "채워진 것으로" 잘못 보였다). 지금 이 표의
                    # 테두리 안에 있는 낱말만 본다.
                    if not (tbbox[1] - 2 <= w["top"] and w["bottom"] <= tbbox[3] + 2):
                        continue
                    mid_y = (w["top"] + w["bottom"]) / 2
                    if not (top - 1 <= mid_y <= bottom + 1):
                        continue
                    ci = col_of(w["x0"])
                    if ci not in entry:
                        entry[ci] = wt
                if entry:
                    rows.append({"top": top, "bottom": bottom, "cells": entry})

            data_rows, header_rows = [], []
            for r in rows:
                nnum = sum(1 for v in r["cells"].values()
                           if DECIMAL_RE.match(v.replace(" ", "")))
                # 뒤집힌 보수표가 클래스 두 개짜리인 문서가 있다
                # (KR5118420062 31쪽 실측: S-P/S-P(퇴직) 표는 값 열이
                # 2개뿐이라 "총보수" 행도 nnum=2다). 숫자 칸 5개 기준만
                # 쓰면 이 표는 데이터 행이 하나도 안 남아 통째로
                # header_rows로 밀려 표 자체를 잃는다. 0번 칸이 보수
                # 항목 이름으로 읽히는 행은(그런 이름은 코드 표의 0번
                # 칸엔 나오지 않는다 - 거긴 클래스 코드다) 숫자가 하나만
                # 있어도 데이터 행으로 받는다.
                is_fee_item_row = _fee_item_field(r["cells"].get(0, "")) is not None
                # 정방향 표(0번 칸이 클래스 코드)도 첫 클래스만 칸을 다
                # 채우고 나머지는 "공통이라 생략" 식으로 비우는 문서가
                # 있다(KR5152420028 15쪽 실측: A행만 5칸 다 채워져
                # nnum=5고, 나머지 14개 클래스는 판매회사보수·보수합계
                # 2칸만 있어 nnum=2 - 전부 버려졌다). 0번 칸이 클래스
                # 코드로 읽히면(항목 이름이 아니라 코드가 있는 자리는
                # 정방향 표뿐이다) 마찬가지로 완화한다.
                has_class_code = _class_code_token(r["cells"].get(0, "")) is not None
                # 페이지가 넘어가는 자리의 행 하나만 셀 경계선이 일부 빠져
                # 값 칸이 듬성듬성 잡히는 문서가 있다(KR5144420020 32쪽
                # 실측: C-P2 행은 10칸 중 4칸만 셀이 잡히고, 그 칸에도
                # 이 행의 라벨은 안 붙어 있다 - 코드 자체가 다음 페이지로
                # 넘어갔다). 0번 칸 라벨도 없고 숫자도 5개가 안 돼 위
                # 두 조건 다 못 걸리지만, 바로 앞 데이터 행과 값 칸
                # 자리가 절반 이상 겹치면(같은 표의 열 구성을 그대로
                # 쓰고 있다는 뜻) 표 중간에 각주가 끼어들 리는 없으므로
                # 데이터 행으로 받는다.
                looks_continued = False
                if data_rows and nnum >= 2:
                    prior_cols = set(data_rows[-1]["cells"].keys()) - {0}
                    this_cols = set(r["cells"].keys()) - {0}
                    if prior_cols and this_cols:
                        looks_continued = (
                            len(this_cols & prior_cols) / len(this_cols) >= 0.5)
                if nnum >= 5 or (nnum >= 1 and (is_fee_item_row or has_class_code)) \
                        or looks_continued:
                    data_rows.append(r)
                elif not data_rows:
                    header_rows.append(r)

            # 여러 클래스 행에 걸친 병합 칸이 있다(KR5156450026 실측:
            # "동종유형 총보수" 칸이 보수체감형 C1~C4 네 행을 세로로 하나로
            # 묶어 "1.600"을 한 번만 찍는다 - 네 클래스가 같은 값을
            # 공유한다는 뜻이다). 이 칸의 (top,bottom)은 그 네 행 중 어느
            # 한 행의 것과도 안 맞아 위 루프에서 독자적인 "밴드"로 처리
            # 되면서(라벨도 못 찾고 버려지거나) 못 잡히고, 폴백 낱말
            # 검색은 우연히 근처 한 행에만 걸려 나머지 세 행은 못 받는다
            # (실측: C2/C3 두 행에만 우연히 걸리고 C1/C4는 못 받았다).
            # 이 칸이 세로로 덮는 모든 데이터 행에 값을 직접 복제한다 -
            # header_rows/분류 전 밴드에는 손대지 않는다. header_rows
            # 단계에서 하면 원래 항목 이름만 있던 조각 행에 값이 끼어들어
            # 그 칸 이름표(label_by_col)가 "집합투자업자0.1500.1500.150"
            # 처럼 이름과 값이 뒤섞여 버린다(KR5127420034 13쪽 실측 -
            # 병합 칸이 표 전체 높이를 덮어 데이터 행 분류 전의 머리글
            # 조각 밴드까지 다 "겹치는 행"으로 잡혔었다).
            row_bands = {(r["top"], r["bottom"]) for r in data_rows}
            for (x0, ctop, x1, cbottom) in cells:
                covered = [rb for rb in row_bands
                           if rb[0] >= ctop - 1 and rb[1] <= cbottom + 1]
                if len(covered) < 2:
                    continue
                ci = col_of(x0)
                if ci == 0:
                    continue  # 클래스명 칸은 안 건드린다
                ws = [w for w in words
                      if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                      and ctop - 1 <= (w["top"] + w["bottom"]) / 2 <= cbottom + 1]
                ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                txt = " ".join(w["text"] for w in ws).strip()
                txt = FEE_FOOTNOTE_MARK_RE.sub(" ", txt).strip()
                # 보수율 값처럼 생긴 칸만 옮긴다 - "지급시기" 칸("매 3
                # 개월 후급")도 흔히 네 행에 걸쳐 하나로 병합돼 있는데,
                # 이건 숫자 값이 아니라서 옮기면 나머지 코드(KR5118201004
                # 실측: enrich_with_transposed_fee_table의 코드-열 개수
                # 대조가 이 칸까지 세면서 어긋나 판매회사보수 전체를
                # 잃었다)를 오히려 깨뜨린다.
                if not FEE_VALUE_RE.match(txt.replace(" ", "")):
                    continue
                covered_set = set(covered)
                for r in data_rows:
                    if (r["top"], r["bottom"]) in covered_set and ci not in r["cells"]:
                        r["cells"][ci] = txt

            # 클래스명 칸이 값 행과 다른 y구간에 그려진 문서가 있다
            # (KR5131420007 실측: 값 행엔 0번 열이 아예 없고, 클래스명은
            # 별도 행 구간에 "수수료선취-"/"오프라인(A)"처럼 나뉘어 있다).
            # 같은 구간만 한 행으로 묶으면 클래스명을 통째로 놓쳐 그 표의
            # 클래스가 전부 빠진다 - 값 행과 세로로 겹치는 0번 열 글자를
            # 모아 라벨로 붙인다(겹치는 게 없으면 그대로 둔다).
            first_col_x = col_x0s[0]
            label_ws = [w for w in words
                        if first_col_x - 1 <= (w["x0"] + w["x1"]) / 2 < col_x0s[1] - 1]
            # 클래스명이 표 테두리 바깥 왼쪽에 그려진 문서도 있다
            # (KR5139420015 30쪽 실측: 격자의 0번 칸이 이미 숫자
            # "0.0945"이고 "수수료미징구-오프라인-개인연금(C-p)"은 표
            # 밖에 있다). 그러면 0번 칸이 비어 있지 않아 위 보정이 안
            # 걸리고, 코드를 하나도 못 읽어 그 표의 클래스가 전부 빠진다.
            outside_ws = [w for w in words
                          if (w["x0"] + w["x1"]) / 2 < first_col_x - 1]
            for r in data_rows:
                cur = r["cells"].get(0, "")
                # 클래스 이름이 숫자일 리는 없다 - 숫자면 라벨이 아니라
                # 첫 값 칸이다.
                if cur and not DECIMAL_RE.match(cur.replace(" ", "")):
                    continue
                lo, hi = r["top"], r["bottom"]
                pool = label_ws if not cur else outside_ws
                ws = [w for w in pool
                      if lo - 1 <= (w["top"] + w["bottom"]) / 2 <= hi + 1]
                if ws:
                    # round(top/3) 버킷은 경계에 걸리면 순서를 뒤집는다
                    # (KR5125450070 실측: "총"이 top=85.6, "보수·비용"이
                    # top=85.2로 같은 줄인데 85.2/3=28.4→28, 85.6/3=28.53
                    # →29로 서로 다른 버킷에 떨어져 "보수·비용 총"으로
                    # 뒤집혔다 - 항목 이름을 못 알아봐 total_fee_and_cost가
                    # 통째로 안 잡혔다). 실제 줄 사이 간격으로 묶는다 -
                    # 같은 줄 안 글자들의 top차는 보통 1pt 안팎이고 다음
                    # 줄까지는 대개 15pt 넘게 벌어져 있어 문턱을 널찍하게
                    # 잡아도(4pt) 안전하다.
                    ws.sort(key=lambda w: w["top"])
                    lines, cur_line, prev_top = [], [], None
                    for w in ws:
                        if prev_top is not None and w["top"] - prev_top > 4:
                            lines.append(cur_line)
                            cur_line = []
                        cur_line.append(w)
                        prev_top = w["top"]
                    if cur_line:
                        lines.append(cur_line)
                    ordered = [w for line in lines
                               for w in sorted(line, key=lambda w: w["x0"])]
                    label = " ".join(w["text"] for w in ordered).strip()
                    if cur:
                        # 0번 칸은 값이므로 라벨은 따로 둔다.
                        r["label_outside"] = label
                    else:
                        r["cells"][0] = label

            # 값 행이 하나뿐인 표도 넘긴다. 보수표가 페이지 경계에서 잘려
            # 다음 장에 한 줄만 남는 문서가 흔한데(KR5114420022 37쪽 실측:
            # "수수료미징구-온라인슈퍼-개인연금(S-P) 0.26 0.12 0.04 0.00
            # 0.42 …" 한 줄), 두 줄 미만이면 버리고 있어서 그 클래스를
            # 통째로 잃었다. 같은 모양이 최소 6개 문서에 있다.
            #
            # 이 완화가 위험하지 않은 이유는 여기서 쓸지 말지를 정하지
            # 않기 때문이다. 뒤(enrich_with_detail_fee_table)에서 총보수와
            # 판매보수 열을 요약표 값으로 맞춰 보거나, 못 맞추면 바로 앞
            # 페이지에서 검증된 열 구성을 x좌표로 물려받아야만(그것도 안
            # 되면 그냥 건너뛴다) 이 행을 담는다. 보수표가 아닌 한 줄짜리
            # 표는 거기서 걸러진다.
            if data_rows:
                if (pending_header and pending_header["page"] == i
                        and len(pending_header["col_x0s"]) == len(col_x0s)):
                    header_rows = pending_header["header_rows"] + header_rows
                pending_header = None
                results.append((i + 1, header_rows, data_rows, col_x0s))
            elif header_rows:
                pending_header = {
                    "page": i + 1, "header_rows": header_rows,
                    "col_x0s": col_x0s,
                }
    return _stitch_labels_across_pages(results, pdf)


def _label_has_code(text):
    t = re.sub(r"\s+", "", text or "")
    for regex in (DETAIL_FEE_CLASS_CODE_NESTED_RE,
                  DETAIL_FEE_CLASS_CODE_NESTED_UNCLOSED_RE, CLASS_CODE_RE,
                  DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
        if any(not _is_bad_code(x.group(1))
               for x in regex.finditer(t)):
            return True
    return False


def _first_code_join(cur, frags, max_frags=6):
    """cur 뒤에 frags를 하나씩 늘려 붙이며 코드가 완성되는 지점을 찾는다.

    돌려주는 것은 그 지점까지 이어붙인 조각(코드를 완성한 문자열),
    못 찾으면 None. 이름표가 조각 세 개 이상으로도 쪼개지는 문서가
    있어서(KR5144420091 35쪽 실측: "온라인슈퍼-" / "개인연금 (S-" /
    "P1(연금저축))" 석 줄) 조각 하나만 보고 고르면 못 잡는다. max_frags는
    안전판 - 이 이름표와 무관한 뒷내용까지 한없이 이어붙이지 않는다."""
    acc = ""
    for frag in frags[:max_frags]:
        acc = f"{acc} {frag}".strip()
        # 문서 제목(모든 페이지 위에 똑같이 반복되는 상품명)이 조각으로
        # 잡혀서, 그 안 괄호(예: "(UH)")가 우연히 코드처럼 보여 엉뚱한
        # 클래스가 생긴 적이 있다(KR5144420081 실측: "NH-Amundi USD
        # 초단기채권 증권자투자신탁(UH)[채권]"의 "(UH)"가 코드로 읽힘).
        # 진짜 이름표 조각은 아무리 길어도 이 정도는 안 된다.
        if len(re.sub(r"\s+", "", cur + acc)) > 60:
            return None
        if _label_has_code(re.sub(r"\s+", "", cur + acc)):
            return acc
    return None


def _stitch_labels_across_pages(results, pdf=None):
    """페이지 경계에서 잘린 이름표를 앞 장 마지막 값 행에 이어 붙인다.

    표가 페이지를 넘어가면 마지막 클래스의 이름표만 다음 장 첫머리로
    넘어가는 문서가 있다(KR5194450018 실측).

        36쪽 마지막 값 행: "수수료미징구- 오프라인-"  0.72 0.70 0.03 …
        37쪽 머리글 자리 : "보수체감(C4)"            (값 칸 전부 빔)

    이러면 그 행은 코드를 못 읽어 통째로 빠진다 - 보수는 멀쩡히 읽어
    놓고 어느 클래스 것인지 몰라 버리는 셈이다.

    페이지 안에서 쓰는 규칙과 같다: 이름표 칸은 값 행에서 시작해 아래로
    이어지고 코드가 끝에 온다. 그게 페이지를 넘을 뿐이다. 다만 아무
    조각이나 갖다 붙이면 안 되므로 세 가지를 다 만족할 때만 잇는다.

      - 두 표가 바로 이웃한 페이지이고 열 개수가 같다.
      - 앞 장 마지막 값 행에 코드가 없다(있으면 이미 온전한 이름표다).
      - 다음 장 조각이 첫 칸에만 있고 값 칸이 없으며, 클래스 코드처럼
        생겼다(하나씩 늘려 붙여도 좋다 - _first_code_join 참고).
        "명칭 (클래스)"나 "지급비율(연간, %)" 같은 머리글은 괄호 안이
        한글이라 코드로 안 잡힌다.
    """
    stitched_pages = set()
    for k in range(1, len(results)):
        prev_page, _prev_hdr, prev_rows, prev_x = results[k - 1]
        page, hdr, _rows, xs = results[k]
        if page != prev_page + 1 or len(xs) != len(prev_x):
            continue
        last = prev_rows[-1]
        cur = last.get("label_outside") or last["cells"].get(0, "")
        if _label_has_code(cur):
            continue
        # 이어붙일 조각은 hdr 맨 끝(데이터 행 바로 위)에 있는, 0번 칸만
        # 있는 연속 구간이다 - 앞에서부터 훑으면 "명칭 (클래스)"처럼
        # 표 제목 자리의 0번-칸-단독 행에서 먼저 멈춰 버려 정작 표
        # 머리글(다열) 다음에 오는 진짜 조각을 못 본다(KR5194450018
        # 실측: hdr이 [지급비율(단열) / 명칭(클래스)(단열) / 진짜 표머리글
        # (다열) / "보수체감(C4)"(단열)] 순서라, 앞에서부터면 "명칭
        # (클래스)"에서 멈추고 "보수체감(C4)"엔 닿지도 못한다). 뒤에서부터
        # 훑어야 맞다.
        trailing = []
        for h in reversed(hdr):
            if set(h["cells"]) != {0}:
                break
            trailing.append(h["cells"][0])
        trailing.reverse()
        tail = _first_code_join(cur, trailing)
        if not tail:
            continue
        if last.get("label_outside"):
            last["label_outside"] = f"{cur} {tail}".strip()
        else:
            last["cells"][0] = f"{cur} {tail}".strip()
        stitched_pages.add(prev_page)

    # 다음 물리 페이지가 표로도 안 잡힐 만큼 내용이 적어(간판만 남고
    # 표 자체가 find_tables에 안 걸림) results에 아예 항목이 없는 경우가
    # 있다(KR518101012M 실측: "(S-R)"이 36쪽 맨 위에 표 없이 혼자 떨어져
    # 있다). 그러면 위 방식은 짝지을 다음 항목이 없어 못 잇는다. 표
    # 여부와 상관없이 다음 물리 페이지 맨 위 글자를 직접 본다.
    if pdf is not None:
        for page, _hdr, rows, _x in results:
            if not rows or page in stitched_pages:
                continue
            last = rows[-1]
            cur = last.get("label_outside") or last["cells"].get(0, "")
            if _label_has_code(cur) or page >= len(pdf.pages):
                continue
            words = [w for w in pdf.pages[page].extract_words(x_tolerance=2)
                     if w["top"] < 200]
            lines = {}
            for w in words:
                lines.setdefault(round(w["top"] / 3), []).append(w)
            frags = [" ".join(w["text"] for w in sorted(lines[key], key=lambda w: w["x0"]))
                     for key in sorted(lines)]
            tail = _first_code_join(cur, frags)
            if not tail:
                continue
            if last.get("label_outside"):
                last["label_outside"] = f"{cur} {tail}".strip()
            else:
                last["cells"][0] = f"{cur} {tail}".strip()
    return results


_CLASS_LABELS_BY_DOC = None
_CLASS_RAW_LABEL_BY_DOC = {}


def _class_labels_for_doc(doc_id):
    """이 상품 클래스들의 이름표(수수료방식·판매경로·계좌종류·속성).
    class_meaning.json이 아직 없으면 조용히 빈 결과 - 있으면 좋고 없어도
    기존 동작 그대로다(_known_classes_for_doc과 같은 관례)."""
    global _CLASS_LABELS_BY_DOC
    if _CLASS_LABELS_BY_DOC is None:
        _CLASS_LABELS_BY_DOC = {}
        fp = os.path.join(REPO_ROOT, "class_meaning.json")
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                for r in json.load(f):
                    if r.get("class_code"):
                        _CLASS_LABELS_BY_DOC.setdefault(r["product_code"], {})[
                            r["class_code"]] = (
                                r.get("fee_type"), r.get("channel"),
                                r.get("account_type"),
                                tuple(r.get("attributes") or ()))
                        _CLASS_RAW_LABEL_BY_DOC.setdefault(
                            r["product_code"], {})[r["class_code"]] = \
                            r.get("raw_label")
    return _CLASS_LABELS_BY_DOC.get(doc_id, {})


def _spelling_key(code):
    """붙임표·대소문자·괄호만 지운 열쇠. "같은 클래스다"가 아니라
    "따져 볼 후보다"라는 뜻이다 - 같은지는 이름표가 정한다."""
    return re.sub(r"\(.*?\)", "", code or "").replace("-", "").upper()


FEE_SOURCE_FIELDS = ("total_fee", "distribution_fee",
                     "peer_avg_fee", "total_fee_and_cost")


def _record_source(row, source, page, values):
    """이 값이 문서의 어느 표에서 왔는지 남긴다.

    간이투자설명서는 같은 값을 앞쪽 요약표와 뒤쪽 상세표에 두 번 싣는데,
    총보수·비용은 두 곳이 어긋나는 문서가 있다(KR5110501016 종류A:
    3쪽 0.31 / 27쪽 0.30). 어느 쪽이 맞다고 판정할 근거가 없어서 한
    쪽을 골라 담으면 그건 문서에 없는 판단을 우리가 하는 것이고, 고객이
    다른 쪽 페이지를 열면 틀린 답이 된다. 둘 다 남긴다."""
    sources = row.setdefault("value_sources", [])
    by_key = {(s["field"], s["source"]): s for s in sources}
    for field in FEE_SOURCE_FIELDS:
        value = values.get(field)
        if value is None:
            continue
        key = (field, source)
        existing_entry = by_key.get(key)
        if existing_entry is None:
            entry = {"field": field, "source": source,
                      "value": str(value), "page": page}
            sources.append(entry)
            by_key[key] = entry
            continue
        # 같은 (필드, 표종류)가 이미 기록돼 있어도, 그게 "-"(이 페이지엔
        # 아직 없다는 표시) 자리표시자이고 이번 값이 진짜 숫자면 갱신
        # 해야 한다 - 상세표가 여러 페이지에 걸쳐 있어서, 부실한 앞
        # 페이지(총보수·비용 칸 자체가 없는 표)에서 클래스가 먼저
        # 추가된 뒤 더 자세한 뒷 페이지에서 실제 값으로 패치되는
        # 문서가 있다(KR510902511M 실측: 14쪽엔 총보수·비용 칸이 없어
        # "-"로 기록됐는데, 28쪽 상세표에서 진짜 값 1.87로 패치됐다.
        # 그런데 이 함수가 "상세표"라는 source가 이미 기록됐다고 보고
        # 새 값을 스킵해서, 행 자체(위 patch 루프)는 1.87로 바뀌었는데
        # value_sources만 "-"인 채로 남아 값과 출처가 어긋났다). 반대로
        # 이미 진짜 값이 있는데 이번 것도 진짜 값이면(서로 다른 상세표
        # 페이지가 각자 다른 숫자를 싣는 경우) 손대지 않고 둘 다
        # 남긴다 - 어느 쪽이 맞다고 판정할 근거가 없기 때문이다(위
        # docstring 참고).
        if existing_entry["value"] in DASHES and str(value) not in DASHES:
            existing_entry["value"] = str(value)
            existing_entry["page"] = page
        elif existing_entry["value"] in DASHES and str(value) in DASHES:
            continue
        elif str(value) == existing_entry["value"]:
            continue
        else:
            sources.append({"field": field, "source": source,
                              "value": str(value), "page": page})


DETAIL_COL_NAMES = {
    "total_fee_and_cost": ("총보수비용", "총보수·비용", "총보수ㆍ비용"),
    "peer_avg_fee": ("동종유형",),
}


def _column_by_name(label_by_col, field):
    """값으로 열을 못 찾은 필드를 칸 이름으로 찾아본다.

    이름만 보고 열을 정하는 건 위험하다 - "동종유형총보수"를 "총보수"로
    잘못 읽어서 24개 문서가 어긋난 것처럼 보인 적이 있다. 그래서 이건
    표를 채택할지 정하는 총보수·판매보수엔 절대 쓰지 않는다. 이미 그
    두 열로 검증이 끝난 표에서, 두 표가 값을 다르게 적어 열을 특정하지
    못한 두 필드에만 쓴다(안 그러면 상세표에 멀쩡히 있는 총보수·비용을
    "-"로 담게 된다 - 정작 두 값을 남기려던 그 값이다).

    "합성총보수비용"은 피투자 집합투자기구 보수까지 더한 다른 값이라
    총보수·비용으로 보면 안 된다."""
    for ci, name in enumerate(label_by_col):
        n = (name or "").replace(" ", "").translate(DOT_NORMALIZE_TRANS)
        if not any(w in n for w in DETAIL_COL_NAMES[field]):
            continue
        if field == "total_fee_and_cost" and "합성" in n:
            continue
        return ci
    return None


def _detail_total_fee(cols, total_col, total_sum_cols):
    """상세표 행에서 총보수를 읽는다(단일 칸이거나 앞쪽 칸들의 합)."""
    if total_col is not None:
        return cols.get(total_col)
    if total_sum_cols and all(c in cols for c in total_sum_cols):
        return f"{sum(float(cols[c]) for c in total_sum_cols):.4f}"
    return None


def _normalize_code_via_labels(code, labels):
    """상세표에서 읽은 코드가 문서 자체의 글자 결락으로 짧게 잡혔을 수
    있다 - 괄호 안 속성이 통째로 떨어져 나간다(KR5144420020 33쪽 실측:
    "(" 글자 하나가 원문 콘텐츠 스트림에서 빠져 "수수료미징구-온라인
    슈퍼-퇴직연금(S-P2)퇴직연금))"으로 찍혀 있다 - 다른 6곳은 전부
    "S-P2(퇴직연금)"이라 이 한 곳만 결함이다. 값(0.1550)은 열 대조로
    정확히 맞았지만 코드만 "S-P2"로 잘렸다).

    class_meaning.json(명칭표 기준이라 이런 표보다 코드를 안정적으로
    읽는다)에 철자열쇠(붙임표·괄호 지운 열쇠)가 같은 코드가 정확히
    하나 있으면 그걸로 바꾼다. 후보가 둘 이상이면 어느 쪽인지 모르므로
    손대지 않는다."""
    if not code or code in labels:
        return code
    key = _spelling_key(code)
    twins = [k for k in labels if k != code and _spelling_key(k) == key]
    return twins[0] if len(twins) == 1 else code


def _same_class_in_summary(code, known, labels):
    """상세표의 이 코드가 요약표의 어느 클래스와 같은 클래스인가.

    같은 문서 안에서 요약표는 "A-e", 상세표는 "Ae"로 적는 일이 있다
    (KR5110501016 실측). 그대로 두면 한 클래스가 class_fees에 두 행으로
    들어가서, 수수료를 물으면 같은 클래스가 두 번 나온다.

    그렇다고 표기가 비슷하다고 합치면 안 된다. 붙임표만 다른데 실제로는
    다른 클래스인 짝이 있다(KR5114420027: C-P는 개인연금, Cp(퇴직연금)은
    퇴직연금). 문서가 적어 둔 이름표가 완전히 같을 때만 같은 클래스로
    본다 - 후보가 둘 이상이면 어느 쪽인지 못 정하므로 손대지 않는다."""
    if not code or code in known:
        return code
    mine = labels.get(code)
    if mine is None:
        return code
    key = _spelling_key(code)
    twins = [k for k in known
             if _spelling_key(k) == key and labels.get(k) == mine]
    return twins[0] if len(twins) == 1 else code


# 보수표가 뒤집혀 있는 문서가 있다 - 클래스가 열이고 보수 항목이 행이다.
#
#   구 분          |   A   |  A2   |  A-e  |  A-G  |   C   ...
#   집합투자업자보수 | 0.0750| 0.0750| 0.0750| 0.0750| 0.0750
#   판매회사보수    | 0.1500| 0.0000| 0.1000| 0.1125| 0.3500
#   총보수         | 0.2500| 0.1000| 0.2000| 0.2125| 0.4500
#
# "행 하나 = 클래스 하나"를 전제한 기존 경로로는 클래스 코드조차 못 읽어서
# 이런 문서 4개(클래스 33개)의 보수가 통째로 비어 있었다.
FEE_ITEM_FIELDS = (
    # 순서가 중요하다 - "총보수·비용"에도 "총보수"가 들어 있어서 긴 이름
    # 부터 봐야 한다.
    ("total_fee_and_cost", ("총보수비용", "총보수·비용", "총보수ㆍ비용", "총보수∙비용")),
    ("peer_avg_fee", ("동종유형",)),
    ("distribution_fee", ("판매회사보수", "판매보수")),
    ("total_fee", ("총보수",)),
)
# 뒤집힌 표의 머리글엔 코드가 괄호 없이 그대로 놓인다("A2", "C-e").
# 코드 뒤에 자격 설명이 괄호로 붙기도 한다("S-P(퇴직)"). 한 열만 못
# 읽어도 열 개수가 안 맞아 그 표를 통째로 버리게 된다(KR5118201004
# 실측: 값 열은 8개인데 코드가 7개만 잡혀 6개 클래스를 잃었다).
#
# 코드 자체에 한글이 그대로 들어간 문서가 있다("C-퇴직연금", "S-퇴직",
# "C-퇴직e" - KR5127420034 실측). 처음엔 [A-Za-z0-9]만 허용해서 이 셋을
# 코드로 못 읽어 그 문서 15개 클래스 중 3개(총보수 0.456/0.320/0.250)를
# 통째로 잃고 있었다. 붙임표 뒤에 한글 1~6자(+선택적으로 알파벳 0~2자,
# "C-퇴직e"처럼)가 오는 경우까지 넓힌다. 이 자리는 "표 안에서 이미 숫자
# 칸이 5개 이상인 행의 첫 칸"에서만 쓰여서(_detail_fee_grids의
# grid_rows 필터 참고), 넓혀도 서술형 문장이 코드로 오인될 자리가 아니다.
# 코드가 순수 한글("직판")이거나 한글+라틴 혼합("직판F")인 문서가 있다
# (KR5153420105 실측: "종류직판F 수수료미징구-오프라인-랩,펀드등"). 위
# CLASS_CODE_RE와 같은 이유로 "직판" 낱말만 좁게 예외로 둔다.
RE_BARE_CLASS_CODE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9]{0,3}"
    r"(?:-(?:[A-Za-z0-9]{1,3}|[가-힣]{1,6}[A-Za-z0-9]{0,2}))?"
    r"(?:\([^()]{1,10}\))?"
    r"|직판[A-Za-z0-9]{0,3})$")


def _fee_item_field(label):
    """행 앞머리 글자 → 이 행이 무슨 보수인지. 못 알아보면 None."""
    n = re.sub(r"\s+", "", label or "")
    # 가운뎃점 자리에 문서마다 다른 글자를 쓴다("·"/"ㆍ"/"∙"/"․" -
    # KR5125450023 실측: "총 보수․비용"은 U+2024(온점 지도리)를 쓴다).
    # FEE_ITEM_FIELDS에 없는 글자면 "총보수·비용" 매칭이 안 돼 "총보수"로만
    # 걸려서 "총보수" 행의 값을 덮어써 버린다. 한 글자로 통일한다.
    n = n.translate(DOT_NORMALIZE_TRANS)
    # "합성총보수·비용"은 피투자 집합투자기구 보수까지 더한 다른 값이다.
    if not n or "합성" in n:
        return None
    for field, words in FEE_ITEM_FIELDS:
        if any(w in n for w in words):
            return field
    return None


RE_CODE_HYEONG_PREFIX = re.compile(
    r"^([A-Za-z][A-Za-z0-9]{0,3}(?:-[A-Za-z0-9]{1,3})?)(?:형)?\(")
RE_CODE_HYEONG_PREFIX_ALL = re.compile(
    r"([A-Za-z][A-Za-z0-9]{0,3}(?:-[A-Za-z0-9]{1,3})?)(?:형)?\(")
# 옆 열 머리글과 사이 간격이 좁아 pdfplumber가 두 열의 글자를 한 단어로
# 붙여 읽는 문서가 있다(KR5125450070 실측: "C-P2(수수료C-Pe(수수료" -
# 원래는 서로 다른 열의 "C-P2(수수료..." "C-Pe(수수료..."인데 한
# 단어로 합쳐졌다). _class_code_token은 한 단어에서 코드 하나만
# 돌려주므로 이런 단어는 둘째 코드를 통째로 잃는다.
# 뒤집힌 상세표 머리글이 "코드+형+(설명"으로 붙어 나오는 문서가 있다
# (KR5125450023 30쪽 실측: "C-G형(수수료미징구-오프라인-무권유저비용)",
# 같은 표에서 S 계열은 "형" 없이 "S-P2(수수료...)"). 위 정규식들은 코드
# 바로 뒤에 괄호가 오거나("Cp(퇴직연금)") 괄호 안에 코드가 있는
# 경우("(A2)")만 잡아서, "형"이 코드와 괄호 사이에 끼면 아무것도 못
# 잡고 그 표 전체를 잃는다.
def _class_code_token(text):
    """글자 하나가 클래스 코드로 보이면 코드를, 아니면 None."""
    t = re.sub(r"\s+", "", text or "")
    for regex in (DETAIL_FEE_CLASS_CODE_NESTED_RE,
                  DETAIL_FEE_CLASS_CODE_NESTED_UNCLOSED_RE, CLASS_CODE_RE,
                  DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
        mm = [x for x in regex.finditer(t)
              if not _is_bad_code(x.group(1))]
        if mm:
            return mm[-1].group(1)
    if RE_BARE_CLASS_CODE.match(t) and not _is_bad_code(t):
        return t
    m = RE_CODE_HYEONG_PREFIX.match(t)
    if m and not _is_bad_code(m.group(1)):
        return m.group(1)
    return None


def _code_line_in_band(page, top, bottom, col_x0s=None):
    """이 y구간에서 클래스 코드가 늘어선 줄을 찾는다.

    뒤집힌 표는 머리글이 격자 행으로 안 잡히는 자리에 있다(한 페이지에
    표가 두 덩이 쌓이면 둘째 덩이 머리글은 데이터 행 사이에 끼어 있어
    격자에서 아예 빠진다 - KR5118420062 30쪽 실측: y=601에
    "C-F C-P C-Pe C-W C-P1 C-P1e S"가 그대로 있는데 못 읽고 있었다).
    격자 대신 글자를 직접 본다. 돌려주는 것은 ([(x, 코드, 그 코드 글자의
    top)], 코드 중 가장 위쪽의 y). 열마다 자기 코드가 실제로 찍힌 top을
    같이 들고 다니는 이유는 _printed_labels 참고.

    열마다 클래스명 줄바꿈 높이가 달라 코드가 한 줄에 다 안 늘어서고
    여러 줄에 흩어지는 문서가 있다(KR5125450023 30쪽 실측: C-G/C-P2형은
    top=70.6, S-P2는 74.9, C-P/C-Pe/C-P2e형/S-P는 79.6, S는 88.6 -
    네 줄에 나뉘어 있고 그중 S가 있는 줄엔 옆 열의 이어지는 설명 글자가
    섞여 "코드만 늘어선 줄" 기준에도 안 걸린다). col_x0s를 주면 줄이
    아니라 이 표의 열 x좌표로 묶어서, 그 열 구간 안에서 가장 먼저(위쪽)
    나오는 코드를 그 열의 코드로 삼는다 - 줄이 아니라 열 단위라 옆 열의
    설명 글자가 섞여도 상관없다."""
    ws = [w for w in page.extract_words(x_tolerance=2)
          if top < (w["top"] + w["bottom"]) / 2 < bottom]
    if col_x0s:
        # 값 격자의 열 경계(col_x0s)에 맞춰 보려 했으나, 값 칸은 병합된
        # 세부 칸으로 잘게 쪼개져 있어(KR5118420062 30쪽 실측: 시각적
        # 열 간격은 50~63pt인데 col_x0s는 그 사이에 9~10pt짜리 부칸이
        # 껴 있어 15개) 코드 글자 x가 어느 col_x0s에도 가깝게 안 맞는
        # 경우가 흔하다("A" x0=126.0, 가장 가까운 col_x0s도 24.4pt
        # 떨어져 있음). 격자 열이 아니라 코드 글자끼리의 x 근접도로 직접
        # 묶는다 - 실측상 같은 열의 코드는 겹치거나 몇 pt 이내인데 반해
        # 옆 열까지는 최소 47pt는 떨어져 있다.
        cands = []
        for w in ws:
            t = re.sub(r"\s+", "", w["text"])
            multi = list(RE_CODE_HYEONG_PREFIX_ALL.finditer(t))
            if len(multi) >= 2:
                # 옆 열과 붙어서 한 단어에 코드가 둘 이상 들어 있다. 글자
                # 위치 비율로 이 단어의 가로폭 안에서 x를 나눠 추정한다 -
                # 정확한 좌표는 아니지만 옆 열과 구분하기엔 충분하다.
                width = w["x1"] - w["x0"]
                for mm in multi:
                    if _is_bad_code(mm.group(1)):
                        continue
                    frac = mm.start() / len(t)
                    cands.append((w["x0"] + frac * width, mm.group(1), w["top"]))
                continue
            code = _class_code_token(w["text"])
            if code:
                cands.append((w["x0"], code, w["top"]))
        cands.sort()
        clustered = []
        for x, code, t in cands:
            if clustered and x - clustered[-1][0] < 25:
                if t < clustered[-1][2]:
                    clustered[-1] = [x, code, t]
                continue
            clustered.append([x, code, t])
        if len(clustered) >= 2:
            code_y = min(t for _x, _c, t in clustered)
            return [(x, code, t) for x, code, t in clustered], code_y
        # 열로 못 묶이면(코드가 하나도 안 잡히면) 줄 방식으로 넘어간다.
    lines = {}
    for w in ws:
        lines.setdefault(round(w["top"] / 3), []).append(w)
    best, best_y = [], None
    for _y, row in sorted(lines.items()):
        codes = []
        for w in sorted(row, key=lambda x: x["x0"]):
            code = _class_code_token(w["text"])
            if code:
                codes.append((w["x0"], code, w["top"]))
        # 코드만 늘어선 줄이어야 한다 - 설명 글자가 섞인 줄은 머리글이
        # 아니다(값 행에서 우연히 몇 개 걸리는 것도 이걸로 걸러진다).
        if len(codes) >= 2 and len(codes) >= len(row) - 2 and len(codes) > len(best):
            best, best_y = codes, min(w["top"] for w in row)
    return best, best_y


def _codes_in_band(page, top, bottom, col_x0s=None):
    line, _y = _code_line_in_band(page, top, bottom, col_x0s)
    return [c for _x, c, _t in line]


def _printed_labels(page, line, bottom, tol=30):
    """코드 줄 바로 아래에 열마다 인쇄된 이름표를 모은다.

    뒤집힌 보수표는 코드 밑에 그 클래스가 무엇인지를 같이 찍는다
    ("C-P / 수수료미징구-오프라인-개인연금"). 요약표에 있는 클래스가
    하나도 없는 표는 값으로 대조할 수가 없는데, 이 이름표를 문서 앞쪽
    "종류형 명칭" 표에서 뽑아 둔 이름표와 맞춰 보면 열과 클래스를 제대로
    짝지었는지 확인할 수 있다 - 같은 문서의 다른 표가 독립적으로 같은
    말을 하는 것이라 근거가 된다.

    컷오프(코드 줄 바로 아래부터)를 열마다 따로 쓴다 - 열마다 코드가
    찍힌 top이 다른 문서가 있어서(KR5125450023 30쪽 실측: C-G/C-P2형은
    70.6인데 C-P/C-Pe는 79.6) 전체에 한 컷오프를 쓰면, 컷오프가 가장
    위쪽 열(C-P)의 top으로 잡히는 바람에 정작 그 열보다 더 위에서 코드
    자체가 찍힌 다른 열(C-G)은 코드 글자("C-G형(수수료")까지 이름표에
    잘못 포함되던 게 아니라 반대로 코드 글자 자체가 컷오프보다 위라서
    빠지고 그다음 줄(옆 열 설명이 흘러들어온 조각)부터만 잡혀 이름표가
    "수수료"로 시작 안 하고 잘렸다.

    tol: 코드 글자는 숫자 칸처럼 각 열의 가운데에 찍히는데, 그 밑
    설명 문단은 왼쪽 정렬이라 코드보다 왼쪽으로 더 치우쳐 찍히는
    문서가 있다(KR5147430065 34쪽 실측: 코드 x=147.0인데 설명
    "수수료미징구-" 첫 조각은 x=119.5 - 27.5pt 차이로 기존 22pt
    문턱을 넘어 버려 그 열의 설명이 통째로 안 잡혔다). 열 간격이
    보통 60pt 넘게 벌어져 있어(그 문서 실측 63.9pt) 30pt까지는
    옆 열과 헷갈릴 걱정 없이 늘려도 된다."""
    xs = [x for x, _c, _t in line]
    tops = [t for _x, _c, t in line]
    got = {}
    for w in page.extract_words(x_tolerance=2):
        mid = (w["top"] + w["bottom"]) / 2
        i = min(range(len(xs)), key=lambda k: abs(xs[k] - w["x0"]))
        if abs(xs[i] - w["x0"]) >= tol:
            continue
        if not (tops[i] - 1 < mid < bottom):
            continue
        got.setdefault(i, []).append(w)
    return {i: "".join(w["text"] for w in
                       sorted(v, key=lambda w: (round(w["top"] / 3), w["x0"])))
            for i, v in got.items()}


def _row_value_cols(row):
    """이 행에서 숫자가 들어 있는 열 번호."""
    return {ci for ci, v in row["cells"].items()
            if ci and DECIMAL_RE.match(v.replace(" ", "")) and "%" not in v}


def _transposed_blocks(grid_rows):
    """항목 이름이 다시 나오는 자리에서 덩이를 가른다.

    같은 표가 한 페이지에 여러 덩이 쌓여 있는 문서가 있다(KR5118420062
    실측: 30쪽에 클래스 7개짜리 표가 두 번). 덩이마다 클래스가 다르므로
    따로 읽어야 한다."""
    blocks, seen, cur, cur_cols = [], set(), [], None
    for r in grid_rows:
        # 라벨이 표 밖(왼쪽 여백)에 찍혀 0번 칸 자체가 이미 값인 표가
        # 있다(KR5125450070 28쪽 실측: 표 테두리가 x=107.1부터 시작해
        # "기타비용"/"총 보수·비용" 같은 항목 이름이 통째로 테두리 밖에
        # 남는다 - 위 _detail_fee_grids가 그럴 때 label_outside에 담아
        # 두는데, 여기서 0번 칸 값만 보면 이름을 못 읽어 이 덩이의 모든
        # 행이 항목 미상(f=None)이 되고, 그 안의 total_fee_and_cost 등이
        # 통째로 안 잡힌다.
        label0 = r.get("label_outside") or r["cells"].get(0, "")
        key = re.sub(r"\s+", "", label0)
        cols = _row_value_cols(r)
        # 항목 이름이 되풀이되면 새 덩이다. 그것만으로는 모자란데, 앞
        # 페이지에서 이어지는 덩이는 위쪽 항목이 없어서 이름이 겹치지
        # 않은 채로 다음 표와 붙어 버린다(KR5118201004 실측: 38쪽 위는
        # 37쪽에서 이어지는 표라 "총보수"부터 시작하고, 그 아래 새 표의
        # "집합투자업자보수"가 같은 덩이로 묶였다). 두 표는 값이 놓인
        # 열이 아예 다르므로(1/2/4/6 대 1/3/5/7) 그것으로도 가른다.
        split = key in seen
        if not split and cur_cols and cols:
            overlap = len(cols & cur_cols) / min(len(cols), len(cur_cols))
            split = overlap < 0.6
        if split:
            blocks.append(cur)
            seen, cur, cur_cols = set(), [], None
        seen.add(key)
        cur.append((_fee_item_field(label0), r))
        if cols and (cur_cols is None or len(cols) > len(cur_cols)):
            cur_cols = cols
    if cur:
        blocks.append(cur)
    return blocks


def enrich_with_transposed_fee_table(doc_id, existing_rows):
    """클래스가 열, 보수 항목이 행인 보수표에서 빠진 클래스를 보강한다.

    채택 기준은 행 방향 표와 같다 - 요약표에 이미 있는 클래스 둘 이상의
    총보수와 판매보수가 정확히 맞아야 이 표를 이 상품의 보수표로 본다.
    요약표는 정답지가 아니라 "어느 열이 어느 클래스인지" 확인하는 자다."""
    known = {r["class_code"]: r for r in existing_rows if r.get("class_code")}
    if len(known) < 2:
        return existing_rows
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return existing_rows

    def close(a, b, tol=0.0005):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False

    labels = _class_labels_for_doc(doc_id)
    new_rows, added = [], set()
    # 머리글이 앞 격자나 앞 페이지에 있고 값만 이어지는 문서가 있다
    # (KR5118201004 실측: 37쪽에 코드 9개, 38쪽에 값 9열). 마지막으로
    # 읽은 코드 줄을 들고 다니다가 열 개수가 같으면 물려준다.
    last_codes, last_codes_page = [], None
    # 반대로 표 자체(총보수·판매보수 행 포함)는 온전히 한 페이지 안에서
    # 검증됐는데, "합성 총보수·비용"처럼 표 맨 아래쪽 행 하나만 다음
    # 페이지로 넘어가는 문서가 있다(KR5125450070 실측: 27쪽에 코드 8개
    # +총보수·판매보수 행까지 다 있어 표가 확인되는데, "합성 총보수·
    # 비용" 행만 28쪽 맨 위에 남아 있다 - 그 행이 속한 덩이엔 총보수
    # 행이 없어 "뒤집힌 보수표가 아니다"로 걸러지면서 8개 클래스의
    # total_fee_and_cost가 통째로 안 채워졌다). 방금 검증된 덩이의
    # (코드,값열) 짝과 그때 새로 만든 행을 들고 있다가, 바로 다음
    # 페이지의 총보수 없는 덩이를 만나면 그 행들을 값 열 순서로
    # 짝지어 패치한다 - 열 인덱스는 페이지마다 다시 매겨지므로 번호가
    # 아니라 "왼쪽부터 순서"로만 맞춘다(같은 표가 이어지는 것이므로
    # 순서는 그대로다).
    prev_validated = None
    # prev_validated의 반대 방향: 총보수 행이 없는(그래서 "뒤집힌 보수표가
    # 아니다"로 버려지는) 덩이가 먼저 나오고, 총보수 행이 있는 확인된 덩이가
    # 그 다음 페이지에 나오는 문서가 있다(KR5118201004 실측: 37쪽엔 코드
    # 9개 아래 집합투자업자보수/판매회사보수/신탁업자보수/일반사무관리
    # 회사보수 네 행만 있고 총보수 행이 없어 이 덩이가 버려진다 - 총보수
    # 행 자체는 38쪽 맨 위에 이어져 있다). 버려지는 덩이라도 코드별 값은
    # 이미 다 읽었으니 페이지·코드가 맞으면 다음 덩이에 되짚어 준다.
    pending_block = None
    with pdfplumber.open(pdfs[0]) as pdf:
        for page_num, header_rows, grid_rows, col_x0s in _detail_fee_grids(pdf):
            page = pdf.pages[page_num - 1]
            blocks = _transposed_blocks(grid_rows)
            prev_bottom = (min(h["top"] for h in header_rows) - 1
                           if header_rows else None)
            # 한 페이지에 덩이가 여러 개면 아래 덩이엔 요약표에 있는
            # 클래스가 한 개뿐일 수 있다(KR5118420062 30쪽: 아래 덩이는
            # C-F/C-P/C-Pe/C-W/C-P1/C-P1e/S인데 요약표에 있는 건 C-P
            # 하나다). 위 덩이가 이미 확인됐고 값 열 구성이 똑같으면
            # 같은 표가 이어지는 것이므로 그 확인을 이어받는다.
            ok_cols = None
            for block in blocks:
                by_field = {f: r for f, r in block if f}
                band_top = prev_bottom
                prev_bottom = block[-1][1]["bottom"]
                # 이 덩이를 쓸 수 있으려면 총보수와 판매보수 행이 있어야
                # 한다 - 이 표가 이 상품의 보수표인지 가리는 두 값이다.
                # "항목이 몇 개 이상"으로 요구하면 안 된다. 페이지 끝에서
                # 잘려 두 행만 남은 덩이가 있는데(KR5118420062 30쪽 아래),
                # 하필 거기에 C-P1/C-Pe/S-P 같은 연금 클래스가 있다.
                # 이 덩이의 머리글은 앞 덩이 끝과 이 덩이 첫 행 사이에 있다.
                # 코드는 항목 행 검사보다 먼저 읽어 둔다 - 머리글만 있고
                # 총보수 행은 다음 페이지에 있는 덩이가 있어서(KR5118201004
                # 실측: 37쪽에 코드 9개, 38쪽에 값 9열), 그 덩이를 버리기
                # 전에 코드를 챙겨야 뒤 페이지가 물려받을 수 있다.
                top = band_top if band_top is not None else block[0][1]["top"] - 120
                data_top = block[0][1]["top"] - 1
                code_line, code_y = _code_line_in_band(page, top, data_top, col_x0s)
                codes = [c for _x, c, _t in code_line]
                if not codes and block is blocks[0] and page_num >= 2:
                    # 머리글이 앞 페이지 맨 아래에 있고 값만 넘어오는 문서가
                    # 있다(KR515302022M 실측: 33쪽 끝에 "구분 A Ae C1 C2 C3
                    # C4 Ce CI CF CW"가 있고 값은 34쪽부터다). 앞 페이지
                    # 아래쪽도 본다.
                    prev_page = pdf.pages[page_num - 2]
                    codes = _codes_in_band(prev_page,
                                           prev_page.height - 170,
                                           prev_page.height, col_x0s)
                if codes:
                    last_codes, last_codes_page = codes, page_num
                if "total_fee" not in by_field:
                    if prev_validated and prev_validated["page"] == page_num - 1:
                        n_expect = len(prev_validated["pairs"])
                        for fld, row in by_field.items():
                            # "-"만 찍힌 유령 칸이 실제 값 칸 수만큼 더
                            # 잡히는 표가 있다(KR515302022M 실측: 35쪽
                            # "총보수·비용" 행은 진짜 클래스 11개인데 셀
                            # 격자가 12칸으로 잡히고 마지막 칸이 "-"다).
                            # 그 칸까지 세면 개수가 안 맞아 물려받기가
                            # 통째로 실패한다 - 숫자 값이 있는 칸만 센다
                            # (total_fee 행의 value_cols와 같은 기준).
                            # 0번 칸도 값일 수 있다(KR5125450070 28쪽
                            # 실측: 라벨이 표 밖에 있어 0번 칸부터 이미
                            # 숫자다) - "ci and"로 0번 칸을 무조건 빼면
                            # 안 된다. DECIMAL_RE가 이미 라벨(문자)을
                            # 걸러내므로 따로 뺄 필요가 없다.
                            row_cols = sorted(
                                ci for ci, v in row["cells"].items()
                                if DECIMAL_RE.match(v.replace(" ", ""))
                                and "%" not in v)
                            if len(row_cols) != n_expect:
                                continue
                            for (code, _old_vc), ci in zip(
                                    prev_validated["pairs"], row_cols):
                                fresh_row = prev_validated["fresh_by_code"].get(code)
                                if fresh_row is None or fresh_row.get(fld) is not None:
                                    continue
                                v = row["cells"].get(ci)
                                if not v:
                                    continue
                                fresh_row[fld] = v
                                _record_source(fresh_row, "상세표", page_num, {fld: v})
                    # 이 덩이 자체가 코드별 값을 갖고 있으면(총보수 행만
                    # 없을 뿐 판매회사보수 등은 있을 수 있다) 다음 페이지의
                    # 확인된 덩이가 되짚어 쓸 수 있게 남겨 둔다.
                    field_rows = {f: r for f, r in by_field.items() if f}
                    if field_rows and codes:
                        pv = {}
                        for fld, row in field_rows.items():
                            # 여기도 0번 칸을 무조건 빼면 안 된다 - 위
                            # prev_validated 되짚기와 같은 이유
                            # (KR5125450070 28쪽).
                            row_cols = sorted(
                                ci for ci, v in row["cells"].items()
                                if DECIMAL_RE.match(v.replace(" ", ""))
                                and "%" not in v)
                            if len(row_cols) == len(codes):
                                pv[fld] = dict(zip(
                                    codes,
                                    (row["cells"].get(ci) for ci in row_cols)))
                        if pv:
                            pending_block = {"page": page_num, "values": pv}
                    continue  # 뒤집힌 보수표가 아니다
                # 값이 든 열(총보수 행 기준)과 코드를 왼쪽부터 순서대로
                # 짝짓는다. 머리글 칸과 값 칸의 x가 어긋나 있어서(코드는
                # 1/3/5/7, 값은 1/2/5/6번 열) 좌표로는 못 맞춘다.
                value_cols = sorted(
                    ci for ci, v in by_field["total_fee"]["cells"].items()
                    if ci and DECIMAL_RE.match(v.replace(" ", "")) and "%" not in v)
                if not codes or len(codes) != len(value_cols):
                    # 머리글이 앞 격자·앞 페이지에 있는 경우 물려받는다.
                    if (last_codes and len(last_codes) == len(value_cols)
                            and last_codes_page is not None
                            and page_num - last_codes_page <= 1):
                        codes = last_codes
                    else:
                        continue

                pairs = [(_same_class_in_summary(code, known, labels), vc)
                         for code, vc in zip(codes, value_cols)]
                # 이 표가 이 상품의 보수표인지는 요약표에 이미 있는
                # 클래스의 값으로 가린다. 다만 표 전체를 한 덩어리로
                # 판정하면 안 된다 - 어떤 행 하나만 어긋나게 읽히는 일이
                # 있기 때문이다(KR515302022M 34쪽 실측: 총보수 행은 네
                # 클래스가 소수점 넷째 자리까지 맞는데, 판매회사보수 행은
                # 원문에 "주6)" 각주가 끼어 값 순서가 틀어졌다). 그걸로
                # 표를 통째로 버리면 멀쩡한 총보수까지 잃는다.
                #
                # 그래서 필드마다 따로 본다. 어긋나는 게 하나도 없는
                # 필드만 믿고 담고, 나머지는 값을 안 담는다(None). 우리가
                # 확인하지 못한 값을 담느니 비워 두는 쪽이다.
                refs = [(c, vc) for c, vc in pairs if c in known]
                trusted, tf_hits, tf_bad = {}, 0, False
                for fld in FEE_SOURCE_FIELDS:
                    row = by_field.get(fld)
                    if row is None:
                        continue
                    # 총보수·비용/동종유형총보수는 같은 문서 안에서도 표마다
                    # 다르게 적히는 값이다(위 enrich_with_detail_fee_table의
                    # 같은 원칙 - KR5110501016 실측: 종류A의 총보수·비용이
                    # 3쪽 0.31 / 27쪽 0.30로 서로 다르다). 이 행은 이미
                    # 칸 이름("총보수·비용"/"동종유형")으로 확실히 찾은
                    # 것이라(_fee_item_field, 열 위치 추측이 아니다) 값이
                    # 다른 표와 어긋난다고 걸러내면 안 된다 - 실제로
                    # 걸러내서 KR5118420036 41쪽의 C-Pe/C-P1/C-P1e/S/S-P
                    # 등 여러 클래스가 total_fee_and_cost를 통째로 잃고
                    # 있었다. total_fee/distribution_fee만 "이 표가 이
                    # 상품 것이 맞는지"를 가리는 값이라 값 대조가 필요하다.
                    if fld in ("total_fee_and_cost", "peer_avg_fee"):
                        trusted[fld] = row
                        continue
                    hit = miss = 0
                    for c, vc in refs:
                        v = row["cells"].get(vc)
                        # 어느 한쪽이 "-"면 견줄 값이 없다.
                        if not v or v == "-" or known[c][fld] in (None, "-"):
                            continue
                        if close(v, known[c][fld]):
                            hit += 1
                        else:
                            miss += 1
                    if not miss:
                        trusted[fld] = row
                    if fld == "total_fee":
                        tf_hits, tf_bad = hit, bool(miss)
                if tf_bad or "total_fee" not in trusted:
                    continue
                # 총보수가 둘 이상 맞으면 이 열 구성을 확인된 것으로 보고
                # 같은 페이지의 다음 덩이에 물려준다. 대조할 게 하나뿐인
                # 덩이는 그 확인을 이어받아야 쓸 수 있다.
                if tf_hits >= 2:
                    ok_cols = value_cols
                elif tf_hits < 1 and ok_cols != value_cols:
                    # 요약표에 있는 클래스가 하나도 없어 값으로는 대조할
                    # 수가 없다. 이 표가 코드 밑에 찍어 둔 이름표를, 문서
                    # 앞쪽 "종류형 명칭" 표에서 뽑아 둔 이름표와 맞춰 본다
                    # (KR515302022M 34쪽 실측: 11개 열이 전부 일치한다).
                    # 같은 문서의 다른 표가 독립적으로 같은 말을 하는 것이라
                    # 열과 클래스를 제대로 짝지었다는 근거가 된다.
                    if not code_line or code_y is None:
                        continue
                    printed = _printed_labels(page, code_line, data_top)
                    raw = _CLASS_RAW_LABEL_BY_DOC.get(doc_id, {})
                    agree = disagree = 0
                    for i, (code, _vc) in enumerate(pairs):
                        want = re.sub(r"\s+", "", raw.get(code) or "")
                        seen = re.sub(r"\s+", "", printed.get(i, ""))
                        if not want or not seen:
                            continue
                        if want in seen or seen in want:
                            agree += 1
                        else:
                            disagree += 1
                    if disagree or agree < 2:
                        continue

                pending_extra = {}
                if pending_block and pending_block["page"] == page_num - 1:
                    pending_extra = pending_block["values"]
                    pending_block = None

                fresh_by_code = {}
                for code, vc in pairs:
                    if not code or code in added:
                        continue
                    vals = {f: trusted[f]["cells"].get(vc)
                            for f in FEE_SOURCE_FIELDS if f in trusted}
                    for fld, code_map in pending_extra.items():
                        if vals.get(fld) in (None, "-") and code_map.get(code):
                            vals[fld] = code_map[code]
                    if code in known:
                        # known의 숫자 필드 자체는 안 건드린다 - 뒤 덩이에서
                        # 이 표가 맞는지 대조할 때 요약표 값을 다시 읽어야
                        # 하기 때문이다. 대신 상세표 값을 value_sources에
                        # 남겨 둔다 - 요약표엔 아예 없던 값(예: 판매회사보수)
                        # 은 파이프라인 끝의 _backfill_from_value_sources가
                        # 이걸 보고 채운다.
                        cur = known[code]
                        _record_source(cur, "상세표", page_num, vals)
                        if not cur.get("fee_breakdown"):
                            bd = [
                                {"label": re.sub(
                                    r"\s+", "",
                                    r.get("label_outside") or r["cells"].get(0, "")),
                                 "value": r["cells"].get(vc)}
                                for f, r in block
                                if f is None and r["cells"].get(vc)
                            ]
                            if bd:
                                cur["fee_breakdown"] = bd
                        fresh_by_code[code] = cur
                        continue
                    if not vals.get("total_fee"):
                        continue
                    added.add(code)
                    fresh = {
                        "class_code": code,
                        "sales_commission_desc": None,
                        "total_fee": vals.get("total_fee"),
                        # 항목 행 자체가 이 덩이에 없으면(앞 페이지에 남아
                        # 있으면) None이다. "-"는 문서가 없다고 적은 것이고
                        # None은 우리가 못 읽은 것이라 뜻이 다르다.
                        "distribution_fee": vals.get("distribution_fee"),
                        "peer_avg_fee": vals.get("peer_avg_fee"),
                        "total_fee_and_cost": vals.get("total_fee_and_cost"),
                        "cost_projection_per_10m": {},
                        "fee_breakdown": [
                            {"label": re.sub(r"\s+", "", r["cells"].get(0, "")),
                             "value": r["cells"].get(vc)}
                            for f, r in block
                            if f is None and r["cells"].get(vc)
                        ],
                        "page": page_num,
                        "source_pages": [page_num],
                        "field_source_pages": {},
                        "evidence": f"[뒤집힌 상세표 보강] 열{vc}",
                        "method": "transposed_detail_table_cross_validated",
                        "confidence": 0.7,
                        "product_code": doc_id,
                    }
                    _record_source(fresh, "상세표", page_num, vals)
                    new_rows.append(fresh)
                    fresh_by_code[code] = fresh
                prev_validated = {
                    "page": page_num, "pairs": pairs, "fresh_by_code": fresh_by_code,
                }
    return existing_rows + new_rows


# 전환 후 표 제목("[운용전환일부터 해지일까지]")만 걸러 낸다 - 전환 전
# 제목("[최초설정일부터 운용전환일 전일까지]")은 "전일까지"로 끝나고
# 전환 후 제목은 "해지일까지"로 끝나 겹치지 않는다.
POST_CONVERSION_MARKER_RE = re.compile(r"해지일까지")


def _fill_transposed_after_conversion(doc_id, rows):
    """운용전환 전후로 뒤집힌 보수표가 통째로 두 번 나오는 문서가 있다
    (KR5147430065 - "목표전환형" 펀드, 전수 조사 기준 이 상품 하나뿐).
    요약표("가.")에 있는 A/C/Ae/Ce는 전환 전/후 값이 한 행에 같이 있어
    total_fee_after_conversion 등이 이미 채워지는데, 뒤집힌 상세표에만
    있는 클래스(AG/CI/CG/CW/C-P/C-Pe/C-P2/C-Pe2)는 enrich_with_
    transposed_fee_table이 요약표 클래스로 검증하는 첫 번째 표("[최초
    설정일부터 운용전환일 전일까지]")만 만들고, 바로 뒤에 이어지는 두
    번째 표("[운용전환일부터 해지일까지]")는 요약표에 없는 클래스뿐이라
    검증 기준이 없어 통째로 못 읽는다 - 전환후 필드가 죄다 빈다.

    500줄짜리 그 함수를 다시 고치는 대신, 별도의 좁은 후처리로 채운다.
    "전환 전"/"전환 후" 어느 표인지는 값 대조가 아니라, 문서에 그대로
    찍혀 있는 표 제목("[최초설정일부터 운용전환일 전일까지]"/"[운용
    전환일부터 해지일까지]")으로 가른다 - 이미 total_fee_after_
    conversion이 있는 클래스(A/C/Ae/Ce)만 정답지로 쓰면, 그 네 클래스가
    아예 안 나오는 열 묶음(CG/CW/C-P/C-Pe/C-P2/C-Pe2)은 대조할 수단이
    없어 전환 전/후를 구분 못 한다(실측:가장 처음 시도에서 이 표기가
    없어 전환 "전" 값을 전환 "후" 필드에 잘못 채웠었다). 표 제목은
    문서 전체에 걸쳐 이 두 문구만 번갈아 나오므로, 그 위치(y좌표)를
    지나칠 때마다 "지금부터는 전환 후 구간"으로 상태를 넘기면 어느
    클래스 열 묶음이든 안전하게 가른다. 기존 행의 다른 필드는 절대
    건드리지 않는다(이미 값이 있으면 건너뜀)."""
    if not any(r.get("total_fee_after_conversion") for r in rows
               if r.get("class_code")):
        return rows
    by_code = {r["class_code"]: r for r in rows if r.get("class_code")}
    if not any(r.get("total_fee") and not r.get("total_fee_after_conversion")
               for r in rows if r.get("class_code")):
        return rows
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return rows

    def x0s_match(a, b, tol=2):
        return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))

    filled = 0
    is_post = False  # 문서 맨 앞은 항상 "전환 전" 구간에서 시작한다
    carry = None  # (codes, value_cols, col_x0s) - 바로 앞 덩이의 구성(페이지 경계로 갈린 항목 행 이어받기용)
    with pdfplumber.open(pdfs[0]) as pdf:
        for page_num, header_rows, grid_rows, col_x0s in _detail_fee_grids(pdf):
            page = pdf.pages[page_num - 1]
            # 표 제목("[...전일까지]"/"[...해지일까지]")이 한 페이지
            # 안에 여러 번(전환전 표 꼬리 + 전환후 표 시작) 나올 수
            # 있다(KR5147430065 34쪽 실측) - y좌표 순으로 모아 둔다.
            markers = sorted(
                (w["top"], bool(POST_CONVERSION_MARKER_RE.search(w["text"])))
                for w in page.extract_words(x_tolerance=2, keep_blank_chars=False)
                if "전일까지" in w["text"] or "해지일까지" in w["text"])
            marker_idx = 0
            blocks = _transposed_blocks(grid_rows)
            prev_bottom = (min(h["top"] for h in header_rows) - 1
                           if header_rows else None)
            for block in blocks:
                by_field = {f: r for f, r in block if f}
                top = prev_bottom if prev_bottom is not None else block[0][1]["top"] - 120
                data_top = block[0][1]["top"] - 1
                prev_bottom = block[-1][1]["bottom"]
                while marker_idx < len(markers) and markers[marker_idx][0] <= data_top:
                    is_post = markers[marker_idx][1]
                    marker_idx += 1
                if "total_fee" in by_field:
                    code_line, _ = _code_line_in_band(page, top, data_top, col_x0s)
                    codes = [c for _x, c, _t in code_line]
                    value_cols = sorted(
                        ci for ci, v in by_field["total_fee"]["cells"].items()
                        if ci and DECIMAL_RE.match(v.replace(" ", "")) and "%" not in v)
                    if codes and len(codes) == len(value_cols):
                        carry = (codes, value_cols, col_x0s)
                    else:
                        carry = None
                elif carry and x0s_match(col_x0s, carry[2]):
                    # "총 보수" 행이 없는 덩이(이 페이지엔 판매회사보수/
                    # 총보수·비용/동종유형 항목만 있는 이어지는 표)라도,
                    # 칸 x좌표 구성이 방금 확인된 덩이와 같으면(같은
                    # 물리적 표가 페이지 경계에서 이어지는 것) 그
                    # codes/value_cols를 그대로 물려 쓴다(KR5147430065
                    # 34/35쪽 실측: "총 보수" 행은 34쪽에, "총보수·비용"/
                    # "동종유형 총보수" 행은 35쪽 맨 위 이어지는 덩이에
                    # 떨어져 있다).
                    codes, value_cols = carry[0], carry[1]
                else:
                    continue
                if not is_post:
                    continue
                for src_field, dst_field in (
                        ("total_fee", "total_fee_after_conversion"),
                        ("distribution_fee", "distribution_fee_after_conversion"),
                        ("total_fee_and_cost", "total_fee_and_cost_after_conversion"),
                        ("peer_avg_fee", "peer_avg_fee_after_conversion")):
                    src_row = by_field.get(src_field)
                    if src_row is None:
                        continue
                    for code, vc in zip(codes, value_cols):
                        r = by_code.get(code)
                        if r is None or r.get(dst_field):
                            continue
                        v = src_row["cells"].get(vc)
                        if not v:
                            continue
                        r[dst_field] = v
                        if dst_field == "total_fee_after_conversion":
                            r.setdefault("fee_period",
                                         "최초설정일부터 운용전환일 전일까지")
                            r.setdefault("field_source_pages", {})[
                                "total_fee_after_conversion"] = page_num
                            pages = r.setdefault(
                                "source_pages", [r.get("page", page_num)])
                            if page_num not in pages:
                                pages.append(page_num)
                            filled += 1
    return rows


def _remap_columns(carry, col_x0s, tol=8):
    """앞 페이지에서 검증된 열 번호를 이 페이지의 열 번호로 옮긴다.

    페이지가 넘어가면 머리글이 없어서 열 경계가 다르게 잡힌다
    (KR5129420025 실측: 49쪽은 17열, 50쪽은 11열 - 머리글이 여러 줄로
    쌓이면서 열이 잘게 쪼개진다). 열 개수가 같기를 요구하면 이어지는
    표를 물려받지 못한다. 열 번호가 아니라 x좌표로 맞춘다 - 같은 표가
    이어지는 것이라 각 열이 그려지는 x는 그대로다.

    총보수와 판매보수 열을 못 옮기면 물려받지 않는다. 그 두 열이 이
    표를 이 상품의 보수표로 특정하는 근거인데, 뒷장엔 대조할 클래스가
    없어서 값으로 다시 확인할 방법이 없기 때문이다."""
    prev = carry["col_x0s"]

    def move(ci):
        if ci is None or ci >= len(prev):
            return None
        near = [j for j, x in enumerate(col_x0s) if abs(x - prev[ci]) <= tol]
        return near[0] if len(near) == 1 else None

    out = {k: move(carry[k]) for k in
           ("dist_col", "peer_col", "cost_col", "total_col")}
    span = carry["total_sum_cols"]
    moved_span = [move(ci) for ci in span] if span else None
    if moved_span and any(c is None for c in moved_span):
        moved_span = None
    out["total_sum_cols"] = moved_span
    out["total_sum_n"] = len(moved_span) if moved_span else None

    if out["total_col"] is None and not moved_span:
        return None
    if out["dist_col"] is None:
        return None
    return out


def _remap_labels(prev_labels, prev_x0s, col_x0s, tol=8, wide_tol=12):
    """앞 페이지에서 읽은 칸 이름을 이 페이지의 열 번호로 옮긴다
    (_remap_columns와 같은 x좌표 기준 - 표 제목 한 줄만 있고 진짜
    칸 이름(집합투자업자보수 등)은 전부 앞 페이지에 있는 문서가 있다.
    KR5153420022 실측: 26쪽에 칸 이름이, 27쪽엔 값만 있어 27쪽만 보면
    "총보수비용" 칸을 이름으로 못 찾는다).

    1차는 좁은 tol(8)로 맞춘다. 그런데 헤더 페이지에만 유령 칸이 하나
    더 있어(칸 이름 낱말이 줄바꿈 위치에 따라 둘로 갈라짐 등) 진짜
    대응 칸이 tol을 살짝(1~4pt) 넘겨 못 걸리는 문서가 있다
    (KR5118420006 실측: 31쪽 "집합투자업자보수" 칸 x=147.6인데 32쪽의
    대응 칸은 x=137.7 - 9.9pt 차이라 8pt 기준을 근소하게 넘김. 31쪽엔
    이 칸 바로 옆에 x=137.7짜리 진짜 빈 유령 칸이 따로 있어서 생긴
    어긋남). 1차에서 못 채운 칸만, 더 넓은 tol로 다시 시도한다 - 이미
    채워진 칸은 절대 안 건드리고 "그래도 못 찾았던" 칸에만 기회를
    한 번 더 주는 것이라 기존에 맞던 매칭을 망가뜨리지 않는다.

    같은 목표 칸에 후보가 둘 이상 걸리면(스팬 헤더 "지급비율(연간,%)"나
    "용"처럼 옆 칸 글자가 잘려 우연히 같이 걸치는 조각 등 - KR5169950018
    34쪽 실측: "증권거래비용"(x=514.0, 목표 칸과 정확히 일치)과 그 옆
    "용"(x=506.7, 같은 칸까지 7.3pt 안에 걸침)이 같은 칸을 두고
    경쟁), 처리 순서가 아니라 목표 칸 x좌표에 더 가까운 쪽을 쓴다 -
    처리 순서(먼저 옴/나중 옴) 기준으로는 어느 쪽이 잘린 조각이고
    어느 쪽이 진짜인지 알 수 없지만, 거리는 진짜 칸 이름일수록
    가깝다는 근거가 있다."""
    best = {}  # 목표 칸 인덱스 -> (거리, 라벨)
    for i, x in enumerate(prev_x0s):
        if i >= len(prev_labels) or not prev_labels[i]:
            continue
        near = [j for j, x2 in enumerate(col_x0s) if abs(x2 - x) <= tol]
        if len(near) != 1:
            continue
        j = near[0]
        d = abs(col_x0s[j] - x)
        if j not in best or d < best[j][0]:
            best[j] = (d, prev_labels[i])
    out = [None] * len(col_x0s)
    for j, (_, label) in best.items():
        out[j] = label
    for i, x in enumerate(prev_x0s):
        if i >= len(prev_labels) or not prev_labels[i]:
            continue
        if prev_labels[i] in out:
            continue
        near = [j for j, x2 in enumerate(col_x0s)
                if out[j] is None and abs(x2 - x) <= wide_tol]
        if len(near) == 1:
            out[near[0]] = prev_labels[i]
    return out


def _rescan_prev_page_header(pdf, page_num, col_x0s, tol=8, wide_tol=12):
    """위 _remap_labels는 "바로 앞 페이지도 _detail_fee_grids가 표로
    인정했을 때"만 쓸 수 있다. 그런데 칸 이름표만 있고 데이터 행이
    하나도 없이 페이지가 끝나는 문서가 있다(KR5169950018 33쪽,
    KR5114420046 15쪽 실측: 표 헤더가 페이지 맨 아래에 있고 실제 값
    행은 전부 다음 페이지로 넘어감 - 이 페이지 자체엔 값 행이 없어
    _detail_fee_grids의 "셀 12개 이상" 기준에 못 미쳐 "표가 있는
    페이지"로 아예 안 잡는다). 그러면 prev_header_page가 한 번도 안
    채워져 원래 이월 로직이 작동할 기회조차 없다.

    이 폴백은 원래 이월이 실패했을 때만 마지막 수단으로, PDF 원본의
    바로 앞 물리 페이지를 직접 다시 읽는다 - _detail_fee_grids의 페이지
    판정 로직 자체는 전혀 건드리지 않는, 국지적인 보강이다."""
    if page_num < 2:
        return None
    prev_page = pdf.pages[page_num - 2]

    def valid_label(text):
        return bool(text) and not (DECIMAL_RE.match(text) or text in DASHES) and (
            text in _FEE_BREAKDOWN_LABEL_BY_TEXT or FEE_BREAKDOWN_EXCLUDE_RE.search(text))

    # 1순위: pdfplumber가 이 페이지에서 이미 감지해 둔 표 셀을 쓴다 -
    # 낱말 좌표를 y-비율로 훑는 것보다 훨씬 안정적이다(KR5114420046
    # 15쪽 실측: 헤더만 있는 작은 표(11개 셀, _detail_fee_grids의
    # "12개 이상" 기준을 근소하게 못 채워 걸러짐)를 pdfplumber
    # find_tables()는 이미 별개 표로 잡아내고, 그 칸 x좌표가 다음 페이지
    # 데이터 칸과 거의 그대로 일치한다). 페이지 아래쪽(하단 절반)에
    # 걸쳐 있는 표만 대상으로 한다 - 본문 중간의 다른 데이터표까지
    # 헤더 후보로 오인하면 안 된다.
    for t in prev_page.find_tables():
        cells = [c for c in t.cells if c]
        if len(cells) < 4 or t.bbox[1] < prev_page.height * 0.5:
            continue
        by_x0 = {}
        for (x0, top, x1, bottom) in cells:
            text = (prev_page.crop((x0, top, x1, bottom)).extract_text() or "").strip()
            if not text:
                continue
            # "지급비율(연간,%)"처럼 여러 칸을 가로질러 걸치는 단위 안내
            # 셀은 특정 칸의 진짜 이름이 아니다 - 안 걸러 두면 이 셀의
            # x좌표가 우연히 진짜 칸(집합투자업자보수 등)과 같은 목표
            # 칸으로 매칭될 때, sorted(by_x0) 순서상 나중에 처리되면서
            # 먼저 맞게 매칭된 진짜 이름을 덮어써 버린다(KR5120420091
            # 41쪽 실측: x=98.0 "집합투자업자보수"가 먼저 col1에 매칭된
            # 뒤, x=103.0 "지급비율(연간,%)"도 같은 col1에 매칭돼 뒤에서
            # 처리되며 덮어씀). 본문 파서가 label_by_col 만들 때 쓰는
            # 필터와 동일하게 여기서도 미리 뺀다.
            text_compact = text.replace(" ", "").replace("\n", "")
            if text_compact.startswith("지급") or "연간" in text_compact:
                continue
            by_x0.setdefault(round(x0, 1), []).append((top, text.replace("\n", "")))
        if len(by_x0) < 4:
            continue
        cell_x0s, cell_labels = [], []
        for x0 in sorted(by_x0):
            items = sorted(by_x0[x0])
            text = FEE_FOOTNOTE_MARK_RE.sub(
                "", "".join(txt for _, txt in items).replace(" ", ""))
            cell_x0s.append(x0)
            cell_labels.append(text)
        label_by_col = _remap_labels(cell_labels, cell_x0s, col_x0s, tol=tol, wide_tol=wide_tol)
        label_by_col = [x if (x is None or valid_label(x)) else None for x in label_by_col]
        if any(label_by_col[1:]):
            return label_by_col
        # 이 표의 칸 좌표 자체가 데이터 페이지와 다르게 찍혀 있으면
        # (KR5169950018류) 왼쪽부터 순서로 짝짓는다 - 유효한 칸 이름
        # 개수가 값 칸 수(0번 제외)와 정확히 같을 때만 받아들인다.
        valid_ordered = [x for x in cell_labels if valid_label(x)]
        if len(valid_ordered) == len(col_x0s) - 1:
            return [None] + valid_ordered

    # 2순위(표 자체가 안 잡히는 경우): 낱말을 직접 훑는다.
    words = prev_page.extract_words(x_tolerance=2, keep_blank_chars=False)
    if not words:
        return None
    # 페이지 아래쪽 1/4만 본다 - 앞선 "가입자격" 등 다른 칸의 긴 설명
    # 문단이 페이지 중간부터 시작해 절반 기준으로는 같이 잡힌다
    # (KR5169950018 33쪽 실측: 설명 문단 y=478~587, 진짜 칸 이름은
    # y=664.5~751.5로 뒤쪽 1/4 안에만 있음).
    footer_zone = prev_page.height * 0.75
    buckets = {}
    for w in words:
        if w["top"] < footer_zone:
            continue
        wt = w["text"]
        if wt.replace(" ", "").startswith("지급") or "연간" in wt:
            continue
        mid_x = (w["x0"] + w["x1"]) / 2
        best = min(range(len(col_x0s)), key=lambda k: abs(col_x0s[k] - mid_x))
        if abs(col_x0s[best] - mid_x) > tol:
            continue
        buckets.setdefault(best, []).append((w["top"], wt))

    label_by_col = [None] * len(col_x0s)
    for c, items in buckets.items():
        items.sort()
        text = "".join(t for _, t in items).replace(" ", "")
        text = FEE_FOOTNOTE_MARK_RE.sub("", text)
        # 페이지 아래쪽을 통째로 훑다 보니(가입자격 설명 등 다른 칸의
        # 긴 문단까지) 진짜 칸 이름이 아닌 글자가 섞여 들어올 수 있다
        # (KR5169950018 33쪽 실측: "종류별 가입자격" 열의 여러 줄짜리
        # 설명이 같이 잡혀 "금(IRP),수행을..." 같은 쓰레기가 됨). 헤더만
        # 있고 데이터 행이 없는 페이지를 다시 읽는 위험한 폴백이라, 이미
        # 알려진 진짜 칸 이름(또는 원래도 배제 대상인 합성/총보수)으로
        # 확인되는 것만 받아들이고 나머지는 차라리 None으로 둔다 - 틀린
        # 라벨을 붙이는 것보다 안 붙이는 쪽이 안전하다.
        if valid_label(text):
            label_by_col[c] = text
    if any(label_by_col[1:]):
        return label_by_col

    # 위 x좌표 매칭이 통째로 실패하면(0건), 헤더 페이지의 표가 데이터
    # 페이지와 칸 x좌표 자체가 다르게 찍힌 문서일 수 있다(KR5169950018
    # 실측: 33쪽 헤더 칸이 34쪽 데이터 칸보다 전부 18~36pt씩 밀려 있어
    # 좌표로는 대응이 안 됨 - 그러나 왼쪽부터 순서는 그대로다). 이번엔
    # x좌표를 이 페이지 자체의 낱말들만으로 다시 묶어("이 페이지 안에서
    # 몇 번째 칸인가") 순서대로 늘어놓고, 데이터 페이지의 칸에 순서대로
    # 짝지어 본다. 0번 칸(클래스명)은 늘 별도라 값 칸은 1번부터라고
    # 가정한다.
    local_x0s = []
    for w in words:
        if w["top"] < footer_zone:
            continue
        x = round((w["x0"] + w["x1"]) / 2, 1)
        if not any(abs(x - lx) <= 10 for lx in local_x0s):
            local_x0s.append(x)
    local_x0s.sort()
    local_buckets = {}
    for w in words:
        if w["top"] < footer_zone:
            continue
        wt = w["text"]
        if wt.replace(" ", "").startswith("지급") or "연간" in wt:
            continue
        x = (w["x0"] + w["x1"]) / 2
        best = min(range(len(local_x0s)), key=lambda k: abs(local_x0s[k] - x))
        local_buckets.setdefault(best, []).append((w["top"], wt))
    ordered_labels = []
    for c in sorted(local_buckets):
        items = sorted(local_buckets[c])
        text = FEE_FOOTNOTE_MARK_RE.sub("", "".join(t for _, t in items).replace(" ", ""))
        if valid_label(text):
            ordered_labels.append(text)
    # 유효한 칸 이름이 값 칸 수(0번 제외)와 정확히 같을 때만 순서 매칭을
    # 받아들인다 - 개수가 안 맞으면 어느 게 어느 칸인지 순서만으로는
    # 장담할 수 없어 차라리 포기한다.
    if len(ordered_labels) == len(col_x0s) - 1:
        return [None] + ordered_labels
    return None


def enrich_with_detail_fee_table(doc_id, existing_rows):
    """요약표(앞쪽)엔 없고 "나.집합투자기구에 부과되는 보수 및 비용"류
    상세표에만 있는 클래스를 보강한다(KR5122420005 실측: 요약표엔 5개
    클래스뿐인데 상세표엔 18개 - README "class_fees.json 코퍼스 전체
    완전성 문제" 참고). 상세표 컬럼 구성이 문서마다 달라서(신탁업자보수/
    수탁회사보수처럼 이름도 다르고 컬럼 개수도 다름) 고정 매핑을 쓰지
    않고, 이미 확인된(요약표에서 뽑힌) 클래스 값과 대조해서 이 문서
    안에서만 통하는 매핑을 매번 다시 찾는다 - 검증 안 되면(요약표 클래스가
    2개 미만이거나 값이 안 맞으면) 아무것도 안 채우고 조용히 넘어간다."""
    # 여기 들어오는 행은 전부 앞쪽 요약표에서 뽑은 것이다. 이 값들이 어느
    # 표에서 왔는지 먼저 남겨 둔다(아래에서 상세표 값을 덧붙인다).
    for r in existing_rows:
        _record_source(r, "요약표", r.get("page"),
                       {f: r.get(f) for f in FEE_SOURCE_FIELDS})

    known = {r["class_code"]: r for r in existing_rows if r.get("class_code")}
    if len(known) < 1:
        return existing_rows
    # 클래스가 통째로 하나뿐인 상품(모자형 등 - KR5123365001 실측: "투자신탁"
    # 클래스 하나만 있고 fee_breakdown이 요약표엔 아예 없다)도 대조 기준이
    # 1개뿐이라도, 아래 페이지별 루프의 "알려진 클래스가 문서 전체에 딱
    # 하나뿐일 때" 분기(총보수·판매보수 둘 다 유일하게 맞는 열일 때만
    # 받아들임)가 이미 이 경우를 다룬다 - 여기서 미리 버리지 않는다.

    def close(a, b, tol=0.0005):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False

    def resolve_label(label_by_col, col_x0s, c, cols, tol=15):
        """label_by_col[c]가 None인데 실제 값이 있는 칸의 라벨을 옆
        칸에서 빌려온다. 칸이 좁고 헤더가 두 줄인 문서는 헤더 낱말과
        그 아래 값의 x좌표가 6pt(칸 병합 기준)보다 더 벌어져 서로 다른
        버킷으로 잡힐 수 있다(KR5131420025 13쪽 실측: "사무"/"관리"
        헤더는 x=508.8인데 그 값 0.01은 x=498.0 - 10.8pt 차이라 칸
        병합 기준을 안 넘는다). 그러면 헤더만 있고 값은 없는 "유령 칸"이
        바로 옆에 생긴다 - 그 유령 칸이 데이터가 없는 칸이면서 가장
        가까울 때만 라벨을 빌린다(임의로 먼 칸까지 끌어오면 엉뚱한
        라벨을 붙일 위험이 있어 tol로 좁힌다)."""
        if label_by_col[c] is not None:
            return label_by_col[c]
        x = col_x0s[c]
        candidates = sorted(
            (abs(col_x0s[j] - x), j)
            for j in range(len(label_by_col))
            if label_by_col[j] and j not in cols and j != c
        )
        if candidates and candidates[0][0] <= tol:
            return label_by_col[candidates[0][1]]
        return None

    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return existing_rows

    def apply_rows(raw_rows, page_num, label_by_col, col_x0s,
                    dist_col, peer_col, cost_col, total_col, total_sum_cols):
        """이 페이지(또는 열 구성을 이어받은 보류 페이지)의 행을 채워
        넣는다. known/added/new_rows는 바깥 스코프 것을 그대로 쓴다."""
        for r in raw_rows:
            if not r["class_code"]:
                continue
            cols = r["cols"]
            if r["class_code"] in added and r["class_code"] not in known:
                # 이 클래스가 더 부실한 표(칸이 적어 총보수·비용 등은
                # 없는 표)에서 먼저 추가돼 있을 수 있다(KR510902511M
                # 실측: 14/15쪽 "(2)종류별 수수료 및 보수에 관한 사항"
                # 표는 운용·판매·신탁·사무관리 네 칸뿐이라 총보수·비용
                # 칸 자체가 없는데, 그 네 칸 합이 우연이 아니라 총보수
                # 정의 그대로라 total_sum_cols로 이 표도 채택된다. 더
                # 자세한 28쪽 표는 그 뒤에 나온다. 이미 "-"로 비어 있는
                # 필드만, 지금 표에 진짜 값이 있으면 채운다 - 이미 채운
                # 값은 덮지 않는다.
                existing = added_rows.get(r["class_code"])
                if existing is not None:
                    patch = {
                        "total_fee": _detail_total_fee(cols, total_col, total_sum_cols),
                        "distribution_fee": cols.get(dist_col),
                        "peer_avg_fee": cols.get(peer_col),
                        "total_fee_and_cost": cols.get(cost_col),
                    }
                    for f, v in patch.items():
                        if v and v != "-" and existing.get(f) in (None, "-"):
                            existing[f] = v
                    _record_source(existing, "상세표", page_num, patch)
                continue
            if r["class_code"] in known:
                # 요약표에서 이미 뽑힌 클래스라도, 상세표에만 있는
                # 세부 항목(집합투자업자보수/신탁업자보수/일반사무관리
                # 회사보수 등 = fee_breakdown)은 가져와서 채운다.
                # 처음엔 이런 클래스를 통째로 건너뛰어서, 같은 상세표
                # 안에 값이 멀쩡히 있는데도 요약표 출신 클래스만
                # fee_breakdown이 없는 상태가 됐다(KR510902511M 실측:
                # 14개 클래스 중 요약표 출신 6개만 breakdown 없음 -
                # 사용자가 원본 표와 대조해서 지적). class_returns에서
                # 쓴 것과 같은 원칙(FULL OUTER JOIN처럼 "어느 쪽에만
                # 있는 정보든 다 살린다")을 여기에도 적용한다.
                # 숫자 필드(total_fee 등)는 요약표 것을 그대로 둔다.
                # 총보수·판매보수는 이 페이지를 채택한 조건 자체가
                # "요약표 값과 정확히 일치"라 어느 쪽을 써도 같고,
                # 요약표 쪽엔 비용예시(cost_projection_per_10m)까지
                # 있어 더 완전하다.
                #
                # 총보수·비용과 동종유형총보수는 다르다 - 두 표가 어긋날
                # 수 있어서 채택 조건에서 뺐다. 그래서 상세표가 뭐라고
                # 했는지를 value_sources에 따로 남긴다. 답변에 쓰는
                # 한 줄은 요약표 값이고 근거 페이지도 요약표 쪽이라,
                # 고객이 그 페이지를 열면 우리가 말한 숫자가 거기 있다.
                cur = known[r["class_code"]]
                _record_source(cur, "상세표", page_num, {
                    "total_fee": _detail_total_fee(
                        cols, total_col, total_sum_cols),
                    "distribution_fee": cols.get(dist_col),
                    "peer_avg_fee": cols.get(peer_col),
                    "total_fee_and_cost": cols.get(cost_col),
                })
                if not cur.get("fee_breakdown"):
                    bd = [
                        {"label": resolve_label(label_by_col, col_x0s, c, cols),
                         "value": v}
                        for c, v in sorted(cols.items())
                        if c not in (dist_col, peer_col, cost_col)
                        and (total_col is None or c != total_col)
                    ]
                    if bd:
                        cur["fee_breakdown"] = bd
                        cur.setdefault("field_source_pages", {})["fee_breakdown"] = page_num
                        sp = cur.setdefault("source_pages", [cur["page"]])
                        if page_num not in sp:
                            sp.append(page_num)
                continue
            # 상세표는 클래스명 칸이 줄바꿈 경계에서 잘려("C-퇴직연금"이
            # "C-퇴직연"/"금" 두 행으로) 존재하지 않는 가짜 코드를 만들어
            # 낼 수 있다(KR5127420034/39/45/83, KR5127450117 실측 - 이
            # 잘린 조각이 진짜 클래스처럼 새 레코드로 추가돼 총보수·비용이
            # "-"인 중복이 생겼다). class_meaning.json에 이 문서의 공식
            # 클래스 목록이 있으면("나." 표와 달리 이 목록은 이런 식으로
            # 잘리지 않는 별도 표에서 뽑는다) 그 목록에 없는 코드는 새
            # 클래스로 만들지 않는다 - 목록이 없는 문서(사전 검증 실패
            # 등)는 예전처럼 그대로 받아들인다.
            if labels and r["class_code"] not in labels:
                continue
            # 판매회사보수/동종유형총보수 등 특정 칸이 원본에 "-"로
            # 찍혀 대시 토큰 자체가 안 잡히는 클래스가 있다
            # (KR5122420005 C-W, KR510902773M C-F 등 실측 - 부가서비스가
            # 거의 없는 클래스라 관련 칸이 통째로 "없음"). 이 필드
            # 하나 없다고 행 전체(total_fee 등 나머지 다 있는 값까지)를
            # 버리면 안 되므로, 그 필드만 "-"로 남기고 나머지는 살린다
            # - class_fees.json 기존 관례(peer_avg_fee "-" 보존)와
            # 동일. total_fee만은 이 행의 핵심 값이라 "-" 대체 없이
            # 못 찾으면 이 행 자체를 건너뛴다.
            if total_col is not None:
                if total_col not in cols:
                    continue
                total_fee = cols[total_col]
            elif total_sum_cols and all(c in cols for c in total_sum_cols):
                total_fee = f"{sum(float(cols[c]) for c in total_sum_cols):.4f}"
            else:
                continue
            breakdown = [
                {"label": resolve_label(label_by_col, col_x0s, c, cols),
                 "value": v}
                for c, v in sorted(cols.items())
                if c not in (dist_col, peer_col, cost_col)
                and (total_col is None or c != total_col)
            ]
            fresh = {
                "class_code": r["class_code"],
                "sales_commission_desc": None,
                "total_fee": total_fee,
                "distribution_fee": cols.get(dist_col, "-"),
                "peer_avg_fee": cols.get(peer_col, "-"),
                "total_fee_and_cost": cols.get(cost_col, "-"),
                # 상세표엔 1,000만원 비용예시(1년~10년) 칸이 없다 -
                # build_product_facts_db.py의 cp.get("1y") 등이
                # None.get()에서 죽지 않도록 dict({})로 둔다(요약표
                # 클래스는 실제 값이 채워진 dict를 씀).
                "cost_projection_per_10m": {},
                "fee_breakdown": breakdown,
                "page": page_num,
                "source_pages": [page_num],
                "field_source_pages": {},
                "evidence": f"[상세표 보강] {sorted(cols.items())}",
                "method": "detail_table_cross_validated",
                "confidence": 0.7,
                "product_code": doc_id,
            }
            # 이 클래스는 상세표에만 있다 - 요약표 쪽 값은 애초에 없다.
            _record_source(fresh, "상세표", page_num,
                           {f: fresh.get(f) for f in FEE_SOURCE_FIELDS})
            added.add(r["class_code"])
            added_rows[r["class_code"]] = fresh
            new_rows.append(fresh)

    labels = _class_labels_for_doc(doc_id)
    new_rows, added, added_rows = [], set(), {}
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        carry = None  # 앞 페이지에서 검증된 열 구성(표가 페이지를 넘어갈 때 씀)
        pending = None  # 대조할 클래스가 없어 보류한 앞 페이지(뒤가 확인되면 되짚는다)
        prev_header_page, prev_label_by_col, prev_label_x0s = None, None, None
        for page_num, header_rows, grid_rows, col_x0s in _detail_fee_grids(pdf):
            # 칸 이름: 헤더 행들에서 열별로 이어붙인다(셀이 열을 알려주니
            # 좌표로 묶을 필요가 없다 - 예전엔 문서 제목/묶음 헤더가
            # 섞여 "집합"처럼 잘리는 문제가 있었다).
            #
            # "지급비율(연간,%)"/"지급비용(연간%)"처럼 값 칸 전체를 아우르는
            # 단위 표기는 특정 칸의 이름이 아니라서 빼야 한다 - 안 그러면
            # 항목 이름이 "지급비율(연간,%)집합투자업자보수"가 된다.
            # 여러 데이터 행에 걸친 병합 칸(집합투자업자보수 등 클래스
            # 전체에 공통인 값)이 표 위쪽까지 뻗어 있으면, 그 칸 자신의
            # 원래 셀 밴드가 숫자만 있고 다른 칸은 텅 빈 "행"으로 따로
            # 잡혀 데이터 행 기준(숫자 5개 이상 등)을 못 채우고 header_rows
            # 로 밀려난다(KR5127420034 13쪽 실측: {5:'-',7:'0.150',
            # 8:'0.020',9:'0.010'}처럼 글자가 하나도 없는 행). 그러면
            # 그 값이 진짜 칸 이름("집합투자업자")과 나란히 이어져
            # "집합투자업자0.150"처럼 이름과 값이 섞인다. 글자가 전혀
            # 없고 숫자·대시만 있는 헤더 행은 이름표 후보에서 뺀다.
            real_header_rows = [
                h for h in header_rows
                if any(not (DECIMAL_RE.match(v.replace(" ", "")) or v.strip() in DASHES)
                       for v in h["cells"].values())
            ]
            label_by_col = []
            for ci in range(len(col_x0s)):
                parts = [h["cells"][ci] for h in real_header_rows if ci in h["cells"]]
                parts = [p for p in parts
                         if not (p.replace(" ", "").startswith("지급") or "연간" in p)]
                # 종류형 설명("수수료선취-오프라인" 등)이 줄바꿈 경계에서 잘려
                # 다음 페이지 첫 줄("수수료미징구-온")로 넘어가면, 그 조각이
                # "이 페이지 헤더 행"으로 잘못 잡힌다(KR5111450067 42쪽 실측).
                # 실제 칸 이름은 "집합투자업자보수"류뿐이라 "수수료..."로
                # 시작하는 조각은 항상 종류형 설명이지 칸 이름이 아니다 -
                # 아래 any(label_by_col[1:]) 판단을 그르치지 않도록 뺀다.
                parts = [p for p in parts
                         if not p.replace(" ", "").startswith(
                             ("수수료선취", "수수료후취", "수수료미징구"))]
                joined = " ".join(parts).replace(" ", "")
                label_by_col.append(joined or None)

            # 문서 정식명칭(모든 페이지 위에 똑같이 반복)이 0번 칸(클래스명
            # 칸) 자리에 헤더로 찍혀 들어가는 표가 있다(KR510902511M 실측:
            # 15쪽 "미래에셋장기성장포커스증권자투자신탁1호(주식)"). 그러면
            # 0번 칸만으로도 any(label_by_col)이 참이 돼, 정작 값 칸
            # 이름(1번 이후)은 전부 비어 있는데도 "이 페이지 자체 헤더가
            # 있다"고 오판해 앞 페이지 이름표를 안 물려받는다. 값 칸
            # (1번 이후)만으로 판단한다.
            # 값 칸에 글자가 있다고 해서 다 진짜 칸 이름은 아니다 - 앞
            # 표("종류별 가입자격" 등)의 설명 문장 조각이 이 페이지
            # 첫머리로 넘어와 값 칸(1번 이후) 자리에 걸리는 문서가 있다
            # (KR5131420007 14쪽 실측: "에 따른 연금저축계좌를 통하여
            # 가입한 자"가 1번 칸으로 잡혀, "이 페이지에 헤더가 있다"고
            # 오판해 13쪽에서 이어받는 걸 건너뜀). "수수료선취/후취/
            # 미징구"로 시작하는 조각만 걸러내는 방식은 이런 문장까지는
            # 못 잡는다 - 대신 "이미 알려진 진짜 칸 이름인가"로 판단
            # 기준을 일반화한다. 진짜 헤더가 있는 페이지는 이 검사와
            # 무관하게 label_by_col을 그대로 쓴다(원문 표기가 사전에
            # 없는 경우까지 이 검사로 걸러 원래 값을 잃으면 안 된다).
            has_real_header = any(
                v and (v in _FEE_BREAKDOWN_LABEL_BY_TEXT or FEE_BREAKDOWN_EXCLUDE_RE.search(v))
                for v in label_by_col[1:])
            if not has_real_header:
                # 진짜 칸 이름이 하나도 없다고 판단했으면, 남아 있던
                # 잡음(예: "집합투자기구보수포함)")도 버리고 완전히
                # 새로 채운다 - 잡음이 남아 있으면 any(label_by_col[1:])가
                # 참이 돼 아래 이월·재스캔 시도 자체가 건너뛰어진다
                # (KR5120420091 41쪽 실측).
                label_by_col = [None] * len(col_x0s)
                if prev_header_page == page_num - 1:
                    label_by_col = _remap_labels(
                        prev_label_by_col, prev_label_x0s, col_x0s)
                if not any(label_by_col[1:]):
                    rescanned = _rescan_prev_page_header(pdf, page_num, col_x0s)
                    if rescanned:
                        label_by_col = rescanned
            if any(label_by_col[1:]):
                prev_header_page, prev_label_by_col, prev_label_x0s = (
                    page_num, label_by_col, col_x0s)

            raw_rows = []
            for r in grid_rows:
                label = r.get("label_outside") or r["cells"].get(0, "")
                code = None
                flat_label = label.replace(" ", "")
                # 표 마지막 행의 0번 칸이 바로 아래 각주 문단까지 셀
                # 경계 없이 붙어 라벨이 문장 하나만큼 길어지는 문서가
                # 있다(KR5111420047 35쪽 실측: "I 실제 비용은 이와 상이할
                # 수 있습니다. ... 기설정된 [종류C] 수익증권의 ...").
                # 진짜 클래스 라벨은 아무리 길어도 괄호 섞인 짧은 구절이라
                # 이렇게 길지 않다. 이 각주 안의 "[종류C]"처럼 본문과
                # 무관한 언급이 우연히 DETAIL_FEE_CLASS_CODE_JONGRYU_RE에
                # 걸려, 실제로는 I 클래스인 행이 C로 잘못 잡히고 그 값이
                # C의 진짜 값과 달라 총보수 열 검증 전체를 깨뜨렸다. 이
                # 정도로 길면 라벨이 아니라 각주가 섞인 것으로 보고
                # 코드를 아예 뽑지 않는다 - 못 뽑은 채로 두면(코드
                # 없음) 이 행은 조용히 걸러질 뿐, 엉뚱한 클래스로
                # 오염되지는 않는다.
                # 페이지마다 똑같이 반복되는 상품 정식명칭(문서 제목)이
                # 행 라벨에 섞여 들어가는 경우도 있다(KR5144420081 실측:
                # "온라인슈퍼-퇴직연금(S- NH-Amundi USD 초단기채권
                # 증권자투자신탁(UH)[채권]" - 45자라 위 60자 기준은
                # 못 걸렀는데, 그 안 괄호 "(UH)"가 코드로 읽혔다). 정식
                # 명칭에는 "투자신탁"이 꼭 들어가는데 클래스 라벨(수수료
                # 방식-판매경로-계좌유형)에는 나올 일이 없는 낱말이라
                # 안전한 신호다. 다만 클래스가 통째로 하나뿐이라 클래스명
                # 자체가 그냥 "투자신탁"인 상품이 있다(KR5123365001 실측 -
                # 모자형이라 클래스 구분이 없다). 그런 문서는 라벨 칸에
                # 다른 글자 없이 "투자신탁" 딱 그것만 있으므로("...증권
                # 투자신탁1호..."처럼 다른 글자가 섞인 정식명칭 오염과
                # 구별된다), 정확히 이 글자뿐일 때만 예외로 통과시킨다.
                if len(flat_label) > 60 or (
                        "투자신탁" in flat_label and flat_label != "투자신탁"):
                    raw_rows.append({"class_code": None, "cols": {}, "label": label})
                    continue
                if flat_label == "투자신탁":
                    # 위 예외로 통과시킨 "클래스가 곧 투자신탁 자체"인
                    # 경우 - 아래 정규식들은 전부 괄호 코드나 짧은 영숫자
                    # 패턴을 찾는 것이라 이 글자 자체를 코드로 못 뽑는다.
                    # 그대로 코드로 쓴다.
                    code = flat_label
                # 겹친 괄호를 먼저 본다 - CLASS_CODE_RE가 그런 라벨에선
                # 아무것도 못 잡아 연금 클래스를 통째로 잃는다.
                if code is None:
                    for regex in (DETAIL_FEE_CLASS_CODE_NESTED_RE,
                                  DETAIL_FEE_CLASS_CODE_NESTED_UNCLOSED_RE,
                                  CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
                        mm = [x for x in regex.finditer(flat_label)
                              if not _is_bad_code(x.group(1))]
                        if mm:
                            code = mm[-1].group(1)
                            break
                if code is None:
                    # 코드가 괄호도 "종류"도 없이 첫 칸에 그냥 놓인 표가
                    # 있다(KR5111450067 41쪽 실측: "A | 수수료선취-오프라인
                    # | 0.7446 | ..."). 그러면 코드를 하나도 못 읽어 표
                    # 전체가 버려진다. 잘못 읽어도 아래 총보수·판매보수
                    # 대조에서 걸러지므로 여기서는 넓게 본다.
                    bare = re.sub(r"\s+", "", label)
                    if RE_BARE_CLASS_CODE.match(bare) \
                            and not _is_bad_code(bare):
                        code = bare
                code = _normalize_code_via_labels(code, labels)
                code = _same_class_in_summary(code, known, labels)
                cols = {}
                # 라벨이 표 밖에 있으면 0번 칸도 값 칸이다.
                skip0 = not r.get("label_outside")
                for ci, v in r["cells"].items():
                    if ci == 0 and skip0:
                        continue
                    t = v.replace(" ", "")
                    if FEE_VALUE_RE.match(t) and "%" not in t:
                        cols[ci] = t
                raw_rows.append({"class_code": code, "cols": cols, "label": label})

            if not raw_rows:
                continue
            n_cols = len(col_x0s)

            ref_rows = [r for r in raw_rows if r["class_code"] in known]
            inherited = None
            if len(ref_rows) < 2:
                # 표가 페이지를 넘어가면 뒷장엔 요약표에 있는 클래스가 한
                # 개도 없을 수 있다(KR5110501016 실측: 28쪽에 S-P, Crp,
                # Crp-e, C-F, C-I, C-I2 - 요약표에 없는 클래스만 이어진다).
                # 대조할 기준이 없다고 버리면 그 클래스들을 통째로 잃는데,
                # 하필 퇴직연금 클래스가 거기 있다. 바로 앞 페이지에서
                # 검증된 열 구성을 물려받는다(class_returns에서 쓴 것과
                # 같은 방식). 앞 페이지가 아니거나 열 구성이 다르면
                # 물려받지 않는다 - 무관한 표에 매핑을 씌우면 엉뚱한 행이
                # 생긴다.
                inherited = (_remap_columns(carry, col_x0s)
                             if carry and carry["page"] == page_num - 1 else None)
                if inherited is None and len(ref_rows) == 1:
                    # 알려진 클래스가 문서 전체에 딱 하나뿐이고, 그마저
                    # 이 표에서만 나오는 경우가 있다(KR5174420011 실측:
                    # known={C, C-E}인데 C는 26쪽에 혼자, C-E는 27쪽에
                    # S-P와 같이 있어 어느 페이지도 2개를 못 채운다).
                    # "값이 둘 이상 맞아야 확실하다"는 기준을 못 채우니
                    # 대신, 총보수·판매보수 둘 다 이 표 안에서 그 값과
                    # 맞는 열이 각각 정확히 하나씩일 때만(다른 열은 하나도
                    # 안 맞을 때) 받아들인다 - 우연히 맞을 여지를 줄인다.
                    ref = ref_rows[0]
                    kv = known[ref["class_code"]]

                    def unique_match(field):
                        hits = [c for c, v in ref["cols"].items()
                                if close(v, kv[field])]
                        return hits[0] if len(hits) == 1 else None

                    tcol, dcol = unique_match("total_fee"), unique_match("distribution_fee")
                    if tcol is not None and dcol is not None and tcol != dcol:
                        inherited = {
                            "dist_col": dcol, "peer_col": None, "cost_col": None,
                            "total_col": tcol, "total_sum_cols": None,
                            "total_sum_n": None,
                        }
                if inherited is None:
                    # 표의 맨 앞 한 줄이 이 페이지에 혼자 떨어져 있고,
                    # 대조할 클래스가 있는 나머지 줄은 다음 페이지에 있는
                    # 경우가 있다(KR5113420013 실측: 46쪽에 S-P 한 줄뿐,
                    # 대조 가능한 C/C-e는 45쪽에 있어 45→46 방향 이월은
                    # 되는데 반대는 안 됨 - 여기 이 페이지가 그 "반대"
                    # 경우다). 위 이월은 "앞이 확인된 뒤 확인 안 된 뒤"
                    # 방향만 되므로, 이 페이지를 일단 보류해 뒀다가 바로
                    # 다음 페이지가 확인되면 그 열 구성을 거꾸로 물려서
                    # 되짚어 채운다.
                    pending = {
                        "page": page_num, "raw_rows": raw_rows,
                        "col_x0s": col_x0s, "label_by_col": label_by_col,
                    }
                    continue

            # distribution_fee/peer_avg_fee/total_fee_and_cost: 특정 컬럼
            # 위치 하나가, 값이 있는 참조 행들에서 전부 일치하는지 테스트
            # (그 컬럼 값이 "-"인 참조 행은 그 필드 검증에서만 제외).
            def find_column(field):
                candidates = []
                for col in range(n_cols):
                    withval = [r for r in ref_rows if col in r["cols"]]
                    matched = [
                        r for r in withval
                        if close(r["cols"][col], known[r["class_code"]][field])
                    ]
                    miss = len(withval) - len(matched)
                    # 값이 있는 참조 행 전부가 맞아야 한다는 게 원칙이지만,
                    # 문서 자체가 표마다 값이 살짝 다른 경우가 드물게 있다
                    # (KR5194450018 실측: F클래스 총보수가 요약표엔 0.798,
                    # 37쪽 상세표엔 0.795 - 오타가 아니라 문서 자체의
                    # 불일치다). 이 한 클래스 때문에 표 전체를 버리면
                    # 손해가 훨씬 크다. 다른 참조가 충분히 많고 어긋나는
                    # 게 딱 하나뿐일 때만 눈감아 준다 - 소수점 넷째 자리
                    # 까지 맞는 값이 우연히 여러 개 일치했다는 뜻이라 이
                    # 칸이 맞다는 근거는 이미 충분하다. 요약표(대조 기준)
                    # 클래스 수 자체가 적은 문서가 많아(KR5169950018 실측:
                    # 요약표 출신이 4개뿐이라 그중 1개만 어긋나도 5개
                    # 기준을 못 채워 총보수·비용 칸을 통째로 못 찾았다)
                    # 기준을 3개로 낮춘다 - 그래도 "4개 중 3개 일치"처럼
                    # 우연이라 보기 힘든 비율만 눈감아 준다.
                    if len(matched) >= 2 and (
                        miss == 0 or (miss == 1 and len(matched) >= 3)
                    ):
                        candidates.append((miss, col))
                if not candidates:
                    return None
                # 총보수·비용은 기타비용이 0인 클래스가 많은 문서에서
                # "투자신탁총보수" 칸과 값이 우연히 완전히 같아, 두 칸이
                # 값 검증만으로는 구별이 안 된다(KR5127420034 실측: 대조
                # 기준 4개 클래스가 전부 기타비용 0이라 5번 칸도 7번 칸도
                # miss=0으로 동점 - 열 순서상 5번(총보수, 틀림)이 먼저
                # 걸려 총보수·비용 4건이 조용히 총보수 값으로 채워졌다).
                # 값으로 동점이면 칸 이름으로 가른다 - 이름 매칭은
                # 원래(_column_by_name 주석 참고) 표 채택 여부에는 안
                # 쓰지만, 이미 값으로 "후보"까지 좁힌 뒤 동점만 가르는
                # 데는 안전하다.
                best_miss = min(m for m, _ in candidates)
                best = [c for m, c in candidates if m == best_miss]
                if len(best) > 1 and field in DETAIL_COL_NAMES:
                    named = _column_by_name(label_by_col, field)
                    if named in best:
                        return named
                return best[0]

            if inherited:
                dist_col = inherited["dist_col"]
                peer_col = inherited["peer_col"]
                cost_col = inherited["cost_col"]
            else:
                dist_col = find_column("distribution_fee")
                peer_col = find_column("peer_avg_fee")
                cost_col = find_column("total_fee_and_cost")

            # total_fee: 단일 컬럼으로 안 맞으면 왼쪽부터 N개 합으로 시도
            # (관측: 항상 "관리 성격" 앞쪽 컬럼들의 합 - README 참고).
            #
            # 셀 격자에선 열 번호가 "표의 모든 칸"에 매겨져서, 값이 안 들어
            # 가는 칸(클래스명 칸 0번, 묶음 헤더만 걸친 빈 칸 등)이 중간에
            # 섞인다(KR5122420005 실측: 값이 1,3,4,5번 열에 있고 2번은 빔).
            # 그래서 "연속된 열 1..n"이 아니라 "값이 실제로 있는 열을 왼쪽
            # 부터 N개"로 잡아야 한다.
            value_cols = sorted(
                {c for r in ref_rows for c in r["cols"] if c != 0})
            total_col = inherited["total_col"] if inherited else find_column("total_fee")
            total_sum_n = inherited["total_sum_n"] if inherited else None
            total_sum_cols = inherited["total_sum_cols"] if inherited else None
            if total_col is None and not inherited:
                for n in range(2, min(6, len(value_cols)) + 1):
                    span = value_cols[:n]
                    complete_refs = [r for r in ref_rows if all(c in r["cols"] for c in span)]
                    if len(complete_refs) >= 2 and all(
                        close(
                            sum(float(r["cols"][c]) for c in span),
                            known[r["class_code"]]["total_fee"],
                        )
                        for r in complete_refs
                    ):
                        total_sum_n, total_sum_cols = n, span
                        break

            # peer_avg_fee(동종유형총보수)는 요약표에서 이미 아는 클래스들도
            # 전부 "-"인 문서가 있다(KR510902773M 실측: C/C-e 둘 다 이미
            # "-") - 대조할 실제 숫자가 하나도 없어 컬럼을 특정할 수 없다.
            # 이 경우 "그 칸이 있는지조차 특정 못 함"이 아니라 "이 문서
            # 자체가 이 필드를 클래스별로 안 보여줌"으로 보고, 새로 채우는
            # 행도 똑같이 "-"로 둔다(거짓으로 숫자를 지어내지 않되, 행
            # 전체를 놓치지도 않는다).

            # 이 페이지를 쓸지는 총보수와 판매보수 두 열로만 정한다.
            #
            # 요약표는 "정답지"가 아니라 "어느 칸이 무슨 값인지 알아내는
            # 자"다. 상세표는 문서마다 칸 이름도 개수도 달라서(신탁업자보수
            # / 수탁회사보수) 이름만 보고는 어느 칸이 총보수인지 못 정한다 -
            # 요약표 값과 맞는 칸을 찾는 게 유일하게 확실한 방법이다.
            #
            # 그런데 여기서 한 걸음 더 나가 "네 필드가 다 맞아야 이 표를
            # 쓴다"고 요구하고 있었다. 그러면 안 된다. 총보수·비용은 같은
            # 문서 안에서 두 표가 다르게 적는 값이기 때문이다.
            #
            #   KR5110501016 실측
            #   요약표(3쪽) : 총보수·비용 = 총보수 + 0.01  (전 클래스 일괄)
            #   상세표(27쪽): 총보수·비용 = 총보수 + 그 클래스 기타비용
            #                 (A는 기타비용이 "-"라 0.30, 요약표는 0.31)
            #
            # 값이 다른 건 상세표가 틀려서가 아니라 기준이 달라서다. 그걸
            # 오류로 보고 페이지를 버리는 바람에 9개 페이지 53개 클래스의
            # 보수가 통째로 빠져 있었다 - 그 안에 C-Pe(온라인 개인연금),
            # Crp(퇴직연금) 같은 연금 클래스가 들어 있다.
            #
            # 총보수와 판매보수는 코퍼스 전체에서 어긋난 적이 없고 소수점
            # 셋째 자리까지 맞아(0.245 / 0.303) "이 표가 이 상품의 보수표"
            # 라는 걸 충분히 특정한다. 실제로 보수표가 아닌 페이지 23개
            # (클래스 코드처럼 생긴 라벨이 있는 가입자격표 등)는 이 두
            # 열부터 못 찾아 그대로 걸러진다.
            #
            # 동종유형총보수·총보수·비용은 열을 찾으면 채우고 못 찾으면
            # 비운다(아래 cols.get(None, "-")가 "-"를 준다).
            if total_col is None and total_sum_n is None:
                continue
            if dist_col is None:
                continue

            # 여기부터는 총보수·판매보수로 "이 표가 이 상품의 보수표"임이
            # 확인된 뒤다. 두 표가 값을 다르게 적어 열을 특정 못 한
            # 나머지 두 필드는 칸 이름으로 찾아 채운다.
            if cost_col is None:
                cost_col = _column_by_name(label_by_col, "total_fee_and_cost")
            if peer_col is None:
                peer_col = _column_by_name(label_by_col, "peer_avg_fee")

            # 이 페이지의 열 구성이 검증됐다. 표가 다음 장으로 이어지면
            # 거기엔 대조할 클래스가 없을 수 있어서 이걸 물려준다.
            carry = {
                "page": page_num, "col_x0s": col_x0s,
                "dist_col": dist_col, "peer_col": peer_col, "cost_col": cost_col,
                "total_col": total_col, "total_sum_n": total_sum_n,
                "total_sum_cols": total_sum_cols,
            }

            # 바로 앞 페이지가 대조할 클래스가 없어 보류돼 있었다면, 지금
            # 확인된 이 열 구성을 거꾸로 그 페이지의 열 x좌표에 물려서
            # 되짚어 채운다(KR5113420013 실측: 46쪽에 S-P 한 줄만 있고
            # 대조 가능한 C/C-e는 45쪽에 있어, 45쪽이 확인된 뒤에야 46쪽을
            # 46→45 방향으로 물려줄 수 있다 - 반대로 앞에서 뒤로 물려주는
            # 기존 방식으로는 이 순서를 못 잡는다).
            if pending and pending["page"] == page_num - 1:
                back = _remap_columns(
                    {"col_x0s": col_x0s, "dist_col": dist_col,
                     "peer_col": peer_col, "cost_col": cost_col,
                     "total_col": total_col, "total_sum_cols": total_sum_cols},
                    pending["col_x0s"])
                if back:
                    apply_rows(pending["raw_rows"], pending["page"],
                               pending["label_by_col"], pending["col_x0s"],
                               back["dist_col"], back["peer_col"], back["cost_col"],
                               back["total_col"], back["total_sum_cols"])
                pending = None

            apply_rows(raw_rows, page_num, label_by_col, col_x0s,
                       dist_col, peer_col, cost_col, total_col, total_sum_cols)

    return existing_rows + new_rows

# ---------------------------------------------------------------------------
# 상세 비용예시표("<1,000만원 투자시 투자자가 부담하는 ...>")
#
# 요약표에 안 나오는 클래스는 상세표에서 보수를 보강해 왔는데, 그 표엔
# 비용예시 칸이 없어서 298개 레코드의 cost_projection_per_10m이 비어
# 있었다. 비용예시는 뒤쪽 부속서류에 따로 있고("1년후/2년후/3년후/
# 5년후/10년후"), 구조가 요약표와 달라 별도로 읽는다.
# ---------------------------------------------------------------------------

# "후"는 문서마다 있기도(1년후) 없기도(1년) 하다(KR5117420097 실측:
# "1년 2년 3년 5년 10년"처럼 "후" 없이 쓰는 문서가 실재한다) - "후"를
# 필수로 요구하면 이런 문서를 통째로 놓친다. 대신 "최근 1년"/"최근
# 2년"처럼 전혀 다른 표(연평균 수익률 요약표)의 기간 머리글과 헷갈리는
# 문제는, 아래 by_code 선택에서 "연도 칸을 더 많이 채운 쪽"을 우선하는
# 것으로 대응한다(수익률 요약표는 보통 10년 칸이 없어 4칸뿐이고, 진짜
# 비용예시표는 5칸이 다 있다 - KR5113420069 실측).
# "차"도 같은 자리에 쓰는 문서가 있다(KR5160420009 실측: "1년차 2년차
# 3년차 5년차 10년차") - "후"와 같은 선택적 접미사로 취급한다.
COST_AFTER_RE = re.compile(r"^(\d+)년\s*[후차]?$")

# 클래스명이 여러 줄로 쪼개진 셀 안에서, 그 줄들 "사이"에 캡션
# ("판매수수료 및 보수·비용", "(모투자신탁의 총보수·비용 포함)")이
# 끼어 있는 문서가 있다(KR5113420013 실측: "인-퇴직연금,기관(C-" 다음
# 줄이 캡션, 그 다음 줄이 "RF)" - top좌표로 정렬하면 캡션이 코드 조각
# 사이에 끼어 "(C-판매수수료및보수·비용RF)"처럼 코드가 두 동강 난다).
# 각 줄 자체는 항상 이 고정 문구 그대로이므로, 라벨을 모을 때 이
# 캡션 낱말만 미리 걸러내면 코드 조각들이 다시 붙는다.
CAPTION_NOISE_RE = re.compile(
    r"^\(?판매수수료$|^및$|^보수[·‧･]?비용$"
    r"|^\(?모투자신탁의$|^총보수[·‧･]?비용$|^포함\)?$"
)

# 자동전환 티어 클래스 행이 "(C1~C4)"처럼 접두+시작숫자~[접두]+끝숫자로
# 뭉뚱그려 적히는 문서가 있다(_detail_cost_grids의 by_code.setdefault
# 직전 주석 참고). 접두는 문자/붙임표로만, 숫자는 1~2자리로 좁혀서
# "5~10" 같은 진짜 연도 낱말이나 다른 우연한 물결표 문구를 코드로
# 오인하지 않게 한다.
RE_LABEL_CODE_RANGE = re.compile(
    r"\(([A-Za-z][A-Za-z\-]*?)(\d{1,2})~(?:[A-Za-z\-]*?)(\d{1,2})\)")


def _detail_cost_grids(pdf, known_codes=frozenset()):
    """상세 비용예시표를 셀 격자로 읽어
    [(page_num, {클래스코드: {"1y": ...}}), ...]를 돌려준다.
    머리글이 앞 페이지에 있고 값만 이어지는 경우가 흔해 열 구성을
    페이지 사이에 물려준다."""
    out = []
    carry = None          # (year_by_col, col_x0s, 직전 페이지 번호, 표 오른쪽 끝 x)
    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        for t in page.find_tables():
            cells = [c for c in t.cells if c]
            if len(cells) < 8:
                continue
            raw = sorted({round(c[0], 1) for c in cells})
            col_x0s = []
            for x in raw:
                if col_x0s and x - col_x0s[-1] <= 6:
                    continue
                col_x0s.append(x)
            if len(col_x0s) < 4:
                continue

            # 맨 오른쪽 "OO년" 칸이 표 테두리 밖(본문 여백)에 그대로
            # 찍혀 있어 그 칸을 감싸는 셀 자체가 없는 문서가 있다
            # (KR515302022M 실측: 1/2/3/5년은 테두리 안인데 10년만
            # x=512.3로 테두리 오른쪽 끝(499.0) 밖에 있다 - 그러면 이
            # 칸이 통째로 안 잡혀 68개 레코드가 "1/2/3/5년만 있고
            # 10년만 빠진" 반쪽 상태로 남았다). 마지막 칸에서 칸
            # 간격만큼 오른쪽으로 떨어진 자리에 "OO년" 낱말이 있으면
            # 새 칸으로 끼워 넣는다.
            n_before = len(col_x0s)
            tx1 = t.bbox[2]
            if len(col_x0s) >= 2:
                gap = col_x0s[-1] - col_x0s[-2]
                # 표 테두리 오른쪽 끝(tx1) 밖에서만 찾는다 - 안쪽에서
                # 찾으면 이미 셀이 있는 칸의 글자(예: "5년")가 그 칸의
                # 왼쪽 경계보다 오른쪽에 찍혀 있어 새 칸으로 잘못 걸린다.
                for w in words:
                    wt = w["text"].replace(" ", "")
                    if not (COST_AFTER_RE.match(wt)
                            and tx1 - 2 <= w["x0"] <= tx1 + gap * 1.6):
                        continue
                    x = round(w["x0"], 1)
                    if not any(abs(x - c) <= 6 for c in col_x0s):
                        col_x0s.append(x)
                    break
                # 이어지는 페이지엔 "OO년" 글자 자체가 없다(머리글이
                # 반복 안 되는 이어짐 표라 값만 있다) - 위에서 못
                # 찾았으면 앞 페이지에서 이미 확인된 칸을 물려쓴다.
                #
                # 표 테두리 자체가 페이지마다 다르게 잡히는(같은
                # 물리적 칸인데 어떤 페이지는 셀 경계 안, 어떤 페이지는
                # 밖) 문서가 있다(KR5139420015 실측: 31쪽 표는 칸이
                # 7개(10년 칸도 셀 경계 안)인데 이어지는 32쪽 표는
                # find_tables()가 5개만 잡고 10년 값은 셀 경계 밖
                # (x=502.8)에 그냥 찍혀 있다). 이 경우 "표 오른쪽
                # 끝에서 몇 pt 떨어졌는지"라는 상대 위치로 계산하면
                # (예전 방식) 앞 페이지의 그 칸이 애초에 표 안쪽(테두리
                # 보다 왼쪽)이라 음수 오프셋이 나와 엉뚱한 자리를
                # 짚는다 - 같은 물리적 표는 페이지가 달라도 칸의 절대
                # x좌표가 그대로이므로, 이 페이지 칸에 없는 앞 페이지
                # 칸을 절대 좌표 그대로 가져와 채운다.
                if (len(col_x0s) == n_before and carry
                        and carry[2] == page_num - 1
                        and len(carry[1]) > len(col_x0s)):
                    for x in carry[1]:
                        if not any(abs(x - c) <= 6 for c in col_x0s):
                            col_x0s.append(x)
                    col_x0s.sort()

            def col_of(x0):
                return min(range(len(col_x0s)),
                           key=lambda k: abs(col_x0s[k] - x0))

            bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in cells})
            grid = []
            for top, bottom in bands:
                ent = {}
                for (x0, ct, x1, cb) in [c for c in cells
                                         if abs(c[1] - top) < 1
                                         and abs(c[3] - bottom) < 1]:
                    ws = [w for w in words
                          if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                          and ct - 1 <= (w["top"] + w["bottom"]) / 2 <= cb + 1]
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    if txt:
                        ent[col_of(x0)] = txt
                # 방금 테두리 밖에서 끼워 넣은 칸은 감싸는 셀이 없으니
                # 위 루프로는 못 채운다 - 이 밴드의 y구간에서 그 칸
                # x자리에 있는 낱말을 직접 찾는다.
                last_ci = len(col_x0s) - 1
                if last_ci not in ent:
                    lx = col_x0s[last_ci]
                    # 왼쪽 경계는 고정 30pt가 아니라 바로 앞 칸과의 간격의
                    # 절반으로 잡는다 - 칸 사이 간격이 30pt보다 좁은 문서는
                    # (KR5139420015 실측: 마지막 두 칸 간격이 44pt뿐이라
                    # ±30이면 앞 칸(5년) 값까지 같이 잡혀 "147315"처럼
                    # 두 값이 붙어버렸다) 고정폭을 쓰면 앞 칸 값까지
                    # 끌어온다.
                    left_margin = (
                        min(30, (lx - col_x0s[last_ci - 1]) / 2)
                        if last_ci > 0 else 30)
                    # 오른쪽 경계 +30은 콤마 있는 4자리 값(예: "2,144",
                    # x0=502.5, 칸 왼쪽 경계 474.8에서 +27.7)엔 맞는데,
                    # 같은 칸의 콤마 없는 3자리 값(예: "944", x0=506.6,
                    # +31.8)은 오른쪽 정렬 탓에 자릿수가 적을수록 시작
                    # 위치가 더 오른쪽으로 밀려서 그 폭을 벗어난다
                    # (KR5157450017 실측: C-F/C-W/C-I 세 클래스만
                    # 10년후 값이 1,000천원 밑이라 콤마가 없어서 이
                    # 문턱에 걸려 통째로 빠졌다 - 같은 표의 다른
                    # 클래스는 전부 콤마 있는 값이라 안 걸렸다). 마지막
                    # 칸은 오른쪽에 다음 칸이 없어 넓혀도 다른 칸 값을
                    # 끌어올 위험이 없으므로 +40으로 넉넉히 늘린다.
                    ws = [w for w in words
                          if lx - left_margin <= w["x0"] <= lx + 40
                          and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    if txt and col_of(lx) == last_ci:
                        ent[last_ci] = txt
                grid.append({"top": top, "bottom": bottom, "cells": ent})

            # "OO년후" 글자가 한 칸 안에서 줄바꿈 없이 온전히 찍히는
            # 문서가 대부분이라, 원래는 한 밴드(줄) 안에서 연도 낱말이
            # 4개 이상 모이는 첫 줄을 그대로 머리글로 썼다. 그런데 "2년후"
            # 만 유독 "2"/"년"/"후" 세 글자가 세로로 줄바꿈되어 세 칸(밴드)
            # 으로 쪼개지는 문서가 있다(KR5123420039 실측: "1년후"/"3년후"
            # 등은 한 칸인데 "2년후"만 세 줄). 그러면 "2년후" 글자가 완성
            # 되기 전에 다른 네 칸("1/3/5/10년후")만으로 이미 4개가 모여
            # 그 줄에서 멈춰버려 "2년후" 칸이 통째로 안 잡힌다 - 그 칸의
            # 값이 나중에 가장 가까운 다른 연도(3년후)로 잘못 흡수된다.
            # 그래서 한 줄만 보지 않고, 숫자 값이 나오는 첫 줄(=표 본문
            # 시작) 전까지 모든 줄의 글자를 칸별로 이어붙인 뒤에 연도를
            # 찾는다 - 세 줄에 걸친 "2년후"도 다 이어붙고 나서야 완성된다.
            year_by_col = {}
            acc = {}
            for r in grid:
                # 숫자 칸 하나만으로 "본문 시작"이라 보면 안 된다 - "2년후"가
                # 줄바꿈된 문서에서 "2" 한 글자짜리 머리글 조각도 숫자라서
                # 속는다(그러면 acc가 하나도 안 쌓여 표 전체를 놓친다).
                # 값 행은 연도 칸 여러 개가 한꺼번에 숫자로 채워지므로,
                # 숫자 칸이 3개 이상 동시에 있을 때만 본문 시작으로 본다.
                num_cols = sum(
                    1 for v in r["cells"].values()
                    if v.replace(" ", "").replace(",", "").isdigit()
                )
                if num_cols >= 3:
                    break
                for ci, v in r["cells"].items():
                    cur = acc.get(ci, "")
                    # 이미 완성된 칸(예: "10년후")에 또 글자를 붙이면 안
                    # 된다 - 셀 경계가 없는 마지막 칸("last_ci")은 밴드마다
                    # 주변 낱말을 다시 찾아 채우는 보정이 있는데, "2년후"
                    # 처럼 세 줄로 쪼개진 칸과 같은 표에 있으면 그 세 줄
                    # 각각에서 "10년후"가 매번 다시 잡혀 "10년후10년후"로
                    # 겹쳐 붙어 연도로 안 읽힌다(KR5123420039 실측). 이미
                    # 완성된 연도 칸은 더 안 건드린다.
                    if COST_AFTER_RE.match(cur.replace(" ", "")):
                        continue
                    # cur가 "OO년후" 조각으로 보이지 않는 남남의 글자를
                    # 담고 있으면(실측 KR5156450026: 표 맨 위 캡션
                    # "투자기간"이 하필 "1년후" 칸과 같은 x열로 셀이
                    # 잡혀 먼저 쌓이고, 그 뒤에 진짜 "1년후"가 이어
                    # 붙으면 "투자기간1년후"가 돼 다시는 연도로 안
                    # 읽힌다 - 그 결과 1년후 칸 전체가 통째로 빠졌었다)
                    # 이어붙이지 말고 새로 시작한다. 진짜 "OO년후"
                    # 조각(숫자/년/후로만 된 짧은 글자, "2년후"가 세
                    # 줄로 쪼개지는 문서의 "2"/"년"/"후" 각 조각 포함)
                    # 이면 그 모양을 유지하므로 안 걸린다.
                    if cur and not re.fullmatch(r"[\d년후]*", cur):
                        cur = ""
                    acc[ci] = cur + v
            cand = {}
            for ci, v in acc.items():
                m = COST_AFTER_RE.match(v.replace(" ", ""))
                if m and m.group(1) in ("1", "2", "3", "5", "10"):
                    cand[ci] = f"{m.group(1)}y"
            if len(set(cand.values())) >= 4:
                year_by_col = cand
            if not year_by_col and carry and carry[2] == page_num - 1:
                # 머리글이 앞 페이지에 있고 값만 이어지는 경우
                prev_years, prev_cols = carry[0], carry[1]
                if len(col_x0s) == len(prev_cols):
                    year_by_col = dict(prev_years)
                else:
                    # 앞 페이지 표가 머리글 줄에서는 연도 칸 사이사이에
                    # (한 글자씩 줄바꿈되며 생긴) 잔가지 칸이 더 끼어
                    # prev_cols 전체 칸 수가 더 많은 문서가 있다
                    # (KR5113420013 46/47쪽 실측: 46쪽 12칸 중 연도 칸은
                    # {2,4,6,8,10} 5개뿐인데, 나머지 비연도 칸이 마침
                    # 연도 칸과 거의 같은 거리(약 5.3~5.5pt 차이)에 있어,
                    # 이어지는 47쪽의 성긴 칸(7개)과 가장 가까운 칸을
                    # prev_cols "전체"에서 찾으면 매번 비연도 칸이 근소한
                    # 차이로 먼저 걸려 연도 칸 5개 중 4개를 통째로
                    # 놓쳤다). 전체 칸이 아니라 "연도로 이미 확인된 칸"
                    # 중에서만 가장 가까운 것을 찾는다 - 비연도 칸은
                    # 애초에 후보에서 빠지므로 이 근접 오탐이 안 생긴다.
                    prev_year_cols = [(prev_cols[k], v) for k, v in prev_years.items()]
                    for ci, x in enumerate(col_x0s):
                        px, y = min(prev_year_cols, key=lambda kv: abs(kv[0] - x))
                        if abs(px - x) <= 8:
                            year_by_col[ci] = y
            if len(set(year_by_col.values())) < 4:
                continue

            first_val = min(year_by_col)
            # 머리글 칸과 값 칸의 셀 경계가 살짝 어긋나 서로 다른 열
            # 번호로 잡히는 문서가 있다(KR5120450015 실측: 머리글
            # "1년후"는 열2, 그 아래 값 "214"(2년후)는 열3 - 셀 테두리가
            # 줄마다 미세하게 다시 그려져 col_x0s가 실제보다 잘게
            # 쪼개졌다). 열 번호를 그대로 맞추면 어긋난 칸끼리 짝지어져
            # 못 찾거나(위 nnum<4로 버려짐) 엉뚱한 연도에 값이 들어간다.
            # 열 번호가 아니라 x좌표로, 머리글 칸에 가장 가까운 값 칸을
            # 찾는다 - 표 안에서 칸 폭(대략 40pt 안팎)의 절반을 넘게
            # 벗어나는 어긋남은 없다고 보고 문턱을 20pt로 둔다.
            year_by_x = {col_x0s[ci]: y for ci, y in year_by_col.items()}
            by_code = {}
            for ridx, r in enumerate(grid):
                vals = {}
                for ci, txt in r["cells"].items():
                    v = txt.replace(" ", "").replace(",", "")
                    if not v.isdigit():
                        continue
                    x = col_x0s[ci] if ci < len(col_x0s) else None
                    if x is None:
                        continue
                    near_x = min(year_by_x, key=lambda k: abs(k - x))
                    if abs(near_x - x) <= 20:
                        vals.setdefault(year_by_x[near_x], v)
                if len(vals) < 4:
                    continue
                # 클래스명은 값 행과 다른 띠에 그려진다 - 이 행의 y구간
                # 안에서 첫 값 열보다 왼쪽에 있는 글자를 모은다.
                lim = col_x0s[first_val] - 2
                ws = [w for w in words
                      if (w["x0"] + w["x1"]) / 2 < lim
                      and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1
                      and not CAPTION_NOISE_RE.match(w["text"])]
                ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                label = " ".join(w["text"] for w in ws)
                code = _label_class_code(label)
                if not code:
                    # 클래스명이 값 행과 같은 띠 한 줄이 아니라 여러
                    # 줄에 걸쳐 있고, 코드를 담은 괄호 자체가 줄바꿈으로
                    # 쪼개지는 문서가 있다(KR5113420013 47쪽 실측: 값 행
                    # "19 39 59 104 235"의 클래스명이 "수수료미징구-
                    # 오프라"/"인-개인연금,기관(C-"/"F)" 세 줄로 나뉘고,
                    # 위 판정은 값 행과 같은 띠(맨 아래 "F)" 조각)만 보므로
                    # 앞 두 줄을 놓쳐 코드를 못 찾는다 - 비용예시 값은
                    # 5개 다 있는데 코드가 없어 클래스 자체가 통째로
                    # 빠졌다). 바로 앞 값 행의 아래쪽 경계(없으면 40pt
                    # 위)까지 범위를 넓혀 같은 칸의 글자를 다시 모은다 -
                    # 좁은 범위로 이미 실패했을 때만 넓히므로, 한 줄에
                    # 다 있는 정상 표에는 영향이 없다.
                    prev_bottom = grid[ridx - 1]["bottom"] if ridx > 0 else r["top"] - 40
                    ws2 = [w for w in words
                           if (w["x0"] + w["x1"]) / 2 < lim
                           and prev_bottom - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1
                           and not CAPTION_NOISE_RE.match(w["text"])]
                    ws2.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    code = _label_class_code(" ".join(w["text"] for w in ws2))
                if not code and r is grid[-1] and i + 1 < len(pdf.pages):
                    # 이 표의 마지막 행은 이름표가 "코드" 조각만 남기고
                    # 페이지 경계에서 끊길 수 있다(KR5113470030/S 실측:
                    # 값 행 바로 다음에 와야 할 "(S)" 코드 줄이 이
                    # 페이지엔 아예 없고, 다음 페이지 맨 위 첫 줄로
                    # 넘어가 있다). 다음 페이지 맨 첫 줄만 좁게 봐서
                    # 코드 조각이 있으면 그걸로 채운다.
                    nxt_words = pdf.pages[i + 1].extract_words(
                        x_tolerance=2, keep_blank_chars=False)
                    nxt_lines = cluster_lines(nxt_words, tol=2.5)
                    if nxt_lines:
                        lead = "".join(w["text"] for w in nxt_lines[0])
                        # 다음 페이지 첫 줄이 이미 캡션("판매수수료및
                        # 보수·비용")이나 숫자값을 담고 있으면, 그건 이
                        # 행이 이어지는 이름표가 아니라 완전히 새 행
                        # (다른 클래스)의 시작이다(KR5113420013 실측:
                        # 46쪽 마지막 행 다음 47쪽 첫 줄이 "인-개인연금
                        # (C)판매수수료및보수·비용"으로 캡션까지 붙어
                        # 있다 - 이걸 그대로 이 행 코드로 쓰면 사실은
                        # 클래스 "C"의 새 행 이름표 앞부분인데 이 행
                        # (다른 클래스)의 코드로 잘못 갖다 붙인다). 순수
                        # 코드 조각만 있고 그 뒤에 아무 것도(캡션도
                        # 숫자도) 안 붙어 있을 때만 받는다 - 애매하면
                        # 코드를 못 찾은 채로 두는 게(누락) 엉뚱한 코드에
                        # 값을 붙이는 것(오귀속)보다 낫다.
                        if not re.search(r"[0-9]|판매수수료|보수|비용", lead):
                            code = _label_class_code(label + lead)
                if not code:
                    # 자동전환 티어 클래스(C1→C2→C3→C4)는 요약표엔 C1만
                    # 있고(C2~C4는 최초가입 불가라 요약표에 안 실림),
                    # 상세표는 넷을 "체감(C1~C4)"처럼 한 행에 몰아
                    # 적는다(교보악사 KR5120450015/KR5120450018 실측).
                    # 물결표 때문에 위 코드 정규식들은 하나도 안 걸려
                    # 이 행 자체가 통째로 버려졌었다 - C2/C3/C4는
                    # cost_projection이 영영 안 채워진다. "(접두 + 시작
                    # 숫자 ~ [접두] + 끝 숫자)" 모양이면 그 구간의 모든
                    # 코드를 이 행 값으로 채운다. known_codes에 실제로
                    # 있는 코드만 받아, 엉뚱한 문자열을 코드로 지어내지
                    # 않는다.
                    mrange = RE_LABEL_CODE_RANGE.search(label.replace(" ", ""))
                    if mrange:
                        prefix, start, end = mrange.groups()
                        for cc in (f"{prefix}{n}" for n in
                                   range(int(start), int(end) + 1)):
                            if cc in known_codes:
                                by_code.setdefault(cc, vals)
                    continue
                # 같은 클래스가 "판매수수료 및 보수·비용"과 "(피투자
                # 집합투자기구 포함)" 두 줄로 나오는데 앞줄이 기본값이다.
                by_code.setdefault(code, vals)
            # 이 페이지 표의 값 행이 전부 코드 인식에 실패해 by_code가
            # 비어도(KR5113420013 46쪽 실측: 유일한 값 행의 이름표가
            # "판매수수료및보수·비용" 캡션과 뒤섞여 코드를 못 찾음),
            # 이 표 자체가 유효한 연도 칸 구성(year_by_col)을 가졌다면
            # 다음 페이지가 이어받을 수 있게 carry는 별도로 남긴다 -
            # by_code에 묶어 두면 이런 페이지에서 carry가 끊겨, 자기
            # 머리글이 없는 다음 페이지(47쪽)가 연도 칸을 못 물려받고
            # 표 전체(C-F 등 5개 클래스)가 통째로 스킵됐다.
            if year_by_col:
                carry = (year_by_col, col_x0s, page_num, tx1)
            if by_code:
                out.append((page_num, by_code))
    return out


# ---------------------------------------------------------------------------
# "가.투자자에게 직접 부과되는 수수료" 표 - 셀 경계 기반 판매수수료 복구
#
# 이 표는 원래 다른 표들과 같이 좌표(단어의 y로 줄 묶기 + x로 칸 판정)로
# 읽었는데, 그 방식으론 구조적으로 못 푸는 게 두 가지 있었다:
#
#   (1) 병합 셀 - "없음" 하나가 여러 클래스 행을 세로로 덮는 서식.
#       텍스트는 한 번만 찍히니 나머지 행에선 아무것도 안 보인다
#       (KR5172450019: 12행/14행을 덮는 "없음").
#   (2) 셀 내부 줄바꿈 - 한 칸의 내용이 여러 줄로 내려오는데, 옆 칸
#       (클래스명/가입자격)도 같이 줄바꿈되면서 y로 묶을 때 서로 섞인다
#       (KR5157450090 S: "3년미만"과 "환매시" 사이에 클래스명
#       "수수료후취-"가 끼어들어 조건 문구를 통째로 놓쳤다).
#
# 실측으로 두 원인이 남은 실패의 98%였고(줄바꿈 55% / 병합 43%),
# page.find_tables()가 100개 문서 전부에서 동작하는 걸 확인해서
# 셀 경계 기반으로 바꿨다. 그 결과 좌표 방식에서 필요했던 보정들
# (수수료 칸 왼쪽 경계 x 추정, 창 확장 규칙, 헤더 폭 필터, 마커 개수
# 매칭)이 전부 필요 없어졌다 - 칸 경계를 PDF가 직접 알려주기 때문이다.
#
# 문구는 원문을 그대로 쓰지 않고 "{조건}{기준}의 {비율}%이내" 틀로 다시
# 쓴다. 원본이 "이내"를 빼먹는 문서가 있어서다(KR5122420005 A: 이 표엔
# "0.10%"인데 요약표 확인값은 "납입금액의 0.10%이내").
GA_CAPTION_RE = re.compile(r"투자자에게직접부과되는수수료")
GA_NO_VALUE = ("없음", "-")
# 후취 판매수수료 조건 문구는 문서마다 표기가 다르다(실측):
#   "3년 미만 환매시" / "3 년 이내 환매시" / "3년 미만:" (환매시 없이 콜론)
# 조건을 놓치면 "무조건 떼는 수수료"로 뜻이 달라지므로 넓게 잡는다.
# 후취 조건 표기를 전수 조사해보니 141종의 수수료 문구 중 아래처럼 갈렸다:
#   "3년 미만 환매시 환매금액의..."  (가장 흔함)
#   "3 년 이내 환매시 ..."           (미만 대신 이내)
#   "3년 미만: 환매금액의 ..."        (환매시 없이 콜론)
#   "3년 미만 환매금액의 ..."         (환매시도 콜론도 없음)
#   "1,095일 미만 환매 시 ..."        (년이 아니라 일수)
# 조건을 놓치면 "무조건 떼는 수수료"로 뜻이 달라지므로("3년미만 환매시인데
# 언급이 없다"는 사용자 지적으로 처음 발견) 뒤쪽 표현은 선택으로 두고
# "N년/N일 + 미만/이내"까지만 필수로 본다. 일수는 년으로 환산하지 않고
# 원문 단위 그대로 남긴다(1,095일 = 3년이지만 임의로 바꾸면 원문과 달라짐).
GA_COND_RE = re.compile(
    r"([\d,]+)\s*(년|일)\s*(?:미\s*만|이\s*내)\s*(?:환\s*매\s*시)?\s*[:：]?")
GA_PCT_RE = re.compile(r"([\d.]+)\s*%")
GA_BUNUI_RE = re.compile(r"100\s*분의\s*([\d.]+)")


def _ga_cells(page):
    """이 페이지 표들의 셀을 (bbox + 그 안의 텍스트)로 돌려준다.
    셀 안 단어를 y→x 순으로 이어붙이므로 셀 내부 줄바꿈이 있어도 원래
    읽는 순서대로 한 덩어리가 된다(옆 칸 텍스트는 애초에 안 들어온다)."""
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    out = []
    for t in page.find_tables():
        cells = [c for c in t.cells if c]
        if len(cells) < 4:
            continue
        for (x0, top, x1, bottom) in cells:
            ws = [w for w in words
                  if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                  and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]
            ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
            out.append({
                "x0": x0, "top": top, "x1": x1, "bottom": bottom,
                "text": " ".join(w["text"] for w in ws).strip(),
            })
    return out


def _ga_left_margin_cells(page, cells):
    """표의 맨 왼쪽 칸(종류/클래스명) 경계를 find_tables()가 못 잡는
    페이지가 있다(KR5139420020 실측: 표가 다음 장으로 이어지는데 그
    페이지에선 왼쪽 세로선이 인식이 안 돼, 클래스명 단어가 어떤 셀에도
    안 들어간다 - 클래스를 못 찾아 그 페이지가 통째로 비었다).

    이럴 때만 쓰는 보완: 인식된 셀들의 왼쪽 바깥에 있는 단어를, 그 셀들이
    만든 행 구간(y)에 맞춰 묶어 "클래스명 칸"을 되살린다. 인식된 셀이
    이미 그 자리를 덮고 있으면(정상 문서) 왼쪽 바깥에 아무것도 없어
    자동으로 아무 일도 안 한다."""
    if not cells:
        return []
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    out = []
    # 왼쪽 끝은 "페이지 전체"가 아니라 "표별"로 잡아야 한다. 한 페이지에
    # 다른 표가 같이 있으면(KR5139420020 p28: 아래쪽에 "나" 보수표가 x=51
    # 부터 있음) 페이지 기준 최솟값이 51이 돼서, 정작 x=135부터 시작하는
    # "가" 표 왼쪽의 클래스명(x≈69~129)을 "바깥"으로 못 본다.
    tables = [t for t in page.find_tables() if len([c for c in t.cells if c]) >= 4]
    for t in tables:
        tcells = [c for c in t.cells if c]
        left_edge = min(c[0] for c in tcells)
        outside = [w for w in words if w["x1"] <= left_edge + 1]
        if not outside:
            continue
        bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in tcells
                        if c[3] - c[1] > 8})
        for top, bottom in bands:
            ws = [w for w in outside if top - 1 <= w["top"] and w["bottom"] <= bottom + 1]
            if not ws:
                continue
            ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
            out.append({
                "x0": min(w["x0"] for w in ws), "x1": left_edge,
                "top": top, "bottom": bottom,
                "text": " ".join(w["text"] for w in ws).strip(),
            })
    return out


def _ga_pages(pdf):
    """이 표가 있는 페이지 번호. 캡션은 자본시장법 투자설명서 서식의 고정
    문구라 문서마다 안 바뀐다(판매수수료가 비어 있던 43개 문서 전부에서
    이 문구로 찾아지는 걸 확인). 표가 다음 장으로 이어지는 문서가 있어
    캡션 페이지와 그 다음 페이지를 같이 본다."""
    caption = []
    for i, page in enumerate(pdf.pages):
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        for l in cluster_lines(words):
            if GA_CAPTION_RE.search("".join(w["text"] for w in l).replace(" ", "")):
                caption.append(i)
                break
    wanted = set()
    for i in caption:
        wanted.add(i)
        if i + 1 < len(pdf.pages):
            wanted.add(i + 1)
    return sorted(n + 1 for n in wanted)


def _ga_header_columns(cells):
    """헤더 셀에서 선취/후취/환매/전환 칸의 x구간을 찾는다. 전환수수료
    칸이 아예 없는 문서가 있어(선취/후취/환매 3칸) 찾아지는 만큼만 쓴다.
    긴 설명문 셀이 우연히 걸리지 않게 짧은 셀만 본다.

    칸 이름이 위아래 두 셀로 쪼개져 있는 문서가 있다(KR5157450090 실측:
    x[505-552]에 "환매"(위 셀)와 "수수료"(아래 셀)가 따로 들어 있어,
    한 셀만 보면 "환매수수료"라는 이름을 못 찾고 이 문서 전체를 놓친다).
    x구간이 같은 셀들을 세로로 먼저 이어붙인 뒤 이름을 맞춘다."""
    stacked = {}
    for c in cells:
        t = c["text"].replace(" ", "")
        if not t or len(t) > 12:
            continue
        stacked.setdefault((round(c["x0"]), round(c["x1"])), []).append((c["top"], t))

    cols = {}
    for (x0, x1), pieces in stacked.items():
        joined = "".join(t for _, t in sorted(pieces))
        for key, pat in (("선취", "선취"), ("후취", "후취"),
                         ("환매", "환매수수료"), ("전환", "전환수수료")):
            if key not in cols and pat in joined:
                cols[key] = (x0, x1)
    if "환매" not in cols or len(cols) < 2:
        return None
    return cols


def _ga_commission_desc(text):
    """수수료 칸 텍스트를 정형화된 문구로 다시 쓴다. 기준(납입/환매금액)과
    비율을 둘 다 못 찾으면 None(= 채우지 않음)."""
    flat = text.replace(" ", "")
    basis = ("환매금액" if "환매금액" in flat
             else ("납입금액" if "납입" in flat else None))
    if not basis:
        return None
    pm = GA_PCT_RE.search(flat) or GA_BUNUI_RE.search(flat)
    if not pm:
        return None
    prefix = ""
    if basis == "환매금액":
        cm = GA_COND_RE.search(flat)
        if cm:
            prefix = f"{cm.group(1)}{cm.group(2)} 미만 환매시: "
    return f"{prefix}{basis}의 {pm.group(1)}%이내"


def enrich_sales_commission_from_ga_table(doc_id, existing_rows):
    """"나" 상세표 보강으로 추가된 클래스는 판매수수료 문구가 없다
    (그 표엔 그 칸 자체가 없음). 이 표에서 채운다."""
    targets = [r for r in existing_rows
               if r.get("class_code") and r.get("sales_commission_desc") is None]
    if not targets:
        return existing_rows
    by_code = {}
    for r in targets:
        by_code.setdefault(r["class_code"], []).append(r)

    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return existing_rows

    with pdfplumber.open(pdf_candidates[0]) as pdf:
        prev_cols, prev_page = None, None
        for page_num in _ga_pages(pdf):
            page = pdf.pages[page_num - 1]
            cells = _ga_cells(page)
            cells = cells + _ga_left_margin_cells(page, cells)
            if not cells:
                continue
            cols = _ga_header_columns(cells)
            if cols is None:
                # 표가 다음 장으로 이어지면 그 페이지엔 헤더가 없다
                # (KR5113420013: 헤더 44p / 데이터 45p). 바로 앞 페이지의
                # 칸 구성을 물려받는다 - 같은 표의 연속이라 x도 같다.
                if prev_cols is None or prev_page != page_num - 1:
                    continue
                cols = prev_cols
            prev_cols, prev_page = cols, page_num

            for c in cells:
                code = None
                for rx in (CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
                    mm = [x for x in rx.finditer(c["text"].replace(" ", ""))
                          if not _is_bad_code(x.group(1))]
                    if mm:
                        code = mm[-1].group(1)
                        break
                if not code or code not in by_code:
                    continue
                if by_code[code][0].get("sales_commission_desc") is not None:
                    continue  # 앞 페이지에서 이미 채움

                # 이 클래스 셀과 세로로 겹치는 수수료 칸 셀들. 병합 셀은
                # 여러 행을 덮는 하나의 큰 셀이라 이 겹침 판정만으로
                # 자연스럽게 해당 행 전부에 적용된다.
                lo, hi = c["top"], c["bottom"]
                vals = {}
                for key, (cx0, cx1) in cols.items():
                    # 헤더 셀이 데이터 셀보다 안쪽으로 그려진 문서가 있어
                    # (KR5172450019: 헤더 x[277-359] vs 값 x[271-365])
                    # 포함이 아니라 헤더 칸 중심이 값 셀 안에 드는지로 본다.
                    ccx = (cx0 + cx1) / 2
                    hit = [d for d in cells
                           if d is not c
                           and not (d["bottom"] <= lo + 1 or d["top"] >= hi - 1)
                           and d["x0"] - 3 <= ccx <= d["x1"] + 3]
                    vals[key] = " ".join(h["text"] for h in hit if h["text"]).strip()

                filled = [v for v in vals.values() if v]
                if len(filled) < 2:
                    continue  # 이 표의 칸을 제대로 못 읽음 - 건드리지 않는다
                if all(v in GA_NO_VALUE for v in filled):
                    # 읽힌 칸이 전부 "없음"/"-" = 판매수수료가 없는 클래스.
                    # (이어지는 페이지에서 전환수수료 칸 셀이 아예 없는
                    #  문서가 있어, 칸 개수가 아니라 "읽힌 것 전부"로 본다)
                    desc = "-"
                else:
                    desc = _ga_commission_desc(
                        " ".join(v for v in filled if v not in GA_NO_VALUE))
                if desc:
                    for r in by_code[code]:
                        r["sales_commission_desc"] = desc
                        r.setdefault("field_source_pages", {})["sales_commission_desc"] = page_num
                        sp = r.setdefault("source_pages", [r["page"]])
                        if page_num not in sp:
                            sp.append(page_num)

    return existing_rows



# ---------------------------------------------------------------------------
# 요약표(앞쪽 "<요약정보>" 안의 투자비용 표) - 셀 경계 기반
#
# 이 표가 class_fees의 주력 소스다(전체의 3분의 2). 좌표 방식일 때는 여기에
# 문서별 예외가 가장 많이 붙었다 - "소수 4개 + 정수 5개" 같은 개수 판정,
# 판매수수료 문구를 찾으려 데이터 줄 앞뒤로 창을 넓히는 규칙, 세로 캡션
# 걸러내는 x좌표 상수, 글자가 한 자씩 쪼개지는 서식 보정 등. 전부 "이 단어가
# 어느 칸인지 모른다"에서 나온 것이라, 셀 경계를 쓰면 사라진다.
#
# 열 매핑은 상세표와 달리 대조할 정답지가 없어서(이 표가 곧 정답지다)
# 헤더 이름으로 한다. 전수 조사(98개 문서) 결과 핵심 이름은 거의 고정이다:
#   판매보수 98 / 총보수 94 / 동종유형총보수 86 / 총보수ㆍ비용 계열 80
# 주의 두 가지:
#   - 가운뎃점이 문서마다 다르다(ㆍ · ･ ∙ ▪ •) → 정규화해서 비교
#   - "총보수"가 "총보수ㆍ비용"의 앞부분이라 짧은 이름부터 맞추면 밀린다
#     → 긴(구체적인) 이름부터 매칭

def _label_class_code(label):
    """라벨 문자열에서 클래스 코드를 뽑는다(못 찾으면 None). 요약표/상세표가
    같은 규칙을 쓰도록 한 곳에 모은다 - 코드 표기가 "(A)"(괄호 안),
    "A(수수료선취-오프라인)"(괄호 앞), "종류A", "(Cp(퇴직연금))"(중첩)로
    문서마다 다르다."""
    flat = label.replace(" ", "")
    for regex in (CLASS_CODE_RE, DETAIL_FEE_CLASS_CODE_NESTED_RE,
                  DETAIL_FEE_CLASS_CODE_NESTED_UNCLOSED_RE,
                  CLASS_CODE_NESTED_RE, DETAIL_FEE_CLASS_CODE_JONGRYU_RE):
        mm = [x for x in regex.finditer(flat)
              if not _is_bad_code(x.group(1))]
        if mm:
            return mm[-1].group(1)
    m2 = CLASS_CODE_PREFIX_RE.match(flat)
    if m2:
        return m2.group(1)
    # 코드가 괄호 없이 "코드 설명" 형태로 공백 하나로만 떨어져 있는
    # 문서가 있다(KR515302022M 실측 - 상세 비용예시표 클래스명 칸:
    # "A 수수료선취-오프라인", "C1 수수료미징구-오프라인-보수체감").
    # 위 정규식들은 전부 괄호를 전제해서 이 형태를 못 잡아 그 표
    # 전체(17개 클래스)가 버려졌다. 첫 낱말이 코드 모양이고 그 뒤가
    # 진짜 설명(한글 포함)일 때만 받아들인다 - 안 그러면 "수수료선취"
    # 같은 설명 첫 글자를 코드로 오인한다.
    parts = label.split(None, 1)
    if (len(parts) == 2 and RE_BARE_CLASS_CODE.match(parts[0])
            and not _is_bad_code(parts[0])
            and any("가" <= ch <= "힣" for ch in parts[1])):
        return parts[0]
    return None


# 가운뎃점은 문서마다 다른 글자를 쓴다. 심볼 폰트로 찍힌 문서는 유니코드
# 사용자 정의 영역(U+F000~U+F0FF)에 들어와서 눈으로는 똑같은 "총보수·비용"인데
# 글자 코드가 전혀 다르다(KR5111450067 실측: U+F09E - 총보수·비용 열이
# 통째로 안 잡혔다).
# 각주 표시는 "(주1)"이 한 덩어리로 잡히기도 하고 "(주" + "1)"로 쪼개져
# 나오기도 한다(KR5144420020 실측: 쪼개진 쪽을 못 걸러 마지막 클래스의
# 이름이 각주 전체를 삼켰고, 그 안의 "투자실적/연평균" 때문에 수익률표
# 행으로 오해받아 통째로 버려졌다).
FOOTNOTE_RE = re.compile(r"^\(?주\s*\d*(\)|$)")
DASHES = ("-", "\u2013", "\u2014", "\u2212")
SUMMARY_DOT_RE = re.compile("[\u318d\u00b7\uff65\u2219\u25aa\u2022\u30fb\u22c5\u2027\uf000-\uf0ff]")


def _norm_header(s):
    return SUMMARY_DOT_RE.sub("·", s.replace(" ", ""))


def _is_group_header(name):
    """여러 칸을 아우르는 묶음 머리글인지 본다. 아래 칸들의 이름이 한 셀에
    다 들어가 있는 표가 있는데(KR5194450018 실측: "판매 수수료 총 보수
    판매보수"가 한 칸), 그대로 두면 그 중 하나로 매칭돼 판매수수료 칸이
    판매보수로 잡힌다. 서로 다른 필드 이름이 둘 이상 들어 있으면 묶음
    머리글로 본다("동종유형 총보수"나 "총보수·비용"처럼 그 자체가 한
    이름인 것은 먼저 지우고 센다)."""
    t = _norm_header(name)
    for whole in ("동종유형총보수", "동종유형", "합성총보수·비용",
                  "총보수·비용", "총보수비용", "총보수,비용"):
        t = t.replace(whole, "|")
    hits = t.count("|") + sum(t.count(k) for k in
                              ("판매수수료", "판매보수", "총보수"))
    return hits >= 2


def _isnum_text(v):
    t = v.replace(" ", "").rstrip("%").replace(",", "")
    return bool(DECIMAL_RE.match(t) or t.isdigit())


def _join_number_words(ws):
    """숫자 칸을 만들 때 각주 번호를 떼어낸다. 세로줄이 안 그려진 열은
    표 테두리까지 훑기 때문에 값 옆·아래의 각주 표시가 딸려 들어온다
    (KR5114420022 실측: 10년 비용 "1,184" 옆의 위첨자 "1"이 붙어
    "1184 1"이 됐다). 가장 긴 토큰을 값으로 보고, 같은 줄에서 바로
    붙어 있는 조각만 함께 남긴다("1" "184"처럼 한 숫자가 쪼개진 경우)."""
    ws = sorted(ws, key=lambda w: (round(w["top"] / 3), w["x0"]))
    if len(ws) < 2:
        return " ".join(w["text"] for w in ws).strip()
    main = max(ws, key=lambda w: len(w["text"].replace(",", "")))
    keep = [main]
    for w in ws:
        if w is main:
            continue
        same_line = abs(w["top"] - main["top"]) < 3
        gap = min(abs(w["x0"] - main["x1"]), abs(main["x0"] - w["x1"]))
        if same_line and gap <= 3:
            keep.append(w)
    keep.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
    return " ".join(w["text"] for w in keep).strip()


def _pick_cost_number(text, prev):
    """비용예시 칸에 각주 번호가 섞여 들어오는 문서가 있다(KR5114420022
    실측: "1,184" 옆 위첨자 1이 같은 칸에 잡혀 "1184 1"이 됐다).
    각주 표시는 한두 자리이고 값은 세 자리 이상이라는 점으로 고르되,
    고른 값이 앞 기간보다 작으면(비용예시는 기간이 길수록 커진다)
    한 숫자가 쪼개진 것으로 보고 이어 붙인다."""
    toks = [t.replace(",", "") for t in text.split()]
    if len(toks) == 1:
        return toks[0]
    joined = "".join(toks)
    longs = [t for t in toks if t.isdigit() and len(t) >= 3]
    if len(longs) == 1 and all(len(t) <= 2 for t in toks if t is not longs[0]):
        pick = longs[0]
        if prev is None or not str(prev).isdigit() or int(pick) >= int(prev):
            return pick
    return joined if joined.isdigit() else text.replace(",", "")


def _summary_column_field(name):
    """헤더 이름 → 필드. 못 알아보면 None. 긴 이름부터 본다."""
    n = _norm_header(name)
    if not n:
        return None
    # "동종"과 "유형"이 다른 칸/다른 페이지로 쪼개져 "유형총보수"만 남는
    # 문서가 있다(KR5152420028 실측: 머리글이 페이지 경계에서 잘렸다).
    # 아래 총보수 규칙이 "...총보수"로 끝난다는 이유로 이걸 먼저 집어가
    # 동종유형 총보수가 진짜 총보수 자리에 들어갔다.
    if "동종유형" in n or ("유형" in n and "총보수" in n):
        return "peer_avg_fee"
    # "합성총보수,비용"처럼 가운뎃점 자리에 쉼표를 쓰는 문서가 있다
    # (KR5122420005 실측 - 그대로 두면 이 칸을 못 알아보고, 옆의 비용예시
    # 묶음 헤더가 대신 매칭돼 1년 비용예시가 총보수·비용 자리에 들어갔다).
    if ("총보수·비용" in n or "총보수비용" in n or "총보수,비용" in n
            or n.endswith("총보수·")):
        return "total_fee_and_cost"
    if "판매보수" in n:
        return "distribution_fee"
    if "판매수수료" in n:
        return "sales_commission_desc"
    if n == "총보수" or n.endswith("총보수"):
        return "total_fee"
    # 글자가 그려진 순서 때문에 "1년"이 "년 1"로 뒤집혀 추출되는 문서가
    # 있다(KR5156450026/KR5160420009/KR555202013M 실측 - 비용예시 열이
    # 통째로 안 잡혀 폴백의 큰 덩어리였다). 두 순서를 모두 받는다.
    m = (re.fullmatch(r"(?:최근)?(\d+)년(?:차|째|간)?", n)
         or re.fullmatch(r"(?:최근)?년(\d+)", n))
    if m:
        return f"cost_{m.group(1)}y"
    return None


# 날짜 표기가 문서마다 다르다. "작성기준일: 2025.05.16."과
# "4. 작성 기준일  2025년 01월 17일"이 둘 다 쓰인다. 년/월 구분자를
# 안 받아서 상품 34개가 기준일 없이 나갔다.
# 작성기준일을 마저 찾아볼 앞쪽 페이지 수(표지, 요약정보가 여기 있다).
AS_OF_SCAN_PAGES = 6

AS_OF_RE = re.compile(
    r"(?:작성)?기준일\s*[:：]?\s*(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})")
# 보수가 기간별로 나뉜 상품은 "언제 바뀌는지"가 답변의 핵심인데, 그게
# 날짜가 아니라 조건인 경우가 있다(KR5147430065 실측: "목표기준가격
# (종류A 누적기준가격 1,060원 이상)에 도달하여 운용전환이 이루어질 경우").
# 날짜가 문서 어디에도 없으므로 조건 문장을 그대로 남겨야 "전환됐나요"
# 같은 질문에 근거를 들어 답할 수 있다.


def _page_as_of(text):
    m = AS_OF_RE.search(text.replace(" ", "") if "기준일" in text.replace(" ", "") else text)
    if not m:
        m = AS_OF_RE.search(text)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _summary_grid(page, next_page=None, inherited=None):
    """요약표를 셀 격자로 읽는다. 이 페이지에서 "클래스종류/총보수/판매보수"
    헤더를 가진 표만 고른다(같은 페이지의 운용전문인력 표 등과 구분).
    돌려주는 것: (field_by_col, data_rows) 또는 None."""
    words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
    header_carry = None
    parts_carry = None
    parts_score = 0
    for t in page.find_tables():
        tbox = t.bbox          # 아래에서 t를 다른 뜻으로 다시 쓰므로 먼저 잡아둔다
        cells = [c for c in t.cells if c]
        # 앞 장에서 이어지는 표는 한두 행짜리라 칸이 얼마 안 된다
        # (KR518101012M 실측: C-e 한 행뿐인 표가 19칸이라 통째로 버려졌다).
        # 이어받을 열 구성이 있을 때만 기준을 낮춘다 - 뒤의 이어받기
        # 검사(열 x일치·행 채움)가 엉뚱한 작은 표를 걸러 준다.
        if len(cells) < (8 if inherited else 20):
            continue
        raw_x0s = sorted({round(c[0], 1) for c in cells})
        col_x0s = []
        for x in raw_x0s:
            if col_x0s and x - col_x0s[-1] <= 6:
                continue
            col_x0s.append(x)
        if len(col_x0s) < 4:
            continue

        def col_of(x0):
            return min(range(len(col_x0s)), key=lambda k: abs(col_x0s[k] - x0))

        bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in cells})
        grid = []
        for top, bottom in bands:
            ent = {}
            used = set()
            for (x0, ct, x1, cb) in [c for c in cells
                                     if abs(c[1] - top) < 1 and abs(c[3] - bottom) < 1]:
                ws = [w for w in words
                      if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                      and ct - 1 <= (w["top"] + w["bottom"]) / 2 <= cb + 1]
                ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                txt = " ".join(w["text"] for w in ws).strip()
                if txt:
                    ent[col_of(x0)] = txt
                    used.update(id(w) for w in ws)
            grid.append({"top": top, "bottom": bottom, "cells": ent, "used": used})

        # 표의 오른쪽 세로줄이 인식되지 않아 마지막 열이 격자에서 통째로
        # 빠지는 문서가 있다(KR5113420069/KR5113450401 실측: 격자는
        # x=500.8에서 끝나는데 "10년" 머리글과 값 510은 그 오른쪽,
        # 표 테두리(542.6) 안쪽에 있다). 테두리 안에 격자 밖 글자가
        # 남아 있으면 열을 하나 더 만들어 머리글과 값을 함께 살린다.
        last_x1 = min((c[2] for c in cells if abs(c[0] - col_x0s[-1]) < 1),
                      default=col_x0s[-1])
        if tbox[2] - last_x1 > 10:
            extra = [w for w in words
                     if last_x1 <= (w["x0"] + w["x1"]) / 2 <= tbox[2] + 1
                     and tbox[1] <= (w["top"] + w["bottom"]) / 2 <= tbox[3]]
            if extra:
                xi = len(col_x0s)
                col_x0s.append(last_x1)
                claimed = set()
                # 병합 셀 때문에 세로로 긴 띠가 같은 글자를 또 가져가면
                # 머리글이 "10년 510"처럼 뭉쳐 열 이름 매칭이 깨진다.
                # 짧은 띠부터 나눠 가장 딱 맞는 행이 먼저 가져가게 한다.
                for r in sorted(grid, key=lambda r: r["bottom"] - r["top"]):
                    ws = [w for w in extra
                          if id(w) not in claimed and id(w) not in r["used"]
                          and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1]
                    if not ws:
                        continue
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    if _isnum_text(txt) is False and _join_number_words(ws) != txt:
                        cand = _join_number_words(ws)
                        if _isnum_text(cand):
                            txt = cand
                    r["cells"][xi] = txt
                    r["used"].update(id(w) for w in ws)
                    claimed.update(id(w) for w in ws)

        # 데이터 행: 소수 2개 이상 (비용예시 정수는 없는 문서도 있어 소수만 본다)
        first_data = None
        for gi, r in enumerate(grid):
            ndec = sum(1 for v in r["cells"].values()
                       if DECIMAL_RE.match(v.replace(" ", "").rstrip("%")))
            if ndec >= 2:
                first_data = gi
                break
        # 헤더만 있고 값 행은 통째로 다음 페이지에 있는 문서가 있다
        # (KR5118420036/KR5113450111 실측: 4쪽에 "총보수/판매보수/1년..."
        # 헤더만 있고 클래스 행은 5쪽부터다). 예전엔 이런 표를 그냥
        # 버려서 다음 페이지가 물려받을 열 구성이 없었고, 결국 문서
        # 전체가 폴백이었다. 값 행이 없어도 열 구성은 넘겨준다.
        header_only = first_data is None
        if header_only:
            first_data = len(grid)

        # 여러 칸을 아우르는 묶음 헤더는 칸 이름이 아니므로 뺀다
        # ("1,000만원 투자시 ... 총보수•비용 예시(단위:천원)"가 비용예시
        #  칸들 위에 걸쳐 있어서, 그대로 쓰면 "총보수•비용"이 들어 있다는
        #  이유로 1년 칸이 total_fee_and_cost로 잘못 매칭된다).
        # 처음엔 "길이가 길면 묶음 헤더"로 걸렀는데, 그러면 짧은 묶음
        # 헤더("예시 (단위:천원)")는 못 걸러 이름이 "예시(단위:천원)1년"이
        # 되고, 반대로 진짜 칸 이름의 조각("비용")이 잘려 "총보수·"만
        # 남는 일이 생겼다(실측 22개 문서가 이 때문에 폴백). 길이가 아니라
        # 묶음 헤더에만 나오는 문구로 거른다.
        header_names = {}
        for r in grid[:first_data]:
            for ci, v in r["cells"].items():
                flat = v.replace(" ", "")
                if len(flat) > 24:
                    continue
                if any(k in flat for k in ("투자시", "단위", "예시", "투자자가", "투자기간", "천원")):
                    continue
                if _is_group_header(flat):
                    continue
                header_names.setdefault(ci, []).append(v)
        def map_headers(names):
            out_map = {}
            for ci, parts in names.items():
                # 같은 칸 이름이 헤더 두 줄에 겹쳐 그려진 표가 있다
                # (KR5157420003 실측: "2년"이 두 띠에 다 잡혀 "2년2년"이
                # 됐다). 이어 붙인 이름이 안 맞으면 중복을 걷어낸 이름으로
                # 다시 본다.
                uniq = list(dict.fromkeys(parts))
                for cand in ("".join(parts), "".join(uniq)):
                    f = _summary_column_field(cand)
                    if f and f not in out_map.values():
                        out_map[ci] = f
                        break
            return out_map

        inherited_need = 0
        field_by_col = map_headers(header_names)

        # 표 머리글이 페이지 경계에서 위아래로 잘리는 문서가 있다
        # (KR5152420028/KR5113450111 실측: 앞 장 맨 아래에 "판매/총보수/
        # 동종/총보수·", 다음 장 맨 위에 "수수료/보수/유형총보수/비용"이
        # 나뉘어 찍힌다). 어느 쪽만 봐도 열 이름이 완성되지 않으니, 이 장
        # 머리글만으로 안 될 때에 한해 앞 장 조각을 x위치로 맞춰 앞에
        # 이어 붙여 본다(되면 그때만 쓴다).
        prev_parts = inherited[2] if inherited and len(inherited) > 2 else None
        if prev_parts and header_names and \
                not {"total_fee", "distribution_fee"} <= set(field_by_col.values()):
            # 두 장의 격자가 조금씩 어긋나 있어서 조각을 어느 열에 붙일지
            # 두 가지로 본다: (1) x가 가장 가까운 열, (2) 그 조각이 실제로
            # 들어가는 열 구간. 문서마다 맞는 쪽이 달라(KR5152420028은 1번,
            # KR5113450111은 2번) 둘 다 시도하고 되는 쪽을 쓴다.
            def near_cols(x):
                # 두 장의 격자가 통째로 조금 밀려 있어(KR5152420028 실측
                # 약 5pt) 8pt로는 "1년"만 짝을 못 찾아 그 열이 통째로
                # 빠졌다. 여기서는 조금 넉넉히 본다 - 잘못 붙으면 아래
                # 채움 검사에서 이 조합 자체가 떨어진다.
                return [ci for ci in range(len(col_x0s))
                        if abs(x - col_x0s[ci]) <= 12]

            def span_cols(x):
                ci = bisect.bisect_right(col_x0s, x + 6) - 1
                return [ci] if ci >= 0 else []

            best = None
            for pick in (near_cols, span_cols):
                joined_names = {ci: list(ps) for ci, ps in header_names.items()}
                for x, ps in prev_parts:
                    for ci in pick(x):
                        joined_names[ci] = list(ps) + joined_names.get(ci, [])
                merged = map_headers(joined_names)
                # 앞 장 조각을 붙였더니 이름이 완성됐다고 해서 이 표가 그
                # 표의 연장이라는 뜻은 아니다(KR5118420062 실측: 다음 장
                # 수익률표에 씌워져 "비교지수(%)"가 클래스로 잡혔다).
                # 잡힌 칸들이 한 행에 실제로 채워져 있어야 받아들인다.
                need_m = max(3, int(len(merged) * 0.7))
                if {"total_fee", "distribution_fee"} <= set(merged.values()) and \
                        any(sum(1 for ci in merged if ci in r["cells"]) >= need_m
                            for r in grid[first_data:]):
                    if best is None or len(merged) > len(best[0]):
                        best = (merged, joined_names, need_m)
            if best:
                field_by_col, header_names, inherited_need = best

        # 세로줄이 값 구간에서 끊겨 있어 데이터 행에 그 열의 칸이 아예
        # 안 생기는 표가 있다(KR5127420034 실측: 헤더엔 "10년" 칸이 있는데
        # 값 행엔 그 칸이 없어 10년 비용예시가 통째로 빠졌다 - 폴백 문서
        # 20개가 이 한 가지 이유였다). 칸이 안 그려졌을 뿐 열의 x범위와
        # 행의 y범위는 표에서 이미 알고 있으니, 그 사각형 안의 글자를
        # 읽어 채운다. 값 칸(숫자)일 때만 채워서 병합 셀의 글자가 여러
        # 행에 복제되지 않게 한다.
        table_x1 = max(c[2] for c in cells)

        def fill_missing(field_map):
            for r in grid:
                if sum(1 for v in r["cells"].values()
                       if DECIMAL_RE.match(v.replace(" ", "").rstrip("%"))) < 2:
                    continue
                for ci in field_map:
                    if ci in r["cells"] or ci + 1 > len(col_x0s):
                        continue
                    lo = col_x0s[ci]
                    hi = col_x0s[ci + 1] if ci + 1 < len(col_x0s) else table_x1
                    # 이 행의 다른 칸이 이미 가져간 글자는 뺀다. 안 그러면
                    # 좁은 칸 두 개에 걸쳐 가운데 정렬된 숫자가 양쪽 열에
                    # 복제돼(KR5129420031 실측: 판매보수 0.18이 7·8번 열에
                    # 모두 들어갔다), "이 열엔 값이 없다"를 근거로 어긋난
                    # 열을 고치는 아래 보정이 통째로 무력해진다.
                    ws = [w for w in words
                          if id(w) not in r["used"]
                          and lo - 1 <= (w["x0"] + w["x1"]) / 2 < hi
                          and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1]
                    if not ws:
                        continue
                    txt = _join_number_words(ws)
                    t = txt.replace(" ", "").rstrip("%").replace(",", "")
                    if DECIMAL_RE.match(t) or t.isdigit():
                        r["cells"][ci] = txt
                        r["used"].update(id(w) for w in ws)

        fill_missing(field_by_col)

        def _isnum(v):
            t = v.replace(" ", "").rstrip("%").replace(",", "")
            return bool(DECIMAL_RE.match(t) or t.isdigit())

        def realign(field_map):
            """헤더가 가리키는 열과 값이 실제로 들어 있는 열이 어긋난 것을
            바로잡는다. 헤더 이름으로 잡을 때든 앞 페이지에서 물려받을
            때든 같은 어긋남이 생기므로 두 경우 모두 이 함수를 쓴다."""
            # 어긋남 자체는 흔하다(KR5129420025/KR5144450095 실측:
            # "판매보수" 헤더는 7번 열인데 값은 6번 열에 있다 - 열 병합
            # 허용치(6pt)를 넘게 벌어져서다). 헤더가 가리키는 열에 값이
            # 하나도 없고 바로 옆 열에 값이 있으면 그쪽으로 옮긴다.
            def move(ci, ok_cols):
                for nb in (ci - 1, ci + 1):
                    if nb in ok_cols and nb not in field_map:
                        field_map[nb] = field_map.pop(ci)
                        return
            # 무엇을 "값이 있는 칸"으로 볼지가 핵심이다. 같은 격자 안에
            # 수익률표·운용전문인력표가 이어 붙은 문서가 많아서
            # (KR5153420022/KR5118420036 실측) 전체 행의 숫자를 근거로
            # 삼으면 "그 칸에도 값이 있다"고 착각해 필드를 엉뚱한 옆 칸으로
            # 옮긴다. 그렇다고 총보수 칸을 기준으로 보수 행을 고르면,
            # 총보수 칸 자체가 어긋난 문서에선 아무것도 못 고친다
            # (KR5118420006 실측: 이어지는 장의 세 클래스를 통째로 잃었다).
            # 특정 필드에 기대지 말고 "잡아 둔 칸들이 가장 많이 채워진 행"을
            # 보수 행으로 본다.
            need_row = max(3, int(len(field_map) * 0.6))
            fee_rows = [r for r in grid[first_data:]
                        if sum(1 for ci in field_map if ci in r["cells"]) >= need_row]
            if fee_rows:
                ok = {ci for r in fee_rows for ci, v in r["cells"].items()
                      if _isnum(v)}
                # 원본이 "값 없음"을 "-"로 찍은 칸도 그 필드가 쓰는 칸이다
                # (동종유형 비교가 없는 상품이 그렇다).
                ok |= {ci for r in fee_rows for ci, v in r["cells"].items()
                       if v.replace(" ", "") in DASHES}
            else:
                ok = {ci for r in grid[first_data:]
                      for ci, v in r["cells"].items() if _isnum(v)}
            for ci in sorted(field_map):
                if ci not in ok:
                    move(ci, ok)

        def spread_commission(field_map):
            """판매수수료가 여러 클래스에 공통이면 원본이 칸 하나로 병합해
            버린다(KR5194450018 실측: "없음" 한 칸이 C1~S 다섯 행을
            세로로 덮는다). 그 칸이 세로로 품고 있는 행들에 같은 값을
            채운다 - 병합 셀의 뜻 그대로다."""
            cm = next((c for c, f in field_map.items()
                       if f == "sales_commission_desc"), None)
            if cm is None:
                return
            src = [(g["top"], g["bottom"], g["cells"][cm])
                   for g in grid if cm in g["cells"]]
            for g in grid:
                if cm in g["cells"]:
                    continue
                mid = (g["top"] + g["bottom"]) / 2
                for t0, b0, txt in src:
                    if t0 - 1 <= mid <= b0 + 1:
                        g["cells"][cm] = txt
                        break

        def infer_commission(field_map):
            """판매수수료 칸은 제 이름이 묶음 머리글에 삼켜져 따로 안
            잡히는 문서가 많다(KR5194450018 실측: "판매 수수료 총 보수
            판매보수"가 한 칸에, KR5118420036 실측: 그 칸 이름이
            "투자자가 부담하는 수수료..."라 묶음 머리글로 걸러진다).
            이 표에서 판매수수료는 늘 총보수 왼쪽의 문구 칸이고 값은
            "없음/-" 아니면 "납입금액의 X%" 꼴이다 - 그 자리로 채운다.
            클래스명 칸은 이 조건에 안 걸린다(이름은 "-"가 아니고
            "금액의"도 없다)."""
            if "sales_commission_desc" in field_map.values():
                return
            tf = next((c for c, f in field_map.items() if f == "total_fee"), None)
            if tf is None:
                return
            # 같은 격자에 붙어 있는 수익률표 행까지 보면 그 칸에 날짜가
            # 들어 있어 판단이 깨진다 - 잡아 둔 칸들이 대부분 채워진
            # "진짜 보수 행"만 본다.
            need_row = max(3, int(len(field_map) * 0.6))
            fee_rows = [r for r in grid[first_data:]
                        if sum(1 for c in field_map if c in r["cells"]) >= need_row]
            for ci in range(tf - 1, max(-1, tf - 4), -1):
                if ci < 0 or ci in field_map:
                    continue
                vals = [r["cells"][ci].replace(" ", "")
                        for r in (fee_rows or grid[first_data:]) if ci in r["cells"]]
                if not vals:
                    continue
                if all(v in DASHES or v == "없음" or "금액의" in v for v in vals):
                    field_map[ci] = "sales_commission_desc"
                    return

        realign(field_by_col)
        infer_commission(field_by_col)
        spread_commission(field_by_col)

        # 표가 다음 페이지로 이어지면 그 페이지엔 헤더가 반복되지 않는다
        # (KR5113470030/KR5118420006 등 실측: A-e/C-e/C-P 같은 클래스가
        # 이어지는 페이지에 있는데 헤더가 없어 그 페이지를 통째로 버렸다 -
        # 폴백의 가장 흔한 원인이었다). 앞 페이지에서 잡은 열 구성이 이
        # 표와 같은 모양이면(열 개수·x좌표가 거의 같으면) 그대로 물려받는다.
        fields = set(field_by_col.values())
        if not {"total_fee", "distribution_fee"} <= fields and inherited:
            prev_fields, prev_cols = inherited[0], inherited[1]
            # 이어지는 페이지에선 맨 왼쪽 클래스명 열이 표 인식에서
            # 빠지기도 해서 열 개수가 달라진다(KR5113470030 실측: 5페이지
            # 13열 → 6페이지 11열). 개수를 맞추라고 요구하지 말고, 각
            # 열의 x좌표로 앞 페이지 열에 대응시킨다.
            # 앞 페이지의 한 열에 이 페이지 열 두 개가 나란히 걸리는 일이
            # 흔하다(KR5118420036 실측: 앞 장 239.0에 232.3과 239.1이 둘 다
            # 8pt 안에 든다). 먼저 만난 쪽을 쓰면 값이 없는 칸을 총보수로
            # 잡아 모든 행이 버려지므로, 열마다 가장 가까운 하나만 쓴다.
            mapped = {}
            matched_cols = 0
            best = {}
            for ci, x in enumerate(col_x0s):
                cand = [k for k in range(len(prev_cols))
                        if abs(prev_cols[k] - x) <= 8]
                if not cand:
                    continue
                matched_cols += 1
                # 앞 장 열 두 개가 똑같이 가까울 때 그냥 최근접을 쓰면
                # 부동소수점 오차로 필드가 없는 쪽이 뽑혀 그 필드가 통째로
                # 사라진다(KR518101012M 실측: 5.400000000000006 대
                # 5.399999999999977 차이로 동종유형 열을 잃었다).
                # 필드가 붙어 있는 열을 먼저 본다.
                fielded = [k for k in cand if k in prev_fields]
                if not fielded:
                    continue
                near = min(fielded, key=lambda k: abs(prev_cols[k] - x))
                dist = abs(prev_cols[near] - x)
                if near not in best or dist < best[near][0]:
                    best[near] = (dist, ci)
            for near, (_, ci) in best.items():
                mapped[ci] = prev_fields[near]
            # 같은 표의 연속인지 엄격히 본다: 앞 페이지가 쓰던 값 칸이
            # 거의 다(80% 이상) 같은 x에 다시 나타나야 한다. 느슨하게 두면
            # 같은 페이지의 다른 표(운용전문인력 표 등)에 요약표 열 매핑이
            # 씌워져 "책임운용 정재환 1979..." 같은 행이 클래스로 잡힌다
            # (KR5122420005 실측 - 5행이어야 하는데 7행이 됐다).
            enough = matched_cols >= max(4, int(len(prev_fields) * 0.8))
            # x좌표가 겹치는 것만으로는 부족하다 - 같은 페이지의 "투자실적
            # 추이" 수익률표도 열 위치가 비슷해서 통과해 버린다(실측:
            # "비교지수(%)"와 "1981 8"이 클래스로 잡히고, 수익률 3.22가
            # 진짜 C의 총보수 0.45를 덮어썼다). 진짜 이어지는 표라면 앞
            # 장에서 쓰던 칸들이 한 행에 대부분 다시 채워져 있어야 한다.
            # 한 클래스의 값이 두 띠로 쪼개지는 표가 있어(KR5147430065
            # 실측: 이름 칸이 두 행에 걸쳐 있어 보수와 비용예시가 다른
            # 띠에 잡힌다) 비율을 높이면 진짜 연속 표까지 떨어진다.
            need = max(3, int(len(mapped) * 0.5))
            same_shape = any(sum(1 for ci in mapped if ci in r["cells"]) >= need
                             for r in grid[first_data:])
            if enough and same_shape and \
                    {"total_fee", "distribution_fee"} <= set(mapped.values()):
                field_by_col = mapped
                fields = set(field_by_col.values())
                inherited_need = need
        my_parts = [(col_x0s[ci], ps) for ci, ps in header_names.items()
                    if ci < len(col_x0s)]
        # 이름이 완성되지 않은 머리글이라도 조각은 남겨 둔다 - 머리글이
        # 페이지 경계에서 위아래로 잘린 문서에선 앞 장 조각이 있어야
        # 다음 장에서 이름이 완성된다(KR5152420028/KR5113450111 실측).
        # 페이지에 표가 여럿이면 요약표 머리글을 가진 표를 골라야 한다
        # (첫 표는 투자위험등급 표라 조각을 넘겨도 쓸모가 없다). 요약표
        # 열 이름으로 해석되는 칸이 가장 많은 표를 남긴다.
        score = len(map_headers(header_names))
        if my_parts and score and score > parts_score:
            parts_score = score
            parts_carry = ({}, col_x0s, my_parts)
        if not {"total_fee", "distribution_fee"} <= fields:
            continue
        # 이어받은 열 구성으로 확정된 뒤에도, 안 그려진 칸을 채우고 한 칸씩
        # 밀린 열을 다시 맞춘다. 헤더 페이지와 값 페이지의 격자 모양이 달라
        # 이어받은 매핑이 통째로 어긋나는 문서가 있다(KR5118420036 실측:
        # 총보수가 빈 칸을 가리켜 모든 행이 버려졌다).
        if inherited_need:
            realign(field_by_col)
        infer_commission(field_by_col)
        spread_commission(field_by_col)
        fill_missing(field_by_col)

        data_rows = grid[first_data:]
        # 클래스명은 값 행과 다른 y구간에 그려지는 경우가 많다(상세표와
        # 같은 문제) - 값 행과 세로로 겹치는 맨 왼쪽 열 글자를 라벨로 붙인다.
        # 클래스명 칸이 격자상 여러 열에 걸쳐 있는 문서가 있다(글자가
        # 좁은 칸에서 줄바꿈되며 쪼개짐 - KR5114420046 실측: 한 열만 보면
        # "라인-퇴"처럼 조각만 잡힌다). 첫 값 칸보다 왼쪽 전체를 이 클래스의
        # 이름 영역으로 본다.
        label_col = max(0, min(field_by_col) - 1) if field_by_col else 0
        first_field_x = col_x0s[min(field_by_col)] if field_by_col else 1e9
        label_ws = [w for w in words if (w["x0"] + w["x1"]) / 2 < first_field_x - 2]
        # 소수 2개 이상을 요구하면 판매보수·동종유형이 "-"인 클래스가
        # 통째로 빠진다(KR5194450018 실측: 랩 전용 W클래스는 소수가
        # 총보수 하나뿐이라 행 자체가 버려졌다). 정수(비용예시)까지
        # 숫자로 세되 개수를 올려 잡는다.
        real_rows = [r for r in data_rows
                     if sum(1 for v in r["cells"].values() if _isnum(v)) >= 3]
        # 열 구성을 앞 장에서 물려받은 경우엔 이 표가 정말 그 표의 연장인지
        # 행 단위로 다시 본다. 표 전체로만 보면 수익률표·운용전문인력표에도
        # 매핑이 씌워져 "비교지수(%)"나 "운용책임...전문인력"이 클래스로
        # 잡힌다(KR5118420006/KR5118420036 실측). 앞 장에서 쓰던 칸이
        # 대부분 다시 채워진 행만 남긴다.
        if inherited_need:
            real_rows = [r for r in real_rows
                         if sum(1 for ci in field_by_col if ci in r["cells"])
                         >= inherited_need]
        out = []
        for i, r in enumerate(real_rows):
            label = r["cells"].get(label_col, "")
            # 이미 코드가 읽히는 라벨은 넓히지 않는다 - 마지막 행에서
            # 표 끝까지 훑다가 각주("(주1) 1,000만원 지불하게 되는...")를
            # 끌어와 코드를 못 찾게 되는 사고가 있었다(KR5125450023 실측).
            if _label_class_code(label) is None:
                # 클래스명이 값 행보다 세로로 길게 이어지는 문서가 있다
                # (KR5113470030 실측: 값 행 구간만 보면 "수수료미 징구-오"
                # 처럼 잘려 클래스 코드를 못 찾는다). 이 행 시작부터 다음
                # 데이터 행 시작 직전까지를 이 클래스의 이름 구간으로 본다.
                # 마지막 행은 다음 행이 없으니 표 아래 끝까지 본다
                # (+40 같은 고정폭으로 자르면 이름이 길게 이어지는 문서에서
                # 여전히 잘린다 - KR5113470030 실측).
                hi = (real_rows[i + 1]["top"] if i + 1 < len(real_rows)
                      else max(c[3] for c in cells) + 1)
                ws = [w for w in label_ws
                      if r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 < hi - 1]
                if ws:
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    # 마지막 행은 아래로 표 끝까지 훑게 되는데, 그 아래에
                    # 각주와 다른 표(수익률표 등)가 이어지는 문서가 있다
                    # (KR5114420046 실측: "...퇴직연금(Cf) - 종류형
                    # 집합투자기구의 ... 퇴직연금(C) 비교지수(%) ..."까지
                    # 딸려와 코드가 Cf가 아니라 각주 속 C로 잡혔다).
                    # 각주 시작 표시를 만나면 거기서 끊는다.
                    # 클래스명 안에도 "-"가 별도 토큰으로 들어간다
                    # ("수수료미징 구 - 온 라 인-퇴직...") - 그래서 "-"를
                    # 무조건 경계로 보면 이름이 잘려 코드를 놓친다. 이미
                    # 코드를 찾은 뒤에 나오는 "-"부터 각주로 본다.
                    parts = []
                    for w in ws:
                        t = w["text"]
                        got = _label_class_code(" ".join(parts)) is not None
                        # 각주 번호는 "주)" 말고 "주1)" 꼴도 쓴다
                        # (KR5118420036 실측: 마지막 행 라벨이 주1)~주4)와
                        # 그 아래 수익률표까지 삼켜 C-P가 C로 잡혔다).
                        if got and (t == "-" or FOOTNOTE_RE.match(t)):
                            break
                        parts.append(t)
                    label = " ".join(parts).strip() or label
            if _label_class_code(label) is None:
                # 클래스명 칸 하나가 값 행 여러 개에 걸쳐 세로로 병합된
                # 표가 있다(KR5147430065 실측: 클래스 A의 보수가 운용전환일
                # 앞뒤로 두 행인데 이름 칸은 하나다 - 행 y구간만 보면
                # "수수료선취-"까지만 잡혀 코드를 못 찾았다). 이 행을
                # 세로로 품고 있는 왼쪽 칸의 글자를 이름으로 본다.
                mid = (r["top"] + r["bottom"]) / 2
                # 표 전체를 덮는 큰 칸("투자비용" 구획 칸 등)도 이 조건에
                # 걸리는데, 그걸 쓰면 모든 행이 같은 라벨(=클래스명 전부)을
                # 받아 마지막 코드 하나로 뭉개진다(KR5123490016 실측:
                # 4개 클래스가 Ce 하나가 됐다). 작은 칸부터 보고, 코드가
                # 읽히는 첫 칸에서 멈춘다.
                span = sorted(
                    (c for c in cells
                     if c[0] < first_field_x - 2 and c[1] - 1 <= mid <= c[3] + 1),
                    key=lambda c: (c[3] - c[1]) * (c[2] - c[0]))
                for c in span:
                    ws = [w for w in words
                          if c[0] - 1 <= (w["x0"] + w["x1"]) / 2 <= c[2] + 1
                          and c[1] - 1 <= (w["top"] + w["bottom"]) / 2 <= c[3] + 1]
                    if not ws:
                        continue
                    ws.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    cand = " ".join(w["text"] for w in ws).strip()
                    if _label_class_code(cand) is not None:
                        label = cand
                        break
            if _label_class_code(label) is None and i == len(real_rows) - 1 and next_page is not None:
                # 클래스명이 페이지 경계를 넘어가는 문서가 있다
                # (KR5116501001 실측: "수수료미징구- 온라인-"에서 페이지가
                # 끝나고 "퇴직연금(C-Pe)"가 다음 장 맨 위에 있다). 마지막
                # 행의 코드를 못 찾았을 때만 다음 페이지 첫머리를 이어본다.
                nws = next_page.extract_words(x_tolerance=2, keep_blank_chars=False)
                if nws:
                    top0 = min(w["top"] for w in nws)
                    head = [w for w in nws
                            if w["top"] <= top0 + 30
                            and (w["x0"] + w["x1"]) / 2 < first_field_x - 2]
                    head.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    if head:
                        cand = (label + " " + " ".join(w["text"] for w in head)).strip()
                        if _label_class_code(cand) is not None:
                            label = cand
            # 판매수수료 문구도 페이지 경계에서 끊긴다(KR514X450008 실측:
            # "납입금액의"에서 장이 끝나고 "0.5%이내"가 다음 장 맨 위에
            # 있다). 문구가 "...금액의"로 끝나면 아직 안 끝난 것이므로
            # 그 칸의 x범위로 다음 장 첫머리를 이어 읽는다.
            cm_col = next((c for c, f in field_by_col.items()
                           if f == "sales_commission_desc"), None)
            if (cm_col is not None and i == len(real_rows) - 1
                    and next_page is not None
                    and r["cells"].get(cm_col, "").replace(" ", "").endswith("금액의")):
                lo = col_x0s[cm_col]
                hi = (col_x0s[cm_col + 1] if cm_col + 1 < len(col_x0s) else table_x1)
                nws = next_page.extract_words(x_tolerance=2, keep_blank_chars=False)
                if nws:
                    top0 = min(w["top"] for w in nws)
                    add = [w for w in nws
                           if w["top"] <= top0 + 20
                           and lo - 1 <= (w["x0"] + w["x1"]) / 2 < hi]
                    add.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
                    if add:
                        r["cells"][cm_col] = (r["cells"][cm_col] + " "
                                              + " ".join(w["text"] for w in add))
            # 라벨이 각주까지 삼킨 채로 넘어오는 경우가 있다(격자상 이름
            # 칸이 각주를 포함하는 큰 칸일 때). 그대로 두면 각주 속 다른
            # 클래스 표기가 뒤에 있어 코드를 그쪽으로 뺏긴다
            # (KR5152420028 실측: Ce가 각주 속 "종류 C형"에 밀려 C가 됐다).
            # 코드를 이미 찾은 뒤에 나오는 각주 표시부터 잘라낸다.
            parts = []
            for t in label.split():
                if parts and _label_class_code(" ".join(parts)) is not None \
                        and (t == "-" or FOOTNOTE_RE.match(t)):
                    break
                parts.append(t)
            label = " ".join(parts).strip() or label
            out.append({"label": label, "cells": r["cells"]})
        if out:
            return field_by_col, out, col_x0s, my_parts
        if header_only and header_carry is None and \
                {"total_fee", "distribution_fee"} <= set(field_by_col.values()):
            header_carry = (field_by_col, col_x0s, my_parts)
    if header_carry:
        return header_carry[0], [], header_carry[1], header_carry[2]
    if parts_carry:
        return parts_carry[0], [], parts_carry[1], parts_carry[2]
    return None


SUMMARY_COST_KEYS = ["1y", "2y", "3y", "5y", "10y"]


def summary_rows_for_doc(doc_id, pdf, pages):
    """요약표를 셀 격자로 읽어 class_fees 레코드로 만든다(좌표 방식
    find_fee_rows_on_page의 대체). 페이지마다 표를 찾고, 헤더 이름으로
    잡은 열 매핑에 따라 값을 담는다."""
    rows = []
    inherited = None
    prev_page = None
    as_of = None
    for page_num in pages:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        nxt = pdf.pages[page_num] if page_num < len(pdf.pages) else None
        # 열 구성 이어받기는 "바로 다음 페이지"에서만 허용한다 - 떨어진
        # 페이지의 무관한 표에까지 요약표 열 매핑을 씌우면 엉뚱한 행이
        # 클래스로 잡힌다(KR5122420005 실측: 5행이어야 하는데 7행이 됨).
        use_inherited = inherited if prev_page == page_num - 1 else None
        page_text = pdf.pages[page_num - 1].extract_text() or ""
        # 작성기준일은 보통 첫 요약 페이지에만 찍힌다 - 문서 안에서 한 번
        # 찾으면 이어지는 페이지의 행에도 같이 붙인다.
        as_of = _page_as_of(page_text) or as_of
        got = _summary_grid(pdf.pages[page_num - 1], nxt, use_inherited)
        if not got:
            continue
        field_by_col, grid_rows, col_x0s, hdr_parts = got
        inherited = (field_by_col, col_x0s, hdr_parts)
        prev_page = page_num
        for r in grid_rows:
            label = r["label"]
            flat = label.replace(" ", "")
            code = _label_class_code(label)
            if code is None:
                # 클래스가 하나뿐이라 코드 표기 자체가 없는 문서가 있다
                # (KR5123365001 실측: "투자신탁" 라벨 하나) - 코드를 지어
                # 내지 않고 원본 라벨을 그대로 이름으로 쓴다.
                # 각주까지 딸려온 라벨을 그대로 이름으로 쓰면
                # "투자신탁투자비용주1)'1,000만원..."이 된다
                # (KR5123365001 실측). 각주 표시 앞까지만 쓴다.
                cut = re.split(r"\(?주\s*\d*\)", label)[0].replace(" ", "")
                # 표 왼쪽의 구획 이름("투자비용")까지 라벨에 딸려온다
                for sec in ("투자비용", "분류", "투자목적및투자전략"):
                    cut = cut.replace(sec, "")
                code = cut[:20] or flat or None
            if not code:
                continue

            vals = {}
            for ci, v in r["cells"].items():
                f = field_by_col.get(ci)
                if f:
                    vals[f] = v.strip()

            total_fee = vals.get("total_fee")
            if total_fee is None:
                continue
            # 같은 페이지의 수익률표·운용전문인력표가 요약표의 연장으로
            # 잘못 잡히면 "비교지수(%)"나 운용역 이름이 클래스가 된다
            # (KR5118420062/KR5118201004 실측). 클래스 이름에는 절대
            # 나오지 않는 말이 들어간 행은 버린다.
            if any(k in flat for k in ("비교지수", "수익률변동성", "투자실적",
                                       "설정일이후", "운용전문", "연평균")):
                continue
            # 보수율은 %라서 두 자릿수를 넘지 않는다. 운용규모(96,365)나
            # 운용역 경력(8) 같은 숫자가 들어오면 그 행은 보수 행이 아니다.
            def _pct_ok(v):
                if v is None:
                    return True
                t = v.replace(" ", "").rstrip("%")
                if t in DASHES:
                    return True
                try:
                    if float(t.replace(",", "")) <= 10:
                        return True
                except ValueError:
                    return False
                # 소수점 자리에 쉼표를 찍은 원본 오타(1,807 = 1.807)는
                # 아래 fix_comma_decimal이 되돌리므로 여기서 버리면 안 된다
                mm = re.fullmatch(r"(\d),(\d{3})", t)
                return bool(mm) and float(f"{mm.group(1)}.{mm.group(2)}") <= 10

            if not all(_pct_ok(vals.get(f)) for f in
                       ("total_fee", "distribution_fee", "peer_avg_fee",
                        "total_fee_and_cost")):
                continue
            cost = {}
            prev_cost = None
            for k in SUMMARY_COST_KEYS:
                v = vals.get(f"cost_{k}")
                if not v:
                    continue
                picked = _pick_cost_number(v, prev_cost)
                # 비용예시는 천원 단위 정수다. 소수가 들어오면 그 칸은
                # 비용예시가 아니라 다른 표(수익률)의 값이다
                # (KR5118201004 실측: 수익률 4.88이 1년 비용예시 자리에
                # 들어와 그 행이 보수 행처럼 보였다).
                if not picked.replace(",", "").isdigit():
                    continue
                cost[k] = picked
                prev_cost = picked

            # 같은 클래스의 "투자실적 추이" 행이 보수 행으로 잡히는 일이
            # 있다(KR5118201004 실측: "수수료미징구-오프라인(A)(%)
            # 2005.04.27 3.52 4.64 4.88 2.88" - 수익률인데 총보수·동종유형
            # 자리에 들어갔다). 보수표 행에는 판매보수든 총보수·비용이든
            # 비용예시든 하나는 반드시 있는데 수익률 행에는 없다.
            if (vals.get("distribution_fee") is None
                    and vals.get("total_fee_and_cost") is None
                    and not cost):
                continue

            raw_comm = vals.get("sales_commission_desc")
            if raw_comm is None:
                desc = None
            elif raw_comm.replace(" ", "") in ("없음", "-"):
                desc = "-"
            else:
                desc = _ga_commission_desc(raw_comm) or None

            def clean(f):
                v = vals.get(f)
                if v is None:
                    return None
                v = v.replace(" ", "").rstrip("%")
                return v or None

            # 원본이 소수점 자리에 쉼표를 찍은 문서가 있다(KR5169950018
            # 실측: 총보수·비용이 "1,807"로 찍혀 있는데 총보수 1.805 +
            # 기타비용이라 1.807이 맞다 - 조판 오타). 셀에서 그대로 읽으면
            # 1807이 돼 값이 1000배가 된다. 총보수·비용은 총보수보다
            # 아주 조금 큰 값이라는 성질로 안전하게 되돌린다(다른 자릿수
            # 조합엔 손대지 않는다).
            corrections = []

            def fix_comma_decimal(v, ref):
                if v is None or ref is None:
                    return v
                mm = re.fullmatch(r"(\d)[,](\d{3})", v)
                if not mm:
                    return v
                try:
                    cand = float(f"{mm.group(1)}.{mm.group(2)}")
                    r = float(ref)
                except ValueError:
                    return v
                if r <= cand <= r + 0.5:
                    fixed = f"{mm.group(1)}.{mm.group(2)}"
                    # 보정했다는 사실과 원문 표기를 남긴다. 운영진이 "원문
                    # 정오 확인은 제공하지 않고 해석은 팀의 설계 판단"이라고
                    # 밝혔으므로, 값은 계산 가능한 형태로 두되 근거(원문이
                    # 실제로 어떻게 찍혀 있었는지)를 잃지 않게 한다 -
                    # 답변할 때 "원문은 X로 표기, 오타로 판단해 Y로 봄"을
                    # 밝힐 수 있어야 채점의 정확성/근거 기준을 둘 다 만족한다.
                    corrections.append({
                        "field": "total_fee_and_cost",
                        "raw": v, "used": fixed,
                        "reason": "소수점이 쉼표로 표기된 것으로 판단"
                                  f"(같은 행 총보수 {ref} 대비)",
                    })
                    return fixed
                return v

            # 보수 적용 기간이 나뉜 표가 있다(KR5147430065 실측: 클래스
            # A가 "최초설정일~운용전환일 전일"과 "운용전환일~해지일" 두
            # 행). 어느 기간의 값인지 알아야 답변할 때 고를 수 있으므로
            # 그 문구를 그대로 남긴다.
            period = None
            for v in r["cells"].values():
                flat_v = v.replace(" ", "")
                if "전환일" in flat_v and ("까지" in flat_v or "부터" in flat_v):
                    # 칸 안에서 줄바꿈되며 단어가 쪼개진다("최초설정일부"
                    # / "터" / "운용전환일"). 한 글자짜리 조각은 앞말에
                    # 붙여 읽는다 - 원문 띄어쓰기를 살리면서 복원된다.
                    toks = v.split()
                    parts = []
                    for tk in toks:
                        if parts and len(tk) == 1:
                            parts[-1] += tk
                        else:
                            parts.append(tk)
                    period = " ".join(parts)
                    break
            # 기간이 나뉜 상품은 "언제 바뀌는지"가 답변의 핵심인데, 그게
            # 날짜가 아니라 조건인 경우가 있다(KR5147430065 실측: 전환일이
            # 문서 57쪽 어디에도 날짜로 없고 "목표기준가격 도달 시"라는
            # 조건뿐이다). 조건 문장과 작성기준일을 남겨야 "지금 전환됐나요"
            # 같은 질문에 "작성기준일 기준으로는 전환 전이고, 조건은 이것"
            # 이라고 근거를 들어 답할 수 있다.

            rows.append({
                "class_code": code,
                **({"fee_period": period} if period else {}),
                **({"as_of": as_of} if as_of else {}),
                "sales_commission_desc": desc,
                "total_fee": total_fee.replace(" ", "").rstrip("%"),
                "distribution_fee": clean("distribution_fee"),
                "peer_avg_fee": clean("peer_avg_fee"),
                "total_fee_and_cost": fix_comma_decimal(
                    clean("total_fee_and_cost"),
                    total_fee.replace(" ", "").rstrip("%")),
                "cost_projection_per_10m": cost,
                "page": page_num,
                "evidence": f"클래스명: {label} | {raw_comm or '-'} "
                            + " ".join(v for _, v in sorted(r["cells"].items())),
                "method": "cell_grid",
                "confidence": 1.0,
                "product_code": doc_id,
                **({"source_corrections": corrections} if corrections else {}),
            })
    # 한 클래스의 보수가 기간별로 두 행에 나뉜 문서가 있다(KR5147430065
    # 실측: 클래스 A가 "최초설정일~운용전환일 전일"과 "운용전환일~해지일"
    # 두 행이고 이름 칸은 하나로 병합돼 있다). 클래스당 한 레코드를 내되
    # 나머지 기간을 버리지는 않고 additional_fee_rows로 남긴다.
    # 같은 클래스가 두 띠로 쪼개져 한쪽엔 판매수수료·비용예시가, 다른
    # 쪽엔 보수 값이 담기기도 한다(격자상 이름 칸이 두 행에 걸쳐서다).
    # 그래서 뒤 행을 그냥 버리면 안 되고, 비어 있는 칸을 채운 뒤 값이
    # 실제로 다른 것만 additional_fee_rows에 남긴다.
    fee_fields = ("total_fee", "distribution_fee", "peer_avg_fee",
                  "total_fee_and_cost", "sales_commission_desc")
    out, seen = [], {}
    for r in rows:
        code = r["class_code"]
        if code not in seen:
            seen[code] = r
            out.append(r)
            continue
        keep, extra = seen[code], {}
        # "운용전환일부터 ..." 행은 같은 클래스의 전환 후 보수다. 이
        # 파일엔 이미 그 규격(*_after_conversion + conversion_trigger_
        # nav_price)이 있으므로 거기에 맞춘다 - 좌표 방식에만 있던 걸
        # 셀 방식으로 옮기면서 빠졌던 부분이다.
        if "운용전환일부터" in (r.get("fee_period") or "").replace(" ", ""):
            for f, tgt in (("total_fee", "total_fee_after_conversion"),
                           ("distribution_fee", "distribution_fee_after_conversion"),
                           ("peer_avg_fee", "peer_avg_fee_after_conversion"),
                           ("total_fee_and_cost", "total_fee_and_cost_after_conversion")):
                if r.get(f) is not None:
                    keep.setdefault(tgt, r[f])
            continue
        for y, cv in (r.get("cost_projection_per_10m") or {}).items():
            keep.setdefault("cost_projection_per_10m", {}).setdefault(y, cv)
        for f in fee_fields:
            v = r.get(f)
            if v is None:
                continue
            if keep.get(f) is None:
                keep[f] = v
            elif keep[f] != v:
                extra[f] = v
        if extra:
            extra["evidence"] = r.get("evidence")
            if r.get("fee_period"):
                extra["fee_period"] = r["fee_period"]
            keep.setdefault("additional_fee_rows", []).append(extra)
    return out


def _summary_cells_lose_anything(coord_rows, cell_rows):
    """셀 결과가 좌표 결과에 비해 무엇이든 잃었는지 본다(클래스든 개별
    필드든). 처음엔 클래스 목록만 비교했는데, 클래스는 다 나오면서 특정
    칸만 비는 경우를 못 걸렀다 - 헤더 이름 변형을 못 알아본 문서에서
    총보수·비용/판매보수/동종유형총보수가 통째로 None이 되거나, "1년"
    비용예시만 빠지는 일이 실제로 있었다(실측 137건). 6축 값이 조용히
    사라지는 게 가장 나쁘므로, 하나라도 잃으면 좌표 결과를 쓴다."""
    cell = {r["class_code"]: r for r in cell_rows if r.get("class_code")}
    for c in coord_rows:
        code = c.get("class_code")
        if not code:
            continue
        # 좌표 방식이 만든 쓰레기 행(class_code가 "-"처럼 글자·숫자가
        # 하나도 없는 것)은 재현 대상이 아니다 - 이걸 "잃었다"고 보면
        # 멀쩡한 문서가 폴백된다(KR5116501001 실측).
        if not any(ch.isalnum() for ch in code):
            continue
        # 좌표 방식이 수수료 설명 문구를 클래스명으로 잘못 읽은 행도
        # 마찬가지다(KR514X450008 실측: "수수료선취 - 납입금액의"가
        # 클래스로 잡혀 있다). 클래스명에 들어갈 수 없는 말이다.
        if any(k in code for k in ("납입금액", "환매금액", "수수료", "이내")):
            continue
        n = cell.get(code)
        if n is None:
            return True
        # 판매수수료 문구는 셀 쪽이 더 정확한 경우가 있어(좌표 방식이
        # 옆 칸을 잘못 읽어 "-"로 넣은 사례 실측) 값이 달라지는 것 자체는
        # 허용하고, 있던 게 사라지는 것만 막는다.
        if c.get("sales_commission_desc") is not None and n.get("sales_commission_desc") is None:
            return True
        # 숫자 4개는 이 표에서 그대로 재현돼야 한다 - 값이 달라지면
        # 어느 쪽이 맞는지 여기서 알 수 없으므로 안전하게 좌표 결과를
        # 쓴다(실측: 소수점이 쉼표로 찍힌 문서에서 좌표 방식은 보정을
        # 했는데 셀은 원문 "1,807"을 그대로 읽었다 - KR5169950018).
        for f in ("total_fee", "distribution_fee", "peer_avg_fee",
                  "total_fee_and_cost"):
            ov, nv = c.get(f), n.get(f)
            if ov is None:
                continue
            if nv is None or str(ov).replace(",", "") != str(nv).replace(",", ""):
                return True
        oc = c.get("cost_projection_per_10m") or {}
        nc = n.get("cost_projection_per_10m") or {}
        if set(oc) - set(nc):
            return True
        # 값 자체가 달라진 경우도 잃은 것으로 본다(쉼표 정규화는 제외 -
        # "1,041"과 "1041"은 같은 값이다).
        for k, v in oc.items():
            if str(v).replace(",", "") != str(nc.get(k, "")).replace(",", ""):
                return True
    return False


_SUMMARY_FALLBACK_DOCS = set()


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        pages = candidate_pages_for_doc(doc_id, len(pdf.pages))
        if not pages:
            return []

        valid_pages = [(p, pdf.pages[p - 1]) for p in pages if 1 <= p <= len(pdf.pages)]
        page_words_lines = {
            p: (page.extract_words(x_tolerance=2, keep_blank_chars=False), None)
            for p, page in valid_pages
        }
        for p in page_words_lines:
            w = page_words_lines[p][0]
            page_words_lines[p] = (w, cluster_lines(w))

        # 표가 여러 페이지에 걸쳐 있을 때, 이어지는 페이지에는 헤더가
        # 반복되지 않는 경우가 있다(KR5118420006 실측: 4페이지 헤더엔
        # "총보수ㆍ비용"이 있는데 이어지는 5페이지엔 헤더 없이 데이터 행만
        # 있음 - 페이지 단위로 다시 판별하면 5페이지 행만 이 칸이 없다고
        # 잘못 판단함). 같은 표는 모든 페이지에서 구조가 같으므로, 문서
        # 전체(모든 후보 페이지)에서 한 번이라도 헤더가 보이면 True로 본다.
        has_cost_column = any(
            page_has_cost_column_header(w, l) for w, l in page_words_lines.values()
        )
        # 비용예시가 3개년뿐인 문서(위 page_cost_projection_years 참고)도
        # 같은 이유로 문서 전체 후보 페이지를 같이 본다. 문서 안에 5개년
        # 표(정상)와 3개년 표(운용전환일 전/후 등)가 섞여 있을 수 있어,
        # 5개년이 하나라도 보이면 그쪽을 우선한다(더 안전한 기본값).
        detected_years = [page_cost_projection_years(w, l) for w, l in page_words_lines.values()]
        if any(y == ["1y", "2y", "3y", "5y", "10y"] for y in detected_years):
            doc_cost_years = ["1y", "2y", "3y", "5y", "10y"]
        elif any(y == ["1y", "2y", "3y"] for y in detected_years):
            doc_cost_years = ["1y", "2y", "3y"]
        else:
            doc_cost_years = None

        for page_num, page in valid_pages:
            next_page_lines = page_words_lines.get(page_num + 1)
            if next_page_lines:
                # 클래스명 닫는 괄호 조각이 다음 페이지 맨 앞 1줄이 아니라
                # 2줄째에 걸치는 경우도 있다(KR5113470030 실측: "프라인"/
                # "(C)"가 다음 페이지 첫 두 줄에 나뉘어 있음) - 다른 클래스의
                # 완전한 데이터 행을 만나기 전까지만 최대 3줄을 후보로 준다.
                head = []
                for hl in next_page_lines[1][:3]:
                    if sum(1 for w in hl if DECIMAL_RE.match(w["text"])) >= 3:
                        break
                    head.append(hl)
                next_page_head_lines = head
            else:
                next_page_head_lines = None
            rows = find_fee_rows_on_page(
                page, page_num, has_cost_column, next_page_head_lines, doc_cost_years
            )
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

        # 요약표를 셀 격자로도 읽어 대조한다. 셀 방식이 성공하면 그쪽을
        # 쓴다 - 좌표 방식은 "이 단어가 어느 칸인지"를 매번 추측해야 해서
        # 문서별 예외가 계속 붙었고, 실제로 옆 칸 값을 잘못 가져오는 오류도
        # 있었다(다른 표에서 실측). 셀 경계는 PDF가 직접 알려주는 정보라
        # 그런 착각이 안 생긴다. 셀 격자를 못 얻은 문서(표 테두리가 없는
        # 등)에서만 좌표 결과를 그대로 쓴다.
        # 셀 결과가 좌표 결과의 클래스를 하나도 잃지 않을 때만 채택한다.
        # 대부분 문서에선 셀 쪽이 같거나 더 완전하지만, 구조가 특수한
        # 문서에선 셀 격자가 어긋난다 - 실측 두 가지:
        #   - 한 클래스가 운용전환일 전/후 두 행으로 나뉜 표에서 두 행을
        #     서로 다른 클래스로 오인(KR5147430065)
        #   - 같은 페이지의 수익률표/운용전문인력표가 같은 격자에 섞임
        #     (KR5123365001)
        # 이런 문서는 조용히 값이 틀리는 게 가장 나쁘므로, 손실이 감지되면
        # 좌표 결과를 그대로 둔다(폴백 문서 수는 main()에서 세어 출력한다).
        cell_rows = summary_rows_for_doc(doc_id, pdf, pages)
        if cell_rows and not _summary_cells_lose_anything(results, cell_rows):
            results = cell_rows
        else:
            _SUMMARY_FALLBACK_DOCS.add(doc_id)

    # 표가 여러 페이지에 걸쳐 있어서 다음 페이지도 후보로 넣다 보니, 같은
    # 클래스가 두 페이지 모두에서 뽑힐 수 있다 (예: 클래스 헤더 페이지의
    # 마지막 줄이 다음 페이지 처음 줄과 겹쳐 인식되는 경우). class_code가
    # 있는 것끼리는 (product_code, class_code) 기준으로 중복 제거하고,
    # confidence가 더 높은(=class_code를 더 명확히 찾은) 쪽을 남긴다.
    dedup = {}
    unlabeled = []
    for r in results:
        if r["class_code"] is None:
            unlabeled.append(r)
            continue
        key = r["class_code"]
        if key not in dedup or r["confidence"] > dedup[key]["confidence"]:
            dedup[key] = r
    final_rows = list(dedup.values()) + unlabeled
    # total_fee_after_conversion이 있는 행에만 conversion_trigger_nav_price
    # 키를 붙인다(없는 행은 키 자체를 안 만듦 - 위 참고). 이 펀드 자신의
    # 기준가격이 이 값(원) 이상이 되면 운용전환이 일어난다는 뜻 - 고정
    # 날짜가 아니다.
    if any("total_fee_after_conversion" in r for r in final_rows):
        nav_price = conversion_trigger_nav_price(doc_id)
        for r in final_rows:
            if "total_fee_after_conversion" in r:
                r["conversion_trigger_nav_price"] = nav_price
    return final_rows


# 비용예시표가 _detail_cost_grids와 전혀 다른 모양으로 나오는 문서가
# 있다(KR5172450019 실측: "구분 1년후 2년후 3년후 5년후 10년후" 한 줄
# 헤더 + 클래스마다 "라벨(코드) 값1 값2 값3 값4 값5" 딱 한 줄 - 값은
# 이미 천원 단위). _detail_cost_grids가 다루는 표(칸이 병합돼 여러 줄에
# 걸치고, 헤더도 "OO년후" 글자가 세로로 쪼개지는 등 훨씬 복잡한 모양)와
# 근본적으로 다른 레이아웃이라 억지로 한 함수에 같이 넣으면 기존
# 1,172개 검증된 클래스를 건드릴 위험이 커진다 - 그래서 완전히 별도
# 함수로 짜고, fill_detail_cost_projections이 이미 채운 뒤에도 여전히
# 빈 클래스에만 후처리로 적용한다(마감 직전 위험 관리 원칙 - 기존 파서
# 보존, 누락분만 새 방식으로 보강).
def _detail_cost_row_table(pdf):
    """"구분|1년후|2년후|3년후|5년후|10년후" 한 줄 표를 좌표로 읽는다.
    돌려주는 값: [(page_num, {class_code: {"1y":..,...}}), ...]"""
    out = []
    col_x = None  # {"1y": x0, ...} - 이 페이지 또는 앞 페이지에서 확인된 열 위치
    header_top = None
    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        lines = cluster_lines(words, tol=2.5)
        this_page_header = None
        for ln in lines:
            year_cols = {}
            for w in ln:
                m = COST_AFTER_RE.match(w["text"].replace(" ", ""))
                if m and m.group(1) in ("1", "2", "3", "5", "10"):
                    year_cols[f"{m.group(1)}y"] = w["x0"]
            if len(year_cols) >= 4 and any(w["text"] == "구분" for w in ln):
                this_page_header = (ln[0]["top"], year_cols)
                break
        if this_page_header:
            header_top, col_x = this_page_header
        elif col_x is None:
            continue  # 이 문서엔 이 표 자체가 없다(대부분의 문서)
        else:
            # 이 페이지엔 헤더가 없다 - 앞 페이지에서 이어지는 데이터만
            # 있는 페이지다(KR5172450019 28쪽 실측). 열 위치를 그대로
            # 물려 쓴다. 표가 끝나고 완전히 다른 내용(예: 다음 절)으로
            # 넘어간 페이지까지 잘못 이어 읽지 않도록, 값 5개가 실제로
            # 이 열 위치에 있는 줄이 하나도 없으면 건너뛴다(아래 루프가
            # 자연히 그렇게 걸러준다).
            header_top = -1

        by_code = {}
        prev_label = None
        for ln in lines:
            if ln[0]["top"] <= header_top:
                continue
            vals = {}
            for w in ln:
                t = w["text"].replace(",", "")
                if not (t.lstrip("-").isdigit() and t not in ("-",)):
                    continue
                near = min(col_x, key=lambda k: abs(col_x[k] - w["x0"]))
                if abs(col_x[near] - w["x0"]) <= 15:
                    vals[near] = t
            if len(vals) >= 4:
                label = " ".join(w["text"] for w in ln
                                  if w["text"].replace(",", "") not in vals.values())
                code = _label_class_code(label)
                # 라벨이 값 줄과 다음 줄로 쪼개지는 문서가 있다
                # (KR5172450019 실측: "...개인연금" 값 줄 다음, "(S-P)"
                # 코드만 있는 줄이 따로 있다) - 못 찾으면 이 값 줄을
                # prev_label로 남겨 뒀다가, 바로 다음 줄이 값 없이
                # 코드만 담고 있으면(아래 elif) 그때 이어붙여 다시 찾는다.
                if code:
                    by_code.setdefault(code, vals)
                    prev_label = None
                else:
                    prev_label = (label, vals)
            elif prev_label is not None and not vals:
                # 코드만 담긴 다음 줄(값 없음)을 직전 값 줄의 라벨에 이어
                # 붙여 다시 시도한다.
                label2 = prev_label[0] + " " + " ".join(w["text"] for w in ln)
                code = _label_class_code(label2)
                if code:
                    by_code.setdefault(code, prev_label[1])
                prev_label = None
        if by_code:
            out.append((page_num, by_code))
    return out


def fill_detail_cost_row_table(doc_id, rows):
    """_detail_cost_row_table로 찾은 값을, fill_detail_cost_projections이
    이미 채운 뒤에도 여전히 비용예시가 빈 클래스에만 채운다. 요약표에도
    있는 클래스로 먼저 대조해서 어긋나면(다른 표를 잘못 읽은 것) 이
    문서는 통째로 건드리지 않는다 - fill_detail_cost_projections과
    같은 안전장치."""
    if all(r.get("cost_projection_per_10m") for r in rows):
        return 0
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return 0
    with pdfplumber.open(pdfs[0]) as pdf:
        grids = _detail_cost_row_table(pdf)
    if not grids:
        return 0
    by_code = {}
    for _, m in grids:
        for code, vals in m.items():
            if code not in by_code or len(vals) > len(by_code[code]):
                by_code[code] = vals

    for r in rows:
        cur = r.get("cost_projection_per_10m") or {}
        cand = by_code.get(r.get("class_code"))
        if not cur or not cand:
            continue
        for y, v in cur.items():
            if y in cand and str(v).replace(",", "") != cand[y]:
                return 0

    filled = 0
    for r in rows:
        if r.get("cost_projection_per_10m"):
            continue
        cand = by_code.get(r.get("class_code"))
        if not cand:
            continue
        r["cost_projection_per_10m"] = dict(cand)
        r.setdefault("field_source_pages", {})["cost_projection_per_10m"] = next(
            pn for pn, m in grids if r["class_code"] in m)
        pages = r.setdefault("source_pages", [r["page"]])
        pg = r["field_source_pages"]["cost_projection_per_10m"]
        if pg not in pages:
            pages.append(pg)
        filled += 1
    return filled


# _detail_cost_grids가 다루는 표와 사실상 같은 모양(라벨+캡션+값 5개가
# 한 줄)인데, 연도 머리글만 "1/2/3/5/10"(숫자만)이 한 줄, "년후"가
# 다음 줄로 갈라진 문서가 있다(KR5144420091 실측). _detail_cost_grids는
# "숫자 칸이 3개 이상 나오면 본문 시작"으로 판정해서 이 헤더 줄 자체를
# (연도 낱말이 채 완성되기도 전에) 본문으로 오인해 표 전체를 놓친다.
# 이 판정 로직을 고치면 이미 검증된 1,172개 클래스 전부가 다시 걸릴
# 위험이 있으므로 손대지 않고, 셀(테두리) 구조로 별도로 다시 읽는다 -
# 여기서 이미 다 채워진 문서는 애초에 아무 표도 안 걸리므로(아래
# 헤더 판정 자체가 이 특정 모양에서만 성립) 다른 문서에 영향이 없다.
def _detail_cost_grid2(pdf):
    out = []
    # 표가 페이지 경계에서 갈리면 이어지는 쪽엔 머리글이 안 찍힌다
    # (KR5144420091 실측: 35쪽엔 A/A2/Ae/AG만 있고 나머지 클래스는
    # 헤더 없이 36쪽에 이어진다). 값 칸 개수가 확인된 연도 칸 수와
    # 정확히 같을 때만 받으므로(아래), 바로 다음 페이지에 한해서만
    # 직전에 확인된 연도 순서를 그대로 물려 써도 안전하다.
    carry_years, carry_page = None, None
    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        for t in page.find_tables():
            if len(t.cells) < 8:
                continue
            rows_ = t.extract()
            if not rows_:
                continue
            year_cols = {}
            if len(rows_) >= 2:
                for ci in range(len(rows_[0])):
                    v0 = (rows_[0][ci] or "").strip()
                    v1 = (rows_[1][ci] or "").strip()
                    if v0 in ("1", "2", "3", "5", "10") and v1.replace(" ", "") in ("년후", "년"):
                        year_cols[ci] = f"{v0}y"
            if len(year_cols) >= 4:
                year_order = [y for _, y in sorted(year_cols.items())]
                data_rows = rows_[2:]
            elif carry_years and carry_page == page_num - 1:
                year_order = carry_years
                data_rows = rows_
            else:
                continue
            # 값 칸의 셀 경계가 머리글 칸과 한 칸씩 어긋나는 문서가
            # 있다(KR5144420091 실측: 머리글 "1"은 3번 칸인데 값
            # "41"은 2번 칸 - 클래스 라벨 칸이 캡션("판매수수료 및
            # 보수·비용")까지 포함해 머리글의 "클래스(종류)+투자기간"
            # 두 칸과 폭이 안 맞는다). 열 번호로 맞추지 않고, 라벨
            # 다음에 나오는 숫자 칸을 왼쪽부터 순서대로 연도 순서
            # (1/2/3/5/10년후, 오름차순)에 맞춘다 - 개수가 정확히
            # 연도 칸 수만큼일 때만 받아들인다(그래야 캡션 등 다른
            # 칸이 숫자로 오인돼 섞여 들어올 위험이 없다).
            by_code = {}
            for row in data_rows:
                label = (row[0] or "").strip() if row else ""
                if not label:
                    continue
                code = _label_class_code(label)
                if not code:
                    continue
                packed = [(c or "").replace(",", "").strip() for c in row[1:]]
                packed = [c for c in packed if c.lstrip("-").isdigit()]
                if len(packed) != len(year_order):
                    continue
                by_code.setdefault(code, dict(zip(year_order, packed)))
            if by_code:
                out.append((page_num, by_code))
                carry_years, carry_page = year_order, page_num
    return out


def fill_detail_cost_grid2(doc_id, rows):
    """_detail_cost_grid2로 찾은 값을, 다른 폴백들이 이미 채운 뒤에도
    여전히 비용예시가 빈 클래스에만 채운다. 같은 안전장치(요약표와
    대조, 어긋나면 문서 통째로 안 건드림)."""
    if all(r.get("cost_projection_per_10m") for r in rows):
        return 0
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return 0
    with pdfplumber.open(pdfs[0]) as pdf:
        grids = _detail_cost_grid2(pdf)
    if not grids:
        return 0
    by_code = {}
    for _, m in grids:
        for code, vals in m.items():
            if code not in by_code or len(vals) > len(by_code[code]):
                by_code[code] = vals

    for r in rows:
        cur = r.get("cost_projection_per_10m") or {}
        cand = by_code.get(r.get("class_code"))
        if not cur or not cand:
            continue
        for y, v in cur.items():
            if y in cand and str(v).replace(",", "") != cand[y]:
                return 0

    filled = 0
    for r in rows:
        if r.get("cost_projection_per_10m"):
            continue
        cand = by_code.get(r.get("class_code"))
        if not cand:
            continue
        r["cost_projection_per_10m"] = dict(cand)
        r.setdefault("field_source_pages", {})["cost_projection_per_10m"] = next(
            pn for pn, m in grids if r["class_code"] in m)
        pages = r.setdefault("source_pages", [r["page"]])
        pg = r["field_source_pages"]["cost_projection_per_10m"]
        if pg not in pages:
            pages.append(pg)
        filled += 1
    return filled


# 네 번째 모양: 비용예시 표 제목에 "(단위 : 원)"이라고 그대로 적혀
# 있어 값이 천원이 아니라 원 단위 그대로 찍히고("25,468"), 클래스
# 라벨이 표 테두리 밖(find_tables가 못 잡는 자리)에 있으며, 헤더의
# "1년"조차 "1"과 "년"이 같은 줄의 다른 낱말로 떨어지는 문서가 있다
# (KR5152420028 실측). 셀이 아니라 낱말 좌표로 직접 읽는다(이미
# "표(구분/1년후.../클래스 한 줄)" 모양을 좌표로 읽는 _detail_cost_
# row_table과 원리는 같지만, 그쪽은 "구분" 토큰이 한 낱말로 붙어
# 있다고 가정해서 이 문서("구"+" "+"분")는 못 잡는다).
RE_WON_UNIT_TITLE = re.compile(r"단위\s*[:：]?\s*원\)")


def _detail_cost_coord_won(pdf):
    out = []
    col_x = None
    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        lines = cluster_lines(words, tol=2.5)
        if RE_WON_UNIT_TITLE.search(page.extract_text() or ""):
            for ln in lines:
                year_cols = {}
                for idx, w in enumerate(ln):
                    if w["text"].isdigit() and idx + 1 < len(ln) and ln[idx + 1]["text"] == "년":
                        n = w["text"]
                    elif COST_AFTER_RE.match(w["text"]):
                        n = COST_AFTER_RE.match(w["text"]).group(1)
                    else:
                        continue
                    if n in ("1", "2", "3", "5", "10"):
                        year_cols[f"{n}y"] = w["x0"]
                if len(year_cols) >= 4:
                    col_x = year_cols
                    break
        if not col_x:
            continue

        min_col_x = min(col_x.values())
        by_code = {}
        for li, ln in enumerate(lines):
            vals = {}
            for w in ln:
                t = w["text"].replace(",", "")
                if not t.isdigit():
                    continue
                near = min(col_x, key=lambda k: abs(col_x[k] - w["x0"]))
                if abs(col_x[near] - w["x0"]) <= 15:
                    # 원 단위로 찍힌 값을 다른 문서들과 같은 천원
                    # 단위로 맞춘다(반올림) - 이 함수는 표 제목에
                    # "(단위 : 원)"이 실제로 있는 문서에서만 켜지므로
                    # 이미 천원 단위인 문서를 잘못 나눌 위험이 없다.
                    vals[near] = str(round(int(t) / 1000))
            if len(vals) < 4:
                continue
            # 클래스 라벨은 값 줄과 같은 밴드가 아니라 위/아래로 걸쳐
            # 있다(실측: "수수료선취-" 위 줄, 값 줄, "오프라인(A)"
            # 아래 줄 - 세 줄이 한 클래스). 값 칸 왼쪽(라벨 영역)의
            # 글자만 바로 위/아래 줄까지 모은다.
            label_words = []
            for lj in (li - 1, li, li + 1):
                if 0 <= lj < len(lines):
                    label_words.extend(
                        w["text"] for w in lines[lj] if w["x0"] < min_col_x - 10)
            code = _label_class_code(" ".join(label_words))
            if code:
                by_code.setdefault(code, vals)
        if by_code:
            out.append((page_num, by_code))
    return out


def fill_detail_cost_coord_won(doc_id, rows):
    """_detail_cost_coord_won으로 찾은 값을, 다른 폴백들이 이미 채운
    뒤에도 여전히 비용예시가 빈 클래스에만 채운다. 같은 안전장치."""
    if all(r.get("cost_projection_per_10m") for r in rows):
        return 0
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return 0
    with pdfplumber.open(pdfs[0]) as pdf:
        grids = _detail_cost_coord_won(pdf)
    if not grids:
        return 0
    by_code = {}
    for _, m in grids:
        for code, vals in m.items():
            if code not in by_code or len(vals) > len(by_code[code]):
                by_code[code] = vals

    for r in rows:
        cur = r.get("cost_projection_per_10m") or {}
        cand = by_code.get(r.get("class_code"))
        if not cur or not cand:
            continue
        for y, v in cur.items():
            if y in cand and str(v).replace(",", "") != cand[y]:
                return 0

    filled = 0
    for r in rows:
        if r.get("cost_projection_per_10m"):
            continue
        cand = by_code.get(r.get("class_code"))
        if not cand:
            continue
        r["cost_projection_per_10m"] = dict(cand)
        r.setdefault("field_source_pages", {})["cost_projection_per_10m"] = next(
            pn for pn, m in grids if r["class_code"] in m)
        pages = r.setdefault("source_pages", [r["page"]])
        pg = r["field_source_pages"]["cost_projection_per_10m"]
        if pg not in pages:
            pages.append(pg)
        filled += 1
    return filled


# 다섯 번째 모양: _detail_cost_row_table과 표 자체는 완전히 같은데
# (헤더 "1년~10년" + 클래스 한 줄에 값 5개), 표 제목이 "구분" 대신
# "클래스종류 투자기간별 총비용 예시"라 _detail_cost_row_table의
# "구분" 낱말 요구 조건에 안 걸린다(KR5125450023 실측). 이미 검증된
# _detail_cost_row_table의 "구분" 요구 조건을 완화하면 그 함수를 쓰는
# 다른 문서에도 회귀 위험이 생기므로, 게이트만 다르게(표 제목 대신
# 페이지 안에 "1,000만원" 문구가 있는지로) 잡는 별도 함수로 둔다 -
# 나머지 파싱 로직은 동일하다.
def _detail_cost_row_table2(pdf):
    out = []
    col_x = None
    header_top = None
    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        text = page.extract_text() or ""
        words = page.extract_words(x_tolerance=2, keep_blank_chars=False)
        lines = cluster_lines(words, tol=2.5)
        this_page_header = None
        if "1,000만원" in text:
            for ln in lines:
                year_cols = {}
                for idx, w in enumerate(ln):
                    m = COST_AFTER_RE.match(w["text"].replace(" ", ""))
                    if m:
                        n = m.group(1)
                    elif (w["text"].isdigit() and idx + 1 < len(ln)
                          and ln[idx + 1]["text"] == "년"):
                        # "1"과 "년"이 같은 줄의 다른 낱말로 떨어지는
                        # 문서가 있다(KR5125450023 실측).
                        n = w["text"]
                    else:
                        continue
                    if n in ("1", "2", "3", "5", "10"):
                        year_cols[f"{n}y"] = w["x0"]
                if len(year_cols) >= 4:
                    this_page_header = (ln[0]["top"], year_cols)
                    break
        if this_page_header:
            header_top, col_x = this_page_header
        elif col_x is None:
            continue
        else:
            header_top = -1

        by_code = {}
        prev_label = None
        for ln in lines:
            if ln[0]["top"] <= header_top:
                continue
            vals = {}
            for w in ln:
                t = w["text"].replace(",", "")
                if not (t.lstrip("-").isdigit() and t not in ("-",)):
                    continue
                near = min(col_x, key=lambda k: abs(col_x[k] - w["x0"]))
                if abs(col_x[near] - w["x0"]) <= 15:
                    vals[near] = t
            if len(vals) >= 4:
                label = " ".join(w["text"] for w in ln
                                  if w["text"].replace(",", "") not in vals.values())
                code = _label_class_code2(label)
                if code:
                    by_code.setdefault(code, vals)
                    prev_label = None
                else:
                    prev_label = (label, vals)
            elif prev_label is not None and not vals:
                label2 = prev_label[0] + " " + " ".join(w["text"] for w in ln)
                code = _label_class_code2(label2)
                if code:
                    by_code.setdefault(code, prev_label[1])
                prev_label = None
        if by_code:
            out.append((page_num, by_code))
    return out


# _label_class_code의 "코드(설명)" 갈래(CLASS_CODE_PREFIX_RE)는 코드를
# 순수 알파벳 1~3자로만 본다 - 하이픈이 낀 코드("A-G(...)", "C-P(...)")는
# 못 뽑는다(KR5125450023 실측). 여기서만 국소적으로 보강한다 - 공용
# _label_class_code를 고치면 이미 검증된 다른 모든 문서의 판정까지
# 다시 흔들릴 위험이 있어서 손대지 않는다.
_RE_CODE_PREFIX_DASH = re.compile(r"^([A-Za-z][A-Za-z0-9\-]{0,7})\(")


def _label_class_code2(label):
    code = _label_class_code(label)
    if code:
        return code
    m = _RE_CODE_PREFIX_DASH.match(label.replace(" ", ""))
    if m and not _is_bad_code(m.group(1)):
        return m.group(1)
    return None


def fill_detail_cost_row_table2(doc_id, rows):
    """_detail_cost_row_table2로 찾은 값을, 다른 폴백들이 이미 채운
    뒤에도 여전히 비용예시가 빈 클래스에만 채운다. 같은 안전장치."""
    if all(r.get("cost_projection_per_10m") for r in rows):
        return 0
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return 0
    with pdfplumber.open(pdfs[0]) as pdf:
        grids = _detail_cost_row_table2(pdf)
    if not grids:
        return 0
    by_code = {}
    for _, m in grids:
        for code, vals in m.items():
            if code not in by_code or len(vals) > len(by_code[code]):
                by_code[code] = vals

    for r in rows:
        cur = r.get("cost_projection_per_10m") or {}
        cand = by_code.get(r.get("class_code"))
        if not cur or not cand:
            continue
        for y, v in cur.items():
            if y in cand and str(v).replace(",", "") != cand[y]:
                return 0

    filled = 0
    for r in rows:
        if r.get("cost_projection_per_10m"):
            continue
        cand = by_code.get(r.get("class_code"))
        if not cand:
            continue
        r["cost_projection_per_10m"] = dict(cand)
        r.setdefault("field_source_pages", {})["cost_projection_per_10m"] = next(
            pn for pn, m in grids if r["class_code"] in m)
        pages = r.setdefault("source_pages", [r["page"]])
        pg = r["field_source_pages"]["cost_projection_per_10m"]
        if pg not in pages:
            pages.append(pg)
        filled += 1
    return filled


# 위 다섯 폴백을 전부 거치고도 못 채우는 극소수는 PDF 원문 대조로 확인해
# 둔 값을 그대로 못박는다. 전부 "포함"/"합성 총보수" 쪽(이 코퍼스가 쓰는
# 기본 표기 - 위 함수들이 다 그 값을 쓴다) 숫자다. 공통 원인은 없다 -
# 코드가 두 물리적 줄에 걸쳐 있거나("S-P" 다음 줄에 "(퇴직)"), 코드
# 셀이 값 행과 다른 밴드에 떨어져 있거나(KR5118201004의 C-I·C-W처럼
# 바로 위/아래 클래스 이름과 밴드가 겹쳐 보이거나), 표 자체가 아예 다른
# 셀 구조(KR5120420091/KR5120451001)라 코드마다 원인이 다르다.
_KNOWN_COST_PROJECTION_GAPS = {
    "KR5118201004": {
        "B2": {"1y": "71", "2y": "103", "3y": "137", "5y": "209", "10y": "421"},
        "C-I": {"1y": "23", "2y": "46", "3y": "71", "5y": "125", "10y": "283"},
        "C-W": {"1y": "21", "2y": "42", "3y": "65", "5y": "114", "10y": "257"},
        "S": {"1y": "31", "2y": "63", "3y": "97", "5y": "169", "10y": "382"},
        "S-P(퇴직)": {"1y": "28", "2y": "58", "3y": "88", "5y": "154", "10y": "349"},
    },
    "KR5118420006": {
        "S": {"1y": "15", "2y": "31", "3y": "48", "5y": "84", "10y": "191"},
        "S-P(퇴직)": {"1y": "16", "2y": "33", "3y": "51", "5y": "90", "10y": "204"},
    },
    "KR5118420036": {
        "C-I": {"1y": "19", "2y": "40", "3y": "61", "5y": "106", "10y": "241"},
        "S": {"1y": "21", "2y": "43", "3y": "66", "5y": "116", "10y": "263"},
        "S-P(퇴직)": {"1y": "20", "2y": "41", "3y": "63", "5y": "111", "10y": "250"},
    },
    "KR5118420062": {
        "A2": {"1y": "26", "2y": "38", "3y": "50", "5y": "77", "10y": "155"},
        "C-W": {"1y": "11", "2y": "23", "3y": "35", "5y": "62", "10y": "140"},
        "S": {"1y": "20", "2y": "42", "3y": "64", "5y": "112", "10y": "254"},
        "S-P(퇴직)": {"1y": "17", "2y": "35", "3y": "55", "5y": "95", "10y": "216"},
    },
    "KR5120420091": {
        "C-P(연금)": {"1y": "30", "2y": "61", "3y": "94", "5y": "164", "10y": "374"},
        "C-Pe(연금)": {"1y": "21", "2y": "44", "3y": "67", "5y": "118", "10y": "268"},
        "C-R(퇴직연금)": {"1y": "28", "2y": "58", "3y": "89", "5y": "156", "10y": "355"},
        "C-Re(퇴직연금)": {"1y": "20", "2y": "42", "3y": "65", "5y": "113", "10y": "258"},
        "S-R(퇴직연금)": {"1y": "20", "2y": "41", "3y": "63", "5y": "110", "10y": "251"},
    },
    "KR5120451001": {
        "C-Re": {"1y": "55", "2y": "112", "3y": "173", "5y": "303", "10y": "690"},
    },
    "KR5123420049": {
        "C-P2(퇴직연금)": {"1y": "31", "2y": "63", "3y": "97", "5y": "169", "10y": "386"},
        "C-P2e(퇴직연금)": {"1y": "19", "2y": "40", "3y": "61", "5y": "107", "10y": "244"},
    },
    "KR5144420020": {
        "C-P2I(퇴직연금)": {"1y": "22", "2y": "44", "3y": "68", "5y": "120", "10y": "271"},
    },
    "KR5185450009": {
        "C-P2(퇴직연금)": {"1y": "161", "2y": "331", "3y": "509", "5y": "892", "10y": "2031"},
        "S-P2(퇴직연금)": {"1y": "104", "2y": "212", "3y": "326", "5y": "572", "10y": "1302"},
    },
    "KR555202013M": {
        "S-P": {"1y": "99", "2y": "201", "3y": "308", "5y": "535", "10y": "1185"},
    },
    "KR5111450067": {
        "C-P2E": {"1y": "119", "2y": "243", "3y": "374", "5y": "655", "10y": "1491"},
    },
    "KR5113420069": {
        "A-G": {"1y": "35", "2y": "70", "3y": "107", "5y": "186", "10y": "418"},
    },
    "KR5114420027": {
        # 원문이 원 단위(13,000/26,349/40,366/70,538/160,199)라 이
        # 코퍼스의 천원 단위 관행대로 반올림(같은 상품 Cf의 기존 확정값도
        # 같은 방식으로 반올림돼 있다 - 16,434->16, 202,516->203).
        "직판f": {"1y": "13", "2y": "26", "3y": "40", "5y": "71", "10y": "160"},
    },
    "KR5119520012": {
        "C-i2": {"1y": "27", "2y": "55", "3y": "84", "5y": "148", "10y": "333"},
    },
    # KR5147430065는 목표전환형 펀드라 예상비용표 자체가 1~3년만 제공하고
    # 5년·10년 칸이 없다(원문 확인) - 다른 클래스(A/Ae/C/Ce)도 마찬가지로
    # 3개 연차만 있다.
    "KR5147430065": {
        "AG": {"1y": "111", "2y": "154", "3y": "199"},
        "CI": {"1y": "39", "2y": "80", "3y": "123"},
        "CG": {"1y": "86", "2y": "176", "3y": "270"},
        "CW": {"1y": "35", "2y": "72", "3y": "110"},
        "C-P": {"1y": "76", "2y": "156", "3y": "238"},
        "C-Pe": {"1y": "56", "2y": "114", "3y": "175"},
        "C-P2": {"1y": "66", "2y": "135", "3y": "206"},
        "C-Pe2": {"1y": "51", "2y": "103", "3y": "159"},
    },
    "KR514X450008": {
        "S": {"1y": "131", "2y": "273", "3y": "425", "5y": "769", "10y": "1898"},
        "C-Pe": {"1y": "131", "2y": "273", "3y": "425", "5y": "769", "10y": "1898"},
        "C-P2e": {"1y": "126", "2y": "262", "3y": "408", "5y": "738", "10y": "1823"},
    },
    "KR5157420003": {
        "C-W": {"1y": "12", "2y": "26", "3y": "39", "5y": "69", "10y": "157"},
    },
    # KR5122420005는 예상비용표 마지막 두 행이 둘 다 "(A-G)"로 인쇄돼 있다
    # (33/59/88/148/329 그리고 32/65/100/176/400 - 39쪽 실측). 이 상품은
    # A-G와 C-G가 둘 다 실존하는 별개 클래스다(class_meaning/37쪽 보수표
    # 확인: A-G=선취 0.07%이내+총보수 0.25%, C-G=미징구(선취 없음)+총보수
    # 0.31%). 두 행 중 어느 쪽이 진짜 C-G인지는, 이 문서의 다른 선취/
    # 미징구 클래스 쌍으로 앞뒤를 맞춰 봐야 판단할 수 있다:
    #   - 선취 있는 클래스끼리(A 총보수0.34%→10년394 vs A-G 0.25%→?):
    #     첫 번째 행(329)이 훨씬 낮은 총보수와 앞뒤가 맞는다.
    #   - 미징구 클래스끼리(C 총보수0.40%→10년496 vs C-G 0.31%→?):
    #     두 번째 행(400)이 비율(0.31/0.40=0.775)과 10년치 비율
    #     (400/496=0.806)이 가장 가깝게 맞아떨어진다.
    # 두 비교 모두 "첫 번째 행=A-G, 두 번째 행=C-G"로 일치해 C-G에 두
    # 번째 행 값을 채운다(source_corrections에 원문 오기와 판단 근거를
    # 남긴다).
    "KR5122420005": {
        "C-G": {"1y": "32", "2y": "65", "3y": "100", "5y": "176", "10y": "400"},
    },
}

# 위 KR5122420005/C-G처럼 원문 라벨 자체가 잘못 인쇄돼 있어 다른 값과
# 대조해 판단해야 했던 경우, 그 근거를 값과 함께 남긴다(class_fees.json
# 기존 관례인 source_corrections 포맷 그대로).
_KNOWN_COST_PROJECTION_SOURCE_CORRECTIONS = {
    ("KR5122420005", "C-G"): {
        "field": "cost_projection_per_10m",
        "raw": "A-G (39쪽 예상비용표에 두 번째 행도 \"A-G\"로 중복 인쇄됨)",
        "used": "C-G",
        "reason": (
            "선취 클래스쌍(A vs A-G)·미징구 클래스쌍(C vs C-G)의 총보수 "
            "비율과 10년 누적비용 비율을 대조하면 두 번째 행(32/65/100/"
            "176/400)이 총보수 0.31%인 C-G와, 첫 번째 행(33/59/88/148/"
            "329)이 총보수 0.25%인 A-G와 일치한다"
        ),
    },
}

# 상세표(page)와 요약표 예상비용표 페이지가 다른 문서가 많아, 위 override로
# 채운 값의 field_source_pages를 PDF 재검색으로 확인해 둔 것만 기록한다
# (검색 결과가 여러 페이지에 걸쳐 걸리는 모호한 경우는 "모르면 지어내지
# 않는다" 원칙대로 비워 둔다).
_KNOWN_COST_PROJECTION_PAGES = {
    ("KR5118201004", "B2"): 40, ("KR5118201004", "C-I"): 40,
    ("KR5118201004", "C-W"): 40, ("KR5118201004", "S"): 40,
    ("KR5118420036", "C-I"): 42, ("KR5118420036", "S"): 43,
    ("KR5118420036", "S-P(퇴직)"): 43,
    ("KR5120420091", "C-P(연금)"): 43, ("KR5120420091", "C-Pe(연금)"): 43,
    ("KR5120420091", "C-Re(퇴직연금)"): 43,
    ("KR5120420091", "S-R(퇴직연금)"): 44,
    ("KR5120451001", "C-Re"): 43,
    ("KR5185450009", "C-P2(퇴직연금)"): 31,
    ("KR5185450009", "S-P2(퇴직연금)"): 31,
    ("KR5111450067", "C-P2E"): 43,
    ("KR5122420005", "C-G"): 39,
}


def fill_known_cost_projection_gaps(doc_id, rows):
    """_KNOWN_COST_PROJECTION_GAPS 정의부 주석 참고."""
    fixes = _KNOWN_COST_PROJECTION_GAPS.get(doc_id)
    if not fixes:
        return 0
    filled = 0
    for r in rows:
        if r.get("cost_projection_per_10m"):
            continue
        cand = fixes.get(r.get("class_code"))
        if not cand:
            continue
        r["cost_projection_per_10m"] = dict(cand)
        key = (doc_id, r.get("class_code"))
        correction = _KNOWN_COST_PROJECTION_SOURCE_CORRECTIONS.get(key)
        if correction:
            r.setdefault("source_corrections", []).append(dict(correction))
        page = _KNOWN_COST_PROJECTION_PAGES.get(key)
        if page:
            r.setdefault("field_source_pages", {})["cost_projection_per_10m"] = page
            sp = r.setdefault("source_pages", [r.get("page")])
            if page not in sp:
                sp.append(page)
        filled += 1
    return filled


# KR5147430065의 CG/CW/C-P/C-Pe/C-P2/C-Pe2 여섯 클래스는 "(동종유형
# 총보수)" 행이 전환 전(34쪽)·전환 후(35쪽) 표 둘 다에서 "-"로 명시돼
# 있는데(실측), 같은 문서의 AG/CI 등 다른 열블록은 이 값을 정상적으로
# "-"/"-"까지 잡아 peer_avg_fee_after_conversion을 남기면서 유독 이
# 여섯만 peer_avg_fee가 null(필드 자체는 있으나 못 채움)이고
# peer_avg_fee_after_conversion 필드가 아예 없다 - 다른 항목(총보수/
# 판매보수/총보수·비용, 전환 전후 모두)은 이 여섯도 정상 추출됐으므로
# 표 자체를 놓친 게 아니라 이 한 행만 놓쳤다. 원인이 된 표 블록이
# 문서마다 다른 이 파서의 특성상 한 줄로 못박는다.
_KNOWN_PEER_AVG_FEE_DASH = {
    "KR5147430065": {"CG", "CW", "C-P", "C-Pe", "C-P2", "C-Pe2"},
}


def fix_known_peer_avg_fee_gaps(doc_id, rows):
    """_KNOWN_PEER_AVG_FEE_DASH 정의부 주석 참고."""
    codes = _KNOWN_PEER_AVG_FEE_DASH.get(doc_id)
    if not codes:
        return 0
    fixed = 0
    for r in rows:
        if r.get("class_code") not in codes:
            continue
        if r.get("peer_avg_fee") is None:
            r["peer_avg_fee"] = "-"
            fixed += 1
        if "total_fee_after_conversion" in r and "peer_avg_fee_after_conversion" not in r:
            r["peer_avg_fee_after_conversion"] = "-"
    return fixed


# KR5116501001은 "나. 집합투자기구에 부과되는 보수 및 비용"(36쪽)
# 표의 머리글이 "총 보 수) 총보수 비율"처럼 각주성 문구
# ("(모투자신탁의 총보수·비용 포함)")가 칸 이름 사이에 끼어들며
# 두 줄로 어긋나 있다. 그 결과 C-P/C-Pe의 fee_breakdown이
# management_fee가 중복되고("0.2"·"0.02" 둘 다 management_fee),
# 라벨이 "비용"/"및비용비율"처럼 잘린 조각으로 남았다(36쪽 원문
# 재대조 결과 0.2=집합투자업자, 0.3=판매회사[제외], 0.02=신탁업자,
# 0.015=사무관리회사, 0.0070/0.0000=기타비용, 0.0016/0.0000=
# 증권거래비용 - 나머지 0.535/0.385(총보수)·0.5420/0.3850(총보수
# 비용)·"-"(동종유형)는 원래도 breakdown 제외 대상). 이 머리글
# 모양이 이 문서 하나뿐이라 일반 파서를 고치는 대신 값 자체를
# 못박는다.
_KNOWN_FEE_BREAKDOWN_FIXES = {
    ("KR5116501001", "C-P"): [
        {"label": "management_fee", "value": "0.2"},
        {"label": "trustee_fee", "value": "0.02"},
        {"label": "admin_fee", "value": "0.015"},
        {"label": "other_expense", "value": "0.0070"},
        {"label": "transaction_cost", "value": "0.0016"},
    ],
    ("KR5116501001", "C-Pe"): [
        {"label": "management_fee", "value": "0.2"},
        {"label": "trustee_fee", "value": "0.02"},
        {"label": "admin_fee", "value": "0.015"},
        {"label": "other_expense", "value": "0.0000"},
        {"label": "transaction_cost", "value": "0.0000"},
    ],
}


def fix_known_fee_breakdown_corruption(doc_id, rows):
    """_KNOWN_FEE_BREAKDOWN_FIXES 정의부 주석 참고."""
    fixed = 0
    for r in rows:
        fix = _KNOWN_FEE_BREAKDOWN_FIXES.get((doc_id, r.get("class_code")))
        if fix:
            r["fee_breakdown"] = [dict(item) for item in fix]
            fixed += 1
    return fixed


def fill_detail_cost_projections(doc_id, rows):
    """상세표에서만 나온 클래스는 요약표에 없어 비용예시가 비어 있다
    (실측 298건). 뒤쪽 부속서류의 "<1,000만원 투자시 ...>" 표에서
    가져온다. 요약표에도 있는 클래스로 먼저 대조해서, 값이 어긋나면
    (다른 기준일 표를 잘못 읽은 것) 그 문서는 통째로 건드리지 않는다."""
    if all(r.get("cost_projection_per_10m") for r in rows):
        return 0
    pdfs = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdfs:
        return 0
    known_codes = {r["class_code"] for r in rows if r.get("class_code")}
    with pdfplumber.open(pdfs[0]) as pdf:
        grids = _detail_cost_grids(pdf, known_codes)
    if not grids:
        return 0
    # 같은 코드가 여러 표(부속서류가 여러 군데 있는 문서)에서 나오면,
    # 연도 칸을 더 많이 채운 쪽을 믿는다 - "1년후~10년후" 5칸이 전부
    # 있는 진짜 비용예시표가, 일부만 걸린 표보다 항상 더 완전하다.
    by_code = {}
    for _, m in grids:
        for code, vals in m.items():
            if code not in by_code or len(vals) > len(by_code[code]):
                by_code[code] = vals

    checked = conflict = 0
    for r in rows:
        cur = r.get("cost_projection_per_10m") or {}
        cand = by_code.get(r.get("class_code"))
        if not cur or not cand:
            continue
        for y, v in cur.items():
            if y in cand:
                checked += 1
                if str(v).replace(",", "") != cand[y]:
                    conflict += 1
    if conflict:
        return 0

    filled = 0
    for r in rows:
        if r.get("cost_projection_per_10m"):
            continue
        cand = by_code.get(r.get("class_code"))
        if not cand:
            continue
        r["cost_projection_per_10m"] = dict(cand)
        r.setdefault("field_source_pages", {})["cost_projection_per_10m"] = next(
            pn for pn, m in grids if r["class_code"] in m)
        pages = r.setdefault("source_pages", [r["page"]])
        pg = r["field_source_pages"]["cost_projection_per_10m"]
        if pg not in pages:
            pages.append(pg)
        filled += 1
    return filled


def _backfill_from_value_sources(rows):
    """value_sources엔 상세표에서 읽은 진짜 값이 남았는데 정작 필드
    자체는 아직 None인 행을 채운다.

    요약표에 이미 있는 클래스는 enrich_with_transposed_fee_table/
    enrich_with_detail_fee_table 두 곳 다 값 대조용 기준을 지키려고
    known 행의 숫자 필드를 직접 안 건드리고 value_sources에만 상세표
    값을 남긴다. 그런데 요약표가 애초에 그 필드를 안 보여주는 문서가
    있다(KR5118201004 실측: 요약표엔 총보수만 있고 클래스별 판매회사
    보수는 상세표에만 있다 - A-G/B2/B-G/I/C-I 다섯 클래스). 이런
    경우엔 필드가 계속 None으로 남아, 값이 버젓이 상세표에 있는데도
    누락으로 보인다. None(못 읽음)과 "-"(원본에 이미 없다고 확인됨)는
    뜻이 다르므로, 이미 "-"로 확정된 필드는 건드리지 않는다."""
    for r in rows:
        vs = r.get("value_sources") or []
        for field in FEE_SOURCE_FIELDS:
            if r.get(field) is not None:
                continue
            for s in vs:
                if s["field"] == field and s["value"] not in DASHES:
                    r[field] = s["value"]
                    r.setdefault("field_source_pages", {}).setdefault(
                        field, s["page"])
                    # field_source_pages엔 이 값을 읽은 페이지가 남는데
                    # source_pages(이 행 전체의 근거 페이지 목록)엔 안
                    # 더해지는 문서가 있었다(KR5153420022 실측:
                    # field_source_pages.total_fee_and_cost=27인데
                    # source_pages=[3,14]로 27이 빠짐) - 근거 페이지를
                    # 보여줄 때 실제로 값을 읽은 페이지가 목록에서
                    # 통째로 빠지는 문제였다.
                    pages = r.setdefault("source_pages", [r["page"]])
                    if s["page"] not in pages:
                        pages.append(s["page"])
                    break
    return rows


# fee_breakdown 항목 이름이 문서마다 표기가 갈린다(전수 조사 결과 실제
# 관측된 표기만 51종). "같은 뜻인 게 확실한 것"만 6개 고정 이름으로
# 묶는다 - 사용자가 제안한 이름 그대로(management_fee/distribution_fee/
# trustee_fee/admin_fee/other_expense/transaction_cost) 쓴다.
# distribution_fee(판매회사보수)는 애초에 breakdown에 안 들어간다 -
# dist_col/peer_col/cost_col/total_col은 breakdown을 만들 때부터
# 빼놓는 값이라(판매보수·동종유형·총보수·비용은 이미 별도 필드로
# 담긴다) 실제로 breakdown에 등장하는 건 나머지 4개뿐이다.
FEE_BREAKDOWN_LABEL_MAP = {
    "management_fee": (
        "집합투자업자보수", "보수운용보수", "집합투자업자", "투자업자", "업자",
        # 칸 이름이 여러 줄에 걸쳐 있는 표에서, 실제 칸 이름("집합투자")
        # 앞에 다른 칸들과 공유하는 단위 안내("보수(연,%)")가 그대로 붙어
        # 읽히는 문서가 있다(KR5114420016/KR5131420007 실측 - 45+20건).
        "보수(연,%)집합투자", "투자신탁보수(연,%)집합투자업자",
        # "집합투자업자보수" 대신 "운용보수"라는 표기를 쓰는 문서가 있다
        # (KR5123420039 16쪽 실측: "운용보"/"수" 두 줄로 잘려도 합치면
        # "운용보수" - 집합투자업자가 받는 보수라는 같은 뜻).
        "운용보수"),
    "trustee_fee": (
        "신탁업자보수", "신탁보수", "수탁회사보수", "신탁회사보수", "신탁업자", "신탁"),
    "admin_fee": (
        "일반사무관리회사보수", "사무관리", "일반사무관리보수", "사무관리회사보수",
        "일반사무관리", "일반사무보수", "일반사무회사보수", "사무관리회사"),
    "other_expense": ("기타비용", "개별기타비용"),
    "transaction_cost": ("증권거래비용",),
}
_FEE_BREAKDOWN_LABEL_BY_TEXT = {
    text: field for field, texts in FEE_BREAKDOWN_LABEL_MAP.items()
    for text in texts
}
# breakdown에 원래 안 들어가야 할 값이 섞여 들어온 표기들이다 -
# "합성총보수·비용"(모투자신탁 보수까지 더한 다른 개념)과 "총보수(·비용)"
# (이미 total_fee/total_fee_and_cost로 따로 담기는 값)이다. 가운뎃점
# 표기가 문서마다 갈려서(·/ㆍ/∙/•/․) 정규화 없이 부분 문자열로 넓게
# 잡는다 - "총비용"은 두 낱말 순서가 뒤집혀 찍힌 문서(KR5125450070류
# "보수·비용합성총(...)")까지 걸러내려는 것이다.
FEE_BREAKDOWN_EXCLUDE_RE = re.compile(r"합성|총비용|^총보수")


def _normalize_fee_breakdown(rows):
    """fee_breakdown 라벨을 정리한다 - 확실한 동의어만 6개 필드명으로
    통일하고, breakdown에 안 어울리는 값(합성총보수·비용, 총보수 자체)은
    뺀다. 뜻을 확신 못 하는 표기(각주·머리글이 섞여 든 원문 등)는 원문
    그대로 둔다 - 잘못 묶어 서로 다른 값을 합치는 것보다, 정규화 안 된
    채로 남기는 쪽이 안전하다."""
    for r in rows:
        bd = r.get("fee_breakdown")
        if not bd:
            continue
        out = []
        for item in bd:
            label = item.get("label")
            if label is None:
                out.append(item)
                continue
            n = re.sub(r"\s+", "", label).translate(DOT_NORMALIZE_TRANS)
            if FEE_BREAKDOWN_EXCLUDE_RE.search(n):
                continue
            n_clean = FEE_FOOTNOTE_MARK_RE.sub("", n).strip()
            canonical = _FEE_BREAKDOWN_LABEL_BY_TEXT.get(n_clean)
            if canonical:
                item = dict(item, label=canonical)
            out.append(item)
        if len(out) != len(bd) or any(a is not b for a, b in zip(out, bd)):
            r["fee_breakdown"] = out
    return rows


def main():
    parser = argparse.ArgumentParser(description="클래스별 총보수 좌표 기반 추출 (1차: 총보수 표만)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_tables.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_tables.json"))
    )

    all_rows = []
    docs_with_hits = 0
    docs_with_missing_class_code = 0
    detail_enriched = 0
    cost_filled = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            # 요약표(좌표 방식)가 뽑은 코드도 class_meaning(명칭표 기준이라
            # 더 안정적으로 읽는다)과 표기가 갈릴 수 있다(KR514X450008
            # 실측: 요약표는 "Ae", 명칭표는 "A-e" - 상세표 쪽엔 이미 같은
            # 정규화가 있었는데(_normalize_code_via_labels) 요약표 결과는
            # 안 거쳐서 같은 클래스가 두 표기로 남아 있었다).
            labels = _class_labels_for_doc(doc_id)
            for r in rows:
                r["class_code"] = _normalize_code_via_labels(r.get("class_code"), labels)
            docs_with_hits += 1
            if any(r["confidence"] < 0.7 for r in rows):
                docs_with_missing_class_code += 1
        # 요약표엔 없고 상세표("나.집합투자기구에 부과되는 보수 및 비용")
        # 에만 있는 클래스를 보강한다 - README "class_fees.json 코퍼스
        # 전체 완전성 문제" 참고. 요약표에서 뽑힌 클래스가 2개 미만이면
        # 대조 기준이 없어 조용히 그대로 넘어간다.
        before = len(rows)
        rows = enrich_with_detail_fee_table(doc_id, rows)
        # 클래스가 열이고 보수 항목이 행인 보수표를 쓰는 문서가 있다.
        # 위 경로는 "행 하나 = 클래스 하나"를 전제해서 그런 문서에선
        # 클래스 코드조차 못 읽는다.
        rows = enrich_with_transposed_fee_table(doc_id, rows)
        if len(rows) > before:
            detail_enriched += 1
        # 운용전환 전/후로 뒤집힌 상세표가 통째로 두 번 나오는 문서는
        # 위 enrich_with_transposed_fee_table이 (요약표로 검증되는)
        # 전환 "전" 표만 읽고 전환 "후" 표는 못 읽는다.
        rows = _fill_transposed_after_conversion(doc_id, rows)
        if any("total_fee_after_conversion" in r for r in rows):
            nav_price = conversion_trigger_nav_price(doc_id)
            for r in rows:
                if "total_fee_after_conversion" in r:
                    r.setdefault("conversion_trigger_nav_price", nav_price)
        # "나" 상세표 보강으로 새로 생긴 클래스들의 sales_commission_desc
        # null을 "가.투자자에게 직접 부과되는 수수료" 표에서 채운다(확실한
        # "없음"만 - 위 enrich_sales_commission_from_ga_table 주석 참고).
        rows = enrich_sales_commission_from_ga_table(doc_id, rows)
        # 상세표에서만 나온 클래스는 요약표에 비용예시 칸이 없어 비어
        # 있다 - 뒤쪽 부속서류의 "<1,000만원 투자시 ...>" 표에서 채운다.
        # 요약표에도 있는 클래스로 먼저 대조해서 어긋나면 안 채운다.
        cost_filled += fill_detail_cost_projections(doc_id, rows)
        # 위 표와 아예 다른 모양(칸이 안 병합되고 클래스당 한 줄)의
        # 비용예시표를 쓰는 문서가 있다 - 기존 파서(_detail_cost_grids)는
        # 그대로 두고, 그걸로도 여전히 못 채운 클래스에만 별도 방식을
        # 적용한다.
        cost_filled += fill_detail_cost_row_table(doc_id, rows)
        # 세 번째 모양: _detail_cost_grids가 다루는 표와 같은 셀
        # 구조인데, 연도 머리글이 "1/2/3/5/10"(숫자만) 한 줄 + "년후"
        # 다음 줄로 갈라져 기존 파서의 헤더 인식이 실패하는 문서.
        cost_filled += fill_detail_cost_grid2(doc_id, rows)
        # 네 번째 모양: 값이 원 단위 그대로 찍히고 라벨이 표 테두리
        # 밖에 있는 문서.
        cost_filled += fill_detail_cost_coord_won(doc_id, rows)
        # 다섯 번째 모양: row_table과 표는 같은데 "구분" 대신 다른
        # 표 제목("클래스종류 투자기간별 총비용 예시")을 쓰는 문서.
        cost_filled += fill_detail_cost_row_table2(doc_id, rows)
        # 위 다섯 폴백을 다 거치고도 못 채운 극소수는 원문 대조로 확인해
        # 둔 값을 그대로 못박는다.
        cost_filled += fill_known_cost_projection_gaps(doc_id, rows)
        fix_known_peer_avg_fee_gaps(doc_id, rows)
        fix_known_fee_breakdown_corruption(doc_id, rows)
        rows = _backfill_from_value_sources(rows)
        rows = _normalize_fee_breakdown(rows)
        # 출처 필드는 모든 행이 갖도록 맞춘다(class_returns.json과 같은
        # 규칙) - 합쳐진 행만 갖고 있으면 조회하는 쪽이 매번 존재 여부를
        # 따져야 한다. 보강 함수 안에서 하면 그 함수가 일찍 return하는
        # 문서(클래스가 1개뿐이라 대조 기준이 없는 KR5123365001 등)가
        # 빠지므로 여기서 한다.
        # 작성기준일은 문서 하나에 하나다 - 요약표 행에서만 찾히므로
        # 상세표 보강으로 생긴 행에도 같이 붙인다(없으면 그 행만 숫자가
        # "언제 기준"인지 모르는 상태가 된다).
        doc_as_of = next((r.get("as_of") for r in rows if r.get("as_of")), None)
        if doc_as_of is None:
            # 요약표가 있는 페이지만 훑어서, 작성기준일이 표지나 요약정보
            # 쪽에만 찍힌 문서(6개)를 통째로 놓치고 있었다. 못 찾았을 때만
            # 앞쪽 페이지를 마저 본다.
            for path in glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))[:1]:
                with pdfplumber.open(path) as doc_pdf:
                    for page in doc_pdf.pages[:AS_OF_SCAN_PAGES]:
                        doc_as_of = _page_as_of(page.extract_text() or "")
                        if doc_as_of:
                            break
        for r in rows:
            if doc_as_of:
                r.setdefault("as_of", doc_as_of)
            r.setdefault("source_pages", [r["page"]])
            r.setdefault("field_source_pages", {})
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print(f"{len(all_rows)}개 클래스 레코드 ({docs_with_hits}개 문서) → {args.output}")
    print(f"클래스 코드 인식 실패(confidence<0.7): {docs_with_missing_class_code}개 문서")
    print(f"상세표 보강으로 클래스 추가된 문서: {detail_enriched}개")
    print(f"상세 비용예시표로 비용예시 채운 레코드: {cost_filled}건")
    print(f"요약표 좌표 방식 폴백 문서: {len(_SUMMARY_FALLBACK_DOCS)}개 {sorted(_SUMMARY_FALLBACK_DOCS)}")


if __name__ == "__main__":
    main()
