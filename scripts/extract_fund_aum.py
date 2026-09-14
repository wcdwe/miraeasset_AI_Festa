"""
연금 Agent 과제 - 펀드 자체 재무상태표에서 순자산총계(AUM) 추출

이전에 "AUM은 원본에 없다"고 결론 냈었는데, 이건 "순자산총액"/"설정액"/
"운용규모" 같은 키워드로만 찾아서 놓친 것이었다. 실제로는 제3부.1.재무정보
"나. 재무상태표"(또는 "가. 요약재무정보")에 이 펀드 자체의 자산총계/부채총계가
있고, 순자산총계 = 자산총계 - 부채총계로 계산하면 그게 사실상 이 펀드의
AUM이다(사용자가 실제 표를 캡처해서 보여줘서 확인함, KR510902511M: 자산총계
5,711 - 부채총계 14 = 56.97억원).

주의: "자산총계"라는 단어가 이 펀드 재무상태표 말고도 완전히 다른 곳에
나온다 - 제4부(집합투자기구 관련회사) 섹션의 **운용사(회사) 자체 법인
재무제표**에도 "자산총계"가 있는데, 거기는 "유동자산/고정자산" 같은 일반
기업회계 용어를 쓴다(펀드 재무상태표는 "운용자산" 용어를 씀). 이 둘을
혼동하면 안 되므로 "유동자산"/"고정자산"이 근처에 있으면 회사 재무제표로
보고 제외한다.

숫자 단위(원/백만원)는 문서마다 다르므로 "단위: 백만원" 같은 문구를 같이
찾아서 unit 필드로 남긴다(못 찾으면 관측된 자릿수 규모로 봤을 때 대부분
"원" 단위라 기본값 "원"으로 둔다 - 확신 없는 경우 evidence로 원문을 남겨
검증 가능하게 함).

여러 회계기간(기수)이 나란히 열거되는데, 모든 문서에서 일관되게 "가장
최근 기수가 첫 번째"로 나왔다(KR510902511M/KR5111420047/KR5113420012/
KR5119450058 등 4개 문서로 확인). 그래서 asset_total/liability_total의
첫 번째 값을 "가장 최근 기준 순자산"으로 취급한다.

사용법:
    python scripts/extract_fund_aum.py
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
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "fund_aum.json")

NUM_OR_DASH = r"(?:-|[\d,]+)"
ASSET_TOTAL_RE = re.compile(rf"자산총계\s*({NUM_OR_DASH}(?:\s+{NUM_OR_DASH}){{0,2}})")
LIAB_TOTAL_RE = re.compile(rf"부채총계\s*({NUM_OR_DASH}(?:\s+{NUM_OR_DASH}){{0,2}})")
# "자본총계"(순자산총액 = 자산총계-부채총계)가 재무상태표에 직접 표기돼
# 있는 문서가 많다(부채총계 몇 줄 뒤, "원본/수익조정금/이익잉여금" 항목
# 다음). 자산총계-부채총계로 직접 계산한 값과 비교해보면 ±1(단위 기준)
# 차이가 나는 경우가 실제로 있다(사용자가 evidence를 원본과 대조하다
# 발견, KR510902511M 실측: 계산값 9186 vs 원본 표기 9185) - 각 하위 항목이
# 독립적으로 반올림돼 있어 더해도/빼도 딱 안 맞는 회계상 흔한 반올림
# 오차다. 우리가 계산한 값보다 원본이 직접 밝힌 값이 더 정확하므로, 있으면
# 이 값을 우선 채택한다.
CAP_TOTAL_RE = re.compile(rf"자본총계\s*({NUM_OR_DASH}(?:\s+{NUM_OR_DASH}){{0,2}})")
UNIT_RE = re.compile(r"단위\s*[:：]\s*(백만원|천원|원)")
# 펀드 재무상태표에서 "자산총계"와 "운용자산"의 상대 위치가 문서마다
# 다르다 - 어떤 문서는 "운용자산"이 세부항목 첫 줄로 "자산총계"보다
# *먼저*(KR510902511M: 운용자산/증권/.../자산총계), 어떤 문서는 "자산총계"가
# 소계로 먼저 나오고 "운용자산"이 그 바로 *다음* 줄에 온다(KR5114420027:
# 자산총계/운용자산/증권/...). 한쪽 방향만 보면 절반의 문서를 놓친다.
# "운용자산규모"(전혀 다른 섹션 제목 - 운용사 자체 재무제표 뒤에 붙는
# "라. 운용자산규모")는 제외해야 하므로 부정형 전방탐색을 둔다.
OPERATING_ASSET_RE = re.compile(r"운용자산(?!규모)")


def to_num(token):
    token = token.strip()
    if token == "-":
        return 0
    return int(token.replace(",", ""))


def find_fund_balance_sheet(doc_id):
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_text.json")
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        pages = json.load(f)

    for p in pages:
        t = p.get("text", "")
        # "자산총계"가 한 페이지에 여러 번 나올 수 있다(운용사 자체
        # 재무제표 + 펀드 재무상태표가 같은 페이지에 같이 있는 문서도
        # 있음) - 첫 번째 등장만 보면 그게 운용사 것일 때 펀드 것을
        # 영영 못 본다. 등장할 때마다 각각 확인한다.
        for m in re.finditer("자산총계", t):
            idx = m.start()
            # "운용자산"과 "자산총계"의 순서가 문서마다 다르다(아래
            # OPERATING_ASSET_RE 주석 참고) - 앞뒤 양쪽을 다 본다.
            window_around = t[max(0, idx - 400):idx + 400]
            # 진짜 펀드 재무상태표는 "운용자산"이 자산총계 바로 앞/뒤 몇 줄
            # 안에 항목으로 찍혀 있다. 이전엔 "운용자산"이 페이지 어딘가에만
            # 있으면 통과시켰는데, 운용사 자체 재무제표 밑에 우연히 "라.
            # 운용자산규모"라는 완전히 다른 섹션 제목이 같은 페이지에 있어서
            # 그 회사 재무제표(자본금/법인세 같은 일반 기업회계 항목)를
            # 펀드 것으로 잘못 집어온 사고가 있었다(KR5144420091 실측 -
            # 사용자가 "법인세랑 자본금 어디서 찾은거야"라고 지적해서
            # 발견). "운용자산"이 바로 이 자산총계 근처에도 있는지로
            # 좁힌다("운용자산규모"는 제외 - OPERATING_ASSET_RE 참고).
            if not OPERATING_ASSET_RE.search(window_around):
                continue
            # 운용사 자체 법인 재무제표(제4부)는 "유동자산/고정자산" 같은
            # 일반 기업회계 용어를 쓴다 - 펀드 재무상태표가 아니므로 제외.
            if "유동자산" in window_around or "고정자산" in window_around:
                continue
            return p.get("page"), t
    return None


def extract_fund_aum(doc_id):
    found = find_fund_balance_sheet(doc_id)
    if not found:
        return None
    page, t = found

    asset_m = ASSET_TOTAL_RE.search(t)
    liab_m = LIAB_TOTAL_RE.search(t)
    if not asset_m or not liab_m:
        return None

    asset_vals = [to_num(v) for v in asset_m.group(1).split()]
    liab_vals = [to_num(v) for v in liab_m.group(1).split()]
    n = min(len(asset_vals), len(liab_vals))
    if n == 0:
        return None
    net_asset_vals = [asset_vals[i] - liab_vals[i] for i in range(n)]

    # "자본총계"가 재무상태표에 직접 표기돼 있으면(위 CAP_TOTAL_RE 주석
    # 참고) 우리가 계산한 값(자산총계-부채총계)보다 그게 더 정확하다 -
    # 항목별 반올림 때문에 계산값과 ±1(단위 기준) 차이가 날 수 있다. 개수가
    # 안 맞으면(다른 표의 "자본총계"를 잘못 집었을 위험) 계산값을 그대로
    # 쓴다.
    cap_m = CAP_TOTAL_RE.search(t)
    if cap_m:
        cap_vals = [to_num(v) for v in cap_m.group(1).split()]
        if len(cap_vals) == n:
            net_asset_vals = cap_vals

    unit_idx = t.find("자산총계")
    unit_m = UNIT_RE.search(t[max(0, unit_idx - 600):unit_idx])
    unit = unit_m.group(1) if unit_m else "원"

    idx = t.find("자산총계")
    # 고정 글자수(앞 120자)로 자르면 글자/줄 경계를 무시하고 잘라서, 표
    # 헤더의 기수 라벨("12기(22.12.22)")이 ".22)"처럼 중간에서 뚝 끊긴
    # 파편으로 남는 경우가 있었다(KR510902511M 실측 - 사용자가 evidence에서
    # 발견). "항목 N기(...)..." 헤더 줄부터 "자산총계" 몇 줄 뒤(부채총계
    # 포함)까지, 줄 경계 기준으로 온전하게 잘라서 헤더 라벨이 안 잘리게
    # 한다.
    lines_t = t.split("\n")
    fee_line_idx = next((i for i, l in enumerate(lines_t) if "자산총계" in l), None)
    if fee_line_idx is not None:
        # 표 헤더 표기가 문서마다 다르다 - "항목"/"항 목"(글자 간격 벌어짐,
        # KR5111420047), "제 10 기"류 기수 표기(KR5156450026), "14기(24.12.22)"
        # 류 기수+날짜 표기(KR510902511M) 등. 이 중 하나라도 있는 줄을
        # 헤더로 본다.
        HEADER_ANCHOR_RE = re.compile(r"항\s*목|제\s*\d+\s*기|\d+기\(")
        header_idx = next(
            (
                i for i in range(fee_line_idx - 1, max(-1, fee_line_idx - 15), -1)
                if HEADER_ANCHOR_RE.search(lines_t[i])
            ),
            max(0, fee_line_idx - 3),
        )
        # "자본총계"를 채택한 경우(위 참고) evidence에도 그 줄까지 보여야
        # 어떤 값을 왜 썼는지 대조할 수 있다 - 부채총계 몇 줄 뒤에 있는
        # "자본총계" 줄을 찾으면 거기까지, 못 찾으면(그런 표기가 없는
        # 문서) 기존처럼 자산총계 뒤 몇 줄만 남긴다.
        cap_line_idx = next(
            (
                i for i in range(fee_line_idx + 1, min(len(lines_t), fee_line_idx + 10))
                if "자본총계" in lines_t[i]
            ),
            None,
        )
        end_idx = min(len(lines_t), (cap_line_idx + 1) if cap_line_idx is not None else fee_line_idx + 3)
        evidence = " / ".join(lines_t[header_idx:end_idx])
    else:
        evidence = t[max(0, idx - 120):idx + 60].replace("\n", " / ")

    return {
        "product_code": doc_id,
        "unit": unit,
        "asset_total": asset_vals,
        "liability_total": liab_vals,
        "net_asset_total": net_asset_vals,
        "net_asset_latest": net_asset_vals[0],
        "page": page,
        "evidence": evidence,
        "method": "text_regex",
        # class_fees가 "class_code를 깔끔하게 찾았으면 1.0"으로 매기는
        # 것과 같은 기준 - 이 경로는 "운용자산"이 자산총계 근처에 있는지
        # 확인(회사 재무제표 배제)까지 마친 뒤 "자산총계 N N N"을 정규식
        #으로 바로 잡아낸, 재구성 단계 없는 깔끔한 구조적 매치다. 아래
        # 좌표 폴백(글자 간격 벌어진 라벨/숫자를 휴리스틱으로 재구성하는
        # 우회 경로)과 성격이 다르므로 거기보다 높게 둔다. 이전엔 실제
        # 확실성과 무관하게 두 경로 다 0.8로 고정해뒀다가(사용자: "fund에서
        # confidence가 0.8인거 있는데 그러면 안 되는거 아녀?") 방식을
        # method별로 나눴는데, 처음엔 0.9로 깎아뒀다 - "class_code
        # 깔끔하게 찾은 것"과 성격이 같은데 왜 1.0이 아니냐는 재지적을
        # 받고서야 class_fees와 완전히 같은 기준으로 맞췄다.
        #
        # 주의: 이 confidence는 "이 행의 모든 필드가 다 맞다"는 뜻이
        # 아니다(class_fees와 동일한 한계 - 그쪽 주석 참고) - "자산총계/
        # 부채총계를 회사 재무제표가 아니라 이 펀드 것으로 확신할 수
        # 있는가"만 본다. unit 판별(못 찾으면 "원"으로 기본값 처리)이나
        # "첫 번째 열이 가장 최근 기수"라는 가정(4개 문서 표본으로 확인한
        # 규칙, 전수 검증은 아님)까지 이 숫자가 보장하진 않는다. "행이
        # 실제로 맞는지"는 asset_total>=liability_total, 원 단위 환산
        # 범위(억원~조원 상식선) 같은 전수 스캔(README 참고)이 실질적으로
        # 그 역할을 한다.
        "confidence": 1.0,
    }


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


NUM_TOKEN_RE = re.compile(r"^-$|^[\d,]+$")


def extract_fund_aum_coordinates(doc_id):
    """text_regex가 실패한 문서용 - "자산총계"/"부채총계" 라벨이 "자 산 총
    계"처럼 글자 사이가 벌어진 폰트로 나와(총보수/수익률 표에서 겪은 것과는
    다른 종류의 문제 - x_tolerance를 올려도 안 붙는 의도적인 자간) 캐시된
    _text.json의 문자열 매칭이 실패한 경우를 좌표 기반으로 재시도한다.
    숫자 자체는 글자처럼 벌어지지 않고 한 토큰으로 붙어 나오므로, 줄의
    텍스트를 공백 제거 후 비교해 라벨을 찾고 숫자 토큰만 따로 뽑는다."""
    pdf_candidates = glob.glob(os.path.join(DATA_DIR, doc_id, "*.pdf"))
    fp = os.path.join(EXTRACTED_DIR, f"{doc_id}_text.json")
    if not pdf_candidates or not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        pages_text = json.load(f)
    candidate_pages = [p["page"] for p in pages_text if "재무상태표" in p.get("text", "")]
    if not candidate_pages:
        return None

    with pdfplumber.open(pdf_candidates[0]) as pdf:
        for page_num in candidate_pages:
            if page_num < 1 or page_num > len(pdf.pages):
                continue
            page = pdf.pages[page_num - 1]
            words = page.extract_words(x_tolerance=5, keep_blank_chars=False)
            lines = cluster_lines(words)
            line_norms = [re.sub(r"\s+", "", " ".join(w["text"] for w in line)) for line in lines]

            asset_vals = liab_vals = unit = None
            operating_asset_line_text = None
            seen_balance_sheet_heading = False
            for i, (line, norm) in enumerate(zip(lines, line_norms)):
                if "재무상태표" in norm or "요약재무정보" in norm:
                    seen_balance_sheet_heading = True
                    unit = None  # 이전(다른 표의) 단위 표기는 무효화
                # "(단위:백만원,%)"처럼 재무상태표와 무관한 앞쪽 표의 단위
                # 표기를 잘못 주워올 수 있어, 재무상태표 제목을 본 뒤부터만
                # 단위를 채택한다.
                if seen_balance_sheet_heading and unit is None:
                    um = UNIT_RE.search(norm)
                    if um:
                        unit = um.group(1)
                if norm.startswith("자산총계") and asset_vals is None:
                    # 진짜 펀드 재무상태표에서 "운용자산"과 "자산총계"의
                    # 순서가 문서마다 다르다(위 OPERATING_ASSET_RE 주석
                    # 참고: 어떤 문서는 운용자산이 세부항목 첫 줄로 자산
                    # 총계보다 먼저, 어떤 문서는 자산총계가 소계로 먼저
                    # 나오고 운용자산이 바로 다음 줄) - 앞뒤 양쪽 몇 줄을
                    # 다 본다. 이전엔 페이지 전체에서 "운용자산"이 있는지만
                    # 봤는데, 운용사 자체 재무제표 밑에 우연히 "라. 운용
                    # 자산규모"라는 완전히 다른 섹션 제목이 같은 페이지에
                    # 있어서 그 회사 재무제표(자본금/법인세 같은 일반
                    # 기업회계 항목)를 펀드 것으로 잘못 집어온 사고가
                    # 있었다(KR5144420081/091 실측 - 사용자가 "법인세랑
                    # 자본금 어디서 찾은거야"라고 지적해서 발견, text_regex
                    # 경로는 먼저 고쳤는데 이 좌표 폴백 경로엔 같은 문제가
                    # 그대로 남아 있었다).
                    lo, hi = max(0, i - 15), min(len(line_norms), i + 15)
                    nearby = "".join(line_norms[lo:hi])
                    if not OPERATING_ASSET_RE.search(nearby):
                        continue
                    if "유동자산" in nearby or "고정자산" in nearby:
                        continue
                    nums = _merge_number_fragments(line)
                    if nums:
                        asset_vals = nums
                        # evidence에 "운용자산"이 실제로 근처에 있다는 걸
                        # 사람이 눈으로도 바로 확인할 수 있게, 그 줄의
                        # 원문을 같이 남긴다(사용자가 "회사 관련은 다 뺀거지?
                        # 내가 확인할때 어떤거 봐야 하는지" 물어서 - 이전엔
                        # "자산총계 .../부채총계 ..."만 보여줘서 운용자산이
                        # 실제로 걸렸는지 evidence만 보고는 알 수 없었다).
                        op_idx = next(
                            (k for k in range(lo, hi) if OPERATING_ASSET_RE.search(line_norms[k])),
                            None,
                        )
                        if op_idx is not None:
                            operating_asset_line_text = " ".join(w["text"] for w in lines[op_idx])
                elif norm.startswith("부채총계") and liab_vals is None:
                    nums = _merge_number_fragments(line)
                    if nums:
                        liab_vals = nums
            if not asset_vals or not liab_vals:
                continue

            asset_nums = [to_num(v) for v in asset_vals]
            liab_nums = [to_num(v) for v in liab_vals]
            n = min(len(asset_nums), len(liab_nums))
            if n == 0:
                continue
            net_vals = [asset_nums[i] - liab_nums[i] for i in range(n)]

            evidence_parts = [f"자산총계 {' '.join(asset_vals)}", f"부채총계 {' '.join(liab_vals)}"]
            if operating_asset_line_text:
                evidence_parts.insert(0, operating_asset_line_text)
            return {
                "product_code": doc_id,
                "unit": unit or "원",
                "asset_total": asset_nums,
                "liability_total": liab_nums,
                "net_asset_total": net_vals,
                "net_asset_latest": net_vals[0],
                "page": page_num,
                "evidence": " / ".join(evidence_parts),
                "method": "coordinate_reconstruction",
                # text_regex 경로보다 낮게 둔다 - 이 경로는 글자 간격이
                # 벌어진 라벨을 좌표로 다시 붙이고, 숫자도 토큰 사이 간격
                # (`_merge_number_fragments`의 3pt 임계값)으로 조각을
                # 합칠지 말지 휴리스틱으로 판단하는 재구성 단계를 거쳐서,
                # 그 자체가 정규식 직접 매치보다 오차 여지가 더 있다.
                "confidence": 0.75,
            }
    return None


def _merge_number_fragments(line):
    """일부 문서는 라벨뿐 아니라 숫자까지 글자 단위로 쪼개져 나온다
    ("1 0 3 ,4 3 2 ,1 1 3 ,0 0 5"). 같은 숫자의 조각들은 토큰 사이 간격이
    좁고(문자 간격), 서로 다른 열(기수)의 숫자들은 간격이 훨씬 넓다(표
    컬럼 간격) - 이 간격 차이로 조각을 하나의 숫자로 합칠지 다음 열로
    넘어갈지 구분한다."""
    num_tokens = [w for w in line if NUM_TOKEN_RE.match(w["text"])]
    if not num_tokens:
        return []
    num_tokens.sort(key=lambda w: w["x0"])
    groups = [[num_tokens[0]]]
    for prev, cur in zip(num_tokens, num_tokens[1:]):
        gap = cur["x0"] - prev["x1"]
        # 실측: 같은 숫자가 쪼개진 조각 사이 간격은 ~0.2~0.5pt(거의 붙어
        # 있음)인데, 서로 다른 열(기수)의 숫자 사이 간격은 50pt 이상이다.
        # (직전 토큰 너비에 비례한 임계값은 이미 한 토큰으로 잘 뽑힌
        # 긴 숫자("2,920,496,678,257" 같은 15자)에서 열 간격보다 커져
        # 버려 다른 열까지 잘못 합치는 문제가 있었다 - 고정값으로 변경)
        if gap <= 3:
            groups[-1].append(cur)
        else:
            groups.append([cur])
    return ["".join(w["text"] for w in g) for g in groups]


def main():
    parser = argparse.ArgumentParser(description="펀드 자체 재무상태표에서 순자산총계(AUM) 추출")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_text.json", "")
        for p in glob.glob(os.path.join(EXTRACTED_DIR, "*_text.json"))
    )

    results = []
    fallback_used = 0
    for doc_id in doc_ids:
        r = extract_fund_aum(doc_id)
        if not r:
            r = extract_fund_aum_coordinates(doc_id)
            if r:
                fallback_used += 1
        if r:
            results.append(r)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"{len(results)}/{len(doc_ids)}개 문서 → {args.output} (좌표 기반 폴백으로 {fallback_used}건 추가 회복)")


if __name__ == "__main__":
    main()
