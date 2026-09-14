#!/usr/bin/env python3
"""Build the unified RAG corpus from suhyeon page text and pension metadata."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATED = ROOT / "data" / "integrated"
EXTRACTED = ROOT / "extracted"
BASE_CHUNKS = ROOT / "data" / "processed" / "chunks.jsonl"

HEADINGS = {
    "investment_objective": "투자목적 및 펀드 개요",
    "investment_target": "투자대상 및 투자제한",
    "investment_strategy": "투자전략 및 위험관리",
    "risk": "투자위험",
    "purchase_redemption": "매입·환매·전환",
    "pricing": "기준가격",
    "fees": "수수료·보수 및 비용",
    "performance": "투자실적 및 수익률",
    "asset_composition": "자산구성 및 AUM",
    "tax_distribution": "이익분배 및 과세",
    "institution": "연금제도 및 세제",
    "other": "기타 원문",
}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def compact(text):
    return re.sub(r"\s+", "", text or "")


def clean(text):
    text = re.sub(r"페이지\s*\d+\s*/\s*\d+", " ", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_section(text, doc_type):
    if doc_type == "institution":
        return "institution"
    value = compact(text)
    rules = [
        ("fees", r"보수및수수료|총보수.?비용|투자비용"),
        ("performance", r"연평균수익률|연도별수익률|투자실적추이"),
        ("asset_composition", r"자산구성현황|자산구성내역|자산총계|순자산총액"),
        ("purchase_redemption", r"매입.?환매|환매수수료|전환기준"),
        ("pricing", r"기준가격"),
        ("tax_distribution", r"이익분배및과세|수익자에대한과세|과세에관한사항"),
        ("risk", r"투자위험|원본손실위험|위험등급"),
        ("investment_strategy", r"투자전략|위험관리방법"),
        ("investment_target", r"투자대상|투자제한"),
        ("investment_objective", r"투자목적"),
    ]
    for section, pattern in rules:
        if re.search(pattern, value):
            return section
    return "other"


def split_long(text, max_chars=1200, overlap=100):
    text = clean(text)
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks, buffer = [], ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            sentences = [s.strip() for s in re.split(r"(?<=[.!?다요함됨])\s+", paragraph) if s.strip()]
            if len(sentences) == 1:
                sentences = [paragraph[i:i + max_chars] for i in range(0, len(paragraph), max_chars - overlap)]
        else:
            sentences = [paragraph]
        for sentence in sentences:
            candidate = f"{buffer}\n\n{sentence}".strip() if buffer else sentence
            if buffer and len(candidate) > max_chars:
                chunks.append(buffer)
                buffer = f"{buffer[-overlap:]} {sentence}".strip()
                if len(buffer) > max_chars:
                    chunks.extend(buffer[i:i + max_chars] for i in range(0, len(buffer), max_chars - overlap))
                    buffer = ""
            else:
                buffer = candidate
    if buffer:
        chunks.append(buffer)
    # Merge fragments under 80 characters instead of indexing them alone.
    merged = []
    for chunk in chunks:
        if len(chunk) < 80 and merged and len(merged[-1]) + len(chunk) + 2 <= max_chars:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        elif len(chunk) < 80:
            merged.append(chunk)
        else:
            merged.append(chunk)
    if len(merged) > 1 and len(merged[0]) < 80:
        merged[1] = f"{merged[0]}\n\n{merged[1]}"
        merged = merged[1:]
    return [c for c in merged if c.strip()]


def text_hash(text):
    return hashlib.sha256(compact(text).encode("utf-8")).hexdigest()


def merge_short_records(rows, minimum=80, maximum=1400):
    """Merge tiny page fragments with an adjacent record from the same document."""
    result = []
    pending = None
    for row in rows:
        if pending is not None:
            if row["doc_id"] == pending["doc_id"] and len(pending["text"]) + len(row["text"]) + 2 <= maximum:
                row = dict(row)
                row["text"] = f"{pending['text']}\n\n{row['text']}"
                row["chunk_hash"] = text_hash(row["text"])
                pending = None
            else:
                result.append(pending)
                pending = None
        if len(row["text"]) < minimum:
            if result and result[-1]["doc_id"] == row["doc_id"] and len(result[-1]["text"]) + len(row["text"]) + 2 <= maximum:
                result[-1]["text"] = f"{result[-1]['text']}\n\n{row['text']}"
                result[-1]["chunk_hash"] = text_hash(result[-1]["text"])
            else:
                pending = row
        else:
            result.append(row)
    if pending is not None:
        result.append(pending)
    return result


def main():
    mappings = read_csv(INTEGRATED / "fund_products.csv")
    by_code = {r["product_code"]: r for r in mappings}
    aliases = defaultdict(list)
    for row in mappings:
        aliases[row["canonical_product_code"]].append(row["product_code"])
    canonical = {r["product_code"] for r in mappings if r["is_canonical"] == "true"}

    chunks = []
    skipped_products = []
    within_seen = set()
    for code in sorted(canonical):
        path = EXTRACTED / "products" / f"{code}_text.json"
        if not path.exists():
            skipped_products.append(code)
            continue
        mapped = by_code[code]
        pages = json.loads(path.read_text(encoding="utf-8"))
        for page in pages:
            page_no = page.get("page")
            content = clean(page.get("text", ""))
            section = detect_section(content, "product")
            for index, part in enumerate(split_long(content), 1):
                digest = text_hash(part)
                key = (mapped["source_doc_id"], str(page_no), digest)
                if key in within_seen:
                    continue
                within_seen.add(key)
                chunks.append({
                    "chunk_id": "", "doc_type": "product", "fund_id": mapped["fund_id"],
                    "product_codes": sorted(aliases[code]), "canonical_product_code": code,
                    "doc_id": mapped["source_doc_id"], "section": section,
                    "heading": HEADINGS[section], "text": part, "page": page_no,
                    "source_file": f"R2_{code}.pdf", "chunk_index_on_page": index,
                    "extraction_method": page.get("extraction_method", "pdf_text"),
                    "quality_status": "NORMAL", "chunk_hash": digest,
                })

    for path in sorted((EXTRACTED / "institution").glob("*_text.json")):
        doc_id = path.name.removesuffix("_text.json")
        pages = json.loads(path.read_text(encoding="utf-8"))
        for page in pages:
            content = clean(page.get("text", ""))
            for index, part in enumerate(split_long(content), 1):
                chunks.append({
                    "chunk_id": "", "doc_type": "institution", "fund_id": "",
                    "product_codes": [], "canonical_product_code": "", "doc_id": doc_id,
                    "section": "institution", "heading": HEADINGS["institution"],
                    "text": part, "page": page.get("page"),
                    "source_file": doc_id, "chunk_index_on_page": index,
                    "extraction_method": page.get("extraction_method", "document_text"),
                    "quality_status": "NORMAL", "chunk_hash": text_hash(part),
                })

    chunks = merge_short_records(chunks)

    # Preserve curated/synthetic records that do not exist as raw page text.
    if BASE_CHUNKS.exists():
        for line in BASE_CHUNKS.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("section") not in {"class_inception", "source_quality_issue"}:
                continue
            chunks.append({
                **row, "doc_type": "product", "product_codes": [],
                "canonical_product_code": "", "extraction_method": "curated",
            })

    counts = Counter(row["chunk_hash"] for row in chunks)
    boilerplate_marked = 0
    for sequence, row in enumerate(chunks, 1):
        row["chunk_id"] = f"UCHUNK{sequence:06d}"
        row["duplicate_hash_count"] = counts[row["chunk_hash"]]
        row["boilerplate"] = counts[row["chunk_hash"]] >= 5
        boilerplate_marked += int(row["boilerplate"])

    output = INTEGRATED / "chunks.jsonl"
    output.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in chunks), encoding="utf-8")
    lengths = sorted(len(r["text"]) for r in chunks)
    audit = {
        "status": "passed" if not skipped_products else "failed",
        "chunks": len(chunks), "product_chunks": sum(r["doc_type"] == "product" for r in chunks),
        "institution_chunks": sum(r["doc_type"] == "institution" for r in chunks),
        "canonical_products": len(canonical), "skipped_products": skipped_products,
        "funds_covered": len({r["fund_id"] for r in chunks if r["fund_id"]}),
        "documents_covered": len({(r["doc_type"], r["doc_id"]) for r in chunks}),
        "short_chunks_under_80": sum(len(r["text"]) < 80 for r in chunks),
        "median_length": lengths[len(lengths) // 2], "max_length": max(lengths),
        "boilerplate_chunks": boilerplate_marked,
        "section_counts": dict(Counter(r["section"] for r in chunks)),
    }
    (ROOT / "data" / "validation" / "integration_rag_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
