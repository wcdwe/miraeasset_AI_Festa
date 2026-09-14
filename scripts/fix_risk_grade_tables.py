"""
연금 Agent 과제 - 위험등급 바 표 보정 스크립트 (일회성)

pdfplumber는 선(border)이 아니라 배경색 박스로 셀을 구분하는 "투자위험등급"
6단계 바 그래픽에서 셀 인식에 실패하는 경우가 있다. 실패 유형은 두 가지다:
  (A) 라벨 셀이 통째로 빈 문자열로 인식 (매우높은위험 등 라벨 소실)
  (B) 라벨 텍스트가 줄바꿈 때문에 여러 행으로 쪼개져 들어감 (데이터는 있으나
      단일 셀로는 안 잡힘)

이 6단계 라벨("매우높은위험"~"매우낮은위험")은 금융투자협회 표준 투자설명서
서식이라 운용사와 무관하게 문구/순서가 완전히 동일하다 (직접 3개 문서
- 키움/브이아이/KB - 원본 페이지를 렌더링해 육안으로 확인함). 따라서 페이지마다
VLM으로 다시 읽는 대신, "1~6 등급 숫자 행" 다음에 오는 깨진 라벨 영역을 검증된
표준 라벨로 직접 덮어쓴다.

원본 중 등급 숫자 행("1"~"6")과 그 앞의 설명 문단은 그대로 두고, 숫자 행
다음에 오는 행들(라벨이 있어야 할 자리)만 교체 대상으로 본다. 그 행들에
6개 표준 라벨의 부분 문자열이 아닌 다른 내용이 섞여 있으면(예상 밖 구조) 안전을
위해 그 표는 건드리지 않고 건너뛴다 - 원본을 지우면 되돌릴 수 없기 때문에,
"확실한 경우만" 덮어쓰는 보수적인 방식을 쓴다.

검색(FTS5)에서 "위험등급" 키워드로 바로 찾을 수 있도록 표 맨 앞에
"투자 위험등급: N등급" 헤더 행을 추가한다 (N은 원본 설명 문단에서 추출).

사용법:
    python scripts/fix_risk_grade_tables.py            # 실제 반영
    python scripts/fix_risk_grade_tables.py --dry-run   # 대상만 확인
"""

import argparse
import glob
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS_DIR = os.path.join(REPO_ROOT, "extracted", "products")

CANONICAL_LABELS = ["매우높은위험", "높은위험", "다소높은위험", "보통위험", "낮은위험", "매우낮은위험"]
GRADE_RE = re.compile(r"(\d)\s*등급")


def norm(s):
    return re.sub(r"\s+", "", s or "")


def find_digit_row(data):
    """'1'~'6'이 순서대로 있는 행의 (행 인덱스, {digit: 열 인덱스}) 반환. 없으면 None."""
    for row_idx, row in enumerate(data):
        cells = [norm(c) for c in row]
        pos_map = {}
        pos = -1
        ok = True
        for digit in "123456":
            found = None
            for i in range(pos + 1, len(cells)):
                if cells[i] == digit:
                    found = i
                    break
            if found is None:
                ok = False
                break
            pos = found
            pos_map[digit] = found
        if ok:
            return row_idx, pos_map
    return None


def is_broken(data, digit_row_idx):
    flat = [norm(c) for row in data for c in row]
    matched = sum(1 for lbl in CANONICAL_LABELS if lbl in flat)
    return matched < 6


def find_label_area_end(data, digit_row_idx):
    """숫자 행 다음부터, '6개 라벨의 부분 문자열이거나 빈 칸'인 행이 이어지는
    구간의 끝(그 다음 행 인덱스, exclusive)을 찾는다. 라벨과 무관한 내용(다음
    섹션 제목 등)이 나오면 거기서 멈춘다 - 그 이후 행은 건드리지 않고 보존한다."""
    end = digit_row_idx + 1
    while end < len(data):
        row = data[end]
        if all(
            (not norm(c)) or any(norm(c) in lbl for lbl in CANONICAL_LABELS)
            for c in row
        ):
            end += 1
        else:
            break
    return end


def extract_fund_grade(data):
    """표 텍스트에서 이 펀드의 실제 등급(예: '2등급')을 추출.
    운용사 공통 문구 패턴("N등급으로 분류" 등)으로 항상 들어있어 신뢰할 수 있다."""
    flat = " ".join(c for row in data for c in row if c)
    m = GRADE_RE.search(flat)
    return m.group(1) if m else None


def build_fixed_data(data, digit_row_idx, pos_map):
    grade = extract_fund_grade(data)
    # FTS5는 공백 없는 한글 연속 문자열을 하나의 토큰으로 취급한다
    # ("투자위험등급" != "위험등급" 토큰) - "위험등급"이 독립 토큰으로 검색되도록
    # 반드시 띄어쓴다.
    header_row = [f"투자 위험등급: {grade}등급"] if grade else ["투자 위험등급 6단계 기준표"]

    digit_row = data[digit_row_idx]
    width = len(digit_row)
    label_row = [""] * width
    for digit, col in pos_map.items():
        label_row[col] = CANONICAL_LABELS[int(digit) - 1]

    label_area_end = find_label_area_end(data, digit_row_idx)
    # 라벨 영역 뒤에 다른 내용(다음 섹션 제목 등)이 있으면 그대로 보존
    trailing = data[label_area_end:]
    return [header_row] + data[: digit_row_idx + 1] + [label_row] + trailing


def process_file(path, dry_run):
    with open(path, "r", encoding="utf-8") as f:
        tables = json.load(f)

    # 이전 방식(합성 표 추가)으로 만들어둔 항목이 있으면 정리하고 원본 덮어쓰기로 교체.
    tables = [t for t in tables if t.get("extraction_method") != "canonical_fix"]

    fixed = 0
    skipped = []
    for t in tables:
        data = t.get("data", [])
        found = find_digit_row(data)
        if not found:
            continue
        digit_row_idx, pos_map = found
        if not is_broken(data, digit_row_idx):
            continue  # 이미 정상 (6개 라벨 다 있음)
        if find_label_area_end(data, digit_row_idx) == digit_row_idx + 1:
            # 숫자 행 바로 다음 행부터 라벨 조각이 하나도 안 잡힘 - 예상 밖 구조라
            # 자동 보정 없이 건너뛰고 수동 확인 대상으로 남긴다.
            skipped.append((t.get("page"), t.get("table_index")))
            continue

        fixed += 1
        print(f"  {os.path.basename(path)} p.{t.get('page')} table_index={t.get('table_index')} -> 덮어씀")
        if not dry_run:
            t["data"] = build_fixed_data(data, digit_row_idx, pos_map)
            t["rows"] = len(t["data"])
            t["extraction_method"] = "canonical_fix_inplace"
            t["note"] = (
                "금융투자협회 표준 투자위험등급 6단계 라벨로 보정됨 "
                "(원본은 배경색 박스 셀 인식 실패로 라벨 소실/분산 - git 이력에서 원본 확인 가능)"
            )

    if skipped:
        for page, idx in skipped:
            print(f"  [건너뜀] {os.path.basename(path)} p.{page} table_index={idx} -> 예상 밖 구조, 수동 확인 필요")

    if fixed and not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tables, f, ensure_ascii=False, indent=2)

    return fixed, len(skipped)


def main():
    parser = argparse.ArgumentParser(description="위험등급 바 표 보정 (원본 덮어쓰기)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(PRODUCTS_DIR, "*_tables.json")))
    total_fixed = 0
    total_skipped = 0
    affected_docs = 0
    for fp in files:
        n, s = process_file(fp, args.dry_run)
        if n:
            affected_docs += 1
            total_fixed += n
        total_skipped += s

    print(f"\n총 {total_fixed}개 표 {'덮어쓸 예정' if args.dry_run else '덮어씀'} ({affected_docs}개 문서), 건너뜀 {total_skipped}건")


if __name__ == "__main__":
    main()
