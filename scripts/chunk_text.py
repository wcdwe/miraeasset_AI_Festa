"""
연금 Agent 과제 - 서술형 텍스트 청크 분할 스크립트

extract_tables.py --output-text 로 뽑은 페이지별 텍스트를
의미 단위(청크)로 나눈다. 문단 경계를 우선 존중하고,
너무 길면 글자 수 기준으로 강제 분할한다.

각 청크는 (source_doc, page, chunk_id, text) 메타데이터를 포함해서
나중에 "근거 문서 표시" 요구사항을 충족시킬 수 있게 한다.

사용법:
    python scripts/chunk_text.py --input extracted/institution/doc1_text.json \
        --output extracted/institution/doc1_chunks.json --source-doc doc1.pdf
"""

import argparse
import json
import re


def split_into_paragraphs(text):
    """빈 줄 기준으로 문단을 나눈다."""
    paras = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paras if p.strip()]


def chunk_page(page_num, text, max_chars=500, overlap_chars=50):
    """
    한 페이지 텍스트를 문단 단위로 묶되, max_chars를 넘으면 쪼갠다.
    문단 중간에서 끊기지 않도록 문단 경계를 우선한다.
    """
    paragraphs = split_into_paragraphs(text)
    chunks = []
    buffer = ""

    for para in paragraphs:
        if len(buffer) + len(para) <= max_chars:
            buffer = f"{buffer}\n\n{para}".strip()
        else:
            if buffer:
                chunks.append(buffer)
            # 문단 자체가 max_chars보다 길면 강제 분할
            if len(para) > max_chars:
                for i in range(0, len(para), max_chars - overlap_chars):
                    chunks.append(para[i:i + max_chars])
                buffer = ""
            else:
                buffer = para

    if buffer:
        chunks.append(buffer)

    return chunks


def build_chunks(page_documents, source_doc, max_chars=500):
    """페이지별 문서 리스트를 청크 레코드 리스트로 변환."""
    all_chunks = []
    chunk_counter = 0

    for doc in page_documents:
        page_chunks = chunk_page(doc["page"], doc["text"], max_chars=max_chars)
        for c in page_chunks:
            chunk_counter += 1
            all_chunks.append({
                "chunk_id": f"{source_doc}_{chunk_counter:04d}",
                "source_doc": source_doc,
                "page": doc["page"],
                "text": c,
            })

    return all_chunks


def main():
    parser = argparse.ArgumentParser(description="페이지별 텍스트를 청크로 분할")
    parser.add_argument("--input", required=True, help="extract_tables.py --output-text 결과 JSON")
    parser.add_argument("--output", required=True, help="청크 결과 JSON 저장 경로")
    parser.add_argument("--source-doc", required=True, help="원본 문서 이름 (근거 표시용)")
    parser.add_argument("--max-chars", type=int, default=500, help="청크 최대 글자 수")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        page_documents = json.load(f)

    chunks = build_chunks(page_documents, args.source_doc, max_chars=args.max_chars)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    print(f"{len(page_documents)}페이지 → {len(chunks)}개 청크 생성 → {args.output}")


if __name__ == "__main__":
    main()
