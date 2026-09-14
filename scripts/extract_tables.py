"""
연금 Agent 과제 - PDF 표 추출 스크립트

용도:
- data/institution/ 안의 제도·세제 문서 (표 위주, 예: 세액공제표)
- data/products/ 안의 투자설명서 (클래스별 비용표, 위험등급 등)

사용법:
    python scripts/extract_tables.py --input data/institution/doc1.pdf --output extracted/institution/doc1_tables.json
    python scripts/extract_tables.py --input data/products/R2.pdf --output extracted/products/R2_tables.json

주의:
- 선(격자)이 명확한 표는 잘 뽑히지만, 이미지로 삽입된 표나 색상 박스로
  디자인된 표(예: 위험등급 바)는 컬럼이 깨지거나 빈 값으로 나올 수 있음.
- 결과는 반드시 원본 페이지와 육안 대조해서 검증할 것.
  실패한 페이지는 render_page_as_image.py로 이미지 렌더링 후 VLM으로 재처리 권장.
"""

import argparse
import json

from extractors import extract_pdf_tables as extract_tables_from_pdf
from extractors import extract_pdf_text as extract_text_by_page


def main():
    parser = argparse.ArgumentParser(description="연금 PDF 표/텍스트 추출기")
    parser.add_argument("--input", required=True, help="입력 PDF 경로")
    parser.add_argument("--output", required=True, help="출력 JSON 경로 (표)")
    parser.add_argument("--output-text", help="출력 JSON 경로 (서술형 텍스트, 선택)")
    parser.add_argument("--start", type=int, default=None, help="시작 페이지 (1-indexed)")
    parser.add_argument("--end", type=int, default=None, help="끝 페이지 (포함)")
    args = parser.parse_args()

    page_range = (args.start, args.end) if args.start and args.end else None

    tables = extract_tables_from_pdf(args.input, page_range)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(tables, f, ensure_ascii=False, indent=2)
    print(f"[표] {len(tables)}개 추출 → {args.output}")

    # 표가 없거나 깨진 페이지 경고
    empty_or_broken = [
        t for t in tables
        if all(all(c == "" for c in row) for row in t["data"])
    ]
    if empty_or_broken:
        pages = sorted(set(t["page"] for t in empty_or_broken))
        print(f"  ⚠ 빈 표 감지된 페이지: {pages} → 이미지 렌더링 후 VLM 재처리 검토 필요")

    if args.output_text:
        texts = extract_text_by_page(args.input, page_range)
        with open(args.output_text, "w", encoding="utf-8") as f:
            json.dump(texts, f, ensure_ascii=False, indent=2)
        print(f"[텍스트] {len(texts)}페이지 추출 → {args.output_text}")


if __name__ == "__main__":
    main()
