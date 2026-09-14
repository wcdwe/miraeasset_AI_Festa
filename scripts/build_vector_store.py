"""
연금 Agent 과제 - 벡터 스토어(Chroma) 적재 스크립트

extracted/{institution,products}/*_chunks.json 을 모아 임베딩하고
Chroma persistent collection(vector_store/chroma)에 적재한다.
임베딩은 embeddings.get_provider()로 선택 (기본: 로컬 다국어 모델,
HyperCLOVA X 키가 준비되면 --provider hyperclova 로 전환).

메타데이터(doc_type, doc_id, source_doc, product_code, page, chunk_id)는
그대로 저장해서 검색 결과에서 근거 문서를 바로 표시할 수 있게 한다.

사용법:
    python scripts/build_vector_store.py
    python scripts/build_vector_store.py --provider local --batch-size 64
"""

import argparse
import json
import os

try:
    import chromadb
except ImportError:
    chromadb = None
from tqdm import tqdm

from embeddings import get_provider

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted")
INTEGRATED_CHUNKS = os.path.join(REPO_ROOT, "data", "integrated", "chunks.jsonl")
DEFAULT_STORE_DIR = os.path.join(REPO_ROOT, "data", "integrated", "vector_store", "chroma")
COLLECTION_NAME = "pension_chunks"


def iter_chunk_files():
    for doc_type in ("institution", "products"):
        d = os.path.join(EXTRACTED_DIR, doc_type)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith("_chunks.json"):
                yield os.path.join(d, name)


def load_chunks():
    if os.path.exists(INTEGRATED_CHUNKS):
        with open(INTEGRATED_CHUNKS, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    chunks = []
    for path in iter_chunk_files():
        with open(path, "r", encoding="utf-8") as f:
            chunks.extend(json.load(f))
    return chunks


def sanitize_metadata(chunk):
    """Chroma 메타데이터는 str/int/float/bool만 허용 → None 필드 제거."""
    meta = {}
    for key in ("doc_type", "doc_id", "source_doc", "canonical_product_code", "fund_id",
                "section", "heading", "page", "chunk_id", "extraction_method",
                "quality_status", "boilerplate"):
        v = chunk.get(key)
        if v is not None:
            meta["product_code" if key == "canonical_product_code" else key] = v
    return meta


def provider_state_path(store_dir):
    return os.path.join(store_dir, "embedding_provider.pkl")


def build(provider_name, store_dir, batch_size, reset):
    if chromadb is None:
        raise RuntimeError("chromadb is not installed; install requirements.txt before vector indexing")
    chunks = load_chunks()
    if not chunks:
        print("청크가 없습니다. 먼저 scripts/extract_all.py를 실행하세요.")
        return

    provider = get_provider(provider_name)

    os.makedirs(store_dir, exist_ok=True)

    print(f"임베딩 프로바이더 fit 중: {provider.name} ({len(chunks)}개 청크)")
    provider.fit([c["text"] for c in chunks])
    provider.save(provider_state_path(store_dir))
    print(f"임베딩 프로바이더: {provider.name} (dim={provider.dimension})")
    client = chromadb.PersistentClient(path=store_dir)

    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        COLLECTION_NAME,
        metadata={"embedding_provider": provider.name},
    )

    n = len(chunks)
    for i in tqdm(range(0, n, batch_size), desc="임베딩 + 적재"):
        batch = chunks[i:i + batch_size]
        ids = [c["chunk_id"] for c in batch]
        texts = [c["text"] for c in batch]
        metadatas = [sanitize_metadata(c) for c in batch]
        embeddings = provider.embed(texts, is_query=False)
        collection.upsert(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)

    print(f"{n}개 청크 적재 완료 → {store_dir} (collection={COLLECTION_NAME})")


def main():
    parser = argparse.ArgumentParser(description="청크를 임베딩해 Chroma 벡터 스토어에 적재")
    parser.add_argument("--provider", default=None, help="local(기본) | hyperclova")
    parser.add_argument("--store-dir", default=DEFAULT_STORE_DIR)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--reset", action="store_true", help="기존 컬렉션 삭제 후 재적재")
    args = parser.parse_args()

    build(args.provider, args.store_dir, args.batch_size, args.reset)


if __name__ == "__main__":
    main()
