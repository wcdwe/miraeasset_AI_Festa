"""해마다 얼마를 벌었는지 뽑는다 ("나. 연도별 수익률 추이").

    "작년에 얼마 벌었어요?"
    "2023년에는 어땠나요?"

지금은 답하지 못한다. class_returns에는 연평균(누적) 수익률만 있어서
"최근 3년 -31.08%"까지만 말할 수 있는데, 그건 3년을 묶은 값이라
"작년 성과"와는 다른 것이다. 해마다의 값은 따로 실려 있다.

    나. 연도별 수익률 추이(단위:%)
    연도       최근 1년차   최근 2년차   최근 3년차   최근 4년차   최근 5년차
    (기간)     24.01.01~   23.01.01~   22.01.01~   21.01.01~   20.01.01~
               24.12.31    23.12.31    22.12.31    21.12.31    20.12.31
    종류A ...   -23.62      35.66      -31.08       18.99       33.38

연평균 표와 헷갈리면 안 된다. 문서에 따라 연평균 표도 "1년차"라고 쓴다.
가르는 법은 기간이다.

    연평균: 24.03.26~25.03.25 / 23.03.26~25.03.25 ... 끝나는 날이 같다
    연도별: 24.01.01~24.12.31 / 23.01.01~23.12.31 ... 끝나는 날이 다르다

값은 표의 칸에서 읽는다. 클래스 이름이 이름표째 들어 있어서
("종류A 수수료선취-오프라인") class_meaning의 파서를 그대로 쓴다.

실행:
    python3 scripts/extract_yearly_returns.py
    python3 scripts/extract_yearly_returns.py --check
"""

import argparse
import glob
import json
import os
import re
import sqlite3

import pdfplumber

import pdf_words  # noqa: F401  (import만으로 Page.chars 전역 패치가 걸린다)
from extract_class_meaning import _parse_row, _squash

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "yearly_returns.json")
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")

# "년차"가 아니라 "년"으로만 쓰는 문서도 있다(연평균 표와 글자가 같다) -
# 그런 문서는 _is_yearly()의 날짜 비교가 유일한 가림판이 된다.
RE_YEAR_COL = re.compile(r"최근\s*(\d+)\s*년(?:차)?")
# "24.01.01~ 24.12.31" 처럼 물결로 이은 기간
# 물결표를 문서마다 다르게 쓴다(~ ∼ 〜 ～ -). 아스키 물결만 받다가
# 상품 하나를 통째로 놓쳤다. 날짜 구분자도 "."/"-" 말고 "/"를 쓰는 문서가
# 있다("2024/04/25"). 연도 앞에 어깻점을 붙이는 문서도 있다("'24.01.17
# ~'25.01.16" - KR5194450018 실측). 어깻점 뒤에 공백이 없어서 "~" 다음
# \s*가 그 자리에서 멈추고 숫자가 아닌 어깻점 때문에 통째로 매칭이
# 깨졌다 - 그러면 이 표가 "가.연평균"인지조차 못 가려서(periods={}),
# 날짜 대신 페이지 텍스트의 "연도별 수익률" 문구 유무로 가리는 폴백으로
# 넘어가는데, 같은 페이지 아래쪽에 다음 절 "나.연도별 수익률 추이"
# 제목만 걸쳐 있어도 그 폴백이 속아 "가" 표를 "나" 표로 잘못 받아들였다.
RE_PERIOD = re.compile(
    r"['‘’]?(\d{2,4}[./\-]\d{1,2}[./\-]\d{1,2})\s*[~∼〜～]\s*"
    r"['‘’]?(\d{2,4}[./\-]\d{1,2}[./\-]\d{1,2})")
RE_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
# 날짜 끝자리 숫자 하나가 줄바꿈으로 다음 줄에 떨어지는 표가 있다
# (KR5129420031 실측: 같은 칸의 4줄이 "2024/11/0" / "7~" / "2025/11/0"
# / "6"으로 갈라진다 - "07"이 "0"과 "7" 사이에 개행이 들어가 쪼개짐).
# 이 함수는 그 조각들을 공백으로 이어 붙이는데, 숫자 사이에 남은
# 공백을 그대로 두면 "2024/11/0 7"이 되어 RE_PERIOD의 "일"자리
# (\d{1,2})가 "0"에서 멈춰 통째로 매치가 깨진다. 숫자와 숫자 사이의
# 공백만 지운다 - 그 밖의 공백(물결 앞뒤 등)은 그대로 둬도 RE_PERIOD가
# 이미 \s*로 허용한다.
def _squash_split_digits(text):
    return re.sub(r"(?<=\d)\s+(?=\d)", "", " ".join(text.split()))
# 날짜로 못 가릴 때(위 참고) "나"인지 최후 보루로 페이지 문구를 보는데,
# 단순히 "연도별 수익률"이 텍스트에 있는지만 보면 각주 설명문(예:
# "연평균 수익률은 ...연도별 수익률은 기간별 수익률 변동성을 나타낸
# 것입니다")에도 이 문구가 그냥 나와서 속는다(KR5113420069 실측: 이
# 설명문 때문에 "가" 표의 머리글 조각까지 "나" 표로 오인되고, 그 상태가
# 뒤이은 진짜 "가" 데이터 표에 물려져 클래스 전체가 연도별 값으로
# 오염됐다). 진짜 절 제목("나.연도별수익률" 또는 "...추이")은 뒤에
# 조사(은/는/이/가/을/를/및)가 안 붙는다 - class_returns.py의 같은
# 문제(SECTION_NA_RE)에서 검증된 것과 같은 구분법을 쓴다.
# 뒤에 조사가 붙는 각주 문장을 막으려고 부정형 lookahead로 조사를
# 막아봤지만("...연도별 수익률에 관한 정보는...") "에"처럼 놓친 조사가
# 계속 나온다(KR5113420069 실측) - 조사 목록을 완벽히 다 막을 수는
# 없다. 대신 "나."/숫자) 같은 절 번호 접두를 필수로 요구한다 - 이
# 파일이 보는 문서들에서 진짜 절 제목은 항상 이 접두가 붙어 있고
# (파일 맨 위 예시 참고), 각주 설명문 한가운데에 "나."가 우연히
# 바로 붙어 나올 일은 없다.
RE_NA_TITLE = re.compile(r"(?:나[\.．]|\d[\).])연도별\s*수익률(?:\s*추이)?")

# 클래스 행이 아니라 펀드 전체/비교지수를 나타내는 이름
# 클래스 구분 없이 펀드(모펀드) 전체 수익률 한 줄만 싣는 문서가 있는데,
# 그 한 줄의 표기가 회사마다 다르다 - "투자신탁"/"집합투자기구"/"펀드"
# 외에 "종류 모"(KR5113420069), "운용(모)"(KR5123365001 - 클래스가 아예
# 없는 모자형 펀드로, 이 한 줄이 전체 실적이다), 그냥 "운용"(KR5125450023
# 등 - 클래스별 줄과 나란히, 클래스 구분 없는 전체 실적 줄을 이렇게만
# 표시)도 쓴다. "운용"은 "나" 표 안에서만(_row_label이 이 표의 행에만
# 불려 나온다) 보므로 다른 표의 "운용수수료" 류 항목과 섞일 위험이 없다.
WHOLE_FUND = ("투자신탁", "집합투자기구", "펀드", "종류모", "(모)", "운용")
BENCHMARK = ("비교지수", "벤치마크", "BM")
# 수익률 표의 클래스명 칸은 명칭표와 달리 뜻(수수료방식-판매경로...)이
# 안 붙고 코드만 덜렁 있는 경우가 많다("ClassA1", "종류S-P"). _parse_row는
# "수수료..." 문구를 기준점으로 삼아 코드를 찾으므로 이런 줄은 못 읽는다
# (KR5118420006 46쪽 실측 - ClassA1/ClassS-P 등 전 클래스가 통째로
# 빠졌었다). 아무 글자나 코드로 받으면 위험하므로, class_fees.json에
# 이미 있는 코드일 때만 받아들인다.
RE_BARE_CODE_LABEL = re.compile(r"^(?:종류|Class)[- ]?([A-Za-z0-9][A-Za-z0-9\-]{0,8})$")
# 수수료방식·판매경로 설명 없이, 상품 정식명칭 뒤에 코드만 괄호로 붙는
# 문서가 있다("한국투자골드플랜연금증권전환형투자신탁1호(국공채)(C)").
# _parse_row는 "수수료..." 문구를 기준점으로 삼아서 이런 줄은 아예 못
# 읽고, 그러면 이름표 안의 "투자신탁"이라는 낱말 때문에 클래스 행까지
# 전부 fund로 잘못 분류된다(KR5113420012 실측 - C/C-e/S-P 세 클래스가
# 전부 이렇게 사라졌다). 맨 끝 괄호 코드가 보수표에 이미 있는 코드일
# 때만 받는다(정식명칭 안에도 괄호가 여럿 있어 - "...(국공채)(C)" -
# 아무 괄호나 코드로 오인하면 위험하다).
RE_TRAILING_CODE = re.compile(r"\(([A-Za-z0-9][A-Za-z0-9\-]{0,8})\)\s*$")
# 테두리 없는 표(좌표 폴백, _coord_table_rows)는 라벨 칸에 최초설정일이
# 붙어 나온다("펀드ClassA2 2017-01-09"). 코드 바로 뒤에 이 날짜꼴이
# 와도(중간에 아무 구분자 없이 붙어도) 코드로 인정한다. 연도는 반드시
# 4자리여야 한다 - 2~3자리까지 허용하면, 코드 뒤에 진짜 공백을 두고
# 4자리 연도가 이어지는 정상 표기("Class A 2016-04-18")가 squash로
# 공백이 지워진 뒤 "숫자 하나가 코드에 붙고 나머지 3자리가 연도"인
# 모양("A2" + "016-04-18")으로도 우연히 읽혀서, 같은 상품에 실제로
# 존재하는 다른 코드(A2)로 잘못 끊길 위험이 있다(KR5111420047 실측:
# "ClassA 2016-04-18"가 "ClassA2"+"016-04-18"로 잘못 읽혀 클래스 A가
# 통째로 사라지고 존재하지도 않을 매치가 A2 뒤에 붙을 뻔했다 - 같은
# 이유로 "ClassC-P 2017-01-31"도 "C-P2"+"017-01-31"로 잘못 읽혔다).
RE_DATE_TOKEN = r"\d{4}[./\-]\d{1,2}[./\-]\d{1,2}"
# "나"표의 마지막 줄은 바로 다음 절 제목("다. 집합투자기구의 자산구성
# 현황" 등)이 같은 줄로 딸려 들어오기도 한다(KR5111450067 실측: "펀드
# ClassS-P2 2020-06-29 다. 집합투자기구의 자산구성 현황" - 날짜 뒤에
# 다음 절 제목이 그대로 붙어 라벨이 "맨 끝"에서 끝나지 않는다). 그래서
# 코드가 문자열 맨 끝일 필요는 없고, 아는 코드 뒤끝이 문자열 끝·한글·
# 날짜꼴 중 하나로 막혀 있으면 그걸로 본다. 앞쪽은 안 본다 - "Class"/
# "종류"가 코드 바로 앞에 붙어(중간에 구분자 없이, "ClassS-P2"처럼)
# 나오는 게 이 말뭉치의 표준 표기라 앞쪽 경계까지 요구하면 오히려
# 흔한 표기를 못 읽는다. 클래스 코드는 로마자·숫자뿐이고 나머지는
# 전부 한글이라, 뒤끝 경계만으로도 다른 낱말 안에 우연히 걸릴 위험은
# 없다(더 짧은 코드가 긴 코드 앞부분과 우연히 겹치는 경우는 길이가
# 긴 것부터 먼저 찾아서 가른다).


def _suffix_class_code(flat, known_codes):
    """라벨에 이미 아는 클래스 코드가 박혀 있고 뒤끝이 낱말 경계인가.

    상품 정식명칭 한가운데에 "투자신탁"/"펀드" 같은 WHOLE_FUND 낱말이
    끼어 있어도, 클래스 코드는 늘 라벨 뒤쪽에 붙는다("펀드ClassA2",
    "...투자신탁[채권]Class C-P(연금)", "...자투자신탁 A" - 표기가
    "종류"/"Class"/"펀드Class"/맨살 등 제각각이라 접두사를 일일이 나열하는
    대신, 아는 코드가 박혀 있는지만 본다). known_codes 중 가장 긴 것부터
    맞춰야 "S"가 "S-P"보다 먼저 걸려 잘못 짧게 끊기지 않는다.

    뒤끝(오른쪽) 경계만 보고 왼쪽은 안 보면, 클래스 코드가 아예 안 박혀
    있는 라벨인데 우연히 끝 글자만 코드와 같아도 걸린다(실측:
    KR5194450018 51쪽 자산구성표의 통화 라벨 "KRW"가 클래스 "W"의
    접미사로 잘못 걸려, 그 옆 순수 자산총액 숫자 행이 "W"클래스의
    연도별 수익률 행으로 둔갑했다 - 원문 어디에도 이 문서의 "W"클래스
    연도별 수익률은 없다). 왼쪽 경계도 함께 본다: 코드 바로 앞이
    (1) 문자열 시작이거나 (2) 영숫자가 아니거나(공백/괄호 등) (3)
    "class"(대소문자 무관)로 끝나거나(위 "펀드ClassA2") (4) "종류"로
    끝날 때만("종류A"처럼 붙여 쓰는 문서) 진짜 클래스 코드로 본다 -
    "KR" 같은 임의의 영문 접두사 뒤에 붙은 우연의 일치는 전부 걸러진다."""
    for code in sorted(known_codes, key=len, reverse=True):
        if not code:
            continue
        pattern = re.escape(code) + rf"(?:$|(?![A-Za-z0-9])|(?={RE_DATE_TOKEN}))"
        for m in re.finditer(pattern, flat):
            start = m.start()
            prev = flat[start - 1] if start > 0 else ""
            before = flat[:start]
            if (not prev or not prev.isalnum()
                    or before.lower().endswith("class")
                    or before.endswith("종류")):
                return code
    return None


def _clean_num(v):
    # "9.53 %"처럼 단위를 붙여 쓰는 표가 있다. 안 떼면 숫자로 못 읽는다.
    v = (v or "").replace(",", "").replace(" ", "").rstrip("%")
    return float(v) if RE_NUM.match(v) else None


def _year_columns_all(rows):
    """'최근 N년차' 열이 있는 모든 후보 줄. [(열번호->N, 줄번호), ...]

    표 위에 막대그래프를 얹은 문서가 있는데, 그 그래프의 X축 눈금
    라벨도 "최근 N년차" 글자를 그대로 쓴다(KR5118201004 53쪽 실측).
    첫 매치만 쓰면 진짜 표 머리글보다 먼저 나오는 이 그래프 라벨 줄을
    머리글로 잘못 집어, 그 아래 진짜 (기간) 줄과 열 번호가 한 칸씩
    어긋난다 - 최근1년차 기간이 통째로 빠지고 나머지 연차는 죄다 한
    칸 앞 연차의 기간을 뒤집어쓰는 오류로 이어졌다(같은 상품 실측:
    fund 최근2년차의 period가 실제로는 최근1년차 기간이었다). 후보를
    전부 모아 두면 호출부(_parse_table)가 기간이 실제로 다 채워지는
    후보를 골라 쓸 수 있다."""
    out = []
    for i, row in enumerate(rows):
        cols = {}
        for j, cell in enumerate(row):
            m = RE_YEAR_COL.search(_squash(cell or ""))
            if m:
                cols[j] = int(m.group(1))
        if len(cols) >= 2:
            out.append((cols, i))
    if out:
        return out
    # "최근"과 "1년차"가 위아래 두 줄로 나뉜 표가 있다 - 인접한 두 줄을
    # 열 번호끼리 이어 붙여서 다시 찾는다.
    for i in range(len(rows) - 1):
        width = max(len(rows[i]), len(rows[i + 1]))
        cols = {}
        for j in range(width):
            a = rows[i][j] if j < len(rows[i]) else ""
            b = rows[i + 1][j] if j < len(rows[i + 1]) else ""
            m = RE_YEAR_COL.search(_squash(f"{a} {b}"))
            if m:
                cols[j] = int(m.group(1))
        if len(cols) >= 2:
            out.append((cols, i + 1))
    return out


def _periods(rows, header_row, cols, need=2):
    """(기간) 줄에서 열마다의 기간을 읽는다.

    시작일과 끝일을 위아래 두 줄로 나눠 싣는 표가 있어서
    ("19.12.30" / "~20.12.29") 한 줄만 보면 못 읽는다. 이어지는 줄을
    열마다 이어 붙여 본다."""
    # 시작일/구분선("~")/끝일이 각각 딴 줄로 흩어지는 표가 있다
    # (KR5139420015 실측: "기간"/시작일 줄/"~~~~~"줄/끝일 줄 - 헤더
    # 포함 5줄에 걸쳐 있다). 좁은 창이면 끝일 줄이 통째로 창 밖으로
    # 밀려나 기간을 하나도 못 읽는다.
    window = rows[header_row: header_row + 6]
    # 날짜 줄 자체에 라벨 줄("최근N년차")에는 없는 칸("설정일" 등)이
    # 하나 더 끼어들어, 그 뒤로 모든 날짜가 라벨보다 한 칸씩 밀려 있는
    # 문서가 있다(KR5118420062 46쪽 실측: "최근2년차" 라벨은 3번 칸인데
    # 그 날짜("2023.11.01~2024.10.31")는 5번 칸에 있다 - 아래 cols
    # 제한 방식으로는 5개 기간 중 2개만 건지고, need=2를 만족해 그
    # 불완전한 결과로 조용히 멈춰버린다). 값 칸(_row_values)은 칸이
    # 밀려도 "왼쪽부터 순서대로" 다시 맞추는 길이 있는데 여기엔 없었다 -
    # 같은 원칙을 기간에도 먼저 적용해본다: cols 제한 없이 표 전체에서
    # 날짜꼴을 찾아, 그 개수가 cols 칸 수와 정확히 같을 때만(그래야
    # 우연히 섞인 다른 날짜에 안 속는다) 왼쪽부터 순서대로 cols 칸에
    # 맞춰 배정한다 - 이게 성공하면 아래 칸 제한 방식보다 항상 더
    # 완전한 결과이므로 먼저 시도한다.
    # 열마다 실제로 몇 줄에 걸쳐 있는지가 서로 다른 표가 있다
    # (KR5129420031 실측: 2·3년차는 병합 칸이라 한 줄에 날짜가 통째로
    # 들어 있는데, 1·4·5년차는 시작일/물결/종료일이 진짜로 네 줄에
    # 걸쳐 있다). span을 늘려가며 "모든 칸이 채워지는 첫 조합"을 바로
    # 쓰면, 아직 다 안 이어진 중간 span에서 우연히 칸 수는 맞아도
    # 일부 칸의 날짜가 끝자리 없이 잘린 채(예: "2025/11/0") 채택될 수
    # 있다. 칸 수가 맞는 후보를 다 모아 두고, 그중 매치된 날짜 글자
    # 수 합이 가장 큰(=가장 안 잘린) 조합을 쓴다.
    best = None
    for span in (1, 2, 3, 4, 5):
        for start in range(len(window) - span + 1):
            joined = {}
            for row in window[start: start + span]:
                for j, cell in enumerate(row):
                    if (cell or "").strip():
                        joined[j] = joined.get(j, "") + " " + cell
            found = {}
            match_len = 0
            for j, text in joined.items():
                candidate = _squash_split_digits(text)
                m = RE_PERIOD.search(candidate)
                if m:
                    found[j] = f"{m.group(1)}~{m.group(2)}"
                    match_len += len(m.group(0))
            if len(found) == len(cols) and len(cols) > 2:
                if best is None or match_len > best[0]:
                    best = (match_len, found)
    if best:
        ordered_vals = [v for _, v in sorted(best[1].items())]
        return dict(zip(sorted(cols), ordered_vals))
    for span in (1, 2, 3, 4, 5):
        for start in range(len(window) - span + 1):
            joined = {}
            for row in window[start: start + span]:
                for j, cell in enumerate(row):
                    if (cell or "").strip():
                        joined[j] = joined.get(j, "") + " " + cell
            got = {}
            for j, text in joined.items():
                if j not in cols:
                    continue
                candidate = _squash_split_digits(text)
                # 날짜 앞자리나 끝자리 숫자 한두 글자가 바로 옆 칸(보통
                # 빈 칸으로 쓰이는 스페이서)으로 잘못 삐져나온 문서가
                # 있다(KR516702010M 실측: "최근 2년차" 줄의 시작연도
                # "23"이 앞 칸엔 "2", 이 칸엔 "3.05.21 ~ 24.05.20"으로
                # 갈라지고, "최근5년차" 줄의 끝날짜 "21.05.20"은 이 칸엔
                # "21.05.2", 뒤 칸엔 "0"만 갈라져 찍힌다 - 뒤엣것은 정규식
                # 자체는 (일 칸이 한 자리도 허용해서) "매치"는 되지만
                # 날짜가 한 자리 잘려 틀리게 찍힌다). 옆 칸이 숫자 한두
                # 글자뿐이면 먼저 붙여서 시도하고, 안 붙였을 때보다 더
                # 길게 매치되면(진짜 잘린 숫자였다는 뜻) 그걸 쓴다.
                prev = (joined.get(j - 1, "") or "").strip()
                nxt = (joined.get(j + 1, "") or "").strip()
                ext = candidate
                if prev and len(prev) <= 2 and prev.isdigit():
                    ext = prev + ext
                if nxt and len(nxt) <= 2 and nxt.isdigit():
                    ext = ext + nxt
                m_ext = RE_PERIOD.search(ext) if ext != candidate else None
                m_plain = RE_PERIOD.search(candidate)
                m = None
                if m_ext and (not m_plain or len(m_ext.group(0)) > len(m_plain.group(0))):
                    m = m_ext
                elif m_plain:
                    m = m_plain
                if m:
                    got[j] = f"{m.group(1)}~{m.group(2)}"
            if len(got) >= need:
                return got
    # 라벨("최근N년차")과 날짜가 칸 하나씩 밀려 있는데, 뒤쪽 년차는
    # 문서 자체에 값이 없어(설정한 지 얼마 안 된 펀드라 5년차까지 없는
    # 경우 등) 위 두 시도가 다 실패하는 표가 있다(KR5169950018 실측:
    # "최근1년차"는 2번 칸인데 날짜("24.07.12"+"~25.07.11", 두 줄로
    # 나뉨)는 3번 칸 - 첫 시도는 날짜 칸 수(2개)가 라벨 칸 수(5개)와
    # 안 맞아서, 두 번째 시도는 cols={2,5,8,9,10}에 3·6이 없어서 둘 다
    # 못 건진다). cols 제한 없이 표에서 찾은 날짜를, 있는 만큼만
    # 왼쪽부터 순서대로 라벨에 배정한다 - "값 칸은 밀려도 순서로
    # 맞춘다"는 이 파일의 기존 원칙(_row_values)과 같다. 앞의 두
    # 시도가 이미 실패한 뒤에만 쓰는 마지막 수단이라, 우연히 다른
    # 날짜가 섞여도 need(최소 개수)를 못 채우면 그냥 실패로 끝난다.
    best = None
    for span in (1, 2, 3, 4, 5):
        for start in range(len(window) - span + 1):
            joined = {}
            for row in window[start: start + span]:
                for j, cell in enumerate(row):
                    if (cell or "").strip():
                        joined[j] = joined.get(j, "") + " " + cell
            found = {}
            match_len = 0
            for j, text in joined.items():
                candidate = _squash_split_digits(text)
                m = RE_PERIOD.search(candidate)
                if m:
                    found[j] = f"{m.group(1)}~{m.group(2)}"
                    match_len += len(m.group(0))
            if need <= len(found) < len(cols):
                if best is None or match_len > best[0]:
                    best = (match_len, found)
    if best:
        ordered_vals = [v for _, v in sorted(best[1].items())]
        return dict(zip(sorted(cols)[:len(ordered_vals)], ordered_vals))
    return {}


def _is_yearly(periods):
    """연도별 표인가. 연평균 표는 기간이 모두 같은 날 끝난다."""
    ends = {p.split("~")[1] for p in periods.values()}
    return len(ends) > 1


def _row_label(cells, known_codes=()):
    """행 앞머리에서 클래스 코드나 '투자신탁'/'비교지수'를 읽는다."""
    head = ""
    for cell in cells:
        if (cell or "").strip():
            head = cell
            break
    flat = _squash(head)
    if any(b in flat for b in BENCHMARK):
        return "benchmark", None
    # known_codes를 안 넘기면 _parse가 "(국공채)"처럼 클래스 코드가
    # 아닌 괄호 낱말과 진짜 코드("(C)")를 못 가려, 클래스 행 전체가
    # 이름 조각만 보고 fund로 잘못 분류된다(KR5113420012 실측: 클래스
    # 행("...(국공채)(C)")까지 전부 fund가 됐다).
    got = _parse_row(cells, known_codes)
    if len(got) == 1:
        return "class_return", next(iter(got))
    if not got:
        m = RE_TRAILING_CODE.search(flat)
        if m and m.group(1) in known_codes:
            return "class_return", m.group(1)
    if not got:
        # WHOLE_FUND 낱말("투자신탁"/"펀드" 등)이 상품 정식명칭 조각으로
        # 라벨에 섞여 있어도, 라벨 맨 끝이 아는 클래스 코드로 끝나면
        # 클래스 행이다 - 이걸 WHOLE_FUND 판정보다 먼저 봐야 한다(안
        # 그러면 "펀드ClassA2"/"...투자신탁[채권]ClassC-P(연금)" 같은
        # 줄이 라벨 속 "펀드"/"투자신탁" 낱말 때문에 통째로 fund로
        # 잘못 분류돼 코드별 클래스 행이 대량으로 사라진다 - KR5111420047
        # 51쪽 실측: 11개 클래스 55건이 이렇게 사라졌었다).
        code = _suffix_class_code(flat, known_codes)
        if code:
            return "class_return", code
    if any(w in flat for w in WHOLE_FUND) and not got:
        return "fund", None
    if not got:
        m = RE_BARE_CODE_LABEL.match(flat)
        if m and m.group(1) in known_codes:
            return "class_return", m.group(1)
    return None, None


def _looks_like_value(c):
    c = (c or "").strip()
    if not c:
        return False
    return bool(RE_TILDE_ONLY.match(c)) or _clean_num(c) is not None


def _row_values(row, cols, periods):
    """한 행에서 년차 열마다의 값을 읽는다. {year_rank: (return_pct, period)}

    (_parse_table 본문에서 쓰던 로직을 그대로 뽑아냈다 - 페이지 경계에서
    라벨과 값이 갈린 행을 나중에 따로 다시 짝지을 때도(_recover_split_row)
    똑같은 정렬 로직이 필요해서다.)"""
    label_idx = next((i for i, c in enumerate(row) if (c or "").strip()), 0)
    val_start = next(
        (i for i in range(label_idx + 1, len(row)) if _looks_like_value(row[i])),
        label_idx + 1,
    )
    ranks_sorted = [r for _j, r in sorted(cols.items())]
    periods_sorted = [periods.get(j) for j, _r in sorted(cols.items())]
    vals = {}
    packed = [c for c in row[val_start:] if _looks_like_value(c)]
    if len(packed) == len(cols):
        for rank, pd, cell in zip(ranks_sorted, periods_sorted, packed):
            v = _clean_num(cell)
            if v is not None:
                vals[rank] = (v, pd)
    elif 0 < len(packed) < len(cols):
        # 물려받은 칸 번호(cols)가 앞 페이지의 성긴 칸 나누기 그대로인데,
        # 이 조각은 아예 촘촘한 칸으로 다시 짜인 문서가 있다(KR5185450009
        # 실측: 45쪽 머리글은 16칸짜리 성긴 표인데 46쪽은 "라벨,설정일,
        # 값1..값5" 7칸짜리 촘촘한 표로 이어진다 - 최근에 설정된 클래스는
        # 마지막(5년차) 값이 아예 없어 값이 4개뿐이기까지 하다). 이럴 때
        # 어긋난 양(shift)을 한 값으로 억지로 맞추면 칸 하나가 통째로
        # 밀려 엉뚱한 연차에 값이 들어간다(실측: 최근2년차 칸에 최근4년차
        # 값이 들어갔다). 값이 촘촘하게 이어지면서 칸 수만 모자랄 때는
        # 최근 연차(1년차)부터 순서대로 채워진 것으로 본다 - 최근에 설정된
        # 클래스일수록 오래된(옛) 연차 값이 없는 것이지, 중간 연차만 빠지는
        # 경우는 이 말뭉치에 없다("-"로 표시되는 진짜 결측은 이미 값
        # 칸으로 세어져 있어 이 분기에 안 걸린다).
        for rank, pd, cell in zip(ranks_sorted[:len(packed)],
                                   periods_sorted[:len(packed)], packed):
            v = _clean_num(cell)
            if v is not None:
                vals[rank] = (v, pd)
    else:
        shift = val_start - min(cols)
        for j, rank in sorted(cols.items()):
            jj = j + shift
            if jj < len(row):
                v = _clean_num(row[jj])
                if v is not None:
                    vals[rank] = (v, periods.get(j))
    return vals


def _recover_split_row(prev_rows, cur_rows, known_codes, state):
    """표가 페이지 경계에서 갈릴 때, 클래스 라벨 글자 자체가 앞 페이지
    끝 줄(값과 함께)과 뒤 페이지 첫 줄(라벨 나머지만)로 쪼개지는 문서가
    있다(KR5157420003 실측: "마이다스 우량채권 증권"+값이 48쪽 마지막
    줄, "자투자신탁 제1호(채권)A"가 49쪽 첫 줄로 갈린다 - 앞 줄은 라벨이
    안 끝나 코드를 못 읽어 값과 함께 통째로 버려지고, 뒤 줄은 코드는
    읽히는데 자기 자신은 값이 없어 역시 버려진다 - 이 상품 한 곳에서만
    클래스 14개, 70여 건이 이렇게 사라졌었다). 앞 페이지 마지막 줄이
    코드를 못 읽었는데 값은 있고, 뒤 페이지 첫 줄이 코드는 읽히는데
    값이 없으면 - 코드는 뒤 줄에서, 값은 앞 줄에서 가져와 짝짓는다."""
    if not prev_rows or not cur_rows or not state:
        return None
    cols, periods, confirmed = state
    if not confirmed or not cols:
        return None
    last_row, first_row = prev_rows[-1], cur_rows[0]
    kind, _code = _row_label(last_row, known_codes)
    if kind is not None:
        return None
    fkind, fcode = _row_label(first_row, known_codes)
    if fkind != "class_return" or not fcode:
        return None
    if _row_values(first_row, cols, periods):
        return None  # 뒤 줄 자체에 값이 있으면 원래 로직이 이미 처리한다
    vals = _row_values(last_row, cols, periods)
    if not vals:
        return None
    return fcode, vals


def _merge_unresolved_label_rows(rows, known_codes):
    """한 표 안에서, 값이 있는 줄의 라벨이 코드를 못 읽을 만큼 짧게
    잘리고 그 뒤로 라벨 글자만(값 없이) 있는 줄이 이어지면 하나로
    합친다(KR5157420003 48쪽 실측: 테두리 표에서 pdfplumber가 줄바꿈마다
    표 행을 따로 잘라, "마이다스 우량채권 증권"(값 포함) / "자투자신탁"
    (라벨만) / "제1호(채권)(운용)"(라벨만) 석 줄로 갈렸다 - "투자신탁"/
    "(운용)" 낱말이 셋째 줄에야 나와서, 값이 있는 첫 줄만 봐서는
    fund도 class_return도 못 읽고 통째로 버려진다). 다음 줄들을
    하나씩 이어 붙여 보다가 라벨이 실제로 읽히는 지점에서 멈춘다 -
    끝까지 이어도 안 읽히면 원래 줄 그대로 둔다(괜히 무관한 다음
    행까지 잘못 삼키지 않도록)."""
    out = []
    i, n = 0, len(rows)
    while i < n:
        row = rows[i]
        kind, _code = _row_label(row, known_codes)
        has_val = any(_looks_like_value(c) for c in row)
        label_idx = next((k for k, c in enumerate(row) if (c or "").strip()), None)
        if kind is None and has_val and label_idx is not None:
            # 뒤이은 라벨 전용 줄을 값 줄이 나올 때까지 일단 다 모은
            # 뒤에 한 번에 판정한다 - 한 줄씩 이어 붙일 때마다 판정하면
            # "투자신탁" 같은 WHOLE_FUND 낱말이 중간에 걸려("자투자신탁"
            # 만 붙인 시점에 벌써 fund로 풀려버린다) 진짜 클래스 코드가
            # 든 마지막 조각(KR5157420003 실측: "...Ae")까지 못 가고
            # 일찍 멈춰 fund로 잘못 굳어진다.
            parts = [row[label_idx]]
            j = i + 1
            while j < n:
                nxt = rows[j]
                nxt_label_idx = next(
                    (k for k, c in enumerate(nxt) if (c or "").strip()), None)
                if nxt_label_idx is None or any(_looks_like_value(c) for c in nxt):
                    break
                parts.append(nxt[nxt_label_idx])
                j += 1
            resolved_row, consumed = None, 0
            if len(parts) > 1:
                trial = list(row)
                trial[label_idx] = " ".join(parts)
                if _row_label(trial, known_codes)[0] is not None:
                    resolved_row, consumed = trial, j - i
            if resolved_row is not None:
                out.append(resolved_row)
                i += consumed
                continue
        out.append(row)
        i += 1
    return out


def _parse_table(rows, page_text="", known_codes=(), inherited=None):
    """표 하나를 읽는다.

    클래스 수가 많은 표는 페이지(또는 표) 경계에서 끊겨 이어지는데,
    이어지는 조각엔 "최근 N년차" 헤더가 다시 안 찍히는 문서가 있다
    (KR5129420025 실측: "나.연도별" 표가 63쪽에서 시작해 64쪽으로
    이어지는데 64쪽 조각엔 헤더가 없다 - CG/C-Pe/C-RPe/A-u 네 클래스가
    통째로 빠졌었다). 그런 조각은 이 표 혼자서는 cols를 못 찾으므로,
    바로 앞 조각에서 물려받은 (cols, periods)를 대신 쓴다.

    반환값은 (records, (cols, periods, confirmed)) - 다음 조각에 물려줄
    상태를 같이 돌려준다. confirmed는 이 (cols, periods)가 "나" 표라고
    확실히 확인된 것인지 표시한다 - 다음 조각이 이걸 물려받을 때(아래
    elif inherited) 확인 안 된 상태까지 이어받으면 "가" 표 머리글
    조각이 "나" 표로 오인된 채로 뒤이은 진짜 "가" 데이터 표까지
    오염시킨다(KR5113420069 실측: 각주 설명문 때문에 텍스트 확인이
    속아 confirmed=False로 남아야 할 상태가 그대로 물려져 클래스
    전체가 연도별 값으로 잘못 채워졌었다). 못 찾았고 물려받은 것도
    없으면 (None, None)."""
    candidates = _year_columns_all(rows)
    cols, hdr = candidates[0] if candidates else ({}, -1)
    if len(candidates) > 1:
        # 후보가 여럿이면(그래프 축 라벨 등 가짜 머리글이 섞였을 수
        # 있다) 기간이 모든 열에 다 채워지는 후보를 고른다 - 그런
        # 후보가 하나도 없으면 그냥 첫 후보를 쓴다(원래 동작 유지 -
        # 표 자체에 날짜가 아예 없는 문서는 아래 RE_NA_TITLE 폴백으로
        # 넘어간다).
        for c_cols, c_hdr in candidates:
            full = _periods(rows, c_hdr, c_cols, need=len(c_cols))
            if len(full) == len(c_cols):
                cols, hdr = c_cols, c_hdr
                break
    if cols:
        periods = _periods(rows, hdr, cols) or _periods(rows, hdr, cols, need=1)
        if not periods:
            # 표 자체에 기간(날짜) 줄이 아예 없는 문서가 있다(예: KR5127420034
            # p36 - "최근 1년차"라고만 쓰고 "24.01.01~24.12.31" 같은 날짜를
            # 안 싣는다). 날짜로 가릴 수 없으니 바로 위 "나. 연도별 수익률
            # 추이"라는 절 제목이 이 페이지에 실제로 있는지로만 가린다 -
            # "가. 연평균 수익률" 표는 이 제목 문구를 쓰지 않는다. 단순
            # 부분일치("연도별 수익률"이 어디든 있으면 통과)로는 각주
            # 설명문에도 이 문구가 그냥 나와서 속으므로, 진짜 절 제목
            # 형태(RE_NA_TITLE)로만 확인한다.
            if not RE_NA_TITLE.search(_squash(page_text)):
                return None, None
        elif not _is_yearly(periods):
            # 기간이 하나뿐이면 끝나는 날을 견줄 수가 없다(설정한 지 얼마 안 된
            # 펀드는 1년차만 있고 나머지가 "-"다). 그럴 땐 표 제목에 기대서
            # 연도별 표인지 가린다.
            flat = _squash(" ".join((x or "") for r in rows for x in r))
            if len(periods) > 1 or not RE_NA_TITLE.search(flat):
                return None, None
        start = hdr + 1
        confirmed = True
    elif inherited:
        cols, periods, inh_confirmed = inherited
        if not inh_confirmed:
            return None, None
        start = 0
        confirmed = True
    else:
        return None, None

    out = []
    for row in rows[start:]:
        kind, code = _row_label(row, known_codes)
        if not kind:
            continue
        # 헤더 열 번호(cols)는 헤더가 찍힌 조각의 칸 나누기를 기준으로
        # 잡힌다. 그런데 표가 페이지를 넘어가면서 이어지는 조각은(또는
        # 같은 조각 안에서도) 칸 나누기 자체가 다른 문서가 있다 - 라벨
        # 앞에 빈 칸이 하나 더 끼거나(KR510902777M), 헤더는 병합 칸이
        # 섞여 훨씬 넓은데 데이터 행은 촘촘하거나(KR5113420069 "종류
        # 모"/"...(C-F)" - 표 하나 안에서 헤더 줄과 데이터 줄의 칸
        # 나누기 자체가 한 칸 어긋나 있다), 라벨과 값 사이에 "최초설정일"
        # 칸이 하나 더 끼기도 한다(KR5125450070 CG클래스 - "라벨,설정일,
        # 값1..값5" 순인데 물려받은 칸 번호는 이 설정일 칸을 모른다).
        #
        # 공통된 실마리는 "년차 수(len(cols))만큼 값 칸이 촘촘하게 이어
        # 지는 지점이 어디서 시작하는가"이다. 그 시작점(val_start)을
        # "라벨 다음에 나오는, 값처럼 생긴(숫자로 읽히거나 "-"류 결측
        # 표시인) 첫 칸"으로 찾는다 - 라벨/설정일처럼 숫자도 "-"도 아닌
        # 칸은 건너뛴다. 거기서부터 끝까지의 칸 수가 정확히 년차 수와
        # 같으면 그 구간이 촘촘한 값 구간이라고 보고 왼쪽부터 순서대로
        # (결측 "-"도 제자리를 지킨 채) 년차에 맞춘다. 아니면(그 조각
        # 자신이 헤더를 낸 넓은 조각이거나 넓은 칸 나누기를 그대로 물려
        # 받은 조각) val_start가 cols의 첫 칸(min(cols))과 몇 칸
        # 어긋나 있는지로 어긋난 양을 재서 물려받은 칸 번호를 그만큼
        # 옮겨 읽는다.
        #
        # (예전에는 "칸 수가 안 맞으면 값이 있는 칸만 모아 왼쪽부터
        # 순서대로 다시 맞춘다"는 방식을 숫자로만 걸러서 썼는데, 이러면
        # 일부 년차만 값이 있고 나머지가 "-"인 정상 결측 행(KR5153420105
        # I클래스 실측: "- - 1.36 - -"는 3년차만 있는 게 정상)에서 "-"가
        # 숫자가 아니라며 같이 걸러지고 남은 값 하나가 무조건 1년차로
        # 배정되는 오류가 있었다. "-"도 값 칸으로 쳐서(_looks_like_value가
        # 이미 그렇게 본다) 빈 칸("")만 걸러내면, "- - 1.36 - -"는 칸
        # 수가 그대로 5개로 남아 자리를 지키면서 걸러지므로 이 문제 없이
        # 빈 칸만 안전하게 없앨 수 있다 - 그러면 값 칸 사이사이에 병합
        # 때문에 생긴 빈 칸이 끼어 있어도(KR5131420025 실측: 헤더 칸
        # 나누기와 값 칸 나누기가 서로 다른 위치에 빈 칸을 넣어, 값
        # 칸까지 밀려 최근2년차 전체가 통째로 빠졌었다) 왼쪽부터 순서
        # 대로 정확히 맞는다.
        vals = _row_values(row, cols, periods)

        for rank, (val, period) in sorted(vals.items()):
            out.append({
                "row_kind": kind,
                "class_code": code,
                "year_rank": rank,
                "period": period,
                "return_pct": val,
            })
    return (out or None), (cols, periods, confirmed)


# ---------------------------------------------------------------------------
# 테두리 없는 표 - 좌표 기반 폴백
#
# structured_store.db의 tables 테이블은 extract_tables.py가
# page.extract_tables()(표 테두리선 기반)로 미리 만들어 둔 것이다. 그런데
# "가"/"나" 표를 테두리선 없이 찍는 문서가 있다(KR5111420047 51쪽 실측:
# find_tables()가 0개를 돌려줌). 그러면 이 표가 tables에 아예 없어서
# 상품 전체가 통째로 빠진다(10개 상품 실측). 그런 페이지만 낱말 좌표로
# 격자를 다시 만들어 pdfplumber 표와 같은 모양(rows[i][j] 문자열)으로
# 돌려주면, 위 _parse_table을 그대로 재사용할 수 있다 - "가"/"나" 판별,
# 연도 열 찾기, 절 제목 확인 등 안전장치를 다시 만들 필요가 없다.
# ---------------------------------------------------------------------------

RE_TILDE_ONLY = re.compile(r"^[~∼〜～\-]+$")
RE_PLAIN_NUM = re.compile(r"^-?\d+\.\d+$")
RE_YEAR_PREFIX = re.compile(r"^최근$")


def _coord_table_rows(page, line_gap=4, val_tol=10, label_gap=30):
    """페이지 전체 낱말을 줄(y)·칸(x)으로 묶어 표 격자를 만든다.

    페이지 전체 낱말의 x0을 한 묶음으로 놓고 가까운 값끼리 사슬처럼
    묶으면(단순 인접 거리 기준) 클래스명 칸의 자잘한 글자 조각들
    (KR5111420047 실측: "펀드ClassA" 근처에 1~2pt 간격 x0가 줄줄이
    있음) 사이에서 기준점이 계속 밀려, 정작 뒤쪽 "최근"/"1년차"처럼
    떨어져 있어야 정상인 값 칸끼리도 엉뚱하게 갈린다. 값 칸(보수율
    숫자)의 x좌표는 문서 전체에서 아주 정확히 반복되므로, 그 숫자들의
    x0만 따로 모아 칸 기준을 잡는다 - 클래스명 등 나머지 글자는 그
    값 칸들보다 왼쪽에 있다는 것만 이용해 통째로 0번 칸에 몰아넣는다
    (더 잘게 가를 필요가 없다 - _row_label은 0번 칸 글자 하나만 본다).
    "최근"은 항상 바로 다음 낱말(N년차)과 한 칸이어야 하므로, 값 칸
    기준과 무관하게 무조건 다음 낱말에 붙인다. "~"도 같은 이유로
    바로 앞 낱말에 붙인다(테두리 표라면 앞 칸 글자와 한 셀이었을
    글자이기 때문).

    엉뚱한 페이지(본문 문단 등)에 걸어도 안전하다 - 여기서 걸러내지
    않고 그대로 _parse_table에 넘기며, 그쪽의 연도 열 정규식·절 제목
    확인이 실제 표가 아니면 알아서 None을 돌려준다."""
    words = pdf_words.extract_words(page)
    if not words:
        return []
    words.sort(key=lambda w: w["top"])
    lines, cur, prev_top = [], [], None
    for w in words:
        if prev_top is not None and w["top"] - prev_top > line_gap:
            lines.append(cur)
            cur = []
        cur.append(w)
        prev_top = w["top"]
    if cur:
        lines.append(cur)

    val_x0s = sorted({round(w["x0"], 1) for line in lines for w in line
                       if RE_PLAIN_NUM.match(w["text"])})
    merged = []
    for x in val_x0s:
        if merged and x - merged[-1] <= val_tol:
            continue
        merged.append(x)
    val_x0s = merged
    if not val_x0s:
        return []
    label_boundary = val_x0s[0] - label_gap

    def col_of(x0):
        if x0 < label_boundary:
            return 0
        return 1 + min(range(len(val_x0s)), key=lambda k: abs(val_x0s[k] - x0))

    rows = []
    for line in lines:
        line = sorted(line, key=lambda w: w["x0"])
        cells, last_ci, last_x1 = {}, None, None
        pending_prefix, last_was_tilde = False, False
        for w in line:
            is_tilde = bool(RE_TILDE_ONLY.match(w["text"]))
            # "~"만 연달아 한 줄에 있는 표가 있다(KR5139420015 실측:
            # 기간 줄이 "시작일"/"~~~~~"/"끝일" 석 줄로 나뉘어, 물결표
            # 줄은 앞에 붙을 날짜가 같은 줄에 없다). 그런 물결표는
            # "바로 앞 낱말"이 아니라 원래 제 칸(col_of)에 그대로
            # 둔다 - 안 그러면 뒤 물결표들이 전부 첫 물결표 칸으로
            # 쓸려간다. 바로 앞 낱말이 물결표가 아닌 진짜 글자일 때만
            # (KR5111420047류처럼 "2024/04/17 ~"가 한 줄에 붙어 있을
            # 때만) 그 앞 낱말 칸에 붙인다.
            #
            # "붙인다"의 기준은 "바로 앞"이 아니라 "얼마나 가까운가"다 -
            # 끝일을 "~21.09.14"처럼 물결표가 날짜 앞에 붙는 문서가
            # 있는데(KR5144420091 실측: "~ 25.09.14  ~ 24.09.14  ~
            # 23.09.14 ..."), 이런 문서는 다음 칸 물결표가 이전 낱말
            # 바로 뒤에 (넓은 간격을 두고) 나온다. 간격을 안 보고
            # 무조건 "바로 앞 낱말"에 붙이면 두 번째 물결표부터 전부
            # 앞 칸(1년차)에 쓸려가 그 칸만 물결표를 두 개 먹고 나머지
            # 칸은 죄다 물결표를 하나씩 잃는다 - 그러면 뒤 칸들의 기간이
            # 전부 앞 칸 것과 뒤섞여 한 칸씩 밀린다(같은 상품 실측:
            # 최근2년차 period가 실제로는 최근1년차 기간으로 나왔었다).
            # 간격이 좁을 때만(같은 시각 뭉치, 보통 3pt 안팎) 붙이고,
            # 넓으면(다음 칸이 새로 시작하는 뭉치, 보통 20pt 이상) 제
            # 칸(col_of)을 그대로 쓴다.
            close_to_prev = (
                last_x1 is not None and w["x0"] - last_x1 <= 10)
            if is_tilde and last_ci is not None and not last_was_tilde and close_to_prev:
                ci = last_ci
            elif pending_prefix:
                ci = last_ci
                pending_prefix = False
            else:
                ci = col_of(w["x0"])
            cells[ci] = (cells.get(ci, "") + " " + w["text"]).strip()
            last_ci = ci
            last_x1 = w["x1"]
            last_was_tilde = is_tilde
            pending_prefix = bool(RE_YEAR_PREFIX.match(w["text"]))
        rows.append([cells.get(ci, "") for ci in range(len(val_x0s) + 1)])
    return _merge_split_label_rows(rows)


def _merge_split_label_rows(rows):
    """라벨이 값 줄 위·아래로 두 줄에 걸쳐 있으면 값 줄과 하나로 합친다
    (KR5139420015 44쪽 실측: "흥국멀티크레딧증권" 줄 / (숫자만 있는
    줄) / "자투자신탁[채권]" 줄 - 값 행을 세로 가운데 정렬해서 긴
    라벨을 감싼다). 합치지 않으면 라벨 조각 어디에도 클래스 코드
    전체("(C)" 등)가 없어 _row_label이 코드를 못 읽고 값은 값대로
    라벨 없는 행에 남아 둘 다 버려진다."""
    # 값 행 하나가 그 자체로 라벨 조각을 이미 하나 담고 있는 문서도
    # 있다(KR5113420012 실측: "한국투자골드플랜연금"(라벨만) / "증권
    # 전환형투자신탁1 5.93 4.86 ..."(라벨 조각+값) / "호(국공채)(모)"
    # (라벨만) - 3줄에 걸친 정식명칭 한가운데 줄에 값이 얹혀 있다).
    # 그래서 "값 행"을 0번 칸이 비어 있는 경우로만 좁히면 안 되고,
    # 값이 있는 행이면 0번 칸에 무엇이 있든 대상으로 삼는다.
    def has_values(r):
        return any(RE_PLAIN_NUM.match((c or "").strip()) for c in r[1:])

    def is_label_only(r):
        return bool((r[0] or "").strip()) and not has_values(r)

    out = []
    i, n = 0, len(rows)
    while i < n:
        r = rows[i]
        if has_values(r):
            own = (r[0] or "").strip()
            # "비교지수"는 그 자체로 완결된 라벨이다 - 뒤이어 나오는
            # 다음 클래스의 라벨 조각("한국투자골드플랜연금" 등)을
            # 잘못 끌어와 붙이면 안 된다. 펀드 정식명칭은 "투자신탁"을
            # 마디 하나에 포함한 채로도 뒤가 더 이어질 수 있어
            # (예: "증권전환형투자신탁1" 다음 줄에 "호(국공채)(모)")
            # WHOLE_FUND 낱말 포함 여부로는 못 가린다 - "비교지수"
            # 정확히 일치할 때만 완결로 본다.
            is_complete = own in BENCHMARK
            parts = []
            while out and is_label_only(out[-1]):
                parts.insert(0, out.pop()[0])
            if own:
                parts.append(own)
            j = i + 1
            if not is_complete:
                # 라벨이 두 줄을 넘어 세 줄 이상으로 쪼개지는 문서도
                # 있다(KR5139420015 실측: "수수료미징구"/"-오프라인"
                # /"-기관,펀드 등(C-f)" 석 줄) - 앞뒤로 라벨 조각이
                # 이어지는 한 계속 모은다.
                #
                # 그런데 값이 전부 "-"(결측)인 클래스가 바로 다음 줄에
                # 있으면(값이 숫자가 아니라 has_values가 False라 "라벨
                # 조각"으로 오인된다) 그 클래스의 라벨 전체를 앞 클래스의
                # 라벨 계속으로 잘못 삼켜버린다(KR5153420079 44쪽 실측:
                # C-I 값 행 뒤에 "종류C-PI\n...퇴직연금, 펀드\n등"이
                # 통째로 삼켜져, 합쳐진 라벨에서 _row_label이 "C-PI"를
                # 골라 C-I의 진짜 값이 있지도 않은 C-PI 것으로 둔갑했다 -
                # C-PI 자신은 정말 결측이라 값 행이 하나도 안 남는다).
                # "종류"로 시작하는 줄은 이 표기에서 언제나 새 클래스의
                # 시작이라 계속 이어붙이면 안 된다 - 여기서 멈추면 그
                # 줄은 자기 차례에 라벨만 있는 행으로 그대로 남아 결측
                # 클래스로 정확히 처리된다.
                while j < n and is_label_only(rows[j]) \
                        and not (rows[j][0] or "").strip().startswith("종류"):
                    parts.append(rows[j][0])
                    j += 1
            if len(parts) > (1 if own else 0):
                out.append([" ".join(parts)] + list(r[1:]))
                i = j
                continue
        out.append(r)
        i += 1
    return out


# 클래스마다 표 하나를 통째로 페이지 하나에 따로 싣는 문서가 있다
# (KR5114420016 실측: "가/나" 표가 전 클래스를 한 표에 쌓아 보여주는
# 다른 문서들과 달리, 클래스마다 "(N) 펀드이름_코드(설명)" 제목이 붙은
# 자기 페이지가 따로 있다 - 삼성자산운용 계열 문서 다수(같은 계열 6개
# 상품 실측)가 이 형식을 쓴다). 위 _row_label/_year_columns_all은 "한
# 표 안에 클래스가 여러 줄"이라고 가정해서 이 형식은 아예 못 읽어
# 상품 전체가 통째로 빠진다. 다만 표 자체는 테두리가 있어 structured
# _store.db의 tables에 깨끗하게 잡히므로(제목 표 하나 + 값 표 하나,
# 연속 table_index) 별도 좌표 보정 없이 바로 읽을 수 있다.
# 코드가 "_코드(설명)"으로 붙거나(밑줄 뒤에 바로, 자기 괄호 없이),
# "[분류](코드)(설명)"으로 자기 괄호에 따로 싸여 붙는 두 표기가 다
# 있다(KR5114420016 실측: "..._R-Ae(수수료선취-온라인)" 대 "...[채권]
# (A)(수수료미징구...)").

# 퇴직연금 클래스는 코드 자체에 한글 괄호가 덧붙는 문서가 있다
# (class_fees.json 실측: "Cp(퇴직연금)", "Cp-f(퇴직연금)" - 코드
# 표기 자체가 이 한글 괄호까지 포함한다). 제목 줄의 코드 뒤에도
# 똑같이 이 한글 괄호가 붙어 나오므로 같이 잡아야 known_codes와
# 맞는다.
_CODE_BODY = r"[A-Za-z0-9][A-Za-z0-9\-]{0,8}(?:\([가-힣]+\))?"
RE_PAGE_TITLE_CODE = re.compile(
    r"(?:_(" + _CODE_BODY + r")\("
    r"|\]\((" + _CODE_BODY + r")\)\()")


def _per_class_page_records(doc_id, conn, known_codes):
    """클래스당 페이지 하나짜리 문서에서 "나. 연도별 수익률" 값을 읽는다.
    "가.연평균" 페이지(기간에 "년차"가 안 붙음)는 이 파일의 대상이
    아니므로 거른다."""
    out = []
    pending_code, pending_page = None, None
    for page, dj in conn.execute(
            "SELECT page, data_json FROM tables WHERE doc_id = ? "
            "ORDER BY page, table_index", (doc_id,)):
        try:
            rows = json.loads(dj)
        except (ValueError, TypeError):
            continue
        if not rows:
            continue
        first_cell = (rows[0][0] or "").strip() if rows[0] else ""
        # 제목 표: [('(N)', ''), ('', '펀드이름_코드(설명)')] 두 줄짜리.
        if re.fullmatch(r"\(\d+\)", first_cell):
            title_text = " ".join(
                c for r in rows for c in r if (c or "").strip())
            m = RE_PAGE_TITLE_CODE.search(title_text)
            code = (m.group(1) or m.group(2)) if m else None
            # 명칭표에 없는 표기는 이 표 자신의 표기 차이로 보지 않고
            # (다른 상품과 섞일 위험) 그냥 버린다 - known_codes로 검증된
            # 코드만 받는다. 코드가 아예 없는 제목("(1) 펀드이름")은
            # 클래스 구분 없는 펀드 전체 페이지이므로 code=None 그대로
            # 둔다(이 함수는 class_return만 다루므로 이 페이지의 값은
            # 안 쓴다 - 펀드 전체 값은 다른 경로가 이미 다룬다).
            pending_code = code if (code and code in known_codes) else None
            pending_page = page
            continue
        if pending_page != page or pending_code is None:
            pending_code, pending_page = None, None
            continue
        header = next((r for r in rows if (r[0] or "").strip() == "연도"), None)
        own_row = next((r for r in rows if (r[0] or "").strip() == "투자신탁"), None)
        if not header or not own_row:
            pending_code, pending_page = None, None
            continue
        if not any("년차" in (c or "") for c in header):
            pending_code, pending_page = None, None
            continue  # "가.연평균" 페이지 - 이 함수는 "나" 표만 다룬다
        date_row = None
        hi = rows.index(header)
        if hi + 1 < len(rows) and (rows[hi + 1][0] or "").strip() == "":
            date_row = rows[hi + 1]
        for ci in range(2, len(own_row)):
            cell = (own_row[ci] or "").strip()
            m = re.search(r"-?\d+\.\d+", cell)
            if not m:
                continue
            rank = ci - 1
            period = None
            if date_row and ci < len(date_row):
                dm = re.findall(r"\d{2}[./]\d{2}[./]\d{2}", date_row[ci] or "")
                if len(dm) == 2:
                    period = f"{dm[0]}~{dm[1]}"
            out.append({
                "row_kind": "class_return", "class_code": pending_code,
                "year_rank": rank, "period": period,
                "return_pct": float(m.group()), "page": page,
            })
        pending_code, pending_page = None, None
    return out


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        seen = set()
        page_texts = {}
        known_codes = {r[0] for r in conn.execute(
            "SELECT DISTINCT class_code FROM class_fees "
            "WHERE product_code = ? AND class_code IS NOT NULL", (code,))}
        # 표가 헤더 없는 조각으로 이어지는 경우를 잇기 위해, 바로 앞/같은
        # 쪽 표에서 찾은 (cols, periods)를 물려준다(_parse_table 참고).
        # 물려줄 게 실제로 쓰였을 때만(rows가 나왔거나, 이번 조각 스스로
        # 새 헤더를 찾았을 때만) 이어 가고, 그 외엔 끊어서 무관한 뒤쪽
        # 표까지 잘못 번지지 않게 한다.
        inherited, prev_page, prev_rows = None, None, None
        # 표(테두리 기반) 쪽에서 페이지별로 확인된 (cols, periods)를
        # 남겨 둔다 - 아래 좌표 폴백이 자기 나름대로 칸을 다시 나누다
        # 헤더 병합 칸(예: "최근2년차 최근3년차 최근4년차 최근5년차"가
        # 좌표상 한 칸으로 뭉침)을 잘못 갈라 특정 연차의 기간(period)만
        # 못 찾는 사고를 막는다(KR5120420091 실측: 60/61쪽 경계에서
        # 빠진 "Class C-R(퇴직연금)" 행을 좌표 폴백이 되찾긴 했는데,
        # 그 폴백 자신의 헤더 인식에서 2~5년차 칸이 뭉쳐 2년차의 기간
        # 문자열을 못 뽑아 값은 있는데 period만 null로 남았다). 이미
        # 표 쪽에서 검증된 진짜 칸 구성이 있으면 그걸 최우선으로 쓴다.
        table_page_state = {}
        for page, dj in conn.execute(
                "SELECT page, data_json FROM tables WHERE doc_id = ? "
                "ORDER BY page, table_index",
                (code,)):
            try:
                rows = json.loads(dj)
            except (ValueError, TypeError):
                continue
            # 라벨이 같은 표 안에서 세 줄로 갈리는 문서가 있다(KR5157420003
            # 48쪽 실측: "마이다스 우량채권 증권"(값 포함)/"자투자신탁"
            # (라벨만)/"제1호(채권)(운용)"(라벨만) 석 줄 - pdfplumber가
            # 줄바꿈마다 표 행을 따로 잘라서, 값이 있는 첫 줄은 라벨이
            # "투자신탁"/"운용" 같은 낱말에 닿지 못해 fund/class 어느
            # 쪽으로도 못 읽혀 값째 버려진다).
            rows = _merge_unresolved_label_rows(rows, known_codes)
            if page not in page_texts:
                page_texts[page] = " ".join(
                    t for (t,) in conn.execute(
                        "SELECT text FROM chunks WHERE doc_id = ? AND page = ?",
                        (code, page)))
            carry = inherited if prev_page in (page, page - 1) else None
            recovered = _recover_split_row(prev_rows, rows, known_codes, carry)
            if recovered:
                rec_code, rec_vals = recovered
                for rank, (val, pd) in sorted(rec_vals.items()):
                    key = ("class_return", rec_code, rank)
                    if key not in seen:
                        seen.add(key)
                        out.append({
                            "row_kind": "class_return", "class_code": rec_code,
                            "year_rank": rank, "period": pd, "return_pct": val,
                            "product_code": code, "page": page,
                        })
            got, state = _parse_table(rows, page_texts[page], known_codes, carry)
            prev_page, prev_rows = page, rows
            inherited = state if (got or (state and not carry)) else None
            if inherited and inherited[2]:
                table_page_state[page] = inherited
            if not got:
                continue
            for r in got:
                key = (r["row_kind"], r["class_code"], r["year_rank"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(dict(r, product_code=code, page=page))

        # 좌표로 보강한다(_coord_table_rows 모듈 docstring 참고) - 테두리
        # 없는 표라 tables에 아예 안 잡힌 페이지뿐 아니라, tables에는
        # 있지만 위에서 하나도 못 건진 페이지도 대상이다. 테두리 표라도
        # 셀 읽기 자체가 깨지는 문서가 있다(KR5156450026 44쪽 실측:
        # extract_tables()는 표를 찾는데, 클래스명 칸과 5년차 칸이
        # 통째로 빈 칸으로 나와 모든 행의 라벨을 못 읽어 표 전체가
        # 조용히 버려졌다).
        #
        # 성공한 페이지도 다시 본다 - 표가 페이지 경계에서 갈리면
        # pdfplumber의 find_tables()가 경계에 걸친 행 하나를 앞뒤 어느
        # 쪽 표에도 안 넣고 통째로 빠뜨리는 경우가 있다(KR5120420091
        # 실측: 60쪽 표가 "...Class C-Pe(연금)" 줄에서 끝나고 61쪽
        # 표는 그다음 클래스의 비교지수 줄부터 시작해, 그 사이에
        # 있어야 할 "Class C-R(퇴직연금)" 데이터 행이 양쪽 표 어디에도
        # 없이 사라졌다 - 좌표 재추출로 61쪽 맨 위에서 되찾았다).
        # seen으로 걸러지므로 이미 잡은 행을 다시 넣지는 않는다 -
        # 새로 찾는 것만 보탠다.
        pdf_candidates = glob.glob(os.path.join(DATA_DIR, code, "*.pdf"))
        if pdf_candidates:
            with pdfplumber.open(pdf_candidates[0]) as pdf:
                inherited2, prev_page2, prev_rows2 = None, None, None
                for i, page in enumerate(pdf.pages):
                    page_num = i + 1
                    rows = _coord_table_rows(page)
                    if not rows:
                        continue
                    text = page.extract_text() or ""
                    # 표(테두리) 쪽에서 이 페이지(또는 바로 앞 페이지)
                    # 것으로 이미 확인된 진짜 칸 구성이 있으면 그걸
                    # 최우선으로 쓴다 - 좌표 재구성 자신의 헤더 인식이
                    # 병합 칸을 잘못 갈라 특정 연차만 기간을 놓치는
                    # 사고를 막는다(위 table_page_state 주석 참고).
                    carry = (table_page_state.get(page_num)
                             or table_page_state.get(page_num - 1)
                             or (inherited2
                                 if prev_page2 in (page_num, page_num - 1) else None))
                    recovered = _recover_split_row(prev_rows2, rows, known_codes, carry)
                    if recovered:
                        rec_code, rec_vals = recovered
                        for rank, (val, pd) in sorted(rec_vals.items()):
                            key = ("class_return", rec_code, rank)
                            if key not in seen:
                                seen.add(key)
                                out.append({
                                    "row_kind": "class_return", "class_code": rec_code,
                                    "year_rank": rank, "period": pd, "return_pct": val,
                                    "product_code": code, "page": page_num,
                                })
                    got, state = _parse_table(rows, text, known_codes, carry)
                    prev_page2, prev_rows2 = page_num, rows
                    inherited2 = state if (got or (state and not carry)) else None
                    if not got:
                        continue
                    for r in got:
                        key = (r["row_kind"], r["class_code"], r["year_rank"])
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(dict(r, product_code=code, page=page_num))

        # 클래스당 페이지 하나짜리 문서(_per_class_page_records 참고) -
        # 위 두 경로 다 "한 표 안에 클래스 여러 줄"을 가정해서 이 형식은
        # 통째로 놓친다. seen으로 걸러지므로 겹치면 안 쓴다.
        for r in _per_class_page_records(code, conn, known_codes):
            key = (r["row_kind"], r["class_code"], r["year_rank"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r, product_code=code))
    conn.close()
    return _drop_suffix_duplicate_codes(out)


def _drop_suffix_duplicate_codes(rows):
    """좌표 폴백(_coord_table_rows)이 줄바꿈으로 갈린 라벨("C-\\nPe형(...)")
    에서 앞쪽 접두사("C-")를 놓쳐 "Pe"만 코드로 읽는 경우가 있다
    (KR5125450023/KR5125450070 실측). 이미 테두리 표 쪽에서 "C-Pe"로
    올바르게 읽혀 있는 같은 연차·같은 수익률 값이 "Pe"라는 별도
    class_code로 한 번 더 들어가, 조회할 때 어느 쪽이 진짜인지 알 수
    없게 중복된다.

    연차 하나만 값이 같은 걸로는 못 믿는다 - "e"와 "Pe"처럼 접두사
    관계에 있는 서로 다른(진짜) 클래스가 우연히 한두 연차에서 반올림된
    값이 같을 수 있다(KR5125450023 실측: "e"클래스 2·3년차가 하필
    "Pe"클래스 2·3년차와 소수점까지 같았지만, 1·4·5년차는 다 달라서
    실제로는 서로 다른 클래스였다 - 처음엔 연차 하나만 보고 지워서
    진짜 "e"클래스 데이터 2건이 잘못 사라졌었다). 짧은 코드가 가진
    연차 전부가 긴 코드의 같은 연차와 하나도 빠짐없이 일치할 때만
    (한쪽이 다른 쪽의 부분집합이 아니라 통째로 겹칠 때만) 같은 원본
    행을 두 번 읽은 것으로 보고, 더 긴(원본에 더 가까운) 쪽만 남긴다."""
    by_code = {}
    for r in rows:
        if r["row_kind"] != "class_return":
            continue
        by_code.setdefault((r["product_code"], r["class_code"]), []).append(r)

    drop_ids = set()
    for (pc, short_code), short_rows in by_code.items():
        if not short_rows:
            continue
        for (pc2, long_code), long_rows in by_code.items():
            if pc2 != pc or long_code == short_code:
                continue
            if len(long_code) <= len(short_code) or not long_code.endswith(short_code):
                continue
            long_by_rank = {r["year_rank"]: r["return_pct"] for r in long_rows}
            if all(long_by_rank.get(r["year_rank"]) == r["return_pct"]
                   for r in short_rows):
                drop_ids.update(id(r) for r in short_rows)
                break
    return [r for r in rows if id(r) not in drop_ids]


# yearly_returns.py 자신의 표 파서가 class_fees.json(보수표 - 이
# 코퍼스의 정식 표기 출처)과 다른 표기로 클래스 코드를 읽어, 같은
# 클래스인데 조인이 끊기는 경우가 있다(실측). 대소문자·붙임표만 다른
# 표기 흔들림이 아니라 진짜 서로 다른 개인연금/퇴직연금 클래스일
# 위험(class_charges.py의 C-P vs Cp(퇴직연금) 사례 참고)이 있어
# 전체적으로 자동 정규화하지 않는다 - 옛 표기가 그 상품의
# yearly_returns 안에 이미 없어(=바꿔도 다른 진짜 클래스와 안 겹침)
# 안전이 확인된 다섯 상품만 딱 그 표기로 못박는다.
_KNOWN_CLASS_CODE_RENAMES = {
    "KR5110501016": {"Ae": "A-e"},
    "KR5123490013": {"A-e": "Ae", "C-e": "Ce"},
    "KR5123490016": {"A-e": "Ae", "C-e": "Ce"},
    "KR5123490017": {"A-e": "Ae", "C-e": "Ce"},
    "KR5125450070": {"CG": "C-G"},
}


def apply_known_class_code_renames(rows):
    renamed = 0
    for r in rows:
        m = _KNOWN_CLASS_CODE_RENAMES.get(r["product_code"])
        if not m:
            continue
        new = m.get(r.get("class_code"))
        if new:
            r["class_code"] = new
            renamed += 1
    return renamed


# 표·좌표 두 경로 다 못 채우는 극소수 period는 PDF 원문 대조로 확인해
# 둔 값을 그대로 못박는다.
#   - KR5119450058 benchmark 1년차: 5쪽 표 헤더("최근1년...2024.02.01
#     ~2025.01.31")가 같은 문서의 다른 class_return 1년차 행과 전부
#     동일한 기간이라 그대로 옮긴다.
_KNOWN_PERIOD_FIXES = {
    ("KR5119450058", "benchmark", None, 1): "2024.02.01~2025.01.31",
}

# 표·좌표 파서가 서로 다른 두 표(연평균수익률표 vs 연도별 수익률
# 추이표)의 칸을 혼동해 만들어낸 가짜 행. KR5153420318 49쪽 원문 재대조
# 결과: 연도별 수익률 추이표의 "최근2년차" 칸은 fund/benchmark 둘 다
# "-"(계산할 2년치 데이터가 없다는 뜻)인데, 파서가 옆에 있는 연평균
# 수익률표의 "설정이후" 칸 값(fund 3.89 / benchmark 4.64, 둘 다 양수)을
# 그 "-"와 붙여 "-3.89"/"-4.64"라는 실재하지 않는 음수로 잘못 만들어냈다
# (한때 이 값을 진짜로 오해해 기간만 못박았던 적이 있는데, 그 기간
# 자체도 이 잘못 이어붙은 값을 정당화하려 든 것이었다 - 값이 없는
# 자리는 다른 class_return 행들처럼 아예 행을 만들지 않는 게 맞다).
_KNOWN_FAKE_ROWS = {
    ("KR5153420318", "fund", None, 2),
    ("KR5153420318", "benchmark", None, 2),
}


def remove_known_fake_rows(rows):
    before = len(rows)
    rows[:] = [
        r for r in rows
        if (r["product_code"], r["row_kind"], r.get("class_code"),
            r["year_rank"]) not in _KNOWN_FAKE_ROWS
    ]
    return before - len(rows)


def apply_known_period_fixes(rows):
    fixed = 0
    for r in rows:
        if r.get("period") is not None:
            continue
        key = (r["product_code"], r["row_kind"], r.get("class_code"), r["year_rank"])
        period = _KNOWN_PERIOD_FIXES.get(key)
        if period:
            r["period"] = period
            fixed += 1
    return fixed


# "나. 연도별 수익률 추이" 표가 페이지 경계(55→56쪽)에서 갈리면서,
# 신설 클래스 2개의 2년차 행이 통째로 안 잡혔다(KR5144420020 실측:
# 56쪽 맨 위, 55쪽 표 바로 이어지는 자리에 "수수료미징구-오프라인-
# 퇴직연금(고액)(C-P2I(퇴직연금)) 4.21 8.38 - - -"와 "수수료선취-
# 오프라인(A2) 4.26 7.73 - - -" 두 줄이 있는데, 표·좌표 두 경로 다
# 1년차(4.21/4.26)만 건지고 2년차(8.38/7.73)는 놓쳤다 - 3~5년차는
# 원문에도 "-"라 정상적으로 행을 안 만드는 게 맞다). 페이지 자체는
# 이미 56으로 맞게 잡혀 있었다(56쪽 위쪽이 이 표의 계속이고, 그
# 아래에서야 "다. 집합투자기구의 자산 구성 현황" 절이 시작된다 -
# 애초에 페이지가 틀렸다고 오판했던 적이 있었는데, 원문을 다시 보니
# 페이지는 처음부터 맞았고 2년차 행 자체가 빠진 것이었다).
_KNOWN_MISSING_ROWS = [
    {"row_kind": "class_return", "class_code": "C-P2I(퇴직연금)", "year_rank": 2,
     "period": "23.09.02~24.09.01", "return_pct": 8.38,
     "product_code": "KR5144420020", "page": 56},
    {"row_kind": "class_return", "class_code": "A2", "year_rank": 2,
     "period": "23.09.02~24.09.01", "return_pct": 7.73,
     "product_code": "KR5144420020", "page": 56},
]


def add_known_missing_rows(rows):
    existing = {(r["product_code"], r["row_kind"], r.get("class_code"), r["year_rank"])
                for r in rows}
    added = 0
    for row in _KNOWN_MISSING_ROWS:
        key = (row["product_code"], row["row_kind"], row.get("class_code"), row["year_rank"])
        if key not in existing:
            rows.append(dict(row))
            added += 1
    return added


def report(rows):
    prods = {r["product_code"] for r in rows}
    kinds = {}
    for r in rows:
        kinds[r["row_kind"]] = kinds.get(r["row_kind"], 0) + 1
    print(f"연도별 수익률 {len(rows)}건 / 상품 {len(prods)}개")
    for k, v in sorted(kinds.items()):
        print(f"  {k}: {v}건")
    if rows:
        s = [r for r in rows if r["row_kind"] == "class_return"][:6]
        print("\n  예시:")
        for r in s:
            print(f"    {r['product_code']} {r['class_code']} "
                  f"{r['year_rank']}년차({r['period']}) {r['return_pct']}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = extract(args.db)
    apply_known_class_code_renames(rows)
    apply_known_period_fixes(rows)
    add_known_missing_rows(rows)
    remove_known_fake_rows(rows)
    report(rows)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
