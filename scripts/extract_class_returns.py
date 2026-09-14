"""
연금 Agent 과제 - 클래스별 수익률(투자실적추이) 좌표 기반 추출

products의 "투자실적추이(연평균수익률)" 표는 클래스마다 3행 구조를 쓴다:
    {클래스명}   {최초설정일}  {최근1년} {최근2년} {최근3년} {최근5년} {설정일이후}
    비교지수      -            {최근1년} {최근2년} {최근3년} {최근5년} {설정일이후}
    수익률\n변동성 {최초설정일}  {최근1년} {최근2년} {최근3년} {최근5년} {설정일이후}

총보수 표(extract_class_fees.py)와 마찬가지로 pdfplumber extract_tables()가
이 표를 셀 뭉침으로 깨뜨리는 경우가 많고(비교지수/수익률변동성 키워드 +
날짜 2개 이상 + 소수 6개 이상이 한 셀에 뭉쳐 있으면 깨진 것으로 판별),
같은 페이지 안에 정상 추출된 버전이 같이 있는 경우도 있다. 좌표 기반
재구성이 정상 페이지에도 동일하게 정확히 동작한다는 걸 총보수 표에서
검증했으므로, 여기서도 "최근"+"설정일" 언급된 페이지는 깨졌든 아니든
전부 좌표로 재구성한다.

주의: 같은 페이지에 "운용전문인력"(운용역/운용사 실적) 표가 비슷한 헤더
문구("최근1년/최근2년")를 쓰는 경우가 있어 혼동하기 쉽다 - 이 표는 클래스
행이 아니라 사람 이름 행이라 무시해야 한다. 데이터 행 판별 시 값 개수
(3~5개, 그것도 클래스 표는 %라 보통 두 자리 소수)로 최대한 걸러내되,
100% 걸러진다는 보장은 없어 evidence를 반드시 같이 남긴다.

사용법:
    python scripts/extract_class_returns.py
"""

import argparse
import glob
import json
import os
import re
from collections import defaultdict

import pdfplumber

from pdf_words import extract_words as _safe_words

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "class_returns.json")

NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")
DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")
# 아직 수익률이 없는(전액 "-") 클래스 행도 있다 (예: 설정된 지 얼마 안 된 클래스).
# 값이 없다는 사실 자체와 class_code/설정일은 여전히 의미가 있어 버리지 않는다.
DASH_RE = re.compile(r"^-+$")
# 클래스 코드는 이 말뭉치에서 절대 4자리 연도 모양이 아니다 - 기간
# 헤더 문구("2024.11.01~")가 데이터 행으로 오인될 때만 이 모양이
# 나온다(process_doc의 가짜 행 필터 참고).
RE_BOGUS_YEAR_CODE = re.compile(r"^(?:19|20)\d{2}$")
CLASS_CODE_RE = re.compile(r"\(([A-Za-z0-9\-]{1,8})\)")
# 일부 문서는 클래스명을 "(A2)"처럼 괄호로 안 감싸고 "ClassA2"처럼 그대로 붙여 쓴다
# (예: KR5120420039). 괄호 형식이 안 잡히면 이 패턴으로 한 번 더 시도한다.
# "S-P"와 "S-P(퇴직)"처럼 괄호 안 한글 접미사만 다르고 나머지 코드가 같은
# 클래스 쌍이 한 상품 안에 같이 있는 문서가 있다(KR5118201004/036/062
# 실측: "ClassS-P(퇴직) ... 3.85" / "ClassS-P ... 3.84" - 값도 설정일도
# 서로 다른 완전히 별개의 클래스인데, 접미사를 못 잡으면 "S-P(퇴직)"의
# 값이 진짜 "S-P"에 붙어버린다. 이미 "S-P"도 이 상품의 정식 코드라
# _normalize_class_code의 사후 보정도 못 걸러낸다 - "이미 아는 코드"라
# 그대로 통과해버리기 때문). 뒤에 "(한글)"이 공백 없이 바로 붙어 있으면
# 처음부터 코드에 포함해서 잡는다.
CLASS_CODE_NOPAREN_RE = re.compile(
    r"Class[- ]?([A-Za-z0-9\-]{1,6}(?:\([가-힣]+\))?)", re.IGNORECASE
)
# 괄호도 "Class"도 없이 그냥 "종류A", "종류C4"처럼 쓰는 문서도 있다(제3부
# "3.집합투자기구의 운용실적" 섹션에서 확인 - KR510902511M 46페이지). 이
# 라벨은 데이터 줄 "위"에 오는 경우가 많아(3줄 구조: 종류코드 / 데이터 /
# 상세설명) 예외적으로 이전 줄까지 같이 본다 - "종류"라는 키워드로 앵커링돼
# 있어서 일반 괄호 패턴과 달리 다른 행의 것을 잘못 가져올 위험이 낮다.
CLASS_CODE_JONGRYU_RE = re.compile(r"종류\s*([A-Za-z0-9\-]{1,6})")
# 클래스 코드 자체에 한글이 섞인 문서도 있다(KR5153420105 실측: "종류직판F
# 수수료미징구-..." - class_fees.json이 아는 정식 코드도 "직판F"). 그런데
# "종류"라는 낱말 자체가 "종류형투자신탁의 경우..." 같은 흔한 각주 문구에도
# 나와서, 한글까지 무조건 다 허용하면 "형" 같은 걸 코드로 잘못 뽑아낼
# 위험이 크다. known_classes에 그대로 있는 경우에만 인정하도록 별도
# 패턴으로 분리한다(아래 사용처에서 known_classes 대조 후에만 쓴다).
CLASS_CODE_JONGRYU_KO_RE = re.compile(r"종류\s*([A-Za-z0-9\-가-힣]{1,6})")
# "가.연평균수익률"(누적 1/2/3/5년+설정후, 우리 스키마와 동일)과 "나.연도별
# 수익률 추이"(1~5년차별 단년도 수익률, 컬럼 의미가 다름)는 둘 다 숫자
# 5개짜리 줄이라 구분 안 하면 "나" 표 값을 "가" 표 컬럼에 잘못 매핑하게
# 된다. 섹션 제목으로 구간을 나눠 "나" 섹션은 아예 스킵한다.
# (공백 다 지운 텍스트에 대해 매칭하므로 \s* 불필요)
#
# 항목 번호가 "가./나."가 아니라 "1)/2)"로 매겨진 문서가 있다
# (KR5123420015 실측: "2) 연도별수익률추이(세전기준)"). 번호를 요구하면
# 이런 문서에서 섹션 전환을 못 잡아 연도별 값이 연평균 표로 새어 들어가고,
# 그 값이 요약표와 어긋나 대조 검증에서 그 페이지가 통째로 버려진다 -
# 실제로 같은 페이지 앞부분에 있던 정상 클래스(C-P, C-Pe)까지 같이
# 잃고 있었다. 번호 매김과 무관하게 제목 문구 자체로 잡는다("연평균
# 수익률"/"연도별수익률"은 이 두 표의 제목에만 쓰이는 표현이라 본문에
# 우연히 걸릴 위험이 낮다 - 설명 문장은 "연평균 수익률은 ...입니다"처럼
# 조사가 붙어서 "추이"/제목 형태와 구분된다).
SECTION_GA_RE = re.compile(
    r"(?:가[\.．]|(?<!주)\d[\).])연평균수익률(?![은는이가을를및])|^연평균수익률\(")
# "추이" 없이 그냥 "나. 연도별 수익률"이라고만 쓰는 문서도 있다
# (KR5169950018 실측: "나.연도별수익률(세전기준,단위:%)" - "추이"가
# 아예 없다). "추이"를 필수로 요구하면 이 표를 "가" 표와 구분 못 해
# "나" 표 클래스 행(값 의미가 다름 - 연도별 단년도)이 "가" 표의 실제
# known과 충돌하는 값으로 섞여 들어가 그 페이지 검증 전체가 실패했다.
# "추이"는 선택으로 두되, 위 "가" 표와 같은 이유로 뒤에 조사가 붙은
# 설명 문장과는 구분한다.
SECTION_NA_RE = re.compile(
    r"(?:나[\.．]|\d[\).])?연도별수익률(?:추이)?(?![은는이가을를및])")
# 클래스 행의 설정일("2016-04-18", "2001.01.31" 등) - 표 데이터가 아니라 각 행에
# 딸린 값이라 구조화 필드로 남겨둘 만하다.
INCEPTION_DATE_RE = re.compile(r"\d{4}[.\-]\d{1,2}[.\-]\d{1,2}")


def _normalize_date(s):
    """설정일 표기가 문서마다 "2013.08.19"와 "2009-04-20"으로 갈린다(실측:
    점 140건 / 하이픈 128건). 이건 원본이 담은 "정보"가 아니라 그냥 조판
    표기 차이라, 다른 값들("-" 같은 건 원본 뜻이 있어 그대로 두는 것과
    달리) 통일해도 잃는 게 없다. 반대로 섞인 채 두면 SQL에서 날짜 비교/
    정렬이 안 돼서("가장 먼저 설정된 클래스는?" 같은 질의) 실제로 답을
    못 하게 된다. ISO(YYYY-MM-DD)로 맞춘다 - 한 자리 월/일도 0을 채운다."""
    m = re.fullmatch(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", s)
    if not m:
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
PERIOD_LABELS = ["1y", "2y", "3y", "5y", "since_inception"]
# 최근3년/최근5년 칸이 원본에 아예 빈칸인(설정된 지 얼마 안 된 펀드) 행이
# 있다(KR5120420091 실측: "최근1년 최근2년 최근3년 최근5년 설정일이후" 5칸
# 헤더인데 실제 값은 "4.23 4.48 4.48" 3개뿐 - 3번째 값은 3년이 아니라
# 설정일이후 값인데, 그냥 순서대로 PERIOD_LABELS[0,1,2]에 채우면
# "3y"라는 잘못된 이름표가 붙는다). 헤더 줄에서 "N년"/"설정일이후" 라벨의
# x좌표를 찾아두고, 각 값 토큰을 순서가 아니라 x좌표가 가장 가까운 헤더
# 칸에 매칭한다 - 헤더를 못 찾은 경우에만 기존 순서 방식으로 되돌아간다.
# "최근"과 "N년" 사이 간격이 좁으면 pdfplumber가 "최근1년"으로 한 토큰에
# 붙여버리는 문서가 있다(KR5113420012 51페이지 실측 - 다른 문서는
# "최근 1년"으로 "최근"/"1년"이 분리돼 있음). "최근" 접두사가 붙어도
# 매치되게 허용한다("10년"/"2024년"처럼 숫자가 여러 자리인 건 여전히
# 안 걸림 - 정확히 한 자리 숫자+"년"만 허용).
YEAR_HEADER_RE = re.compile(r"^(?:최근)?(\d)년$")


def _detect_period_columns(lines):
    # 페이지 전체에서 "N년" 토큰을 찾으면(먼저 시도했던 방식) 같은 페이지의
    # "운용전문인력" 표나 각주 문장("설정일로부터 1년이 경과하지 않은...")에
    # 있는 엉뚱한 "1년"/"2년"까지 걸려서, 실제 수익률 표 헤더가 아닌 좌표를
    # 앵커로 써버리는 사고가 났다(위 파일 docstring의 "운용전문인력 표
    # 혼동" 주의사항과 같은 종류의 문제). 진짜 헤더는 "최근 1년 최근 2년
    # 최근 3년 최근 5년 설정일이후"처럼 여러 개의 "N년"/"설정일이후" 라벨이
    # 한 줄에 다 같이 나온다 - 그런 줄(3개 이상)만 헤더로 인정한다.
    #
    # 이 조건을 만족하는 줄이 페이지에 두 번 나오는 문서가 있다(KR5118420036
    # 54쪽 실측: 표 위에 그려진 막대그래프의 x축 눈금 "최근1년 최근2년
    # 최근3년 최근5년 설정일이후"도 걸린다 - 눈금은 한 칸에 다 붙여 찍혀서
    # "최근1년"이 한 단어가 되는데, 이것도 "최근"이 접두로 붙은 "N년"
    # 형식이라 정규식에 그대로 걸린다). 첫 번째로 걸리는 줄만 쓰면 그래프
    # 눈금 좌표를 표 헤더로 잘못 쓰게 된다 - 값 행 바로 위, 페이지에서 가장
    # 마지막에 나오는 자격 있는 줄이 실제 표 헤더다(그래프는 표보다 항상
    # 위에 그려진다).
    # "설정일"/"이후"가 칸이 좁아 같은 줄이 아니라 아예 다른 두 줄로
    # 세로로 쪼개져 찍히는 문서도 있다(KR5169950018 실측: "설정일"
    # 한 줄, "이후"가 그 아래 다른 줄 - 같은 줄 분리(위 KR5118420036
    # 사례)로도 못 잡는다). 페이지 전체에서 그런 조각을 먼저 모아두고,
    # 각 헤더 후보 줄에서 세로로 가까운(±20pt) 조각을 찾아 붙인다.
    since_candidates = []  # [(top, x0), ...]
    for li, line in enumerate(lines):
        for idx, w in enumerate(line):
            if "설정일이후" in w["text"] or "설정이후" in w["text"]:
                since_candidates.append((w["top"], w["x0"]))
            elif w["text"] == "설정일":
                if idx + 1 < len(line) and "이후" in line[idx + 1]["text"]:
                    since_candidates.append((w["top"], w["x0"]))
                else:
                    # "이후"가 바로 다음 줄이 아니라, 그 사이에 다른 헤더
                    # 조각 줄(예: "종류 최근1년...")이 끼어 있을 수 있다
                    # (KR5169950018 실측) - 세로로 가까운(±20pt) 범위
                    # 안에서 몇 줄 더 찾아본다.
                    for nxt in lines[li + 1: li + 4]:
                        if abs(nxt[0]["top"] - w["top"]) >= 20:
                            break
                        if any(nw["text"] == "이후" for nw in nxt):
                            since_candidates.append((w["top"], w["x0"]))
                            break

    best = None
    for line in lines:
        anchors = {}
        for idx, w in enumerate(line):
            m = YEAR_HEADER_RE.match(w["text"])
            if m:
                label = {"1": "1y", "2": "2y", "3": "3y", "5": "5y"}.get(m.group(1))
                if label:
                    anchors[label] = w["x0"]
            elif "설정일이후" in w["text"]:
                anchors["since_inception"] = w["x0"]
            elif w["text"] == "설정일" and idx + 1 < len(line) and "이후" in line[idx + 1]["text"]:
                # "설정일"과 "이후"가 별도 단어로 떨어져 찍히는 문서도 있다
                # (KR5118420036 실측 - 위 사례와 같은 페이지).
                anchors["since_inception"] = w["x0"]
        if "since_inception" not in anchors and since_candidates and anchors:
            line_top = line[0]["top"]
            near = min(since_candidates, key=lambda c: abs(c[0] - line_top))
            if abs(near[0] - line_top) < 20:
                anchors["since_inception"] = near[1]
        # 설정된 지 2년이 안 된 펀드는 "최근1년/설정일이후" 두 칸짜리
        # 헤더만 찍는 문서가 있다(KR5118420006 44쪽 실측: "최근 1년
        # 설정일 이후" - 2년/3년/5년 칸 자체가 헤더에도 없다). "since_
        # inception"이 명시적으로 잡혔을 때만(우연히 걸릴 각주 문구와
        # 구분되는 확실한 표지) 2칸도 인정한다 - 그 표지 없이 2개짜리를
        # 다 받아주면 무관한 짧은 줄까지 헤더로 오인할 위험이 크다.
        min_anchors = 2 if "since_inception" in anchors else 3
        if len(anchors) >= min_anchors:
            best = anchors
    return best


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


# 표 옆 여백에 "투자실적\n추이\n(연평균\n수익률)"이 세로로 회전돼 한 단어씩
# 별도 줄로 찍히는 문서가 있다(KR5116501001 등). 이게 라벨 이어지는 줄
# 사이에 끼어들면 "바로 다음/이전 줄"만 보는 class_code 탐색이 진짜
# 라벨을 건너뛰고 이 캡션 조각을 잘못 집는다 - 데이터(숫자)가 전혀 없고
# 이 캡션 단어 목록에만 정확히 일치하는 줄은 건너뛰고 그 다음/이전 "진짜"
# 줄을 찾는다. 문서마다 이 캡션이 쪼개지는 위치가 달라서, "연평균"과
# "수익률"이 붙어 "연평균수익률"로 한 토큰이 되거나 앞뒤로 괄호/콤마가
# 붙기도 한다(KR5123420039 실측: "(연평균수익률,"이 한 토큰이라 기존
# 패턴에 안 걸려서 "오프라인-퇴직연"과 "금(C)" 사이의 캡션 줄을 못
# 건너뛰고 멈췄고, class_code(C)를 놓쳐 null로 남았다). 괄호/콤마를
# 부호로 분리해서 허용한다.
CAPTION_FRAGMENT_RE = re.compile(r"^\(?(투자실적|추이|연평균|연환산|연평균수익률|수익률)\)?,?$")


def _skip_caption_lines(lines, start, step):
    idx = start
    while 0 <= idx < len(lines):
        text = re.sub(r"\s+", "", " ".join(w["text"] for w in lines[idx]))
        if CAPTION_FRAGMENT_RE.match(text):
            idx += step
            continue
        return idx
    return None


def _line_text_skipping_captions(lines, idx, step):
    real_idx = _skip_caption_lines(lines, idx, step)
    if real_idx is None:
        return ""
    return " ".join(w["text"] for w in lines[real_idx])


def row_kind(pre_text, prev_line_text="", next_line_text=""):
    # 폰트 문제로 글자가 한 자씩 떨어져 나오는 문서에서는 "비교지수"가
    # "비 교 지 수"처럼 공백 낀 상태로 들어오기도 해서, 공백을 지우고 비교한다.
    normalized = re.sub(r"\s+", "", pre_text)
    # "비교지수"의 동의어를 쓰는 문서도 있다(KR5144450095/KR5153450785
    # 실측: "참조지수", KR516702010M 실측: "참고지수" - 뜻은 같은데
    # 말만 다르다). 이 말들을 못 알아보면 class_code도 없고 행 종류도
    # 못 정해져 기본값인 class_return(class_code=null)으로 새 나가는데,
    # 실제로는 비교지수 행이라 값이 엉뚱한 클래스에 붙지 않는다.
    if "비교지수" in normalized or "참조지수" in normalized or "참고지수" in normalized:
        return "benchmark"
    if "변동성" in normalized:
        return "volatility"
    # "수익률\n변동성"라벨이 데이터 줄 위아래로 걸쳐 있는 경우가 있다
    # ("수익률"이 이전 줄, "변동성"이 다음 줄, 데이터 줄 자체엔 라벨이 거의
    # 없음). class_code 검색과 달리 "비교지수"/"변동성"은 클래스 이름과
    # 겹칠 일이 없는 행 유형 라벨이라, 이전/다음 줄을 같이 봐도 다른 클래스
    # 정보를 잘못 가져올 위험이 낮다.
    around = re.sub(r"\s+", "", prev_line_text) + re.sub(r"\s+", "", next_line_text)
    if "변동성" in around and "비교지수" not in around:
        return "volatility"
    # "투자신탁"만 라벨로 있는 행은 특정 클래스가 아니라 펀드 전체 평균(모든
    # 클래스를 합친 수익률)이다. class_code가 없는 게 아니라 애초에 클래스가
    # 아니므로 별도 종류로 구분한다. 원래 "==" 완전일치였는데, 요약표(3페이지
    # 스타일)에서는 "투자신탁" 옆에 최초설정일이 같은 줄에 붙어 나와
    # normalized가 "투자신탁2013.08.19"처럼 되면서 매치가 실패해 기본값인
    # class_return(class_code=null)으로 잘못 새는 버그가 있었다(KR510902773M
    # 실측 - 상세표(45페이지, 설정일 없이 "투자신탁"만 있어 정상 매치)의
    # fund 행과 row_kind가 달라져 cross-page dedup도 안 먹혔다).
    # "비교지수"/"변동성"과 같은 방식으로 부분일치로 바꾼다.
    if "투자신탁" in normalized:
        return "fund"
    # "투자신탁" 대신 "운용"이라고만 줄여 쓰는 문서도 있다(KR5194450018
    # 5쪽 실측: "운용 2.08 3.46 -4.85 7.78 4.99" 행 바로 아래 "비교지수"/
    # "수익률변동성" 행이 따라오는 것으로 보아 클래스별 행이 아니라 펀드
    # 전체 행이다). "운용"은 "운용사"/"운용역"/"운용전문인력"처럼 다른
    # 말에 흔히 섞여 쓰이므로, "투자신탁"과 달리 부분일치가 아니라 라벨
    # 전체가 정확히 "운용"일 때만 본다.
    #
    # 그런데 "운용"만 정확히 일치해도 가짜로 걸리는 경우가 있다
    # (KR5118420006 실측: "동종집합투자기구 운용현황" 표의 "책임(팀장)
    # ... / 운용 3.35 5.24 / 부책임(대리) ..." 행 - 운용사 평균수익률일
    # 뿐 이 펀드의 수익률이 아닌데 라벨이 우연히 "운용" 한 글자로만
    # 떨어져 나왔다). KR5194450018 실측 근거 자체가 "바로 아래 비교지수/
    # 변동성 행이 따라온다"는 것이었으므로, 그 확인을 실제 조건으로
    # 요구한다 - 근처에 비교지수/변동성이 없으면 fund로 보지
    # 않는다(class_return으로도 보지 않는다 - 클래스도 아니므로 아래
    # 호출부가 class_code 없이 걸러낸다).
    if normalized == "운용" and (
            "변동성" in around or "비교지수" in around
            or "참조지수" in around or "참고지수" in around):
        return "fund"
    return "class_return"


def find_return_rows_on_page(page, page_num, section="가", known_classes=None,
                              inherited_period_anchors=None, next_page=None):
    """section: 이 페이지 시작 시점의 "가/나" 섹션 상태(문서 내 이전 페이지에서
    이어받음). "나.연도별 수익률 추이" 섹션에 들어간 뒤로는 다음 "가" 제목을
    다시 만나기 전까지 데이터 행을 전부 스킵한다 - 컬럼 의미가 다른 표라
    "가" 표 스키마(1y/2y/3y/5y/since_inception)에 잘못 매핑하면 안 되기 때문.
    known_classes: class_fees.json에서 이미 확인된 이 상품의 클래스 코드
    목록(제공되면 라벨이 상품명 전체와 붙어 나오는 상세표에서 class_code
    보강용으로 씀). inherited_period_anchors: 바로 앞 페이지에서 찾은 기간
    헤더 x좌표(이 페이지에 헤더가 없으면 이어받는다 - 아래 참고). next_page:
    바로 다음 페이지의 pdfplumber Page(있으면) - 라벨의 클래스 코드 자체가
    페이지 경계에서 끊길 때 보강용으로 씀(아래 참고). 반환값은
    (rows, 이 페이지가 끝난 시점의 section, 다음 페이지로 넘길 기간 헤더)."""
    # x_tolerance=2(기본)로는 일부 문서에서 폰트 문제로 글자가 한 자씩 떨어져
    # 나오는 케이스(예: "4 .2 1")가 있어 숫자 인식이 아예 안 된다. 5로 올리면
    # 그 문제가 해결되면서도(검증 완료) 다른 문서의 값이 잘못 합쳐지진 않았다.
    #
    # 그런데 이걸로도 못 고치는 결함이 따로 있다. 글자의 회전행렬에 아주
    # 작은 잡음(각도로 0.0000016도)이 섞여 있으면 pdfplumber가 그 글자를
    # "세로쓰기"로 보고 아예 다른 경로로 처리해서, 붙어 있는 글자도 한
    # 자씩 따로 나온다(KR5111450067 56쪽 실측: 1320자 중 1264자가 이
    # 증상, "최근 1년"이 "최","근","1","년" 네 단어로 쪼개진다). 이건
    # 간격 문제가 아니라 아예 다른 묶기 경로를 타는 거라 x_tolerance를
    # 얼마를 줘도 못 고친다(실측: x_tolerance=5도 1118자를 377개의
    # "단어"로 줄이지만 여전히 여러 글자가 잘못 뭉쳐 있다 - 정답은 236
    # 단어). pdf_words.extract_words가 이 회전 잡음만 골라 고친다(정상
    # 문서는 결과가 완전히 같다).
    words = _safe_words(page, x_tolerance=5, keep_blank_chars=False)
    lines = cluster_lines(words)
    # 표가 페이지 경계에서 끊기면 이어지는 쪽엔 기간 헤더("최근1년 ...
    # 설정일이후")가 반복되지 않는 문서가 있다(KR5120420091 58쪽 실측:
    # 57쪽에 헤더가 한 번만 있고 58쪽부터는 클래스별 3줄짜리 블록만
    # 죽 이어진다). 이 페이지에 헤더가 없으면 앞 페이지 것을 그대로
    # 쓴다 - 없으면 순서 방식(PERIOD_LABELS[:len(값)])으로 떨어져 3번째
    # 값(실제로는 "설정일이후")이 "3y"로 잘못 이름 붙는다.
    period_anchors = _detect_period_columns(lines) or inherited_period_anchors
    out_period_anchors = period_anchors
    rows = []
    for i, line in enumerate(lines):
        line_text_for_section = re.sub(r"\s+", "", " ".join(w["text"] for w in line))
        if SECTION_NA_RE.search(line_text_for_section):
            section = "나"
        elif SECTION_GA_RE.search(line_text_for_section):
            section = "가"

        if section == "나":
            continue

        decimals = [w for w in line if DECIMAL_RE.match(w["text"])]
        dashes = [w for w in line if DASH_RE.match(w["text"])]
        value_tokens = sorted(decimals + dashes, key=lambda w: w["x0"])
        # 비교지수(benchmark) 행은 자기 몫의 최초설정일이 없어서 그 칸에
        # 날짜 대신 "-"를 찍는 문서가 있다(KR5113420012 실측: "비교지수 -
        # 5.43 5.18 3.19 1.49 4.00" - 진짜 값 5개 + 설정일 자리의 "-" 1개
        # 해서 6개가 되어 "값 5개 초과"로 행 전체가 통째로 버려지고
        # 있었다). 이 "-"는 항상 실제 값 5개(1y~since_inception) 왼쪽,
        # 최초설정일 칸 위치에 딱 1개만 나온다 - 정확히 6개(진짜 값 5개 +
        # 여분 대시 1개)일 때만, 그 여분이 대시인 걸 확인하고 가장 왼쪽
        # 것만 버린다. 아무 라벨도 없이 대시만 여러 개(예: "- - - - - - -
        # - -" 9개, 설정/환매현황 표의 빈 칸들 - KR510902773M 실측)인
        # 줄까지 5개로 뭉개면 있지도 않은 가짜 행이 생기므로, 6개인
        # 경우로만 좁힌다.
        if len(value_tokens) == 6 and DASH_RE.match(value_tokens[0]["text"]):
            value_tokens = value_tokens[1:]
        # 설정 2년 미만 펀드는 표 자체가 "최근1년/설정일이후" 두 칸만
        # 갖기도 한다(KR5118420006 44쪽 실측, 위 _detect_period_columns
        # 주석 참고) - 이 페이지 헤더가 실제로 그렇게(2칸만) 잡혔을 때만
        # 값 2개짜리 행도 받아준다. 그 표지 없이 그냥 2개로 낮추면 각주의
        # 우연한 숫자 2개짜리 줄까지 행으로 오인한다.
        # 최근에 신설된 클래스는 "설정일이후" 한 칸만 값이 찍히기도 한다
        # (KR5118420062 44쪽 실측: "ClassS-P 3.37"처럼 5칸 헤더 표에서도
        # 값이 딱 1개뿐 - 1년도 안 지나 나머지 기간 칸 자체가 아직 없다).
        # 기간 헤더(period_anchors)가 있으면 값이 몇 개든 x좌표로 정확한
        # 칸에 매칭되므로, 개수 자체를 문지기로 쓸 필요가 없다 - 헤더를
        # 못 찾았을 때(순서 추측 방식)만 최소 3개를 요구한다.
        min_values = 1 if period_anchors else 3
        if len(value_tokens) < min_values or len(value_tokens) > 5:
            continue
        # 값 개수 문지기를 1까지 낮추면, 본문 산문 중 우연히 "-" 하나만
        # 있는 줄(값이 하나도 없는데 DASH_RE만 걸린 경우)까지 행으로
        # 오인한다(KR5111420047 실측: "Top-down 및 Bottom-up을 병행한..."
        # 문장 줄이 class_code="Duration"인 가짜 행으로 잡혔다 - 값이
        # "-" 하나뿐이었다). 값이 1~2개뿐일 때는 그 중 적어도 하나는
        # 진짜 소수여야 한다(대시만으로는 신설 클래스 신호가 안 된다 -
        # 신설 클래스도 최소 하나는 실제 수치를 찍는다).
        if len(value_tokens) <= 2 and not any(
            DECIMAL_RE.match(t["text"]) for t in value_tokens
        ):
            continue
        # 운용전문인력 표(성명/생년/직위 등)와 구분: 그 표는 억원 단위 정수(운용규모)나
        # 4자리 연도(생년) 같은 게 섞여 있고, 클래스 수익률 표는 전부 소수 % 값이다.
        # DECIMAL_RE 자체가 소수점 없는 정수(생년·펀드수·억원단위 운용규모, 쉼표
        # 섞인 "77,772" 포함)는 이미 걸러 주므로, 여기서는 소수점 있는 값이
        # 말도 안 되게 클 때만(운용전문인력 표에 우연히 섞여 든 소수) 거른다.
        # 100은 너무 좁다 - 진짜 수익률이 100%를 넘는 클래스가 있다(KR5131420025
        # 실측: C클래스 최근2년 469.56%, 연도별 표에서는 3,260.76% - 펀드
        # 자체의 급격한 변동으로 원본 문서에 그대로 인쇄된 값이다. 100 기준을
        # 쓰면 이 클래스의 행 전체가 통째로 버려진다).
        if any(abs(float(d["text"])) > 5000 for d in decimals):
            continue

        pre_text_words = [w for w in line if w["x0"] < value_tokens[0]["x0"]]
        pre_text = " ".join(w["text"] for w in pre_text_words)
        # 클래스 코드 탐색용으로는 설정일 토큰("2022-11-14")을 뺀 버전을 따로
        # 만든다 - pre_text_words는 DECIMAL_RE/DASH_RE 형식만 걸러내므로
        # 날짜 토큰은 그대로 통과해 라벨 조각 "인-퇴직연금(C-"와 다음 줄
        # 조각 "P2E)" 사이에 날짜가 끼어들어 "C-2022-11-14P2E)"처럼 되고
        # CLASS_CODE_RE가 매치를 못 한다(KR5131420025 C-P2E 실측: class_code가
        # None으로 빠짐). 날짜 추출(아래 date_m)은 원본 pre_text를 그대로
        # 써야 하므로 이 변수는 코드 탐색에만 쓴다.
        pre_text_words_nodate = [
            w for w in pre_text_words if not INCEPTION_DATE_RE.fullmatch(w["text"])
        ]
        pre_text_nodate = " ".join(w["text"] for w in pre_text_words_nodate)

        # 클래스명이 인접 줄로 이어질 수 있어 다음 줄까지 확인 (총보수 표에서
        # 검증된 대로 - "이전 줄"은 다른 행 것일 위험이 있어 보지 않는다.
        # 단, "종류A" 패턴은 라벨이 데이터 줄 "위"에 오는 3줄 구조라 예외적으로
        # 이전 줄도 함께 본다 - 아래 종류 코드 탐색 참고).
        # 세로로 회전된 옆면 캡션("투자실적"/"추이" 등)이 진짜 라벨 줄
        # 사이에 끼어들 수 있어(KR5116501001), "바로 다음/이전 줄"이 아니라
        # 그 캡션 조각들을 건너뛴 "진짜" 다음/이전 줄을 본다.
        prev_line_text = _line_text_skipping_captions(lines, i - 1, -1)
        next_line_text = _line_text_skipping_captions(lines, i + 1, 1)
        label_search_text = pre_text_nodate + " " + next_line_text
        # 폰트 문제로 글자가 한 자씩 떨어져 나오는 문서(예: "비 교 지 수")에서도
        # 키워드 검사가 되도록, 공백 제거한 버전을 만들어서 모든 문구 검사에 쓴다.
        norm_pre = re.sub(r"\s+", "", pre_text)
        norm_label = re.sub(r"\s+", "", label_search_text)

        # 총보수 표 행이 같은 페이지(같은 뭉친 블록)에 섞여 있다가 잘못 걸리는
        # 걸 제외한다. 두 가지 신호로 구분: (1) 판매수수료 문구("납입금액의"/
        # "없음")를 라벨로 쓰는 건 총보수 표뿐이고, (2) 총보수 표는 소수(%) 뒤에
        # 정수(비용예시, 천원 단위)가 같은 줄에 더 붙어 있는데 수익률 표는
        # 소수(%)만 있고 정수가 안 붙는다.
        if "납입금액의" in norm_label or "없음" in norm_pre:
            continue
        trailing_int_like = [
            w for w in line
            if w["x0"] > value_tokens[-1]["x0"] and re.match(r"^\d{1,4}$", w["text"])
        ]
        if trailing_int_like:
            continue
        # "-"를 값으로 인정하면서 새로 생긴 위험: 총보수 표의 비용예시(천원, 정수)
        # 행이 "- - 240 244 40 40 200 204 -"처럼 대시 사이사이에 정수가 끼어 있는
        # 경우, 대시만 3~5개 세면 수익률 행으로 오인한다. 값 영역(첫~마지막
        # value_token 사이) 안에 순수 정수 토큰이 하나라도 끼어 있으면 총보수
        # 표로 보고 제외한다.
        value_x0, value_x1 = value_tokens[0]["x0"], value_tokens[-1]["x0"]
        stray_ints = [
            w for w in line
            if value_x0 <= w["x0"] <= value_x1
            and w not in value_tokens
            and re.match(r"^\d{1,4}$", w["text"])
        ]
        if stray_ints:
            continue

        # 운용전문인력(운용역) 표 행도 같은 블록에 섞여 있을 수 있다 - "생년(19xx)"이나
        # "운용규모(1,234억원 - 콤마 있는 큰 수)"가 라벨 자리에 있으면 그 표로 본다.
        # 단, 수익률 표의 설정일("2001.01.31")도 4자리 숫자로 시작하니 뒤에 "."이나
        # "-"가 붙어 날짜로 보이면(생년은 그냥 단독 숫자) 제외 대상에서 뺀다.
        # (문자열을 공백 제거 후 정규식 \b로 검사하면 "전준필1996"처럼 한글 뒤에 바로
        # 붙은 숫자에서 단어 경계가 인식되지 않아(둘 다 유니코드 \w) 놓칠 수 있어,
        # 토큰 단위로 직접 검사한다.)
        if any(re.fullmatch(r"(19|20)\d{2}", w["text"]) for w in pre_text_words):
            continue
        if any(re.fullmatch(r"\d{1,3},\d{3}", w["text"]) for w in pre_text_words):
            continue

        # 이 줄 자체가 비교지수/변동성 행인지 먼저 직접 확인한다(같은 줄
        # 텍스트만 본다 - 옆 줄을 보지 않으므로 안전). 비교지수/변동성 행은
        # 애초에 클래스 코드가 없으므로, 여기 해당되면 class_code 검색 자체를
        # 하지 않는다 - 안 그러면 "다음 줄"(보통 바로 다음에 오는 클래스 행의
        # 이름)을 이 행 자신의 클래스 코드로 잘못 가져온다(실측: 변동성 행이
        # 다음 클래스 행 이름을 빌려와 class_code="A"로 잘못 붙는 버그 확인).
        same_line_kind = row_kind(pre_text)

        if same_line_kind in ("benchmark", "volatility"):
            kind = same_line_kind
            class_code = None
        else:
            class_code = None
            m = CLASS_CODE_RE.search(norm_label)
            if m:
                class_code = m.group(1)
            elif known_classes:
                # 코드가 "설명(코드)"가 아니라 "코드(설명)"/"코드형(설명)"로
                # 값 줄 "위" 줄 맨 앞에 오는 문서가 있다(KR5125450023 실측:
                # "C(수수료미징구 –" / 값 줄 / "오프라인)" 3줄 구조, 같은
                # 문서 뒤쪽 상세표는 "A-G형(수수료선취-" / "오프라인- <값들>"
                # / "무권유저비용)" - 코드 뒤에 "형"이 붙기도 한다). 괄호
                # 안은 한글 설명이라 CLASS_CODE_RE로는 못 잡는다. 칸 폭이
                # 좁으면 코드 자체가 위쪽 줄에 걸쳐 쪼개지기도 한다("C-"
                # 한 줄 / "G형(수수료미징구-" 다음 줄, 값 줄은 그 아래 -
                # 또는 "C-" 한 줄 / "Pe형(수수료미징구- <값들>"처럼 코드
                # 뒷부분이 값 줄 맨 앞에 붙기도 한다). 위쪽 줄들과 이 줄
                # 자신의 라벨 부분을 여러 방식으로 이어 붙여 순서대로
                # 시도한다. "C" 같은 짧은 코드는 오탐 위험이 커서
                # known_classes에 있을 때만 인정한다.
                prev_flat = re.sub(r"\s+", "", prev_line_text)
                prev2_flat = (
                    re.sub(r"\s+", "", " ".join(w["text"] for w in lines[i - 2]))
                    if i >= 2 else ""
                )
                for candidate in (prev_flat, prev_flat + pre_text_nodate,
                                  prev2_flat + prev_flat):
                    pm = re.match(r"^([A-Za-z0-9\-]{1,8})형?\(", candidate)
                    if pm and pm.group(1) in known_classes:
                        class_code = pm.group(1)
                        break
            if class_code is not None:
                pass
            else:
                # 공백을 지우면 "ClassA 2006-09-05"가 "ClassA2006-09-05"로
                # 붙어버려서 뒤에 오는 날짜까지 클래스 코드로 삼켜버린다
                # ("A2006-"). 이 패턴은 원본(공백 유지) 텍스트에서 찾아야
                # 단어 경계("ClassA" 다음 공백)에서 멈춘다.
                m2 = CLASS_CODE_NOPAREN_RE.search(label_search_text)
                if m2:
                    class_code = m2.group(1)
                else:
                    # "종류A"는 라벨이 데이터 줄 "위"에 오는 3줄 구조(종류코드
                    # 줄 / 데이터 줄 / 상세설명 줄)라 이전 줄도 함께 본다 -
                    # "종류"라는 명시적 키워드로 앵커링돼 있어 일반 괄호
                    # 패턴과 달리 다른 행 것을 잘못 가져올 위험이 낮다.
                    m3 = CLASS_CODE_JONGRYU_RE.search(
                        prev_line_text + " " + label_search_text
                    )
                    if m3:
                        class_code = m3.group(1)
                    elif known_classes and (m3ko := CLASS_CODE_JONGRYU_KO_RE.search(
                        prev_line_text + " " + label_search_text
                    )) and m3ko.group(1) in known_classes:
                        class_code = m3ko.group(1)
                    elif known_classes:
                        # 상세 부속서류(제2부 등)는 라벨이 "(A1)"처럼 괄호로
                        # 안 떨어지고 "마이다스 책임투자 증권 투자신탁(주식)A1"
                        # 처럼 상품 전체 명칭 뒤에 클래스 코드가 그냥 이어
                        # 붙기도 한다(KR5157450017 실측). 이런 임의의 접미사를
                        # 정규식만으로 뽑으면 엉뚱한 문자열을 클래스 코드로
                        # 오인할 위험이 크다 - 대신 class_fees.json에서 이미
                        # 확인된 "이 상품의 진짜 클래스 코드 목록"에 있는
                        # 것으로 끝나는 경우에만(더 긴 코드 우선, 그 앞
                        # 글자가 영문/숫자가 아닌 경우만 - "BA1"의 "A1"처럼
                        # 엉뚱하게 잘라 오는 걸 방지) 인정한다.
                        # 클래스명이 3줄로 쪼개지면서 코드가 데이터 줄
                        # *두 줄* 아래에 오는 서식이 있다(KR5172450019
                        # 실측: "수수료미징구-" / "오프라인- 14.78 ..." /
                        # "11.6.27"(최초설정일) / "보수체감(C4)"). 한 줄만
                        # 보면 이런 클래스를 통째로 놓친다(이 문서는 15개
                        # 클래스 중 1개만 잡혔다).
                        # 그렇다고 코드 탐색 창을 일반적으로 넓히면 바로
                        # 다음 클래스의 이름을 이 행 것으로 잘못 가져오는
                        # 사고가 난다(이 파일 곳곳의 주석 참고 - 넓은 창은
                        # "틀렸는데 그럴듯한 값"을 만들어 이상치 검사도
                        # 못 걸러낸다). 그래서 두 줄 아래는 known_classes
                        # (class_fees.json으로 검증된 이 상품의 실제 코드
                        # 목록)로 걸러지는 이 경로에서만, 그리고 사이 줄이
                        # 값도 비교지수/변동성도 아닐 때만(=아직 이 행의
                        # 라벨이 이어지는 중일 때만) 본다.
                        # 한 줄까지만 본 텍스트로 "먼저" 맞춰보고, 거기서
                        # 못 찾을 때만 두 줄째를 붙여 다시 본다. 처음엔
                        # 무조건 두 줄째까지 붙여서 봤는데, 라벨이 한 줄
                        # 아래에서 이미 끝나는 문서(KR5157450017: "...
                        # 신탁(주식)A1"에서 끝남)는 그 뒤에 비교지수 줄이
                        # 딸려 붙어 "...3.98"로 끝나게 돼 endswith 매칭이
                        # 깨졌다(그 문서 클래스가 7개→1개로 회귀).
                        candidates = [(prev_line_text + " " + label_search_text).rstrip()]
                        if i + 2 < len(lines):
                            mid_has_value = any(
                                DECIMAL_RE.match(t) or DASH_RE.match(t)
                                for t in next_line_text.split()
                            )
                            if not mid_has_value and row_kind(next_line_text) not in ("benchmark", "volatility"):
                                candidates.append(
                                    (candidates[0] + " " + _line_text_skipping_captions(lines, i + 2, 1)).rstrip()
                                )
                        for combined in candidates:
                            # 괄호형("...보수체감(C3)")도 여기서 같이 본다.
                            # 위 CLASS_CODE_RE 검사는 좁은 창(pre_text +
                            # 한 줄)만 보기 때문에 두 줄 아래에 있는 괄호
                            # 코드를 못 잡는데(KR5172450019), 그렇다고 그
                            # 검사 자체의 창을 넓히면 옆 클래스 코드를
                            # 주워온다. known_classes에 있는 코드만
                            # 인정하는 이 경로에서는 넓혀도 안전하다.
                            for mm in reversed(list(CLASS_CODE_RE.finditer(combined))):
                                if mm.group(1) in known_classes:
                                    class_code = mm.group(1)
                                    break
                            if class_code:
                                break
                            for code in sorted(known_classes, key=len, reverse=True):
                                if combined.endswith(code):
                                    before = combined[: -len(code)]
                                    if not before or not before[-1].isalnum():
                                        class_code = code
                                        break
                            if class_code:
                                break

            class_code = _normalize_class_code(class_code, known_classes)

            if class_code:
                # 클래스 코드가 확실히 잡혔으면 그 자체로 "클래스 행"이라는
                # 확실한 증거라 주변 줄의 변동성 언급을 볼 필요가 없다
                # (KR5120420039처럼 클래스 여러 개가 변동성/비교지수 한 쌍을
                # 공유하는 표에서, 바로 옆에 다른 그룹의 변동성 행이 우연히
                # 붙어 있는 걸 이 행 자신의 유형으로 착각하는 버그가 있었다).
                kind = "class_return"
            else:
                # "수익률\n변동성"라벨이 데이터 줄 위아래로 걸쳐 있는 경우
                # (KR5113420012/69에서 확인)만 좁혀서 앞뒤 줄을 본다. 이때는
                # 캡션을 건너뛴 줄이 아니라 "바로" 앞/다음 줄이어야 한다 -
                # 캡션 건너뛰기로 더 멀리 있는 줄(예: 바로 위의 비교지수 행
                # 전체)까지 가져오면 그 행 자신의 "비교지수" 텍스트가 섞여
                # 들어와 "변동성 행인데 비교지수도 같이 검출됨" 오판정이
                # 생긴다(KR5113420012에서 실제 확인된 회귀).
                raw_prev_line_text = " ".join(w["text"] for w in lines[i - 1]) if i - 1 >= 0 else ""
                raw_next_line_text = " ".join(w["text"] for w in lines[i + 1]) if i + 1 < len(lines) else ""
                kind = row_kind(pre_text, raw_prev_line_text, raw_next_line_text)

        date_m = INCEPTION_DATE_RE.search(pre_text)
        inception_date = _normalize_date(date_m.group()) if date_m else None

        # 클래스 코드 칸과 최초설정일 칸이 붙어 있어서, 설정일 앞자리
        # 숫자("20"/"201"/"2016" 등, 몇 글자가 새는지는 문서마다 다르다)가
        # 코드 뒤에 그대로 눌어붙는 문서가 있다(KR5118420062 13개 클래스
        # 실측: "C-Pe" 뒤에 "20"이 붙어 "C-Pe20"이 되면서, 정작 진짜
        # "C-Pe" 행은 설정일이 빈 채로 따로 남았다 - 가짜 클래스 13개가
        # 한꺼번에 생기고 진짜 클래스 13개는 설정일을 잃는 회귀였다).
        # 코드 끝을 무작정 숫자만큼 떼면 "A2"처럼 진짜 숫자로 끝나는
        # 클래스와 헷갈린다(A22016 -> A2까지만 떼야지 A까지 떼면 안 됨).
        # 이 행이 이미 자기 설정일을 올바로 읽어 뒀으므로(위 inception_date),
        # 그 연도와 실제로 일치하는 접미사만 떼어 known_classes에 있는
        # 코드가 나오면 그걸로 본다 - 우연히 숫자로 끝나는 진짜 코드를
        # 잘못 건드릴 위험이 없다.
        if class_code and inception_date and class_code not in known_classes:
            year_prefix = inception_date[:4]
            for n in (4, 3, 2, 1):
                suffix = year_prefix[:n]
                if len(class_code) > n and class_code.endswith(suffix):
                    candidate = class_code[: -n]
                    if candidate in known_classes:
                        class_code = candidate
                        break

        # 아직 수익률이 없는 신규 클래스 등은 원본이 그 칸에 "-"를 직접
        # 찍어서 "값이 없다"는 걸 명시적으로 밝힌다. 이걸 그냥 None으로
        # 뭉개면 "추출을 못 해서 모른다"와 "원본이 확인해서 없다고
        # 밝혔다"가 구분이 안 된다(class_fees의 peer_avg_fee/
        # sales_commission_desc에서 이미 사용자 지적으로 고친 것과 같은
        # 문제 - 실측: KR510902511M C1 등 56건에서 evidence는 전부
        # "-"인데 값은 None으로 나오고 있었다). 원본 토큰이 "-"면 "-"를
        # 그대로 남기고, "-"도 아니고 소수도 아닌(추출 자체가 안 된)
        # 경우에만 None으로 남긴다.
        # 값이 5개 다 있으면 순서(1y/2y/3y/5y/since_inception)와 x좌표
        # 매칭이 항상 같은 결과를 주지만, 중간 칸(3년/5년)이 원본에서
        # 통째로 비어 있으면(위 PERIOD_LABELS 주석 참고) 순서 방식은
        # 남은 값들을 앞칸부터 밀어 채워서 라벨이 틀어진다 - x좌표가 더
        # 가까운 헤더 칸으로 매칭한다.
        if period_anchors:
            labels = [
                min(period_anchors, key=lambda lbl: abs(period_anchors[lbl] - t["x0"]))
                for t in value_tokens
            ]
            if len(set(labels)) != len(labels):
                # 매칭이 겹치면(이례적인 레이아웃) 안전하게 기존 순서
                # 방식으로 되돌아간다.
                labels = PERIOD_LABELS[: len(value_tokens)]
        else:
            labels = PERIOD_LABELS[: len(value_tokens)]

        values = {
            labels[idx]: (
                t["text"] if DECIMAL_RE.match(t["text"])
                else ("-" if t["text"] == "-" else None)
            )
            for idx, t in enumerate(value_tokens)
        }

        rows.append({
            "row_kind": kind,
            "class_code": class_code,
            "inception_date": inception_date,
            "values": values,
            "page": page_num,
            "evidence": " ".join(w["text"] for w in line),
            "method": "coordinate_reconstruction",
            "confidence": 1.0 if (class_code or kind != "class_return") else 0.5,
            "_top": line[0]["top"],
        })

    _apply_merged_cell_dates(page, words, rows)

    # 라벨과 값은 이 페이지 안에서 다 채워졌는데 클래스 코드 자체가 페이지
    # 경계에서 끊기는 문서가 있다(KR5113420069 실측: 61쪽 마지막 줄이
    # "수수료선취-오프라인 3.35 5.17 6.24 3.52 3.51"로 라벨·값 다 있는데,
    # 닫는 코드 "(A)"만 62쪽 맨 첫 줄에 홀로 떨어져 있다). 이 페이지에서
    # class_code를 못 찾은 class_return 행 중 가장 아래(페이지 마지막) 것에
    # 한해서만, 다음 페이지 첫 줄이 곧바로 괄호코드로 시작하면 이어붙인다 -
    # known_classes에 있는 코드만 인정해 엉뚱한 각주 번호 등을 오인하지
    # 않는다.
    if next_page is not None and known_classes:
        unresolved = [r for r in rows
                      if r["row_kind"] == "class_return" and not r.get("class_code")]
        if unresolved:
            last = max(unresolved, key=lambda r: r["_top"])
            next_words = _safe_words(next_page, x_tolerance=5, keep_blank_chars=False)
            next_lines = cluster_lines(next_words)
            if next_lines:
                first_flat = re.sub(r"\s+", "", " ".join(w["text"] for w in next_lines[0]))
                cm = CLASS_CODE_RE.match(first_flat) or re.match(
                    r"^([A-Za-z0-9\-]{1,8})\)", first_flat
                )
                if cm and cm.group(1) in known_classes:
                    last["class_code"] = _normalize_class_code(cm.group(1), known_classes)

    # "_top"(줄의 y좌표)은 여기서 바로 지우지 않고 호출자의 중복 제거
    # 단계까지 들고 간다 - 값만으로 중복을 판정하면(아래 dedup 주석 참고)
    # 서로 다른 진짜 행을 잘못 지워버리는 사고가 나서, 페이지 안에서의
    # 실제 위치까지 같이 봐야 한다.
    return rows, section, out_period_anchors



# ---------------------------------------------------------------------------
# 셀 경계 기반 추출
#
# 위의 find_return_rows_on_page는 단어를 y좌표로 묶어 "줄"을 만들고, 그
# 줄에서 값 토큰을 세어 행을 판별한다. 그래서 병합 셀이나 셀 안 줄바꿈이
# 있으면 옆 칸 글자가 같은 줄로 섞이고, 문서마다 예외 처리가 계속 붙었다
# (이 파일의 긴 주석들이 그 흔적이다). 표의 셀 경계는 PDF가 직접 그려 둔
# 정보이므로 그걸 쓰면 그런 추측이 필요 없다. class_fees에서 같은 전환을
# 먼저 했고 폴백 0으로 끝냈다 - 같은 방식을 여기에도 적용한다.
# ---------------------------------------------------------------------------

# "1년차"라고 쓰는 연평균 표가 있다(KR5160420009 실측). 연도별 표도 같은
# 표기를 쓰지만 그쪽엔 "설정일 이후" 칸이 없어서 표 단위로 구분된다.
RETURN_PERIOD_RE = re.compile(r"^(?:최근)?(\d)년(?:차)?$")
RETURN_LABEL_NAMES = ("종류", "클래스", "구분", "종류(클래스)")

# "24.01.01~24.12.31" 같은 기간. 물결표를 문서마다 다르게 쓴다.
TABLE_PERIOD_RE = re.compile(
    r"(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})\s*[~∼〜～]\s*"
    r"(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})")


def _period_end_days(texts):
    """글자들에서 기간을 찾아 "끝나는 날"만 모은다.

    시작일과 끝일이 다른 칸으로 쪼개지는 표가 있어서("24.01.01~" /
    "24.12.31" - 가로줄을 글자 줄에서 잡으면 늘 이렇게 된다) 칸마다
    따로 보면 기간을 하나도 못 찾는다. 붙여 놓고 본다 - 정규식이
    "날짜~날짜"가 맞붙은 자리만 잡으므로 엉뚱한 칸끼리 이어져 걸릴
    일은 없다."""
    joined = re.sub(r"\s+", "", " ".join(t or "" for t in texts))
    return {m.group(2) for m in TABLE_PERIOD_RE.finditer(joined)}


def _looks_yearly(texts):
    """"나. 연도별 수익률 추이" 표인가.

    열 이름이 "최근 1년차 / 최근 2년차 ..."로 연평균 표와 거의 같아서
    이름만으로는 못 가른다. 가르는 건 기간이다 - 연평균은 재는 구간의
    길이만 다를 뿐 모두 같은 날에 끝나고(24.01.01~24.12.31 /
    23.01.01~24.12.31 / 22.01.01~24.12.31), 연도별은 해마다 끝나는 날이
    다르다(24.12.31 / 23.12.31 / 22.12.31).

    이걸 안 보다가 연도별 값을 연평균 칸에 넣고 있었다(KR510902511M
    실측: 46쪽 연평균 표의 열 구성을 47쪽 연도별 표가 물려받아
    "최근 2년 35.66%"가 됐다 - 실제 연평균 2년은 1.79%다)."""
    return len(_period_end_days(texts)) > 1


def _is_multi_header(name):
    """여러 칸의 이름이 한 셀에 다 들어가 있는 묶음 머리글인지 본다.
    표 머리글 전체가 하나의 병합 셀로 잡히는 문서가 있는데
    (KR5144420020 실측: "최근1년 최근2년 최근3년 최근5년 종류 최초설정일
    설정일이후"가 한 칸), 그대로 두면 그 중 하나로 매칭돼 이름 칸이
    "설정일 이후"로 잡히고 클래스명을 통째로 잃는다."""
    n = re.sub(r"\s+", "", name)
    hits = len(re.findall(r"(?:최근)?\d년", n))
    for kw in ("설정일이후", "설정이후", "최초설정일", "종류"):
        if kw in n:
            hits += 1
    return hits >= 2


def _return_column_field(name):
    """머리글 이름 → 필드. 못 알아보면 None."""
    n = re.sub(r"\s+", "", name)
    if not n:
        return None
    # 기간 범위를 열 이름과 같은 칸에 함께 찍는 문서가 있다
    # (KR5194450018 실측: "최근 1년 ('24.01.17 ~'25.01.16)").
    # 괄호 앞부분만 이름으로 본다.
    if "(" in n and _return_column_field_base(n) is None:
        n = n.split("(")[0]
    return _return_column_field_base(n)


def _return_column_field_base(n):
    if "설정일이후" in n or n in ("설정후", "설정이후"):
        return "since_inception"
    if "최초설정일" in n or n in ("설정일", "최초설정"):
        return "inception_date"
    if n in RETURN_LABEL_NAMES or n.startswith("종류"):
        return "label"
    # 글자가 그려진 순서 때문에 "최근 1년"이 "최근 년 1"로 뒤집혀 추출되는
    # 문서가 있다(KR555202013M 실측 - class_fees에서도 같은 현상을 봤다).
    m = RETURN_PERIOD_RE.match(n) or re.match(r"^(?:최근)?년(\d)$", n)
    if m:
        return {"1": "1y", "2": "2y", "3": "3y", "5": "5y"}.get(m.group(1))
    return None


def _clean_val(v):
    """칸 글자에서 값 부분만 남긴다. 값마다 "%"를 붙여 찍는 문서가 있다
    (KR5114420027 실측: "5.89 %") - 그대로 두면 숫자로 안 보여 그 표가
    통째로 빠진다."""
    return v.replace(" ", "").rstrip("%")


def _val(v):
    v = _clean_val(v)
    return bool(DECIMAL_RE.match(v) or NUM_RE.match(v) or DASH_RE.match(v))


def _return_grid(page, inherited=None):
    """페이지에서 연평균수익률 표를 읽는다. 표 테두리를 선(line)이 아니라
    채워진 사각형(rect)으로 그린 페이지가 있는데, pdfplumber 기본 설정은
    그런 표의 가로줄을 못 잡는다(KR5120420039 실측: 같은 문서인데 6쪽은
    227칸으로 읽히고 7쪽은 6칸으로만 읽힌다 - 눈으로는 똑같이 테두리가
    보인다). 기본 설정으로 못 읽은 페이지에 한해 가로줄을 글자 줄에서
    잡는 설정으로 다시 읽는다."""
    def nrows(got):
        return sum(len(rows) for _, rows, _, _, _ in got) if got else 0

    first = _return_grid_one(page, inherited, None)
    if nrows(first) >= 3:
        return first
    # 기본 설정으로 거의 못 읽었으면 가로줄을 글자 줄에서 잡는 설정으로
    # 다시 읽어 더 많이 나오는 쪽을 쓴다.
    second = _return_grid_one(
        page, inherited,
        {"vertical_strategy": "lines", "horizontal_strategy": "text"})
    if nrows(second) > nrows(first):
        return second
    return first or second or None


def _return_grid_one(page, inherited=None, settings=None):
    """이 페이지에서 "가.연평균수익률" 표를 셀 격자로 읽는다.
    돌려주는 것: (field_by_col, data_rows, cells) 또는 None.

    "나.연도별 수익률 추이" 표는 열 뜻이 달라(1~5년차 단년도) 절대 섞이면
    안 되는데, 그 표에는 "설정일 이후" 칸이 없고 머리글이 연도(2024년 등)라
    구분된다 - 설정일이후 칸이 있는 표만 연평균 표로 인정한다.

    inherited: 바로 앞 페이지에서 잡은 (열매핑, 열x좌표). 표가 페이지를
    넘어가면 이어지는 쪽엔 머리글이 반복되지 않는다(KR510902773M 실측:
    3쪽에 클래스 행, 4쪽에 비교지수·변동성 행 / 45쪽 상세표는 머리글이
    44쪽에 있다). 이 페이지에서 머리글을 못 찾았을 때만 물려받는다."""
    # 글자가 한 자씩 떨어져 나오는 문서가 있어(폰트 문제 - "C la s s S")
    # x_tolerance를 넉넉히 준다. 값은 공백을 지우고 쓰므로 붙여 읽어도
    # 안전하고, 붙여야 클래스 이름을 알아본다. 회전 잡음으로 글자가 아예
    # 다른 경로로 쪼개지는 문서는 x_tolerance로 못 고쳐서 pdf_words를
    # 쓴다(find_return_rows_on_page 위쪽 설명 참고).
    words = _safe_words(page, x_tolerance=5, keep_blank_chars=False)
    out = []
    header_carry = None
    for t in (page.find_tables(table_settings=settings) if settings
              else page.find_tables()):
        cells = [c for c in t.cells if c]
        if len(cells) < 8:
            continue
        raw_x0s = sorted({round(c[0], 1) for c in cells})
        col_x0s = []
        for x in raw_x0s:
            if col_x0s and x - col_x0s[-1] <= 6:
                continue
            col_x0s.append(x)
        if len(col_x0s) < 3:
            continue

        def col_of(x0):
            return min(range(len(col_x0s)), key=lambda k: abs(col_x0s[k] - x0))

        bands = sorted({(round(c[1], 1), round(c[3], 1)) for c in cells})
        grid = []
        for top, bottom in bands:
            ent = {}
            for (x0, ct, x1, cb) in [c for c in cells
                                     if abs(c[1] - top) < 1 and abs(c[3] - bottom) < 1]:
                ws = [w for w in words
                      if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1
                      and ct - 1 <= (w["top"] + w["bottom"]) / 2 <= cb + 1]
                ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                txt = " ".join(w["text"] for w in ws).strip()
                if txt:
                    ent[col_of(x0)] = txt
            grid.append({"top": top, "bottom": bottom, "cells": ent})

        # 이 표가 통째로 "나. 연도별 수익률 추이"면 여기서 버린다. 머리글을
        # 못 찾아도 앞 페이지 열 구성을 물려받는 길이 아래에 있어서, 표
        # 단위로 먼저 막지 않으면 연도별 표가 연평균 열 이름을 뒤집어쓴다.
        # "설정일 이후" 칸이 보이면 이 격자 안에 연평균 표도 같이 들어
        # 있다는 뜻이라(페이지 전체가 표 하나로 잡히는 문서) 통째로 버리지
        # 않고, 아래 머리글 단위 검사에 맡긴다.
        grid_texts = [v for r in grid for v in r["cells"].values()]
        flat_grid = re.sub(r"\s+", "", " ".join(grid_texts))
        if (_looks_yearly(grid_texts)
                and ("년차" in flat_grid or "연도별" in flat_grid)
                and "설정일이후" not in flat_grid and "설정이후" not in flat_grid):
            continue

        # 페이지 전체가 표 하나로 잡히는 문서가 많다(보수표·수익률표·
        # 운용전문인력표가 한 격자 안에 다 들어 있다). 그래서 "첫 데이터
        # 행 앞이 머리글"이라는 규칙은 못 쓴다 - 대신 수익률 표 열 이름이
        # 실제로 붙어 있는 띠를 격자 어디서든 찾아 그 아래를 데이터로 본다.
        # 보수표의 비용예시 머리글도 "1년/2년/3년"이라 똑같이 걸리는데,
        # 거기엔 "설정일 이후" 칸이 없다는 점으로 구분한다.
        for gi, anchor in enumerate(grid):
            fmap = {}
            for ci, v in anchor["cells"].items():
                if _is_multi_header(v):
                    continue
                f = _return_column_field(v)
                if f and f not in fmap.values():
                    fmap[ci] = f
            if sum(1 for f in fmap.values() if f.endswith("y")) < 2:
                continue
            # "종류/최초설정일/설정일 이후"는 기간 라벨과 다른 띠에 그려지는
            # 문서가 있다(KR555202013M 실측: 626.3띠와 637.4띠,
            # KR5116501001 실측: 212.0띠와 227.4띠). 앵커 띠 근처(±30pt)에서
            # 시작하는 띠들을 같은 머리글로 본다. 한 이름이 여러 띠로
            # 쪼개지기도 해서("설정일" / "이후" - KR5131420007 실측) 칸마다
            # 조각을 모아 이어 붙인 뒤에 이름을 판단한다.
            hb_bottom = anchor["bottom"]
            parts_by_col, band_of = {}, {}
            for other in grid:
                if abs(other["top"] - anchor["top"]) > 30:
                    continue
                # 세로로 병합된 머리글 칸("종류 / 최초설정일 / 설정일 이후"가
                # 기간 라벨 두 줄을 아우르는 높이로 그려진다)이 흔해서
                # 높이 제한을 너무 낮게 잡으면 그 칸들을 통째로 놓친다
                # (KR5129420031 실측: 61pt짜리 띠가 잘려 표를 못 찾았다).
                if other["bottom"] - other["top"] > 100:
                    continue
                for ci, v in other["cells"].items():
                    if len(re.sub(r"\s+", "", v)) > 24 or _is_multi_header(v):
                        continue
                    parts_by_col.setdefault(ci, []).append(v)
                    band_of.setdefault(ci, []).append(other["bottom"])
            for ci, parts in parts_by_col.items():
                if ci in fmap:
                    hb_bottom = max([hb_bottom] + band_of[ci])
                    continue
                uniq = list(dict.fromkeys(parts))
                for cand in ["".join(parts), "".join(uniq)] + uniq:
                    f = _return_column_field(cand)
                    if f and f not in fmap.values():
                        fmap[ci] = f
                        hb_bottom = max([hb_bottom] + band_of[ci])
                        break
            # 이 머리글이 연도별 표의 것인지는 바로 아래 (기간) 줄이
            # 말해 준다. 한 격자 안에 가·나 표가 다 들어 있어 표 단위로는
            # 못 버린 경우를 여기서 거른다.
            if _looks_yearly(v for ps in parts_by_col.values() for v in ps):
                continue
            cols_here, right_limit = col_x0s, max(c[2] for c in cells)
            if "since_inception" not in fmap.values():
                # 표 테두리 오른쪽 바깥에 "설정일 이후" 칸이 칸 선 없이
                # 글자만 놓인 문서가 있다(KR5129420025 실측: 표는 x=487에서
                # 끝나는데 머리글은 500, 값은 508에 있다). 머리글 줄 높이에
                # 그 글자가 보이면 열을 하나 더 만든다.
                head_ws = sorted((w for w in words
                                  if w["x0"] > right_limit - 2
                                  and anchor["top"] - 20 <= w["top"] <= hb_bottom + 5),
                                 key=lambda w: w["x0"])
                joined = re.sub(r"\s+", "", " ".join(w["text"] for w in head_ws))
                if "설정일이후" not in joined and "설정이후" not in joined:
                    # "설정일 이후" 칸이 병합 머리글 안에만 있어 따로 안
                    # 잡히는 문서가 있다(KR5144420020 실측). 이때는
                    # "최초설정일" 칸이 있는지로 수익률 표를 가린다 -
                    # 보수표의 비용예시(1년/2년/3년/5년/10년)에는 최초설정일
                    # 칸이 없어서 이 조건으로 확실히 구분된다.
                    if not ("inception_date" in fmap.values()
                            and sum(1 for f in fmap.values() if f.endswith("y")) >= 3
                            and not any(re.sub(r"\s+", "", v) == "10년"
                                        for ps in parts_by_col.values()
                                        for v in ps)):
                        continue
                    cols_here = col_x0s
                else:
                    cols_here = col_x0s + [right_limit]
                    fmap[len(cols_here) - 1] = "since_inception"
                    right_limit = page.width


            # 머리글 아래로 이어지는 값 행들. 각주·다른 표를 만나면 멈춘다.
            period_cols = [c for c, f in fmap.items()
                           if f.endswith("y") or f == "since_inception"]
            # 표가 어디서 끝나는지는 "값 없는 띠 몇 개"로 세면 안 된다 -
            # 격자에는 빈 띠와 클래스명 조각 띠가 잔뜩 섞여 있어서
            # 금방 3개가 차버린다(KR5116501001 실측: 비교지수·변동성 행을
            # 놓쳤다). 마지막으로 값이 나온 행에서 세로로 얼마나 떨어졌는지
            # 로 본다.
            def collect(cols):
                """cols=None이면 어느 칸이든 값이 2개 이상인 띠를 모은다
                (열 매핑을 아직 확정하기 전에 쓰는 후보)."""
                got, last = [], None
                for r in grid:
                    if r["top"] < hb_bottom - 1:
                        continue
                    n = (sum(1 for v in r["cells"].values() if _val(v)) if cols is None
                         else sum(1 for c in cols if _val(r["cells"].get(c, ""))))
                    if n >= 2:
                        got.append(r)
                        last = r["bottom"]
                    elif last is not None and r["top"] - last > 60:
                        break
                return got

            # 머리글 칸과 값 칸의 x가 어긋나는 표가 있다(KR5113420012 실측:
            # "최근 5년" 머리글은 7번 열인데 값은 6번 열에 있다.
            # KR5174420011 실측: 머리글이 2/4/6/8번인데 값은 1/3/5/7번이라
            # 표 전체가 한 칸씩 밀려 있다 - 이걸 먼저 고치지 않으면 값
            # 행을 하나도 못 찾아 표를 통째로 버린다). 그래서 열 매핑을
            # 확정하기 전에, 머리글 아래에서 값이 있는 띠를 먼저 모아
            # "실제로 값이 들어 있는 칸"을 알아낸 뒤 어긋남을 고친다.
            value_cols = {ci for r in collect(None)
                          for ci, v in r["cells"].items() if _val(v)}
            # 값 칸 하나를 두고 머리글 없는(=제 칸에 값이 없는) 기간이 여럿
            # 후보로 몰릴 수 있다(KR5120420091 6쪽 실측: 설정 2년밖에 안 된
            # 펀드라 3년/5년 칸이 통째로 비어 있고, 값은 딱 1개(설정일이후)
            # 만 남는데 "5년" 헤더도 "설정일이후" 헤더도 둘 다 그 값 칸을
            # 이웃(±1)으로 볼 수 있다. 순서대로 먼저 온 "5년"이 거리와
            # 상관없이 먼저 채가면, 실제로는 그 값이 진짜 "설정일이후"
            # 값인데 "5년" 이름표가 붙고, 뒤에 남은 "설정일이후" 빈 칸은
            # 아래 "칸이 아예 안 생긴 열" 채우기 로직이 같은 값을 원본
            # 단어에서 다시 긁어와 중복까지 만든다). 이웃 후보 전부를
            # 모아 x좌표가 진짜로 더 가까운 헤더부터 먼저 배정한다.
            # "설정일 이후" 칸이 표 테두리 밖에 있어(위 참고) col_x0s 범위
            # 밖의 합성 열 번호(len(cols_here)-1)로 fmap에 들어가 있을 수
            # 있다 - 그런 열은 실제 x좌표(col_x0s[ci])가 없어 거리를 잴 수
            # 없으므로 이 재배정 대상에서 제외한다(원래 첫 번째 값 칸 방식
            # 그대로 유지).
            orphans = [ci for ci in sorted(fmap)
                       if fmap[ci] not in ("label", "inception_date")
                       and ci not in value_cols
                       and 0 <= ci < len(col_x0s)]
            candidates = sorted(
                (abs(col_x0s[nb] - col_x0s[ci]), ci, nb)
                for ci in orphans
                for nb in (ci - 1, ci + 1)
                if nb in value_cols and 0 <= nb < len(col_x0s)
            )
            claimed_targets = set()
            for _, ci, nb in candidates:
                if ci not in fmap or nb in fmap or nb in claimed_targets:
                    continue
                fmap[nb] = fmap.pop(ci)
                claimed_targets.add(nb)
            period_cols = [c for c, f in fmap.items()
                           if f.endswith("y") or f == "since_inception"]
            rows2 = collect(period_cols)
            # "설정일 이후" 칸 이름이 병합 머리글 안에만 있어 따로 안 잡히는
            # 문서가 있다(KR5144420020 실측). 이 표에서 설정일이후는 항상
            # 5년 칸 오른쪽 첫 값 칸이므로 그 자리로 채운다.
            if rows2 and "since_inception" not in fmap.values():
                ymax = max((c for c, f in fmap.items() if f.endswith("y")),
                           default=None)
                vcols = sorted({ci for r in rows2 for ci, v in r["cells"].items()
                                if _val(v) and ci not in fmap
                                and ymax is not None and ci > ymax})
                if vcols:
                    fmap[vcols[0]] = "since_inception"
                    period_cols.append(vcols[0])
                    rows2 = collect(period_cols)
            if not rows2:
                # 머리글만 있고 값 행은 통째로 다음 페이지에 있는 문서가
                # 있다(KR510902773M 실측: 44쪽 맨 아래에 "가.연평균수익률"
                # 머리글, 45쪽부터 클래스 행). 값 행이 없어도 열 구성은
                # 다음 페이지가 물려받을 수 있게 남겨 둔다.
                if header_carry is None:
                    header_carry = (fmap, [], cells, col_x0s, words)
                continue

            # 세로줄이 값 구간에서 끊겨 그 열의 칸이 아예 안 생기는 표가
            # 있다(KR555202013M 실측: "설정일 이후" 값이 통째로 빠졌다).
            # 열의 x범위와 행의 y범위는 표에서 이미 아니까 그 사각형을
            # 직접 읽어 채운다.
            table_x1 = right_limit
            for r in rows2:
                for ci in period_cols:
                    if ci in r["cells"]:
                        continue
                    lo = cols_here[ci]
                    # 다음 열이 이 표에서 실제로 쓰이지 않으면(칸도 안
                    # 그려지고 매핑도 없으면) 그 자리까지가 이 열의 폭이다.
                    # 머리글 칸은 496에서 시작하는데 값은 528에 찍히는
                    # 문서가 있어(KR5127420034 실측: 설정일 이후 값이
                    # 통째로 빠졌다) 폭을 좁게 잡으면 못 읽는다.
                    used = {c for rr in rows2 for c in rr["cells"]} | set(fmap)
                    hi = table_x1
                    for j in range(ci + 1, len(cols_here)):
                        if j in used:
                            hi = cols_here[j]
                            break
                    ws = [w for w in words
                          if lo - 1 <= (w["x0"] + w["x1"]) / 2 < hi
                          and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1]
                    if not ws:
                        continue
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    txt = " ".join(w["text"] for w in ws).strip()
                    if _val(txt):
                        r["cells"][ci] = txt

            out.append((fmap, rows2, cells, cols_here, words))

        if out:
            continue

        # 이 표에서 머리글을 못 찾았으면 앞 페이지 열 구성을 물려받아 본다.
        if not inherited:
            continue
        prev_fields, prev_cols = inherited
        mapped, matched, best = {}, 0, {}
        for ci, x in enumerate(col_x0s):
            cand = [k for k in range(len(prev_cols)) if abs(prev_cols[k] - x) <= 8]
            if not cand:
                continue
            matched += 1
            # 앞 장 열 두 개가 똑같이 가까울 때 필드가 붙은 쪽을 먼저 본다
            # (class_fees에서 부동소수점 동점으로 열을 잃은 적이 있다).
            fielded = [k for k in cand if k in prev_fields]
            if not fielded:
                continue
            near = min(fielded, key=lambda k: abs(prev_cols[k] - x))
            dist = abs(prev_cols[near] - x)
            if near not in best or dist < best[near][0]:
                best[near] = (dist, ci)
        for near, (_, ci) in best.items():
            mapped[ci] = prev_fields[near]
        # 페이지마다 표가 통째로 조금씩 밀려 그려지는 문서가 있다
        # (KR5120420039 실측: 6쪽 456.2 / 7쪽 447.4처럼 4~10pt씩 어긋난다).
        # x만 보면 오른쪽 열부터 매칭이 끊긴다 - 열 개수가 앞 장과 같으면
        # 순서대로 맞추는 게 확실하다(이어지는 표는 열 구성이 같으니까).
        if len(mapped) < len(prev_fields) and len(col_x0s) == len(prev_fields):
            mapped = {ci: prev_fields[k]
                      for ci, k in enumerate(sorted(prev_fields))}
            matched = len(col_x0s)
        pcols = [c for c, f in mapped.items()
                 if f.endswith("y") or f == "since_inception"]
        if matched < 3 or len(pcols) < 3:
            continue
        # 이어지는 표가 맞는지 행 단위로 확인한다 - 열 위치만 비슷한
        # 다른 표(운용전문인력 등)에 매핑이 씌워지면 엉뚱한 행이 생긴다.
        rows3, last = [], None
        for r in grid:
            # 기간 칸이 일부 비어 있는 클래스가 있다(KR5194450018 실측:
            # 설정한 지 얼마 안 된 RP-e/S-P/CP-e는 1년·2년·설정후 세 칸만
            # 있다). "거의 다 채워져야 한다"고 요구하면 그런 클래스를
            # 통째로 잃는다.
            if sum(1 for c in pcols if _val(r["cells"].get(c, ""))) >= max(2, int(len(pcols) * 0.6)):
                rows3.append(r)
                last = r["bottom"]
            elif last is not None and r["top"] - last > 60:
                break
        if rows3:
            # 이어받은 열 구성에 "설정일 이후"가 없으면(앞 장에서 그 칸
            # 이름이 병합 머리글 안에만 있었던 경우) 5년 칸 오른쪽 첫 값
            # 칸으로 채운다(KR5144420020 실측).
            if "since_inception" not in mapped.values():
                ymax = max((c for c, f in mapped.items() if f.endswith("y")),
                           default=None)
                vcols = sorted({ci for r in rows3 for ci, v in r["cells"].items()
                                if _val(v) and ci not in mapped
                                and ymax is not None and ci > ymax})
                if vcols:
                    mapped[vcols[0]] = "since_inception"
            out.append((mapped, rows3, cells, col_x0s, words))
    if out:
        return out
    return [header_carry] if header_carry else None


def _section_marks(words):
    """이 페이지에서 "가.연평균수익률" / "나.연도별 수익률 추이" 제목이
    나오는 세로 위치. [(y, "가"|"나"), ...]"""
    lines = {}
    for w in words:
        lines.setdefault(round(w["top"] / 3), []).append(w)
    marks = []
    for line in lines.values():
        line.sort(key=lambda w: w["x0"])
        flat = re.sub(r"\s+", "", "".join(w["text"] for w in line))
        y = min(w["top"] for w in line)
        if SECTION_NA_RE.search(flat):
            marks.append((y, "나"))
        elif SECTION_GA_RE.search(flat):
            marks.append((y, "가"))
    return sorted(marks)


def return_rows_for_doc(doc_id, pdf, pages, known_classes=None):
    """요약표/상세표의 연평균수익률 표를 셀 격자로 읽어 레코드를 만든다."""
    rows = []
    inherited, prev_page = None, None
    # "가.연평균 / 나.연도별" 구분을 페이지를 넘어 이어 간다.
    #
    # 표 하나만 보는 가드(_looks_yearly)는 그 표 안에 "년차"나 기간
    # 머리글이 있어야 걸린다. 연도별 표가 페이지 경계에서 잘리면 뒷장
    # 조각엔 머리글이 하나도 없어서 그냥 통과한다(KR510902777M 실측:
    # 48쪽에 「가.연평균」과 「나.연도별」이 같이 있고 49쪽은 연도별 표의
    # 이어진 부분인데, 49쪽 조각만 보면 연도별인 줄 알 수가 없다).
    #
    # 그러면 49쪽 연도별 값이 연평균 행으로 들어가고, 뒤에서 같은
    # 클래스끼리 합칠 때 "뒤쪽 페이지가 이긴다"로 3쪽의 맞는 연평균을
    # 밀어낸다(C-e: 68.88/35.97/22.67/16.30/12.17 -> 68.88/9.57/
    # -0.18/-20.87/45.60). 1년 값이 같아서 눈으로도 잘 안 보인다.
    #
    # 좌표 방식(find_return_rows_on_page)은 이 상태를 이미 물려받고
    # 있었다. 셀 격자 방식에만 없었다.
    section = "가"
    for page_num in pages:
        if page_num < 1 or page_num > len(pdf.pages):
            continue
        page = pdf.pages[page_num - 1]
        # 열 구성 이어받기는 "바로 다음 페이지"에서만 허용한다 - 떨어진
        # 페이지의 무관한 표에 씌우면 엉뚱한 행이 생긴다.
        got = _return_grid(page, inherited if prev_page == page_num - 1 else None)
        if not got:
            continue
        inherited = (got[-1][0], got[-1][3])
        prev_page = page_num
        marks = _section_marks(_safe_words(page, x_tolerance=2))
        page_start_section = section
        if marks:
            section = marks[-1][1]
        for field_by_col, data_rows, cells, col_x0s, words in got:
            label_cols = [c for c, f in field_by_col.items() if f == "label"]
            first_val = min((c for c, f in field_by_col.items()
                             if f.endswith("y") or f == "since_inception"),
                            default=len(col_x0s))
            date_col = next((c for c, f in field_by_col.items()
                             if f == "inception_date"), None)
            period_set = {c for c, f in field_by_col.items()
                          if f.endswith("y") or f == "since_inception"}
            for r in data_rows:
                # 이 행이 "나.연도별 수익률 추이" 아래에 있으면 버린다.
                # 한 페이지에 두 표가 같이 있는 문서가 있어서(위 48쪽)
                # 페이지 단위가 아니라 행의 세로 위치로 가른다.
                row_section = page_start_section
                for y, s in marks:
                    if y < r["top"]:
                        row_section = s
                if row_section == "나":
                    continue
                # 클래스명 칸이 여러 행에 걸쳐 병합돼 있으면 이 행 자신의
                # 칸은 비어 있다 - 이 행을 세로로 품고 있는 왼쪽 칸을 쓴다.
                label = " ".join(r["cells"][c] for c in sorted(label_cols)
                                 if c in r["cells"]).strip()
                if not label:
                    # 클래스명이 값 행보다 잘게 쪼개진 여러 띠에 나뉘어
                    # 그려지는 표가 많다(KR5116501001 실측: "수수료미징구-"
                    # / "오프라인-" / "퇴직연금(C-P)(%)" 세 띠). 이 행의
                    # y구간 안에서 첫 값 열보다 왼쪽에 있는 글자를 모은다.
                    lim = col_x0s[first_val] - 2 if first_val < len(col_x0s) else 1e9
                    ws = [w for w in words
                          if (w["x0"] + w["x1"]) / 2 < lim
                          and r["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= r["bottom"] + 1]
                    ws.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
                    label = " ".join(w["text"] for w in ws).strip()

                kind = row_kind(label)
                class_code = None
                if kind == "class_return":
                    class_code = _return_label_code(label, known_classes)
                # 열 구성을 앞 장에서 물려받으면 같은 페이지의 운용전문인력
                # 표에도 매핑이 씌워질 수 있다(KR5120420039 실측: 권용범
                # 1969 본부장 ... 행이 클래스로 잡혔다). 그 표에는 생년
                # (네 자리 연도 단독)이나 운용규모(34,017억원)가 들어 있고
                # 수익률 표에는 그런 값이 없다.
                if kind == "class_return" and class_code is None:
                    toks = " ".join(r["cells"].values()).split()
                    if any(re.fullmatch(r"(19|20)\d{2}", t) for t in toks) or \
                            any(re.match(r"^\d{1,3},\d{3}", t) for t in toks):
                        continue

                values = {}
                for ci, f in field_by_col.items():
                    if not (f.endswith("y") or f == "since_inception"):
                        continue
                    v = _clean_val(r["cells"].get(ci, ""))
                    if not v:
                        continue
                    if DECIMAL_RE.match(v) or NUM_RE.match(v):
                        values[f] = v
                    elif DASH_RE.match(v):
                        values[f] = "-"
                # 같은 격자에 붙어 있는 다른 표(운용전문인력 등)의 행이
                # 값 한두 개만 걸쳐 잡히는 일이 있다 - 이 표가 쓰는 기간
                # 칸이 대부분 채워진 행만 수익률 행으로 본다.
                n_period = sum(1 for f in field_by_col.values()
                               if f.endswith("y") or f == "since_inception")
                if len(values) < max(2, int(n_period * 0.6)):
                    continue

                inception = None
                if date_col is not None:
                    raw = r["cells"].get(date_col, "")
                    if not raw:
                        # 최초설정일 칸이 여러 행에 걸쳐 병합된 경우
                        mid = (r["top"] + r["bottom"]) / 2
                        for c in cells:
                            if abs(col_x0s[date_col] - c[0]) > 3:
                                continue
                            if c[1] - 1 <= mid <= c[3] + 1:
                                ws = [w for w in words
                                      if c[0] - 1 <= (w["x0"] + w["x1"]) / 2 <= c[2] + 1
                                      and c[1] - 1 <= (w["top"] + w["bottom"]) / 2 <= c[3] + 1]
                                raw = " ".join(w["text"] for w in ws)
                                break
                    dm = INCEPTION_DATE_RE.search(raw or "")
                    inception = _normalize_date(dm.group()) if dm else None
                    # 최초설정일 칸에 날짜 대신 숫자가 찍히는 클래스가 있다
                    # (KR5125450070 41쪽 실측: I/C-P/C-P2/C-Pe/C-P2e 클래스 -
                    # 설정일 칸이 원래 비어 있어야 하는데 그 자리에 "최근1년"
                    # 값이 들어가 있고, 나머지 값 칸들은 전부 한 칸씩 밀려
                    # 읽힌다("설정일이후" 칸엔 최근1년 값이 중복으로 다시
                    # 찍힌다 - "나.연도별 수익률" 표의 43쪽 실측으로 대조:
                    # I클래스 최근1년차 -6.93%가 진짜 값이고, 밀리기 전
                    # values['1y']=1.85는 사실 2년째 값이었다). 값 칸이
                    # 정확히 5개(1y~설정일이후) 다 찼을 때만, 밀린 앞칸의
                    # 진짜 값을 되돌리고 마지막(중복된 설정일이후)을 버린다.
                    if kind in ("class_return", "benchmark", "volatility") and dm is None:
                        shift_val = _clean_val(raw or "")
                        if DECIMAL_RE.match(shift_val) or NUM_RE.match(shift_val):
                            ordered_fields = [
                                f for _, f in sorted(
                                    (ci, f) for ci, f in field_by_col.items()
                                    if f.endswith("y") or f == "since_inception")
                            ]
                            if (len(ordered_fields) == 5
                                    and all(f in values for f in ordered_fields)):
                                shifted = {ordered_fields[0]: shift_val}
                                for i in range(1, len(ordered_fields)):
                                    shifted[ordered_fields[i]] = values[ordered_fields[i - 1]]
                                values = shifted
                if inception is None:
                    # 최초설정일 칸이 따로 안 잡히는 표가 있다
                    # (KR5123490013 실측: 날짜가 클래스명 칸에 같이 들어
                    # 있다). 값 칸이 아닌 칸에서 날짜꼴을 찾는다.
                    for ci, v in r["cells"].items():
                        if ci in period_set:
                            continue
                        dm = INCEPTION_DATE_RE.search(v)
                        if dm:
                            inception = _normalize_date(dm.group())
                            break

                # 클래스명 칸과 최초설정일 칸이 붙어 있어서, 설정일 앞자리
                # 숫자가 코드 뒤에 그대로 눌어붙는 문서가 있다(KR5118420062
                # 실측: "ClassC-Pe 2017.08.29 ClassC-Pe ..."처럼 같은 라벨이
                # 두 번 찍히는데, 앞쪽은 날짜와 붙어 "C-Pe20"으로, 뒤쪽은
                # 정상 "C-Pe"로 갈라져 값은 같은데 코드만 다른 행 2개가
                # 생겼다 - 13개 클래스에서 한꺼번에 재현됨). 코드 끝을
                # 무작정 숫자만큼 떼면 "A2"처럼 진짜 숫자로 끝나는 클래스와
                # 헷갈리므로, 이 행 자신의 설정일(연도)과 실제로 일치하는
                # 접미사만 떼어 known_classes에 있는 코드가 나올 때만
                # 받아들인다. 값이 같은 정상 "C-Pe" 행은 아래 dedup에서
                # (row_kind, class_code, values)가 같아져 자동으로 하나로
                # 합쳐진다.
                if class_code and inception and class_code not in (known_classes or ()):
                    year_prefix = inception[:4]
                    for n in (4, 3, 2, 1):
                        suffix = year_prefix[:n]
                        if len(class_code) > n and class_code.endswith(suffix):
                            candidate = class_code[: -n]
                            if candidate in (known_classes or ()):
                                class_code = candidate
                                break

                rows.append({
                    "row_kind": kind,
                    "class_code": class_code,
                    "inception_date": inception,
                    "values": values,
                    "page": page_num,
                    "evidence": (label + " " + " ".join(
                        v for _, v in sorted(r["cells"].items()))).strip(),
                    "method": "cell_grid",
                    "confidence": 1.0 if (class_code or kind != "class_return") else 0.5,
                    "_top": r["top"],
                })
    # 클래스명이 여러 띠에 쪼개져 그려지면 같은 값 행이 라벨만 다르게
    # 두 번 잡히기도 한다(KR5123490013 실측). 값이 똑같은데 한쪽만 클래스
    # 코드를 찾은 경우 코드 있는 쪽만 남긴다.
    # 주의: 값만으로 묶으면 안 된다. 아직 수익률이 없는 신규 클래스는
    # 값이 전부 "-"라 서로 구분이 안 돼 C1/C2/C3가 한 행으로 뭉개진다
    # (KR510902511M 실측). 코드가 다르면 다른 행이고, 값이 같은데 한쪽만
    # 코드를 못 찾은 경우에만 코드 있는 쪽을 남긴다.
    keyed = {}
    with_code = {(r["row_kind"], tuple(sorted((r.get("values") or {}).items())))
                 for r in rows if r.get("class_code")}
    for r in rows:
        vals_k = tuple(sorted((r.get("values") or {}).items()))
        if r.get("class_code"):
            k = (r["row_kind"], r["class_code"], vals_k)
        elif r["row_kind"] == "class_return" and (r["row_kind"], vals_k) in with_code:
            continue          # 같은 값을 코드까지 찾은 행이 이미 있다
        else:
            k = (r["row_kind"], None, vals_k)
        keyed.setdefault(k, r)
    # 비교지수·변동성 행의 최초설정일은 클래스 행과 병합된 칸에 한 번만
    # 찍혀서 이어지는 페이지에선 비어 보인다. 바로 앞 행의 날짜를 잇는다.
    out, last_date = [], None
    for r in rows:
        vals_k = tuple(sorted((r.get("values") or {}).items()))
        k = ((r["row_kind"], r["class_code"], vals_k) if r.get("class_code")
             else (r["row_kind"], None, vals_k))
        if keyed.get(k) is not r:
            continue
        # 비교지수·변동성은 펀드 전체 기준이라 최초설정일도 펀드의 것
        # (표에서 가장 처음 나오는 클래스의 설정일)이다. 마지막 클래스
        # 것을 쓰면 나중에 만들어진 클래스 날짜가 붙는다(KR5153450009
        # 실측: 2006-01-04이어야 하는데 2017-10-24가 붙었다).
        if r["row_kind"] == "class_return" and r.get("inception_date") \
                and last_date is None:
            last_date = r["inception_date"]
        elif r["row_kind"] in ("benchmark", "volatility") and not r.get("inception_date"):
            r["inception_date"] = last_date
        out.append(r)
    return out


def col_of_lt(cell, col_x0s, first_val):
    """셀이 첫 값 열보다 왼쪽에 있는지."""
    return cell[0] < col_x0s[first_val] - 2 if first_val < len(col_x0s) else True


def _normalize_class_code(code, known_classes):
    """이 표에서 찾은 코드가 문서 다른 곳(class_fees.json이 아는 코드)이
    쓰는 정식 표기와 다를 수 있다 - "C-" 같은 클래스 계열 접두가 이 표
    에서만 빠지는 문서상 오탈자가 있다(KR5160420009 41쪽 실측: 가.연평균
    표엔 "(P2e)"인데, 같은 문서 나.연도별 표와 보수표는 전부 "(C-P2e)").
    이미 아는 코드 목록에 그대로 있으면 안 건드리고, 없을 때만 접두가
    붙은 유일한 후보로 바꾼다 - 후보가 둘 이상이면 어느 쪽인지 모르므로
    손대지 않는다."""
    if not code or not known_classes or code in known_classes:
        return code
    candidates = [k for k in known_classes if k != code and k.endswith(code)]
    if len(candidates) == 1:
        return candidates[0]
    # 수익률 표에서만 "-"가 통째로 빠지는 오탈자도 있다(KR5120420039 6쪽
    # 실측: 이 표만 "ClassAi"라 붙여 찍었고, 같은 문서 보수표/나.연도별
    # 표는 전부 "A-i" - class_fees.json이 아는 정식 코드도 "A-i"). "-"를
    # 지운 모양이 이 코드와 유일하게 일치하는 후보로만 바꾼다.
    dash_candidates = [
        k for k in known_classes if k != code and k.replace("-", "") == code
    ]
    if len(dash_candidates) == 1:
        return dash_candidates[0]
    # 반대 방향(이 표만 붙임표를 더 찍는 문서)도 있다(KR5123490013/16/17
    # 실측: 가입자격·수익률 표는 전부 "A-e"/"C-e"인데 class_fees.json이
    # 아는 정식 표기는 "Ae"/"Ce" - 표마다 붙임표를 넣거나 빼는 게 이
    # 문서만의 습관이다. class_charges.py에서 이미 겪은 것과 같은 종류의
    # 표기 흔들림). 이 코드에서 붙임표를 지운 모양이 아는 코드와 유일하게
    # 일치할 때만 바꾼다.
    no_dash_candidates = [
        k for k in known_classes if k != code and code.replace("-", "") == k
    ]
    if len(no_dash_candidates) == 1:
        return no_dash_candidates[0]
    # 수익률 표는 "연금"/"퇴직연금" 같은 괄호 구분자를 통째로 생략하고
    # 찍는 문서가 있다(KR5120420091 실측: 가.연평균/상세표 둘 다 그냥
    # "Class C-P" / "Class C-R"인데, 정식 코드(보수표·class_fees.json)는
    # "C-P(연금)" / "C-R(퇴직연금)" - 판매채널 구분이라 괄호를 지우면
    # 다른 뜻의 클래스가 될 수 있어 함부로 못 뭉갠다). 이 코드로 시작하고
    # 나머지가 온전히 "(한글)" 괄호 하나뿐인 후보가 유일할 때만 바꾼다.
    suffix_candidates = [
        k for k in known_classes
        if k != code and k.startswith(code)
        and re.fullmatch(r"\([가-힣]+\)", k[len(code):])
    ]
    if len(suffix_candidates) == 1:
        return suffix_candidates[0]
    # 수익률 표만 알파벳 대소문자가 다르게 찍히는 문서가 있다(KR5160420009
    # 실측: 보수표·부속서류는 전부 "A-E"인데 가.연평균/나.연도별 표만
    # "A-e" - 같은 클래스의 단순 오탈자다). 대소문자만 다른 후보가 유일할
    # 때만 바꾼다.
    ci_candidates = [
        k for k in known_classes if k != code and k.lower() == code.lower()
    ]
    return ci_candidates[0] if len(ci_candidates) == 1 else code


def _return_label_code(label, known_classes):
    """클래스명 칸 하나에서 클래스 코드를 뽑는다. 셀 기준이라 옆 행 이름이
    섞여 들어올 일이 없어서 좌표 방식의 여러 예외 규칙이 필요 없다."""
    if not label:
        return None
    flat = re.sub(r"\s+", "", label)
    code = None
    m = CLASS_CODE_RE.search(flat)
    if m:
        code = m.group(1)
    if code is None:
        # 글자가 떨어져 나오는 문서("C la s s S")도 있어 공백을 지운 쪽을 본다
        m2 = CLASS_CODE_NOPAREN_RE.search(flat)
        if m2:
            code = m2.group(1)
    if code is None:
        m3 = CLASS_CODE_JONGRYU_RE.search(flat)
        if m3:
            code = m3.group(1)
    if code is None and known_classes:
        m4 = CLASS_CODE_JONGRYU_KO_RE.search(flat)
        if m4 and m4.group(1) in known_classes:
            code = m4.group(1)
    if code is None and known_classes:
        for kc in sorted(known_classes, key=len, reverse=True):
            if flat.endswith(kc):
                before = flat[: -len(kc)]
                if not before or not before[-1].isalnum():
                    code = kc
                    break
    if code is None and known_classes:
        # "I형(수수료미징구-오프라인-기관)"처럼 코드가 뒤가 아니라 맨
        # 앞에 오고, 괄호 안은 코드가 아니라 수수료방식 설명인 문서가
        # 있다(KR5125450070 41쪽 실측: 이 표기라서 위의 모든 갈래가
        # 못 읽었다 - "I"/"C-P"/"CG" 등 6개 클래스 전부가 class_code
        # 없이 통째로 버려졌다). 라벨이 아는 코드로 시작하고 바로 뒤가
        # "형"이면(길이가 긴 코드부터 봐야 "A"가 "Ae"보다 먼저 걸리지
        # 않는다) 그 코드로 본다.
        for kc in sorted(known_classes, key=len, reverse=True):
            if flat.startswith(kc) and flat[len(kc):len(kc) + 1] == "형":
                code = kc
                break
            # 이 표만 "-"가 빠진 표기를 쓰기도 한다(KR5125450070 41쪽
            # 실측: "CG형(...)"인데 class_fees가 아는 코드는 "C-G").
            kc_nodash = kc.replace("-", "")
            if kc_nodash != kc and flat.startswith(kc_nodash) and \
                    flat[len(kc_nodash):len(kc_nodash) + 1] == "형":
                code = kc
                break
    return _normalize_class_code(code, known_classes)

def _apply_merged_cell_dates(page, words, rows):
    """'최초설정일' 칸이 여러 행에 걸쳐 병합된 경우, 날짜 텍스트는 병합된 셀
    안 어딘가(보통 시각적 중앙에 가까운 한 행) 한 번만 찍히고 나머지 행은
    비어 보인다. 인접한 줄 순서만 보고 전파하면 실제로는 안 겹치는 행에
    잘못된 날짜가 번질 위험이 있어(예: 병합 안 된 바로 다음 클래스가 우연히
    날짜가 없는 경우), 대신 PDF에 실제로 그려진 셀 테두리(page.rects)를 찾아
    그 테두리 안에 들어오는 행들에만 정확히 전파한다."""
    if not rows:
        return
    tops = [r["_top"] for r in rows]
    y_lo, y_hi = min(tops) - 15, max(tops) + 15

    # 이 표 범위 안에서 실제로 잡힌 설정일 텍스트의 x좌표로 '최초설정일' 칸의
    # 위치를 추정한다 (표마다 칸 위치가 조금씩 다를 수 있어 페이지별로 다시 잡음).
    date_x0 = None
    for w in words:
        if y_lo <= w["top"] <= y_hi and INCEPTION_DATE_RE.fullmatch(w["text"]):
            date_x0 = w["x0"]
            break
    if date_x0 is None:
        return

    col_cells = [
        rc for rc in page.rects
        if abs(rc["x0"] - date_x0) < 15
        and (rc["bottom"] - rc["top"]) > 8
        and rc["top"] >= y_lo - 5 and rc["bottom"] <= y_hi + 5
    ]
    for rc in col_cells:
        cell_words = [
            w for w in words
            if rc["top"] - 1 <= w["top"] <= rc["bottom"] + 1
            and rc["x0"] - 2 <= w["x0"] <= rc["x1"] + 2
        ]
        cell_date = next((w["text"] for w in cell_words if INCEPTION_DATE_RE.fullmatch(w["text"])), None)
        if not cell_date:
            continue
        for r in rows:
            if rc["top"] - 1 <= r["_top"] <= rc["bottom"] + 1:
                r["inception_date"] = _normalize_date(cell_date)


def candidate_pages_for_doc(doc_id, max_page):
    """"최근"+"설정일"만으로 범위를 잡으면 문서 뒤쪽 상세 부속서류(제2부 등)의
    모펀드 관련 반복 섹션까지 걸려서 범위가 너무 넓어진다 (KR5113420012에서
    확인: 51/52페이지에 모펀드용 표가 또 있음). "투자실적추이"는 요약정보
    섹션에만 붙는 라벨이라 훨씬 정확한 스코핑 기준이다.

    ("최근"+"설정일" 둘 다로 넓혀서 실측해봤다가 되돌림: KR5113420012
    51페이지가 실제로는 C-e/S-P까지 있는 진짜 "가" 표였던 건 맞지만,
    코퍼스 전체로 넓히면 67개 문서에 새 후보 페이지가 생기면서 600건→
    1799건, 문서당 30~54행까지 튀는 걸 확인했다 - 대부분 무관한 표까지
    "최근"+"설정일" 문구가 우연히 같이 들어있어 걸린 것으로 보인다.
    "투자실적" 하나만으로 완전일치 스코핑하던 원래 방식이 안전하고,
    KR5113420012 같은 개별 문서의 후보 페이지 누락은 문서 단위로 더
    구조적인 판정 기준(예: 표 헤더가 "종류"+"최근N년"+"설정일이후" 컬럼
    구조와 정확히 일치하는지)을 새로 설계해야 한다 - 다음 과제로 남김.)"""
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_tables.json")
    if not os.path.exists(fp):
        return []
    with open(fp, "r", encoding="utf-8") as f:
        tables = json.load(f)

    pages = set()
    for t in tables:
        flat = "".join(c for row in t["data"] for c in row if c)
        if "투자실적" in flat:
            pages.add(t["page"])
            if t["page"] + 1 <= max_page:
                pages.add(t["page"] + 1)
    return sorted(pages)


_KNOWN_CLASSES_BY_DOC = None


def _known_classes_for_doc(doc_id):
    """class_fees.json에 이미 확인된 이 상품의 class_code 목록(있으면) -
    상세 부속서류에서 라벨이 상품 전체 명칭에 붙어 나올 때 class_code
    보강용으로 쓴다. class_fees.json이 없으면(아직 안 만들었으면) 그냥
    빈 결과로 조용히 넘어간다 - 이 보강은 있으면 좋고 없어도 기존 동작
    그대로다."""
    global _KNOWN_CLASSES_BY_DOC
    if _KNOWN_CLASSES_BY_DOC is None:
        _KNOWN_CLASSES_BY_DOC = defaultdict(set)
        fp = os.path.join(REPO_ROOT, "class_fees.json")
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                for r in json.load(f):
                    if r.get("class_code"):
                        _KNOWN_CLASSES_BY_DOC[r["product_code"]].add(r["class_code"])
    return _KNOWN_CLASSES_BY_DOC.get(doc_id, set())


# 앞쪽 "요약정보" 섹션의 "가.연평균수익률" 표에는 대표 클래스 한두 개만 싣고,
# 전 클래스 수익률은 문서 뒤쪽(제2부 상세)의 같은 형식 표에만 싣는 문서가 아주
# 많다(KR5122420005 실측: 요약표엔 ClassC 하나뿐인데 52페이지 상세표엔 14개
# 클래스가 전부 있음). candidate_pages_for_doc은 "투자실적" 캡션이 붙은
# 요약 섹션만 보기 때문에 이 상세표를 통째로 놓쳤고, 그 결과 class_fees.json
# 기준 612개 클래스 중 수익률이 있는 건 269개뿐이었다(399개 클래스의 수익률이
# 빠짐 - 6축 중 "수익률" 축이 문서당 사실상 1개 클래스만 있는 상태였다).
#
# 이걸 고치려고 candidate_pages_for_doc의 페이지 후보를 넓히는 방식은 이미
# 3번 시도했다가 전부 되돌렸다(위 그 함수의 주석 참고 - 600건이 1799~1822건
# 으로 튀고, 좌수 변동표처럼 "라벨 + 숫자 5개" 모양이 똑같은 무관한 표까지
# 딸려 들어왔다). 페이지의 "모양"만으로는 이 상품의 진짜 수익률 표와 우연히
# 같은 모양인 다른 표를 구분할 수 없다는 게 그때의 결론이었다.
#
# 그래서 class_fees.py의 enrich_with_detail_fee_table에서 검증된 방식을 쓴다:
# 모양이 아니라 "이미 확실히 아는 값과 대조"해서 판단한다. 요약표에서 뽑아
# 둔 클래스(위 예시의 ClassC = 2.85/3.49/4.08/2.93/2.37)가 그 페이지에도
# 있고 값이 전부 일치할 때만 그 표를 "같은 표"로 인정하고, 거기 있는 나머지
# 클래스를 가져온다. 값이 하나라도 어긋나면 그 페이지는 통째로 버린다.
#
# 이 대조 하나로 "나.연도별 수익률 추이" 표(1~5년차 단년도 수익률 - 컬럼
# 의미가 달라서 같은 스키마에 넣으면 안 되는 표)도 자동으로 걸러진다:
# KR5122420005 53페이지의 ClassC는 2.85/4.13/5.29/1.93/0.50 이라 첫 값만
# 우연히 같고 나머지가 전부 달라 검증에 실패한다(섹션 제목 판정에 더해
# 이중 안전장치가 되는 셈).
def _values_agree(a, b):
    """(일치하는 숫자 칸 수, 어긋난 칸이 있는지) - 한쪽이 "-"인 칸은 비교에서
    제외한다(아직 수익률이 없는 클래스는 원본이 "-"로 두는데, 요약표와
    상세표의 기준일이 달라 한쪽에만 값이 생겼을 수 있어 그것만으로 다른
    표라고 볼 근거는 안 된다)."""
    matched = 0
    for k in PERIOD_LABELS:
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            continue
        try:
            fa, fb = float(va), float(vb)
        except (TypeError, ValueError):
            continue  # "-" 등 숫자가 아닌 칸
        if abs(fa - fb) <= 0.0005:
            matched += 1
        else:
            return matched, True
    return matched, False


def _merge_detail_into_summary(summary, detail):
    """같은 클래스가 요약표와 상세표에 둘 다 있을 때의 합치기 규칙.

    처음엔 "요약표 행을 그대로 두고 최초설정일만 채운다"는 식으로 필요한
    필드만 하나씩 백필했는데, 그러다 values(수익률 값 자체)를 빠뜨렸다
    (사용자 지적: "최초설정일 말고 다른것들은?"). 필드를 하나씩 떠올려
    가며 채우는 방식은 빠뜨리기 쉬워서, SQL의 FULL OUTER JOIN처럼 "이
    행이 가진 모든 필드에 대해 어느 쪽을 쓸지"를 여기 한 곳에 전부
    적어두는 방식으로 바꿨다(사용자 제안: "미리 FULL OUTER JOIN으로
    만들어두면 안 되나"). 새 필드가 생기면 여기만 고치면 된다.

    전제: 호출 전에 이미 값 대조(_values_agree)로 "같은 표의 같은 행"임을
    확인했다 - 숫자가 서로 어긋나는 경우는 애초에 여기까지 안 온다.
    """
    # values: 칸 단위로 COALESCE. 요약표가 "-"(원본이 비워둔 칸)이거나
    # 칸 자체가 없는데 상세표엔 실제 숫자가 있으면 그 숫자를 살린다
    # (반대로 요약표에 숫자가 있으면 그대로 둔다 - 둘 다 숫자면 위
    # 대조에서 같음이 확인된 값이라 어느 쪽을 써도 같다).
    # 상세표에서 가져온 필드는 어느 페이지에서 왔는지 필드 단위로 남긴다.
    # source_pages(=이 행이 어느 페이지들에서 만들어졌는지)만으론 "그
    # 최초설정일 어디서 봤어?"에 "4번 아니면 47번"까지밖에 못 답한다
    # (사용자 지적: "최초설정일을 47에서 가져와도 물어보면 47인지 아는
    # 거야?"). 근거 페이지를 틀리게 대면 사람이 그 페이지를 열어보고
    # 값을 못 찾게 되므로, 채워 넣은 필드는 정확한 페이지를 기록한다.
    # 이 행의 기본 page에서 온 필드는 안 적는다(그게 대부분이라 다
    # 적으면 파일만 커진다) - 여기 없는 필드는 page에서 온 것이다.
    from_detail = summary.setdefault("field_source_pages", {})
    for k in PERIOD_LABELS:
        sv, dv = summary["values"].get(k), detail["values"].get(k)
        if dv is None:
            continue
        try:
            float(dv)
        except (TypeError, ValueError):
            continue  # 상세표도 "-"면 채울 게 없다
        try:
            float(sv)
        except (TypeError, ValueError):
            summary["values"][k] = dv  # 요약표가 "-"이거나 없음 → 상세표 숫자 채택
            from_detail[f"values.{k}"] = detail["page"]
    # inception_date: COALESCE (최초설정일 칸이 요약표에만 있는 문서도,
    # 상세표에만 있는 문서도 실측으로 확인됨 - KR510902773M vs KR5157450017)
    if summary.get("inception_date") is None and detail.get("inception_date"):
        summary["inception_date"] = detail["inception_date"]
        from_detail["inception_date"] = detail["page"]
    # page/evidence/confidence/method: 요약표 것을 유지한다. page는 "이
    # 행의 값이 실제로 적혀 있던 위치"라 근거 표시에 쓰이는데, 요약표
    # 페이지가 문서 앞쪽이라 사람이 찾아보기도 쉽다.
    pages = summary.setdefault("source_pages", [summary["page"]])
    if detail["page"] not in pages:
        pages.append(detail["page"])


def enrich_with_detail_return_table(pdf, doc_id, existing_rows, used_pages, known_classes):
    """요약표엔 없고 뒤쪽 상세표에만 있는 클래스의 수익률 행을 보강한다.
    검증 실패 시(대조할 클래스가 없거나 값이 어긋나면) 아무것도 안 돌려준다."""
    known_rows = {
        r["class_code"]: r
        for r in existing_rows
        if r["row_kind"] == "class_return" and r.get("class_code")
    }
    known = {code: r["values"] for code, r in known_rows.items()}
    if not known:
        return []

    new_rows = []
    seen_codes = set(known)

    # 요약 페이지와 같은 원칙(process_doc 참고): 셀 격자 방식이 좌표
    # 방식보다 코드 인식이 확실하다. 코드 칸과 뜻(수수료방식-판매경로)
    # 칸이 표에서 서로 떨어진 레이아웃에서, 좌표 방식은 텍스트 인접성
    # 으로 코드를 찾다가 통째로 놓친다(KR5127420034 35쪽 실측: class_code
    # 가 13행 전부 None이었다). 페이지 하나씩 셀 격자로 읽어서, 표 테두리가
    # 없어 셀로 못 읽은 페이지만 좌표 결과로 보충한다.
    #
    # 문서 전체를 한 번에 return_rows_for_doc에 넘기면 안 된다 - 그 함수
    # 자체가 (row_kind, class_code, year_rank) 기준으로 문서 전체에 걸쳐
    # 중복을 지운다. 요약 페이지(4쪽)에 대표로 실린 "C"가 먼저 seen에
    # 잡히면, 상세표(35쪽)에 또 나오는 "C"는 "중복"으로 지워진다 - 값이
    # 같은 게 아니라 대조 기준(known) 자체가 이 페이지 결과에서 사라지는
    # 것이다. 그러면 대조할 게 하나도 없어 보강 전체가 조용히 실패한다
    # (직접 겪음: 페이지별로 나눠 부르니 12개 클래스가 다시 잡혔다).
    #
    # 그런데 페이지 하나씩만 넘기면 이번엔 다른 문제가 생긴다.
    # return_rows_for_doc 자신도 "바로 앞 페이지"에서 열 구성(inherited)을
    # 물려받는 방식으로 동작하는데, 표 헤더가 없는 이어지는 페이지를
    # 그 페이지 하나만 넘기면 물려받을 앞 페이지가 아예 없어서 통째로
    # 못 읽는다(KR5160420009 41쪽 실측: 40쪽과 같이 넘기면 13개 클래스가
    # 다 잡히는데 41쪽 하나만 넘기면 0개). 그래서 "앞 페이지, 이 페이지"
    # 두 쪽을 같이 넘겨 열 구성은 이어받게 하되, 결과는 이 페이지 것만
    # 추린다 - 앞 페이지 결과는 그 페이지 자신의 차례에서 이미 처리된다.
    section = "가"
    period_anchors = None
    last_validated_page = None
    # 표가 페이지 경계에서 끊기는데, 이번엔 "뒤" 페이지가 아니라 "앞" 페이지가
    # known과 안 겹치는 경우도 있다(KR5113420069 실측: 61쪽엔 A-e/C-F/C-R/...
    # 등 12개 클래스가 있는데 요약표에 실린 유일한 클래스 "C"는 62쪽에야
    # 나온다 - 61쪽만 보면 대조할 게 하나도 없어 통째로 버려지고, 62쪽이
    # 검증을 통과했을 땐 이미 61쪽 차례가 지나가버려 되돌아가지 않는다).
    # 아직 검증 못 한 페이지를 즉시 버리지 않고 "보류"해뒀다가, 바로 다음
    # 페이지가 검증에 성공하면 그 신뢰를 앞으로 물려서 보류분도 같이
    # 살린다 - class_fees.py에서 이미 검증된 것과 같은 패턴.
    pending = None  # (page_num, class_rows)
    for page_num in range(1, len(pdf.pages) + 1):
        page = pdf.pages[page_num - 1]
        next_page = pdf.pages[page_num] if page_num < len(pdf.pages) else None
        coord_rows, section, period_anchors = find_return_rows_on_page(
            page, page_num, section=section, known_classes=known_classes,
            inherited_period_anchors=period_anchors, next_page=next_page,
        )
        if page_num in used_pages:
            continue  # 이미 요약표로 처리한 페이지 - 섹션 상태만 이어받고 넘어감

        window = [page_num - 1, page_num] if page_num > 1 else [page_num]
        cell_rows_here = [
            r for r in return_rows_for_doc(doc_id, pdf, window, known_classes)
            if r.get("page") == page_num
        ]
        # 표가 머리글 없는 이어지는 쪽으로 두 쪽 넘게 계속되면, 앞 한
        # 쪽만 물려주는 창으로는 못 읽는다(KR5125450070 실측: 41쪽 자체가
        # 40쪽 머리글을 물려받아야 하는 무머리글 쪽이라, 42쪽이 "41쪽,
        # 42쪽" 두 쪽 창만 받으면 41쪽조차 이 창 안에서는 머리글이 없어
        # 42쪽도 같이 통째로 못 읽는다). 두 쪽 창이 비면, 머리글이 있는
        # 곳까지 창을 넓혀 가며 다시 시도한다(무한정 넓히면 무관한 이전
        # 표까지 물릴 수 있어 상한을 둔다).
        for lookback in (3, 4, 5):
            if cell_rows_here:
                break
            wide_window = list(range(max(1, page_num - lookback), page_num + 1))
            cell_rows_here = [
                r for r in return_rows_for_doc(doc_id, pdf, wide_window, known_classes)
                if r.get("page") == page_num
            ]
        # 셀 격자(cell_rows_here)가 좌표 방식(coord_rows)보다 코드 인식이
        # 확실하다는 원칙은 위 주석 그대로다. 그런데 "둘 다 known과 안
        # 겹치는" 흔한 경우(요약표엔 C 하나뿐인데 상세표 이 페이지엔
        # I/C-P/C-P2 등 요약표에 없는 새 클래스만 있는 경우 - 아래 신뢰
        # 이어받기가 원래 이런 페이지를 구하려고 있는 것이다)에, "refs가
        # 없을 때는 셀 격자를 남긴다"는 규칙을 그대로 cell_class_rows에
        # 적용하면 새 버그가 생긴다: 표에 테두리가 없어 셀 격자 자체가
        # 이 페이지에서 통째로 실패하는 문서(KR5113420069 실측: 61쪽 -
        # 셀 격자는 lookback을 5까지 넓혀도 0행, 좌표 방식은 12개 클래스를
        # 전부 찾음)에서는 cell_class_rows가 언제나 빈 리스트라 class_rows도
        # 빈 채로 남는다. 그러면 신뢰 이어받기 조건(`class_rows and
        # last_validated_page == page_num - 1`)과 보류(pending) 둘 다
        # "빈 리스트는 버린다"는 판정을 받아 12개 클래스가 통째로
        # 유실됐다(다음 쪽 62에서 "C"가 검증에 성공해도 61쪽 몫을 물려줄
        # pending 자체가 없었다). 셀 격자가 "이 페이지에서 아무것도 못
        # 찾았다"와 "찾았는데 known과 안 맞다"는 구분해야 한다 - 전자면
        # 좌표 방식 결과로 대체한다.
        cell_class_rows = [r for r in cell_rows_here
                            if r["row_kind"] == "class_return" and r.get("class_code")]
        coord_class_rows = [r for r in coord_rows
                             if r["row_kind"] == "class_return" and r.get("class_code")]
        # 예전엔 "이 페이지는 셀 격자를 믿을지 좌표 방식을 믿을지"를
        # 페이지 하나 단위로 통째로 골랐다(known과 맞는 refs가 있는
        # 쪽 전부를 채택). 그런데 표 형식이 상품마다 다 다른 이
        # 데이터 특성상, 한 상품의 문제(예: KR5113420069 - 셀 격자가
        # 그 페이지에서 통째로 실패)를 고치려고 이 판정 기준을
        # 바꾸면, 같은 판정을 타는 다른 상품에서 다른 방식으로
        # 깨지는 일이 반복됐다(KR5120450015: 신뢰 판정 받은 쪽이
        # 클래스 하나를 놓침 / KR5156450026·KR5160420009: 신뢰 판정
        # 받은 쪽이 클래스는 다 찾았는데 그중 한 칸(설정후)만 놓침).
        # "페이지 단위로 승자를 고른 뒤 패자 쪽에서 모자란 걸
        # 채운다"는 이 구조 자체가 원인이라, 클래스 단위로 바꾼다 -
        # 클래스마다 독립적으로 "이 클래스는 어느 쪽이 더 믿을 만한지"
        # 만 보고, 그 클래스가 다른 쪽에 있는 칸을 놓쳤으면 채운다.
        # 이러면 새 상품의 새 표 형식이 이 판정을 깨도 그 상품(그
        # 클래스)에만 영향이 있고 이미 맞던 다른 상품까지 흔들리지
        # 않는다.
        by_code = {}
        for r in cell_class_rows:
            by_code.setdefault(r["class_code"], {})["cell"] = r
        for r in coord_class_rows:
            by_code.setdefault(r["class_code"], {})["coord"] = r

        class_rows = []
        for code, methods in by_code.items():
            cell_r, coord_r = methods.get("cell"), methods.get("coord")
            if cell_r and coord_r:
                if code in known:
                    cell_matched, cell_bad = _values_agree(cell_r["values"], known[code])
                    coord_matched, coord_bad = _values_agree(coord_r["values"], known[code])
                    if cell_bad and not coord_bad:
                        primary, other = coord_r, cell_r
                    elif coord_bad and not cell_bad:
                        primary, other = cell_r, coord_r
                    elif coord_matched > cell_matched:
                        primary, other = coord_r, cell_r
                    else:
                        primary, other = cell_r, coord_r
                else:
                    # 대조할 known이 없는 새 클래스는 칸이 더 많이
                    # 채워진 쪽을 주로 삼는다(동률이면 코드 인식이
                    # 더 확실한 셀 격자 쪽 원칙대로 셀을 우선한다).
                    n_cell = sum(1 for v in cell_r["values"].values()
                                 if v not in (None, ""))
                    n_coord = sum(1 for v in coord_r["values"].values()
                                  if v not in (None, ""))
                    primary, other = ((cell_r, coord_r) if n_cell >= n_coord
                                       else (coord_r, cell_r))
                for k, v in other.get("values", {}).items():
                    if v not in (None, "") and not primary["values"].get(k):
                        primary["values"][k] = v
                class_rows.append(primary)
            else:
                class_rows.append(cell_r or coord_r)

        refs = [r for r in class_rows if r["class_code"] in known]

        def commit(rows_to_add, this_page_num):
            for r in rows_to_add:
                r.pop("_top", None)  # 요약표 쪽은 _dedupe_and_merge가 지운다
                cur = known_rows.get(r["class_code"])
                if cur is not None:
                    _merge_detail_into_summary(cur, r)
                    continue
                if r["class_code"] in seen_codes:
                    continue
                seen_codes.add(r["class_code"])
                r["product_code"] = doc_id
                r["method"] = "detail_return_table_cross_validated"
                r["source_pages"] = [r["page"]]
                new_rows.append(r)

        if refs:
            total_matched = 0
            conflict = False
            max_possible = 0
            for r in refs:
                matched, bad = _values_agree(r["values"], known[r["class_code"]])
                total_matched += matched
                conflict = conflict or bad
                max_possible += sum(
                    1 for v in known[r["class_code"]].values()
                    if isinstance(v, str) and DECIMAL_RE.match(v)
                )
            # 숫자 3칸 이상이 정확히 일치해야 인정한다 - 소수점 둘째 자리까지
            # 3개가 우연히 맞을 확률은 사실상 없어서, 이 표가 같은 표라는 걸
            # 충분히 특정한다(반대로 "-"뿐이라 대조할 숫자가 없는 문서는 그냥
            # 보강을 포기한다 - 틀린 값을 넣느니 없는 채로 두는 쪽). 다만
            # 설정 2년이 안 된 펀드는 요약표 자체가 실수(1y/설정일이후)
            # 2칸뿐이라 애초에 3칸을 채울 수가 없다(KR5118420006 실측) -
            # 대조 기준(known)이 가진 실수 칸이 3개 미만이면 그만큼만
            # 요구한다(그래도 최소 2개는 일치해야 한다 - 우연 일치 방지).
            required = min(3, max_possible) if max_possible else 3
            required = max(required, min(2, max_possible))
            if conflict or total_matched < required:
                pending = None
                continue
            # 검증 성공 - 바로 앞 쪽이 보류돼 있었다면(대조할 known이
            # 없었을 뿐 같은 표의 연속) 같이 살린다.
            if pending is not None and pending[0] == page_num - 1:
                commit(pending[1], pending[0])
            pending = None
            last_validated_page = page_num
            commit(class_rows, page_num)
            continue

        # 표가 페이지 경계에서 끊기면, 뒤 페이지엔 요약표(known)에 이미 실린
        # 클래스가 하나도 안 남고 그 표에만 있는 새 클래스만 남을 수 있다
        # (KR5131420025 실측: "가" 표가 33쪽에서 34쪽으로 넘어가는데 33쪽에서
        # A/C-F(known)/C/C-E/A-E/C-PE가 이미 다 잡히고, 34쪽엔 새 클래스
        # C-P2E 행 하나만 남는다 - known과 대조할 게 없어 refs가 항상 비고,
        # 그러면 검증 자체가 안 돼 이 클래스가 통째로 버려졌다). 바로 앞
        # 페이지가 이미 검증을 통과했고 이 페이지가 그 바로 다음 쪽이면,
        # 같은 표가 이어지는 것으로 보고(같은 좌표/셀 추출 로직이 "나" 절
        # 헤더를 만나면 스스로 멈추므로 다른 표 내용이 섞일 위험은 낮다)
        # known 대조 없이도 신뢰한다.
        if class_rows and last_validated_page == page_num - 1:
            pending = None
            last_validated_page = page_num
            commit(class_rows, page_num)
            continue

        # 검증도 못 하고 신뢰 이어받기도 안 되면, 다음 쪽이 검증에
        # 성공할 경우를 대비해 보류만 해둔다(바로 이전 쪽이 보류 중이면
        # 그 쪽은 이번 쪽으로 이어지지 않은 것이니 버린다).
        pending = (page_num, class_rows) if class_rows else None
    return new_rows


_RETURN_FALLBACK_DOCS = set()
RETURN_PERIODS = ("1y", "2y", "3y", "5y", "since_inception")


def _return_row_is_junk(r):
    """좌표 방식이 만든 정체불명 행. 클래스 코드를 못 붙인 class_return이나
    코드가 연도인 행은 재현 대상이 아니다(실측: "- - 20,369 20,466"인
    설정/환매현황 행, "설정일 - - - 이후"에서 나온 코드 '2024')."""
    if r["row_kind"] != "class_return":
        return False
    code = r.get("class_code")
    return code is None or bool(re.fullmatch(r"(19|20)\d{2}", str(code)))


def _return_cells_lose_anything(coord_rows, cell_rows):
    """셀 결과가 좌표 결과에 비해 무엇이든 잃었는지 본다. class_fees에서
    쓴 것과 같은 안전장치 - 하나라도 잃으면 그 문서는 좌표 결과를 쓴다."""
    def key(r):
        return (r["row_kind"], r.get("class_code"))

    def fold(rows):
        out = {}
        for r in rows:
            cur = out.setdefault(key(r), {})
            for p in RETURN_PERIODS:
                v = (r.get("values") or {}).get(p)
                if v is not None and cur.get(p) is None:
                    cur[p] = v
            if r.get("inception_date") and not cur.get("date"):
                cur["date"] = r["inception_date"]
        return out

    a = fold([r for r in coord_rows if not _return_row_is_junk(r)])
    b = fold(cell_rows)
    for k, av in a.items():
        bv = b.get(k)
        if bv is None:
            return True
        for f in list(RETURN_PERIODS) + ["date"]:
            if av.get(f) is not None and bv.get(f) != av[f]:
                return True
    return False


# "종류별 가입자격에 관한 사항"(구분/최초설정일/가입자격) 표 - 클래스별
# 개별 설정일이 수익률표 자체엔 없는 문서가 있다(KR510902511M 실측: 3부
# 운용실적표는 상품 전체 기준일만 있고, 신설된 지 얼마 안 돼 값이 전부
# "-"인 클래스는 요약표에도 대표 클래스만 실려 개별 설정일이 아예 안
# 나온다). "6.집합투자기구의 구조 - 종류형 구조" 절의 이 표엔 모든
# 클래스의 최초설정일이 다 있어서, 다른 데서 못 찾은 클래스만 여기서
# 채운다.
def _inception_dates_from_eligibility_table(pdf, known_classes):
    # 표가 페이지 경계에서 이어지면(클래스가 많은 문서) 헤더("최초설정일"/
    # "가입자격")는 첫 페이지에만 있고, 이어지는 페이지는 칸 구성마저
    # 다르다(KR510902511M 실측: 13쪽은 [구분,최초설정일,가입자격] 3칸인데
    # 14쪽 이어지는 표는 맨 앞에 빈 칸이 하나 더 낀 4칸 - 헤더도 없어
    # header_idx를 못 잡으면 A 하나만 건지고 A-e부터 끝까지 다 놓친다).
    # 헤더 위치에 기대지 않고, "라벨 칸에 아는 클래스 코드가 있고 바로
    # 다음 칸이 날짜꼴"이라는 모양 자체로 판정한다 - 페이지 종류나 칸
    # 밀림과 무관하게 항상 같은 모양이라 안전하다.
    #
    # 헤더가 있는 페이지를 찾는 데 page.extract_text()를 썼다가 100개
    # 문서 전체 재실행이 5분 안팎에서 1시간 가까이로 늘어났다(실측:
    # 이 상품 하나(62쪽)만 해도 extract_text()가 9초, find_tables()는
    # 0.8초 - 10배 이상 차이. pdf_words.py의 전역 패치 때문에 이
    # 문서군에서 extract_text()가 유독 무겁다). 이미 각 페이지의 표를
    # 훑어야 하니, 헤더 판별도 find_tables()가 돌려준 셀 텍스트만으로
    # 한다 - extract_text() 호출 자체를 없앤다.
    out = {}
    prev_had_header = False
    found_any = False
    for page in pdf.pages:
        tables = page.find_tables()
        rows_by_table = [t.extract() for t in tables]
        page_has_header = any(
            "최초설정일" in [(c or "").strip() for c in row]
            and "가입자격" in [(c or "").strip() for c in row]
            for rows in rows_by_table for row in rows)
        if not (page_has_header or prev_had_header):
            prev_had_header = False
            continue
        found_any = found_any or page_has_header
        prev_had_header = page_has_header
        for rows in rows_by_table:
            for row in rows:
                cells = [(c or "").strip() for c in row]
                label_idx = next((k for k, c in enumerate(cells) if c), None)
                if label_idx is None or label_idx + 1 >= len(cells):
                    continue
                label = cells[label_idx]
                m = CLASS_CODE_JONGRYU_KO_RE.search(label)
                if not m or m.group(1) not in known_classes:
                    continue
                date_m = INCEPTION_DATE_RE.search(cells[label_idx + 1])
                if date_m:
                    out.setdefault(m.group(1), _normalize_date(date_m.group()))
    return out if found_any else {}


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    known_classes = _known_classes_for_doc(doc_id)
    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        pages = candidate_pages_for_doc(doc_id, len(pdf.pages))
        if not pages:
            return []
        section = "가"
        period_anchors = None
        for page_num in pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            next_page = pdf.pages[page_num] if page_num < len(pdf.pages) else None
            rows, section, period_anchors = find_return_rows_on_page(
                page, page_num, section=section, known_classes=known_classes,
                inherited_period_anchors=period_anchors, next_page=next_page,
            )
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

        # 같은 페이지들을 셀 격자로도 읽어 대조한다. 셀 방식이 좌표 방식이
        # 뽑은 걸 하나도 잃지 않았을 때만 그쪽을 쓴다 - 좌표 방식은 값
        # 개수와 줄 위치로 매번 추측해야 해서 문서별 예외가 계속 붙었고,
        # 실제로 설정일이후 값을 3년 칸에 넣는 오류도 있었다(KR5194450018
        # 실측). 표 테두리가 아예 안 그려진 문서는 셀로 읽을 수 없으니
        # 그대로 좌표 결과를 쓴다.
        cell_rows = return_rows_for_doc(doc_id, pdf, pages, known_classes)
        if cell_rows and not _return_cells_lose_anything(results, cell_rows):
            for r in cell_rows:
                r["product_code"] = doc_id
            results = cell_rows
        else:
            _RETURN_FALLBACK_DOCS.add(doc_id)

        # 요약표 기준으로 중복 제거/merge를 먼저 끝낸 뒤(그 결과가 상세표
        # 대조의 "정답지"가 된다) 뒤쪽 상세표 보강을 돌린다 - pdf 핸들이
        # 필요해서 이 with 블록 안에서 호출한다.
        final = _dedupe_and_merge(results)
        final += enrich_with_detail_return_table(
            pdf, doc_id, final, set(pages), known_classes
        )
        # class_code가 없는 class_return 행은 "어느 클래스의 수익률인지
        # 모른다"는 뜻인데, class_returns.json은 클래스별 수익률을 담는
        # 파일이라 이런 행은 애초에 쓸 데가 없다. 실측해 보니 이런 행은
        # 전부 진짜 수익률이 아니라 표 밖에서 우연히 걸린 잡음이었다
        # (KR5123420049: 운용전문인력 표의 "운용역" 최근1·2년 수익률,
        # KR5153420063: 그래프 Y축 눈금 5개, KR5172450019: 세액공제 소득
        # 기준·총급여액 문구의 숫자). class_code를 못 찾았다고 억지로
        # class_return 취급하지 말고 통째로 버린다 - "모르면 안 낸다"가
        # "모르는데 클래스 수익률인 척 낸다"보다 안전하다. 비교지수/
        # 변동성/투자신탁 합계처럼 class_code가 원래 없는 게 정상인
        # 행 종류는 row_kind가 달라 이 필터에 안 걸린다.
        final = [r for r in final
                 if not (r["row_kind"] == "class_return" and not r["class_code"])]
        # "종류 2024.11.01~ / 설정일 - - - 이후 / 2025.10.31"처럼 기간
        # 헤더 자체가(설정 2년 미만이라 2y/3y/5y 칸이 전부 "-"인 신설
        # 클래스 표 특유의 헤더 모양) 데이터 행으로 오인되는 문서가
        # 있다(KR5118420006 실측: class_code="2024"(기간 문구의 연도
        # 앞자리), inception_date="2025-10-31"(기간 문구의 끝날짜),
        # 값은 전부 "-"인 가짜 행이 생겼다). 클래스 코드는 이 말뭉치에서
        # 절대 4자리 연도 모양이 아니고, 값이 전부 빈 행은 애초에 아무
        # 정보도 없다 - 둘 다 걸리는 행만 좁혀서 버린다.
        final = [r for r in final
                 if not (r["row_kind"] == "class_return"
                         and r.get("class_code")
                         and RE_BOGUS_YEAR_CODE.match(r["class_code"])
                         and not any(v not in (None, "", "-")
                                     for v in r["values"].values()))]
        # 합쳐진 행만 source_pages를 갖고 나머지는 없으면 스키마가 들쭉날쭉
        # 해진다(조회하는 쪽이 매번 존재 여부를 따져야 함) - 모든 행이
        # 갖도록 맞춘다(안 합쳐진 행은 자기 page 하나).
        for r in final:
            r.setdefault("source_pages", [r["page"]])
            r.setdefault("field_source_pages", {})

        # 수익률표 자체엔 개별 설정일이 없어 inception_date가 빈 class_return
        # 행을, "종류별 가입자격에 관한 사항" 표(있으면)로 채운다 - 이미 값이
        # 있는 행은 절대 안 건드린다.
        if any(r["row_kind"] == "class_return" and not r.get("inception_date")
               for r in final):
            elig_dates = _inception_dates_from_eligibility_table(pdf, known_classes)
            if elig_dates:
                for r in final:
                    if (r["row_kind"] == "class_return" and not r.get("inception_date")
                            and r.get("class_code") in elig_dates):
                        r["inception_date"] = elig_dates[r["class_code"]]
        return final


def _dedupe_and_merge(results):
    """요약표 후보 페이지들에서 뽑힌 행들의 중복 제거 + 같은 클래스 merge.
    (원래 process_doc 본문이었는데, 상세표 보강이 "중복 제거까지 끝난
    결과"를 대조 기준으로 써야 해서 별도 함수로 분리했다.)"""
    # 페이지 후보를 넓게 잡다 보니(다음 페이지도 포함) 같은 행이 중복될 수 있다.
    # 처음엔 (row_kind, class_code, values의 1y값)으로 판정했는데, 비교지수/
    # 수익률변동성 행은 class_code가 애초에 없고(None) 같은 상품 안의 여러
    # 클래스가 값까지 우연히 똑같이 나오는 경우가 실제로 있어서(KR5120420091
    # 실측: 초단기우량채/Class A/Class Ae/Class Ce의 비교지수가 전부 "3.72
    # 3.88 3.88"로 동일) 서로 다른 진짜 행 4개 중 1개만 남기고 나머지 3개를
    # "중복"으로 오인해 지워버리고 있었다(사용자가 "클래스는 있는데 비교지수/
    # 변동성이 없다"고 지적해서 발견). page 안에서의 실제 세로 위치(_top)까지
    # 같이 봐야 서로 다른 행을 구분할 수 있다 - 진짜 중복(페이지 후보가
    # 겹쳐서 같은 물리적 줄을 두 번 읽은 경우)은 같은 페이지의 같은 위치에서
    # 다시 나오므로 여전히 걸러진다.
    seen = set()
    deduped = []
    for r in results:
        key = (r["row_kind"], r["class_code"], r["page"], round(r["_top"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    for r in deduped:
        del r["_top"]

    # 페이지 위치까지 다르면 서로 다른 진짜 행으로 봐야 하지만(위 참고),
    # 한 문서 안에 "가.연평균수익률" 표 자체가 통째로 두 번(앞쪽 요약정보 +
    # 뒤쪽 제2부 상세) 나오면서 같은 클래스의 값까지 완전히 똑같이 반복
    # 되는 경우가 실제로 있다(KR5153520012 실측 - 사용자가 "C, C-P2e
    # 없음"이라고 지적해서 다시 살렸는데, 그러면서 같은 클래스가 두 번
    # 잡히는 부작용이 새로 생겼다). class_fees.json이 "같은 클래스는
    # 문서당 행 하나"로 통일하는 것과 같은 원칙으로 맞춘다 - class_code가
    # 있는 행은 (product_code, class_code)로 하나만 남기고, confidence가
    # 같으면 뒤쪽 페이지(제2부 상세표) 것을 남긴다. 상세표 쪽이 클래스
    # 개수도 더 많이 나오는 걸 실측으로 확인해서(같은 문서에서 요약표엔
    # 없던 클래스가 상세표에만 있는 경우 - C-F 등) 상세표를 더 완전한
    # 쪽으로 본다.
    # 뒤쪽(상세표) 행을 통째로 승자로 남기면, 요약표에만 있고 상세표엔
    # 아예 컬럼 자체가 없는 필드(최초설정일 - 실측: KR510902773M의
    # "가.연평균수익률" 표가 요약표(3페이지)엔 최초설정일 칸이 있는데
    # 상세표(45페이지)엔 그 칸이 통째로 빠져 있었다)까지 패자 행과 함께
    # 버려진다. 값(values)/row_kind는 그대로 승자(뒤쪽) 기준으로 두되,
    # 승자에 없는 필드만 패자에서 채워 넣는다 - 사용자 지적: "최초
    # 설정일이 없어지는건데 ㄱㅊ은거야?" → merge로 처리.
    best_by_class = {}
    demoted_pages = set()
    for r in deduped:
        if not r["class_code"]:
            continue
        key = r["class_code"]
        cur = best_by_class.get(key)
        if cur is None:
            best_by_class[key] = r
            continue
        if (r["confidence"], r["page"]) > (cur["confidence"], cur["page"]):
            demoted_pages.add(cur["page"])
            if r.get("inception_date") is None and cur.get("inception_date") is not None:
                r["inception_date"] = cur["inception_date"]
            best_by_class[key] = r
        else:
            demoted_pages.add(r["page"])
            if cur.get("inception_date") is None and r.get("inception_date") is not None:
                cur["inception_date"] = r["inception_date"]
    kept_class_return_ids = {id(r) for r in best_by_class.values()}

    # class_code가 없는 행(비교지수/수익률변동성/투자신탁 합계)은 클래스
    # 처럼 명확한 키가 없다. 같은 페이지 안에서 값이 우연히 같은 건(위
    # KR5120420091 케이스처럼) 무조건 서로 다른 진짜 행이라 절대 지우면
    # 안 되지만, "같은 (row_kind, 값 전부)"인 행이 서로 다른 페이지에
    # 걸쳐 있으면 그건 표 자체가 문서 안에서 반복된 것(위 참고)이므로
    # 가장 뒤쪽 페이지 것만 남긴다 - class_code 유무와 무관하게 이
    # 문서에서 실제로 확인된 반복 패턴(44개 값 그룹, class_return이 한
    # 쪽에서 안 겹쳐도 비교지수/변동성/투자신탁 합계만 따로 반복되는
    # 경우도 있었다)을 포괄하도록 class_code 중복 제거와 독립적으로
    # 처리한다.
    # 값만으로는 KR5120420091 케이스(서로 다른 클래스의 비교지수가
    # 우연히 값까지 같음)와 진짜 중복을 못 가른다 - 최초설정일까지
    # 같이 봐야 한다(서로 다른 클래스는 설정일도 보통 다르다). 그런데
    # "값 + 설정일"을 통째로 그룹 키로 쓰면, 같은 줄이 반복된 두 표
    # 중 한쪽에만 설정일이 찍히고(요약표) 다른 쪽엔 없는 문서에서
    # (KR5153420105 실측: 4쪽엔 설정일 "2008-11-18"이 있고 47쪽 반복본엔
    # 설정일 칸 자체가 없다) 두 그룹으로 갈라져 버려 진짜 중복이 둘 다
    # 남는다. 그래서 값으로 먼저 묶은 뒤, 그 묶음 안에서 설정일이
    # 실제로 서로 다른 값끼리 충돌할 때만(둘 다 있고 다를 때만)
    # 별개의 행으로 보고, 그 외(설정일이 아예 없거나 하나만 있거나
    # 전부 같음)에는 같은 줄의 반복으로 보고 합친다 - 페이지가 가장
    # 뒤쪽이면서 설정일이 채워진 쪽을 남긴다(더 완전한 정보다).
    no_class_groups = defaultdict(list)
    for r in deduped:
        if not r["class_code"]:
            no_class_groups[(r["row_kind"],
                              tuple(sorted(r["values"].items())))].append(r)
    drop_ids = set()
    for group in no_class_groups.values():
        if len(group) <= 1:
            continue
        incs = {r["inception_date"] for r in group if r.get("inception_date")}
        if len(incs) > 1:
            continue  # 설정일이 서로 다른 진짜 다른 행 - 지우지 않는다
        latest = max(group, key=lambda r: (bool(r.get("inception_date")), r["page"]))
        drop_ids.update(id(r) for r in group if r is not latest)

    final = []
    for r in deduped:
        if r["class_code"]:
            if id(r) in kept_class_return_ids:
                final.append(r)
        elif id(r) not in drop_ids:
            final.append(r)
    return final


def main():
    parser = argparse.ArgumentParser(description="클래스별 수익률 좌표 기반 추출")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_tables.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_tables.json"))
    )

    all_rows = []
    docs_with_hits = 0
    for doc_id in doc_ids:
        rows = process_doc(doc_id)
        if rows:
            docs_with_hits += 1
        all_rows.extend(rows)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    class_rows = [r for r in all_rows if r["row_kind"] == "class_return"]
    labeled = sum(1 for r in class_rows if r["class_code"])
    print(f"{len(all_rows)}개 행 ({docs_with_hits}개 문서) → {args.output}")
    print(f"  class_return 행: {len(class_rows)}건, 클래스코드 인식: {labeled}건")
    print(f"  benchmark(비교지수)/volatility(변동성) 행: {len(all_rows) - len(class_rows)}건")
    fb = sorted(_RETURN_FALLBACK_DOCS)
    print(f"  수익률표 좌표 방식 폴백 문서: {len(fb)}개 {fb}")


if __name__ == "__main__":
    main()
