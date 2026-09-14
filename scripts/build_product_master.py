"""
연금 Agent 과제 - 상품 마스터 테이블 생성

products 문서(extracted/products/*_text.json, *_tables.json)에서 신뢰도
높은 필드부터 단계적으로 뽑아 "상품 마스터"를 만든다. 각 필드는 단순 값이
아니라 {value, page, evidence, method, confidence} 구조로 저장해서, 나중에
Agent가 신뢰도 낮은 값은 답변에 쓰지 않도록 걸러낼 수 있게 한다.

1차 대상 필드: product_code, product_name, asset_type, risk_level.
(class/total_fee/return/AUM은 문서마다 표 레이아웃 편차가 커서 별도 단계로
분리 - 이 스크립트에는 아직 포함하지 않음.)

사용법:
    python scripts/build_product_master.py
    python scripts/build_product_master.py --output product_master.json
"""

import argparse
import glob
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS_DIR = os.path.join(REPO_ROOT, "extracted", "products")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "product_master.json")

CANONICAL_LABELS = ["매우높은위험", "높은위험", "다소높은위험", "보통위험", "낮은위험", "매우낮은위험"]

NAME_RE = re.compile(
    r"집\s*합\s*투\s*자\s*기\s*구\s*(?:의)?\s*명\s*칭\s*[:：]?\s*(.+?)(?=\n\s*2\s*[\.\s])",
    re.S,
)
GRADE_RE = re.compile(r"(\d)\s*등급")
BRACKET_RE = re.compile(r"[\(\[]([^()\[\]]{1,20})[\)\]]")

ASSET_TYPE_VOCAB = [
    "주식혼합-재간접형", "채권혼합-재간접형", "혼합-재간접형",
    "주식혼합", "채권혼합", "재간접형", "파생형",
    "주식형", "채권형", "혼합형", "부동산형", "특별자산형",
    "국공채", "단기채", "MMF", "주식", "채권", "혼합",
]

# 표 헤더 텍스트가 명칭에 섞여 들어온 걸 감지하는 신호 (이런 게 보이면 confidence를 낮춘다)
TABLE_LEAK_MARKERS = ["펀드코드", "금융투자협회"]

# "1. 집합투자기구 명칭" 절을 문장이 아니라 표로 적는 문서가 있다
# ("명 칭 | 금융투자협회 펀드코드" 헤더 줄 다음에 "실제 이름 | 코드"
# 데이터 줄이 오는 2행 표 - KR5116501001 실측). 그대로 이어 붙이면
# 헤더 글자와 펀드코드 숫자까지 이름에 섞인다.
RE_NAME_TABLE_HEADER = re.compile(
    r"^명\s*칭\s*금융투자협회\s*펀드코드\s*[\r\n]+\s*(.+?)\s+[A-Za-z0-9]+\s*$", re.S)
# "...KCGI코리아증권투자신탁1호[주식] (펀드코드 : AJ437)"처럼 이름 끝에
# 펀드코드 괄호가 한 번 더 붙는 문서가 있다(KR515302022M 실측) - 이름이
# 아니라 식별자라 떼어낸다.
RE_FUND_CODE_SUFFIX = re.compile(r"\s*\(\s*펀드코드\s*[:：]\s*[A-Za-z0-9]+\s*\)\s*$")

# 운용전환일 전/후로 자산유형·이름 자체가 바뀌는 목표전환형 상품이 있다
# (KR5147430065 실측: "1. 집합투자기구 명칭"이 "...4호[채권혼합](운용전환일
# 이후) KCGI코리아목표전환형증권투자신탁4호[채권]"처럼 전환 전·후 이름
# 둘을 하나로 이어 붙여 등록했다 - 증권신고서 원문 자체가 그렇게 적는다).
# 이 하나를 그대로 두면 asset_type이 뒤쪽(전환 "후") 괄호 "[채권]"에
# 걸리는데, risk_level은 별도 표에서 이미 전환 "전" 값(4등급)을 정확히
# 읽어 온다 - 서로 다른 시점의 값이 한 레코드에 섞인다. 100개 상품 중
# 실제로 운용전환이 있는 상품은 이거 하나뿐이라("증권전환형"이라는
# 이름이 붙은 다른 상품들은 다른 펀드로 갈아탈 수 있다는 뜻일 뿐 자산
# 유형·위험등급이 전후로 갈리지 않는다), 전용 스키마를 새로 만들기보다
# 지금 이 문서 하나에서 "전환 전"(=지금 신규설정 상태) 이름만 골라
# 나머지 필드(asset_type)와 시점을 맞춘다.
RE_CONVERSION_SPLIT = re.compile(r"\s*\(\s*운용전환일\s*이후\s*\)\s*.*$", re.S)


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def page1_text(text_pages):
    if not text_pages:
        return ""
    return next((p["text"] for p in text_pages if p.get("page") == 1), "")


def extract_product_name(page1):
    m = NAME_RE.search(page1)
    if not m:
        return {"value": None, "page": None, "evidence": None, "method": "name_regex", "confidence": 0.0}

    method = "name_regex"
    m2 = RE_NAME_TABLE_HEADER.match(m.group(1).strip())
    if m2:
        raw = re.sub(r"\s+", " ", m2.group(1)).strip()
        method = "name_regex_table"
    else:
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        raw = RE_FUND_CODE_SUFFIX.sub("", raw).strip()
    confidence = 1.0
    notes = []
    full_raw = raw

    conv = RE_CONVERSION_SPLIT.search(raw)
    if conv:
        # 운용전환 전(=지금 신규설정 상태) 이름만 남긴다 - risk_level이
        # 이미 그 시점 값을 쓰고 있으므로 asset_type도 같은 시점을
        # 가리켜야 한다. 원문 전체(전환 후 이름 포함)는 evidence에
        # 그대로 남겨 근거를 잃지 않는다.
        raw = raw[:conv.start()].strip()
        notes.append("운용전환일 이후 별도 이름 있음 - 전환 전(현재) 명칭만 씀")

    if any(marker in raw for marker in TABLE_LEAK_MARKERS):
        confidence = 0.4
        notes.append("표 헤더 텍스트 혼입 의심")
    if len(raw) > 60:
        confidence = min(confidence, 0.5)
        notes.append("비정상적으로 긴 캡처 (개명 이력 등 복수 명칭 가능성)")
    if len(raw) < 4:
        confidence = min(confidence, 0.3)
        notes.append("비정상적으로 짧은 캡처")

    return {
        "value": raw,
        "page": 1,
        "evidence": full_raw[:150],
        "method": method + (" +review_flag" if notes else ""),
        "confidence": confidence,
        **({"note": "; ".join(notes)} if notes else {}),
    }


def extract_asset_type(product_name_value):
    """명칭 끝의 괄호들 중 자산유형 어휘와 일치하는 걸 찾는다. 펀드명 뒤에는
    "...(채권)(41371)"처럼 자산유형 바로 뒤에 펀드코드 괄호가 하나 더 붙는
    경우가 흔해서, 무조건 마지막 괄호가 아니라 어휘가 매칭되는 괄호를 찾는다."""
    if not product_name_value:
        return {"value": None, "page": None, "evidence": None, "method": "bracket_vocab_match", "confidence": 0.0}

    brackets = list(BRACKET_RE.finditer(product_name_value))
    if not brackets:
        return {"value": None, "page": 1, "evidence": product_name_value, "method": "bracket_vocab_match", "confidence": 0.0}

    for m in reversed(brackets):  # 뒤에서부터 어휘 매칭되는 괄호를 우선
        raw = m.group(1).strip()
        if any(v in raw for v in ASSET_TYPE_VOCAB):
            return {
                "value": raw,
                "page": 1,
                "evidence": product_name_value,
                "method": "bracket_vocab_match",
                "confidence": 1.0,
            }

    # 어휘 매칭되는 괄호가 하나도 없으면 마지막 괄호를 낮은 confidence로 보고
    raw = brackets[-1].group(1).strip()
    return {
        "value": raw,
        "page": 1,
        "evidence": product_name_value,
        "method": "bracket_vocab_match",
        "confidence": 0.4,
    }


def extract_risk_level(tables):
    for t in tables or []:
        data = t.get("data", [])
        flat = " ".join(c for row in data for c in row if c)
        m = GRADE_RE.search(flat)
        if not m:
            continue
        grade = int(m.group(1))
        if 1 <= grade <= 6:
            return {
                "value": grade,
                "page": t.get("page"),
                "evidence": CANONICAL_LABELS[grade - 1],
                "method": "risk_table_regex",
                "confidence": 1.0,
            }
    return {"value": None, "page": None, "evidence": None, "method": "risk_table_regex", "confidence": 0.0}


def build_record(doc_id):
    text_pages = load_json(os.path.join(PRODUCTS_DIR, f"{doc_id}_text.json"))
    tables = load_json(os.path.join(PRODUCTS_DIR, f"{doc_id}_tables.json"))
    p1 = page1_text(text_pages)

    name_field = extract_product_name(p1)
    asset_type_field = extract_asset_type(name_field["value"])
    risk_field = extract_risk_level(tables)

    return {
        "product_code": doc_id,
        "product_name": name_field,
        "asset_type": asset_type_field,
        "risk_level": risk_field,
        "classes": None,  # 다음 단계 (총보수/수익률/AUM과 함께 클래스별로 처리 예정)
    }


def main():
    parser = argparse.ArgumentParser(description="상품 마스터 테이블 생성 (1차: name/asset_type/risk_level)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    doc_ids = sorted(
        os.path.basename(p).replace("_text.json", "")
        for p in glob.glob(os.path.join(PRODUCTS_DIR, "*_text.json"))
    )

    records = [build_record(doc_id) for doc_id in doc_ids]

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    def low_conf(field):
        return sum(1 for r in records if r[field]["confidence"] < 0.7)

    print(f"{len(records)}개 상품 처리 → {args.output}")
    for field in ("product_name", "asset_type", "risk_level"):
        hits = sum(1 for r in records if r[field]["value"] is not None)
        print(f"  {field}: {hits}/{len(records)} 추출됨, confidence<0.7 인 것 {low_conf(field)}건")


if __name__ == "__main__":
    main()
