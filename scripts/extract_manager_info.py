"""
연금 Agent 과제 - 운용전문인력 정보 추출 (참고용, AUM 축의 정답 아님)

products의 "운용전문인력" 표에는 "운용현황: 집합투자기구 수 / 운용규모"라는
칸이 있어 언뜻 이 상품의 AUM처럼 보이지만, 실제로는 **그 운용역(매니저)이
동시에 운용하는 모든 펀드를 합산한 규모**다 (예: KR510902511M - 송진용,
37개 펀드 합산 15,747억원). 같은 매니저가 여러 상품 문서에 등장하면 그
문서들마다 똑같은 숫자가 반복되고, 한 상품 문서 안에 매니저가 2명이면
서로 다른(그 매니저 개인 합산) 숫자가 나란히 나온다 - 이 상품 하나만의
AUM이 될 수 없다는 뜻이다.

이 상품 자체의 실제 순자산총액/설정액은 간이투자설명서 어디에도 없다는 걸
이미 확인했다(README AUM 섹션 참고). 그래서 이 스크립트는 AUM 축의 정답을
만드는 게 아니라, "운용역 합산 규모"를 참고 정보로만 남겨두는 용도다 -
class_fees/class_returns처럼 정식 6축 데이터로 취급하지 않는다.

이전 버전은 "운용전문인력" + "생년"이 같은 표 안에 잡힌 문서(39개)에서만
후보 페이지를 골랐는데, 그 판정 기준이 되는 extracted/*_tables.json은
이 표가 셀 경계 없이(또는 복잡하게 병합돼) 실려 있으면 표 자체를 못 찾아
전체 100개 중 61개 상품을 통째로 놓쳤다(전수 확인: 61개 모두 PDF
원문에는 "운용전문인력" 절이 실제로 있다). 표 탐지를 pdfplumber
find_tables()가 아니라 페이지 텍스트에서 "운용전문인력" 낱말을 직접
찾는 방식으로 바꿔 이 누락을 없앤다.

행 하나에서 이름·생년·경력년수·운용현황(개수/규모)가 전부 한 줄에 나란히
있는 문서도 있지만(단순한 경우), 표 칸이 여러 줄로 쪼개져 이름 줄과
개수·규모 줄이 서로 다른 물리적 줄에 떨어져 있는 문서도 있다(KCGI
KR5147430065 실측: "18개 10,120억원"이 "채권 홍사욱 1974 ..." 줄과
분리된 별도 줄). 이름+생년(둘이 붙어 있는 건 어느 문서든 흔들리지 않는
확실한 표시)을 닻으로 삼고, 그 닻에서 가장 가까운 개수·규모/경력 줄을
찾는 방식으로 두 경우를 함께 처리한다.

사용법:
    python scripts/extract_manager_info.py
"""

import argparse
import glob
import json
import os
import re

import pdfplumber

import pdf_words  # noqa: E402  (import만으로 Page.chars 전역 패치가 걸린다 - pdf_words.py 참고)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "manager_info.json")

# 이름(2~5자 한글) 바로 뒤에 4자리 생년(19xx/20xx)이 오는 자리를 이 사람의
# "닻"으로 쓴다 - 표 칸이 몇 줄로 쪼개지든 이름+생년만은 늘 붙어 있었다
# (전수 확인). 뒤에 "년"이 붙기도, 안 붙기도 한다.
NAME_YEAR_RE = re.compile(r"([가-힣]{2,5})\s+((?:19|20)\d{2})년?(?!\d)")
# 뒤에 "OO월...일" 같은 날짜 형식이 바로 이어지면 그건 생년이 아니라
# "책임운용전문인력 변경 내역"(YYYY년 MM월 DD일 ~ 현재/YYYY년...) 표의
# 변경일자다(KR5111450067 실측: "이왕섭 2021년 12월 02일 ~ 현재"를 사람
# "이왕섭"의 생년 "2021"로 잘못 읽었다 - 이 변경내역 절 제목이 이 문서엔
# 안 걸려 표 본문 범위 판정을 못 끊었다). 진짜 생년 뒤에는 곧장 직위나
# 통계 숫자가 오지 날짜가 오지 않는다. NAME_YEAR_RE 자체에 부정형
# lookahead로 넣으면 "년?"이 옵션이라 정규식 엔진이 "년"을 안 먹는
# 쪽으로 백트래킹해 그 lookahead를 우회해 버려서(실측: 그렇게 짰다가
# 여전히 걸렀다), 매치 뒤 남은 텍스트를 별도로 검사한다.
DATE_TAIL_RE = re.compile(r"^\s*년?\s*\d{1,2}\s*월")
# 운용현황(개수/규모) - "18개 10,120억원"처럼 한 줄에 붙어 나온다. 억원
# 앞에 "2조" 같은 조 단위가 더 붙는 문서가 있다(KR5160420009 실측:
# "11개 2조 780억원"). 조 단위는 있어도 되고 없어도 된다. "개"는 있는데
# "억원" 글자 자체가 통째로 빠지는 문서도 있다(KR5157420003/KR5123490017
# 실측: "30개 34,225", "66 개 21,959" - 단위는 표 머리글에만 있다).
# "억원"까지 옵션으로 두되, 없을 땐 값에 쉼표(천 단위 구분)가 있을
# 때만 받는다(_valid_count_aum_match) - 안 그러면 "3개 5"처럼 아무
# 작은 정수 둘이나 다 받아버려 안전판이 없어진다.
COUNT_AUM_RE = re.compile(
    r"(\d{1,3})\s*개\s+(?:(\d+)\s*조\s*)?([\d,]+)\s*(억원?)?")


def _valid_count_aum_match(m):
    has_unit = bool(m.group(4))
    return has_unit or "," in m.group(3)
# "개"/"억원" 글자 없이 숫자만 적고 단위는 표 머리글("집합투자기구수(개)
# 운용규모(억원)")에만 밝히는 문서가 있다(KR5111450067 실측: "책임
# 이왕섭 1979 부장 25 3,132 -15.12 ..." - 25/3,132가 개수/규모인데 낱말
# 단위가 안 붙는다). "개"/"억원" 없이는 아무 숫자 두 개나 걸릴 위험이
# 커서, 둘째 숫자에 쉼표(천 단위 구분)가 있을 때만 받는다 - 수익률
# 같은 소수는 쉼표를 안 쓰므로 안전판이 된다.
COUNT_AUM_NO_UNIT_RE = re.compile(r"(?<!\d)(\d{1,3})\s+(\d{1,3}(?:,\d{3})+)(?!\d)")
# 경력년수는 소수로 적히는 문서도 있다(KR5111450067 실측: "9.3년" -
# 개월 단위 없이 소수 하나로 표시). "\d{1,3}"으로 자릿수를 3자리까지만
# 받는다 - 안 그러면 "2021년 12월"처럼 변경이력의 4자리 연도+월이
# "2021년"(년수)+"12개월"(개월수) 모양으로 오인된다(실측: 위 NAME_YEAR_RE
# 오탐과 같은 자리에서 career까지 "2021년 12월"로 잘못 잡혔었다).
# "년"과 "개월" 조각을 각각 optional로 둔 건 의도적이다 - 뒤에 "18년"과
# "10개월"이 서로 다른 줄에 떨어진 문서를 이 정규식 자신이 아니라
# 그 호출부(붙이는 보정 로직, 아래 참고)가 처리하기 때문에, 이 정규식은
# "개월 없이 년만"도 유효한 매치로 인정해야 한다. 172명 전원이 공유하는
# 정규식이라 섣불리 조이면(예: "\s*\d{1,2}개월"을 통째로 하나의
# optional로 묶기) 다른 문서에서 "개월"만 있고 "년"이 없는 조각,
# 또는 부분적으로만 있는 조각(실측: "23년 1개"처럼 "개"만 있고 "월"이
# 없는 표기)을 매치 못 시켜 172건 중 9건이 통째로 사라지고 19건의 값이
# 달라지는 광범위한 회귀가 났다(2026-09-06 실측, 재현 후 되돌림) -
# 그래서 아래처럼 문제가 확인된 그 한 건만 결과값을 못박는 쪽을 썼다.
CAREER_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{1,2})?년\s*\d{0,2}개?월?)(?!\d)")
# 이름 줄 자신이 "이름 생년 - - - - -"처럼 값 칸이 전부 "-"로 찍혀
# 있으면(=이 사람은 개수·규모·경력 다 없다고 문서가 명시한 것) 표시.
DASH_RUN_RE = re.compile(r"(?:^|\s)-(?:\s+-){1,}")
# 이 절의 표는 늘 "생년"이 표 머리글에 있고, 각주("주1)"/"주 1)"/"*"로
# 시작하는 설명)로 끝난다. 이 사이만 표 본문으로 본다 - 그 밖의 페이지
# 내용(연혁, 위험고지 등)에서 이름+생년 모양이 우연히 걸리는 오탐을 막는다.
HEADER_MARK_RE = re.compile(r"생년")
# 가운뎃점은 문서마다 다른 글자를 쓴다(U+318D/U+2219/U+2022 외에
# U+00B7 "·"도 실측됨 - KR5114420027: "·집합투자증권은 예금자보호법에
# 따라..."로 시작하는 "투자자 유의사항" 문단이 이 글자로 시작하는데
# 블록리스트에 없어 각주 경계로 못 잡혀, 그 뒤 "간이투자설명서는
# 증권신고서 효력발생일까지..." 문장의 "효력발생일"이 사람 이름으로
# 오인됐다). "집합투자증권은...예금자보호법" 자체도 이 유의사항 문단의
# 고정 도입구라 안전한 경계 표시로 같이 쓴다.
FOOTNOTE_START_RE = re.compile(
    r"^\**\s*\(?주\s*\d|^[ㆍ∙•·]|^나\.\s*운용전문인력의|^\d+\.\s*집합투자기구의"
    r"|집합투자증권은[^.\n]{0,20}예금자보호법")


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


def _aum_to_100m(jo, eok):
    """조/억 표기를 억원 단위 정수로 합친다("2조 780억원" -> 20780)."""
    v = int(eok.replace(",", ""))
    if jo:
        v += int(jo) * 10000
    return v


def find_manager_rows(pdf, page_nums, doc_id):
    """page_nums(오름차순, 서로 이웃한 페이지들)를 한 좌표계로 이어붙여
    줄 단위로 훑는다 - 표가 페이지 경계에서 갈려도 이름 줄과 개수·규모
    줄이 같은 "이어붙인 좌표"에서 가까운 줄로 남는다."""
    all_words = []
    y_offset = 0.0
    page_of_top = []  # (누적 top 경계, 실제 페이지 번호)
    for pn in page_nums:
        page = pdf.pages[pn - 1]
        words = page.extract_words(x_tolerance=3, keep_blank_chars=False)
        for w in words:
            w = dict(w)
            w["top"] += y_offset
            w["bottom"] += y_offset
            all_words.append(w)
        y_offset += page.height
        page_of_top.append((y_offset, pn))

    def page_for(top):
        for boundary, pn in page_of_top:
            if top < boundary:
                return pn
        return page_of_top[-1][1] if page_of_top else page_nums[0]

    lines = cluster_lines(all_words)
    line_texts = [" ".join(w["text"] for w in ln) for ln in lines]

    header_idxs = [i for i, t in enumerate(line_texts) if HEADER_MARK_RE.search(t)]
    if not header_idxs:
        return []
    start = max(header_idxs) + 1
    end = len(line_texts)
    for i in range(start, len(line_texts)):
        if FOOTNOTE_START_RE.match(line_texts[i].strip()):
            end = i
            break

    tops = [ln[0]["top"] for ln in lines]

    rows = []
    for i in range(start, end):
        m = NAME_YEAR_RE.search(line_texts[i])
        if not m:
            continue
        if DATE_TAIL_RE.match(line_texts[i][m.end():]):
            continue
        name, birth_year = m.group(1), m.group(2)
        # 생년으로 말이 안 되는 값(연혁·변경내역의 "적용년도"가 잘못
        # 걸린 경우)은 최소한의 나이 상식으로 거른다 - 실무 운용역이면
        # 이 표의 작성기준일(대개 2020년대) 시점에 이미 성년이었을
        # 사람이다. 위 DATE_TAIL_RE로 못 잡는 변형(문서마다 각주
        # 경계가 다 다르다)의 마지막 안전판.
        if int(birth_year) > 2007:
            continue
        # 표 칸 이름표(성명/생년/직위 등)나 각주 설명 문장이 우연히
        # "한글 2~5자 + 4자리 숫자"로 걸리는 걸 막는다 - 진짜 사람
        # 이름은 그 줄이나 이웃 줄에 경력년수/개수·규모 중 하나는
        # 반드시 딸려 있다. 줄 "번호" 차이가 아니라 실제 y좌표 거리로
        # 재는데, 이 표는 이력사항 글머리 줄이 이름 줄 사이사이에
        # 촘촘히 끼어 있어(KCGI KR5147430065 실측) 줄 번호로 재면 더
        # 멀리 있는(실제로 이 사람 것이 아닌) 줄이 더 가깝다고 오판할
        # 수 있다 - 실제 인쇄 거리(포인트)로 재면 이런 오판이 없다.
        # "성명·생년·경력" 표와 "성명·개수·운용규모" 표가 아예 따로
        # 떨어진 두 표로 나뉘고, 사이에 그 표 자신의 머리글까지 끼는
        # 문서가 있다(모자형 펀드 KR510902511M 실측: "서윤석 1988 팀장
        # 11년2개월"과, 10줄 가까이 뒤 별도 소표의 "서윤석 6개 7,742
        # 억원..."). 그런 소표는 생년 없이 이름만 다시 적으므로,
        # 창을 넉넉히 넓히고 이름이 그대로 다시 나오는 줄을 최우선으로
        # 찾는다.
        window = range(max(start, i - 8), min(end, i + 24))
        my_top = tops[i]
        career = None
        career_key = None
        count = aum = None
        count_key = None
        count_line = None
        for j in window:
            # 우선순위: (1) 이 줄에 이름이 그대로 다시 나오면 최우선
            # (2) 이름 줄보다 "앞"에 있는 값 (3) 거리. 이 표는 개수·
            # 규모 칸이 이름보다 위에 인쇄되고, 이름 바로 다음엔
            # "성과보수가 있는 경우"라는 별도(작은) 하위 집계가 뒤이어
            # 나온다(KCGI KR5147430065 실측: 이름보다 47pt 위의 "18개
            # 10,120억원"이 진짜 값인데, 이름보다 36pt 아래의 "1개
            # 24억원"(하위 집계)이 순수 거리로는 더 가까워 오판했었다).
            # 이 줄이 "다른" 운용역의 이름+생년 줄이면(자기 자신 줄은
            # 당연히 자기 이름이 걸리므로 제외 안 됨) 건너뛴다. 이게
            # 없으면 이 사람 칸이 전부 "-"(데이터 없음)인데 창 안에서
            # 가장 가까운 "다른 사람"의 개수·규모·경력을 잘못 빌려 온다
            # (실측: KR5118201004의 전준필(신규 부책임, 표에 전부 "-")이
            # 위 줄 황우성의 "47개 50,832억원 6년3개월"을 그대로
            # 물려받았다 - 두 사람 다 이 표에 있었는데 전준필 자기
            # 줄에는 숫자가 하나도 없었다). "18개 10,120억원"처럼 이름이
            # 아예 없는 줄(KCGI 실측, 표 칸이 갈려 이름 줄과 떨어진
            # 진짜 값)은 이 규칙에 안 걸려 그대로 잡힌다 - 사람이 아닌
            # 순수 값 줄만 빌려오는 걸 막지는 않는다.
            other = NAME_YEAR_RE.search(line_texts[j])
            if other and other.group(1) != name:
                continue
            dist = abs(tops[j] - my_top)
            has_name = name in line_texts[j] and j != i
            key = (0 if has_name else 1, 0 if tops[j] <= my_top else 1, dist)
            cm = CAREER_RE.search(line_texts[j])
            if cm and (career_key is None or key < career_key):
                career, career_key = cm.group(1), key
                # "18년"과 "10개월"이 칸 줄바꿈으로 서로 다른 줄에
                # 떨어지는 문서가 있다(KCGI 실측: "책임 18년" 다음 줄이
                # "(상무) 10개월") - 다만 그 "10개월" 줄이 이름 줄에
                # 가로막혀 바로 다음 줄이 아니라 두 줄 뒤에 있기도 한다
                # (이름이 직위·경력 칸 사이에 끼어 인쇄되는 문서). "개월"
                # 만 있는 짧은 줄이 근처(20pt 이내)에 있으면 몇 줄
                # 떨어져 있든 붙인다.
                if not re.search(r"개월", career):
                    j_top = tops[j]
                    best = None
                    for k in window:
                        if k == j:
                            continue
                        mm = re.match(r"^\(?[가-힣]{0,4}\)?\s*(\d{1,2}\s*개월)(?!\d)",
                                      line_texts[k].strip())
                        if mm and abs(tops[k] - j_top) <= 20:
                            d = abs(tops[k] - j_top)
                            if best is None or d < best[0]:
                                best = (d, mm.group(1))
                    if best:
                        career = career + best[1]
            am = COUNT_AUM_RE.search(line_texts[j])
            if am and not _valid_count_aum_match(am):
                am = None
            am_jo, am_eok = (am.group(2), am.group(3)) if am else (None, None)
            if not am:
                am2 = COUNT_AUM_NO_UNIT_RE.search(line_texts[j])
                if am2:
                    am, am_jo, am_eok = am2, None, am2.group(2)
            if am and (count_key is None or key < count_key):
                count = int(am.group(1))
                aum = _aum_to_100m(am_jo, am_eok)
                count_key, count_line = key, j
        # 이 사람 개수·규모가 끝내 하나도 안 잡혔는데(위에서 다른
        # 사람 줄은 걸렀으니, 정말 이 사람 칸 자체가 비어 있다는 뜻)
        # 자기 줄이 "이름 생년 - - - - -"처럼 값 자리가 전부 "-"로
        # 찍혀 있다면, 경력도 십중팔구 "-"다(실측 5건 전부 이 셋이
        # 같이 빈다). 그런데 "6년3개월"처럼 이름이 안 붙은 순수 숫자
        # 줄(경력년수만 툭 떨어져 있는 줄)은 위의 "다른 사람 줄
        # 건너뛰기"로는 못 거른다 - 이름이 아예 없어서 누구 줄인지
        # 모르기 때문이다. 그래서 개수·규모가 명시적으로 비어 있다는
        # 신호(자기 줄의 "-" 나열)가 있을 때만 경력도 같이 비운다.
        explicit_no_data = False
        if count is None and DASH_RUN_RE.search(line_texts[i]):
            # class_returns.json/class_fees.json도 원문이 "-"면 파싱
            # 실패(null)가 아니라 문자열 "-" 그대로 남기는 게 이
            # 코퍼스의 관행이다 - "확인 안 됨"과 "문서가 명시적으로
            # 없다고 적음"은 다른 사실이기 때문이다. career는 문자열
            # 필드라 그 관행을 그대로 따른다.
            career = "-"
            explicit_no_data = True
        # 위에서 "-" 나열로 확인 사살한 경우(explicit_no_data)는 이
        # 사람이 실존 운용역이라는 것 자체는 확실하니 남긴다 - 통째로
        # 빼버리면 "이 상품엔 이 운용역이 아예 없다"와 "있는데 개별
        # 실적을 공시 안 했다"가 구분이 안 된다. 그 신호가 없는데도
        # career·count가 둘 다 안 잡힌 경우(순수 추출 실패)만 원래대로
        # 버린다. count·aum은 정수 컬럼이라 "-"를 못 담고, fund_aum.py
        # 처럼 0으로 바꾸는 것도 안 맞는다("이 운용역이 펀드 0개를
        # 운용한다"는 다른 사실이 되어버린다) - null로 둔다.
        if career is None and count is None and not explicit_no_data:
            continue
        rows.append({
            "name": name,
            "birth_year": int(birth_year),
            "manager_fund_count": count,
            "manager_aum_100m_won": aum,
            "career": career,
            "page": page_for(lines[i][0]["top"]),
            "evidence": line_texts[i] + (
                f" | {line_texts[count_line]}" if count_line is not None else ""),
            "method": "coordinate_reconstruction",
            "confidence": 0.8,
            "is_product_aum": False,
        })
    return rows


def candidate_page_groups(pdf):
    """"운용전문인력" 낱말이 나오는 페이지를 찾고, 표가 다음 페이지로
    이어지는 문서를 위해 바로 다음 페이지도 같이 묶는다. find_tables()
    기반 판정(이전 버전)은 이 표가 셀 경계 없이 실린 문서를 통째로
    놓쳤다(61개 상품 실측) - 텍스트 낱말 자체를 찾는 쪽이 표 구조와
    무관해 훨씬 덜 놓친다."""
    hit_pages = [
        i + 1 for i, page in enumerate(pdf.pages)
        if "운용전문인력" in (page.extract_text() or "")
    ]
    # 같은 표 안에 "운용전문인력"이 여러 번 나오는 문서가 있다(절 제목
    # + "나. 운용전문인력의 최근 변경 내역" 각주가 같은/이웃 페이지에
    # 같이 있는 경우 - KCGI 실측: 12·13쪽 둘 다 걸림). 페이지별로 따로
    # 그룹을 만들면 같은 표를 두 번(한쪽은 머리글 없이 잘린 채로) 훑어
    # 중복·엉뚱한 값을 만든다. 서로 붙은 히트 페이지는 한 그룹으로
    # 합친다.
    clusters = []
    for pn in hit_pages:
        if clusters and pn - clusters[-1][-1] <= 1:
            clusters[-1].append(pn)
        else:
            clusters.append([pn])
    groups = []
    for cluster in clusters:
        group = list(cluster)
        if cluster[-1] < len(pdf.pages):
            group.append(cluster[-1] + 1)
        groups.append(group)
    return groups


def process_doc(doc_id):
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    if not pdf_candidates:
        return []

    results = []
    with pdfplumber.open(pdf_candidates[0]) as pdf:
        for group in candidate_page_groups(pdf):
            rows = find_manager_rows(pdf, group, doc_id)
            for r in rows:
                r["product_code"] = doc_id
                results.append(r)

    # 같은 운용역 표가 <요약정보>와 본문("제2부") 두 군데에 그대로
    # 반복되는 문서가 있다(KR5147430065 실측: 6쪽과 12~13쪽에 똑같은
    # 이름·개수·규모가 두 번). 이름·생년·개수·규모가 같으면 같은
    # 사람이므로 하나만 남기되, 페이지가 갈리며 "OO개월"을 못 찾은
    # 쪽(경력 문자열이 더 짧은 쪽)이 아니라 더 완전한 쪽을 남긴다.
    best = {}
    for r in results:
        key = (r["name"], r["birth_year"], r["manager_fund_count"], r["manager_aum_100m_won"])
        cur = best.get(key)
        if cur is None or len(r.get("career") or "") > len(cur.get("career") or ""):
            best[key] = r
    out = list(best.values())

    # 위 병합은 "개수·규모까지 완전히 같아야" 합친다 - 그런데 개수·
    # 규모 칸 자체가 안 잡힌 반쪽짜리 중복도 있다(권용범/이우중/조정남
    # 실측: 이름·생년은 같은데 한쪽은 개수·규모까지 다 있고 다른 쪽은
    # 개수·규모가 둘 다 None에 경력마저 어긋난 값(이우중: "2년 9개월"
    # vs "1년" - 원문 어디에도 "1년"은 없다, 순수 오독)이다 - 서로 다른
    # 물리적 줄을 두 번 읽은 게 아니라, 이름 줄 윈도우 탐색이 한 번은
    # 제대로, 한 번은 엉뚱한 조각을 집어 두 벌이 나온 것으로 보인다).
    # 이 상품에 그 이름·생년의 "개수·규모까지 다 있는" 행이 이미
    # 있으면, 개수·규모가 둘 다 None인 나머지 동명이생년 행은 군더더기로
    # 보고 버린다. 이 상품 안에 그런 "완전한" 행이 아예 없으면(=정말
    # 개별 데이터가 없는 사람) 건드리지 않는다 - explicit_no_data로
    # 남긴 null 레코드가 이 규칙에 걸려 사라지면 안 된다.
    complete_people = {
        (r["name"], r["birth_year"]) for r in out
        if r["manager_fund_count"] is not None and r["manager_aum_100m_won"] is not None
    }
    out = [
        r for r in out
        if not (r["manager_fund_count"] is None and r["manager_aum_100m_won"] is None
                and (r["name"], r["birth_year"]) in complete_people)
    ]
    return out


# CAREER_RE는 172명 전원이 공유하는 정규식이라 이 한 건 때문에 조이면
# 다른 문서를 깨뜨린다(위 CAREER_RE 주석 참고 - 2026-09-06에 실제로
# 시도해서 9건 소실·19건 변경의 광범위한 회귀를 냈다 되돌림). 그 대신
# 원문 대조로 확인된 이 한 레코드의 결과값만 못박는다.
#   - KR5113420069(한국투자 크레딧포커스ESG) 15쪽: 부책임운용전문인력
#     홍다정의 표에 "운용경력년수"(6년 5개월)와 "ESG 운용경력년수"
#     (4년 9개월) 두 컬럼이 나란히 인쇄돼 있다. 두 번째 줄("5 개월"/
#     "9개월")이 물리적으로 갈라지면서, 첫 컬럼의 "6년" 뒤에 옆 컬럼
#     "4년"의 "4"가 개월 표시 없이 흡수되고, 그 뒤 "개월"이 없다고
#     판단한 보정 로직이 다음 줄의 "5개월"을 마저 이어붙여 "6년
#     45개월"이라는 존재하지 않는 값이 만들어졌다 - 원문 정답은
#     "6년 5개월"(첫 컬럼 값)이다.
_KNOWN_CAREER_FIXES = {
    ("KR5113420069", "홍다정", 1992, 15): ("6년 45개월", "6년 5개월"),
}


def apply_known_career_fixes(rows):
    fixed = 0
    for r in rows:
        key = (r["product_code"], r["name"], r.get("birth_year"), r.get("page"))
        entry = _KNOWN_CAREER_FIXES.get(key)
        if entry and r.get("career") == entry[0]:
            r["career"] = entry[1]
            fixed += 1
    return fixed


def main():
    parser = argparse.ArgumentParser(description="운용전문인력 정보 추출 (참고용)")
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

    fixed = apply_known_career_fixes(all_rows)
    if fixed:
        print(f"알려진 career 오류 {fixed}건 보정")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print(f"{len(all_rows)}개 행 ({docs_with_hits}개 문서 / 전체 {len(doc_ids)}개) → {args.output}")
    print("주의: manager_aum_100m_won은 이 상품 하나의 AUM이 아니라 해당 운용역/운용사가 운용하는 전체 펀드 합산 규모(참고용, 단위: 억원)")


if __name__ == "__main__":
    main()
