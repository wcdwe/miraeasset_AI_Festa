"""
연금 Agent 과제 - 전체 데이터 일괄 추출 + 청크 생성 스크립트

data/institution/, data/products/*/ 아래 모든 문서(PDF/DOCX/XLSX/PPTX)를
extractors.extract_any()로 표/텍스트를 뽑고, chunk_text.build_chunks()로
청크를 만들어 extracted/ 아래에 저장한다.

폴더 깊이는 고정으로 가정하지 않는다 (institution/products 모두 임의 깊이로
재귀 탐색). 앞으로 데이터셋에 하위 폴더가 더 생겨도(예: 연도별/카테고리별)
안전하게 다 찾도록 os.walk로 재귀 순회한다.

문서 식별자(doc_id):
- institution: data/institution/ 기준 상대경로에서 확장자를 뗀 것, 구분자는 "__"
  (예: doc1.pdf -> doc1, sub/doc1.pdf -> sub__doc1)
- products: data/products/ 바로 아래 폴더명이 상품코드(product_code)가 되고,
  그 상품코드 아래에 파일이 1개뿐이면 doc_id=product_code, 여러 개면
  product_code + "__" + 상품코드 이후 상대경로(구분자 "__")로 구분한다.

출력(문서당 3개 파일, extracted/<institution|products>/ 아래):
- <doc_id>_tables.json  : extractors 표 그대로
- <doc_id>_text.json    : extractors 텍스트 그대로
- <doc_id>_chunks.json  : chunk_text 청크 (source_doc, page, chunk_id, text
                          + doc_type, product_code 메타데이터 추가)

사용법:
    python scripts/extract_all.py                 # institution + products 전체
    python scripts/extract_all.py --only institution
    python scripts/extract_all.py --workers 4      # 프로세스 병렬 처리
    python scripts/extract_all.py --force          # 이미 추출된 문서도 재처리
"""

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from extractors import extract_any
from chunk_text import build_chunks

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted")

SUPPORTED_EXTS = {".pdf", ".docx", ".xlsx", ".pptx"}


def _walk_supported_files(src_dir):
    """src_dir 아래를 임의 깊이로 재귀 순회해서 지원 확장자 파일의 상대경로를 반환.

    파일명 규칙에 의존하지 않고 확장자만 본다 (SUPPORTED_EXTS).
    """
    matches = []
    for root, _dirs, files in os.walk(src_dir):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTS:
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, src_dir)
                matches.append(rel_path)
    return sorted(matches)


def discover_institution_docs():
    """data/institution/ 아래 임의 깊이 -> [(doc_id, path, product_code=None)]"""
    src_dir = os.path.join(DATA_DIR, "institution")
    jobs = []
    for rel_path in _walk_supported_files(src_dir):
        stem, _ext = os.path.splitext(rel_path)
        doc_id = stem.replace(os.sep, "__")
        jobs.append((doc_id, os.path.join(src_dir, rel_path), None))
    return jobs


def discover_product_docs():
    """data/products/<code>/... (임의 깊이) -> [(doc_id, path, product_code=code)]

    <code>는 data/products/ 바로 아래 폴더명. 그 아래 몇 단계가 되든 재귀 탐색한다.
    """
    src_dir = os.path.join(DATA_DIR, "products")
    rel_paths = _walk_supported_files(src_dir)

    # product_code(상대경로 첫 구성요소)별로 묶어서, 코드당 파일이 1개면 doc_id=code,
    # 여러 개면 겹치지 않게 나머지 경로를 이어 붙인다.
    by_code = {}
    for rel_path in rel_paths:
        parts = rel_path.split(os.sep)
        code = parts[0]
        by_code.setdefault(code, []).append(rel_path)

    jobs = []
    for code, paths in sorted(by_code.items()):
        for rel_path in paths:
            remainder = os.path.relpath(rel_path, code)
            remainder_stem = os.path.splitext(remainder)[0].replace(os.sep, "__")
            doc_id = code if len(paths) == 1 else f"{code}__{remainder_stem}"
            jobs.append((doc_id, os.path.join(src_dir, rel_path), code))
    return jobs


def process_one(job, doc_type, force):
    doc_id, path, product_code = job
    out_dir = os.path.join(EXTRACTED_DIR, doc_type)
    os.makedirs(out_dir, exist_ok=True)

    tables_path = os.path.join(out_dir, f"{doc_id}_tables.json")
    text_path = os.path.join(out_dir, f"{doc_id}_text.json")
    chunks_path = os.path.join(out_dir, f"{doc_id}_chunks.json")

    if not force and all(os.path.exists(p) for p in (tables_path, text_path, chunks_path)):
        return doc_id, "skipped", 0, 0

    try:
        tables, texts = extract_any(path)
    except Exception as e:  # noqa: BLE001 - 배치 처리 중 한 문서 실패로 전체를 죽이지 않는다
        return doc_id, f"error: {e}\n{traceback.format_exc()}", 0, 0

    source_doc = os.path.basename(path)
    chunks = build_chunks(texts, source_doc)
    for c in chunks:
        c["doc_id"] = doc_id
        c["doc_type"] = doc_type
        if product_code:
            c["product_code"] = product_code

    for t in tables:
        t["doc_id"] = doc_id
        t["doc_type"] = doc_type
        t["source_doc"] = source_doc
        if product_code:
            t["product_code"] = product_code

    with open(tables_path, "w", encoding="utf-8") as f:
        json.dump(tables, f, ensure_ascii=False, indent=2)
    with open(text_path, "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False, indent=2)
    with open(chunks_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    return doc_id, "ok", len(tables), len(chunks)


def run(doc_type, jobs, workers, force):
    print(f"[{doc_type}] {len(jobs)}개 문서 처리 시작 (workers={workers})")
    t0 = time.time()
    n_ok, n_skip, n_err = 0, 0, 0

    if workers <= 1:
        results = [process_one(job, doc_type, force) for job in jobs]
    else:
        results = []
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(process_one, job, doc_type, force): job[0] for job in jobs}
            for fut in as_completed(futures):
                results.append(fut.result())

    for doc_id, status, n_tables, n_chunks in results:
        if status == "ok":
            n_ok += 1
            print(f"  ok    {doc_id}: {n_tables} tables, {n_chunks} chunks")
        elif status == "skipped":
            n_skip += 1
        else:
            n_err += 1
            print(f"  ERROR {doc_id}: {status}", file=sys.stderr)

    dt = time.time() - t0
    print(f"[{doc_type}] 완료: ok={n_ok} skipped={n_skip} error={n_err} ({dt:.1f}s)")
    return n_err


def main():
    parser = argparse.ArgumentParser(description="연금 데이터 일괄 추출 + 청크 생성")
    parser.add_argument("--only", choices=["institution", "products"], default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true", help="이미 추출된 문서도 재처리")
    args = parser.parse_args()

    os.makedirs(EXTRACTED_DIR, exist_ok=True)

    total_err = 0
    if args.only in (None, "institution"):
        jobs = discover_institution_docs()
        total_err += run("institution", jobs, args.workers, args.force)
    if args.only in (None, "products"):
        jobs = discover_product_docs()
        total_err += run("products", jobs, args.workers, args.force)

    if total_err:
        print(f"\n총 {total_err}개 문서에서 오류 발생, 로그 확인 필요", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
