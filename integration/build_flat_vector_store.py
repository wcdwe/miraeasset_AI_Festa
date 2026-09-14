#!/usr/bin/env python3
"""Build a portable NumPy semantic index (fallback for Chroma/HNSW issues)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from embeddings import get_provider  # noqa: E402

CHUNKS = ROOT / "data" / "integrated" / "chunks.jsonl"
CHROMA_STATE = ROOT / "data" / "integrated" / "vector_store" / "chroma" / "embedding_provider.pkl"
OUT = ROOT / "data" / "integrated" / "vector_store" / "flat"


def main():
    records = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines() if line]
    provider = get_provider("tfidf")
    if CHROMA_STATE.exists():
        provider.load(CHROMA_STATE)
    else:
        provider.fit([r["text"] for r in records])
    vectors = np.asarray(provider.embed([r["text"] for r in records]), dtype=np.float32)
    OUT.mkdir(parents=True, exist_ok=True)
    np.save(OUT / "vectors.npy", vectors)
    metadata = [{k: r.get(k) for k in (
        "chunk_id", "doc_type", "doc_id", "source_file", "canonical_product_code",
        "fund_id", "section", "heading", "page", "text", "quality_status", "boilerplate")}
        for r in records]
    (OUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    provider.save(OUT / "embedding_provider.pkl")
    print({"vectors": len(vectors), "dimension": vectors.shape[1], "output": str(OUT)})


if __name__ == "__main__":
    main()
