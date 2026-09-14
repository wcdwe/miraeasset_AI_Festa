"""
연금 Agent 과제 - 검색 인터페이스 (의미 검색 + 구조화 표 검색)

이후 단계(질의 라우팅, HyperCLOVA X 파이프라인, 평가용 API 서버)가 공통으로
쓸 수 있는 검색 함수를 제공한다:

- semantic_search(query, k, doc_type, product_code)
    Chroma 벡터 스토어에서 의미 기반 top-k 청크 검색.
- table_search(keyword, k, doc_type, product_code)
    SQLite FTS5로 표(세액공제 한도, 위험등급, 총보수 등) 키워드 검색.

두 함수 모두 결과에 근거 문서 메타데이터(doc_id, source_doc, page 등)를
포함해서 "모든 답변에는 근거 문서 표시" 요구사항을 그대로 만족시킨다.

사용법(CLI, 수동 점검용):
    python scripts/search.py --query "DC와 DB, 운용 주체가 어떻게 다른가요?"
    python scripts/search.py --query "세액공제 한도" --mode table
"""

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import logging
from functools import lru_cache

try:
    import chromadb
except ImportError:  # lexical/SQLite search remains available without Chroma
    chromadb = None

from embeddings import get_provider
from build_vector_store import DEFAULT_STORE_DIR, COLLECTION_NAME, provider_state_path
from build_structured_store import DEFAULT_DB_PATH

FLAT_STORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "integrated", "vector_store", "flat")


def _canonical_product_code(product_code):
    """Resolve a duplicate/alias code to the one canonical code indexed in RAG."""
    if not product_code:
        return product_code
    mapping_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "integrated", "fund_products.csv")
    try:
        with open(mapping_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["product_code"] == product_code:
                    return row["canonical_product_code"]
    except OSError:
        pass
    return product_code


def semantic_search(query, k=5, doc_type=None, product_code=None, provider_name=None,
                     store_dir=DEFAULT_STORE_DIR):
    product_code = _canonical_product_code(product_code)
    if chromadb is not None:
        try:
            client = chromadb.PersistentClient(path=store_dir)
            collection = client.get_collection(COLLECTION_NAME)
            provider = get_provider(provider_name)
            state_path = provider_state_path(store_dir)
            if os.path.exists(state_path):
                provider.load(state_path)
            query_embedding = provider.embed([query], is_query=True)[0]
            where = {}
            if doc_type:
                where["doc_type"] = doc_type
            if product_code:
                where["product_code"] = product_code
            result = collection.query(query_embeddings=[query_embedding], n_results=k, where=where or None)
            return [{"chunk_id": cid, "text": doc, "score": 1 - dist, **meta, "retrieval_backend": "chroma"}
                    for doc, meta, dist, cid in zip(result["documents"][0], result["metadatas"][0],
                                                   result["distances"][0], result["ids"][0])]
        except Exception as exc:
            logging.getLogger("agent_v2.audit").info("Chroma fallback: %s", type(exc).__name__)
    return _flat_semantic_search(query, k, doc_type, product_code)


@lru_cache(maxsize=1)
def _load_flat_index(vector_path, metadata_path, state_path, revision):
    import numpy as np
    provider = get_provider("tfidf")
    provider.load(state_path)
    vectors = np.load(vector_path, mmap_mode="r")
    with open(metadata_path, encoding="utf-8") as f:
        metadata = json.load(f)
    if len(vectors) != len(metadata): raise RuntimeError("Vector/metadata row count mismatch")
    return provider, vectors, metadata


def _flat_semantic_search(query, k, doc_type, product_code):
    import numpy as np
    vector_path = os.path.join(FLAT_STORE_DIR, "vectors.npy")
    metadata_path = os.path.join(FLAT_STORE_DIR, "metadata.json")
    state_path = os.path.join(FLAT_STORE_DIR, "embedding_provider.pkl")
    if not all(os.path.exists(p) for p in (vector_path, metadata_path, state_path)):
        raise RuntimeError("No usable semantic index. Run integration/build_flat_vector_store.py")
    revision = tuple((os.stat(p).st_mtime_ns, os.stat(p).st_size) for p in (vector_path, metadata_path, state_path))
    provider, vectors, metadata = _load_flat_index(vector_path, metadata_path, state_path, revision)
    query_vector = np.asarray(provider.embed([query], is_query=True)[0], dtype=np.float32)
    candidates = []
    for index, meta in enumerate(metadata):
        if doc_type and meta.get("doc_type") != doc_type:
            continue
        if product_code and meta.get("canonical_product_code") != product_code:
            continue
        score = float(np.dot(vectors[index], query_vector))
        if meta.get("boilerplate"):
            score *= 0.5
        candidates.append((score, meta))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [{"score": score, "product_code": meta.get("canonical_product_code"), **meta,
             "retrieval_backend": "flat_tfidf_svd"}
            for score, meta in candidates[:k]]


# 질의에서 뽑아 쓰지 않을 말들. 어느 문서에나 있어서 검색을 넓히기만 한다.
LEXICAL_STOPWORDS = {
    "무엇", "뭔가요", "뭐야", "어떻게", "어떤", "얼마", "얼마나", "언제", "누가",
    "가능", "가능한가요", "인가요", "입니까", "있나요", "되나요", "하나요", "알려줘",
    "경우", "관련", "대해", "대한", "그리고", "하지만", "때문", "정도", "정말",
    # 묻는 방식에 딸린 말들. 뜻은 없는데 원문에 잘 안 쓰여서(원문은 "70%"라고
    # 쓰지 "70퍼센트"라고 안 쓴다) 되레 아주 드문 말이 되고, 드물다는 이유로
    # 가장 무거운 점수를 받아 검색을 통째로 망친다. "DC형에서 위험자산은 최대
    # 몇 퍼센트"에서 '퍼센트'가 딱 한 청크에 있다는 이유로 1등을 차지했다.
    "퍼센트", "프로", "최대", "최소", "최근", "이상", "이하", "미만", "초과",
}
# 고객이 쓰는 말 -> 문서가 쓰는 말.
#
# 고객은 "세금"이라 하고 투자설명서는 "소득세/과세/세율"이라고 쓴다. 글자
# 그대로 찾는 검색은 이 차이를 못 넘는다. 검증 세트의 "연금저축을
# 중도해지하면 세금이 어떻게 되나요?"가 정답 청크(기타소득세 16.5%)를
# 8등에 놓고 있었는데, 그 청크에는 "세금"이라는 말이 아예 없고 "소득세",
# "세율"만 있었다.
#
# 답을 미리 넣는 게 아니라 같은 뜻의 말을 넓히는 것이다. 원래 질문의
# 낱말보다 가볍게 쳐서(EXPANSION_WEIGHT) 질문에 없던 말이 질문에 있는
# 말을 이기지 못하게 한다.
# 실제로 검증한 것만 넣는다. 처음엔 해지->중도인출, 받는->수령 같은 것도
# 넣었는데 엉뚱한 청크를 끌어올려 되레 나빠졌다. 넓히는 건 공짜가 아니다.
DOMAIN_EXPANSIONS = {
    "세금": ("소득세", "과세", "세율"),
    # 고객 표현과 문서 표제의 차이를 잇는다. 통합 institution 코퍼스의
    # 근거는 '중도해지'보다 '중도인출/연금외수령'을 주로 사용한다.
    "중도해지": ("중도인출", "연금외수령", "기타소득세"),
}
EXPANSION_WEIGHT = 0.5

# 한글/영문/숫자가 섞인 덩어리를 한 낱말로 본다("DC형에서", "1,000만원").
_TERM_RE = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9,.]*")

# 전체 청크의 이 비율을 넘게 나오는 낱말은 너무 흔해서 찾는 대상에서 뺀다.
COMMON_TERM_RATIO = 0.05

# 낱말 끝에 붙는 조사와 어미. 긴 것부터 떼어 낸다.
#
# 왜 필요한가: trigram 색인은 글자 그대로 찾으므로 "연금저축을"은 원문의
# "연금저축은"/"연금저축에"와 안 맞는다. 검증 세트에서 "연금저축을
# 중도해지하면 세금이..." 질문이 정답 청크(기타소득세 16.5%)를 못 찾은
# 이유가 이것이었다. 형태소 분석기를 붙이지 않고, 붙는 말만 떼어 낸다.
_PARTICLES = (
    "에서는", "에게는", "으로는", "이라는", "이라고", "입니다", "합니다",
    "하면서", "에서", "에게", "으로", "까지", "부터", "보다", "라도",
    "이나", "든지", "조차", "마저", "처럼", "만큼", "하면", "하는", "되면",
    "되는", "이란", "인가", "인지", "한테",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만",
    "로", "라", "한", "할", "된", "됨", "임",
)


def _acronym_min_len(remainder):
    """조사를 뗀 나머지가 영문 대문자 약어(DC/DB/MP 등)면 2글자까지 허용.

    "DC와"는 정확히 3글자라 예전 문턱(3글자 밑으로 못 줄임)에 걸려 조사를
    아예 못 뗐다 - 검색어가 "DC"가 아니라 원문 어디에도 없는 "DC와" 그대로
    남아, "DC와 DB, 운용 주체가 어떻게 다른가요?" 같은 질의에서 정답
    청크(doc10)가 키워드 검색 후보에 통째로 못 들었다(실측). 한글
    2글자는 여전히 3글자 문턱을 유지한다 - 한글은 조사를 떼도 뜻이
    불분명한 경우가 많아 함부로 넓히면 엉뚱한 청크가 올라온다
    (DOMAIN_EXPANSIONS를 검증된 것만 넣는 것과 같은 이유)."""
    return 2 if remainder.isascii() and remainder.isalpha() and remainder.isupper() else 3


def _strip_particles(term):
    """낱말 뒤에 붙은 조사/어미를 뗀다. 3글자(영문 대문자 약어는 2글자) 밑으로 줄면 그만둔다."""
    changed = True
    while changed and len(term) > 2:
        changed = False
        for p in _PARTICLES:
            if term.endswith(p):
                remainder = term[: -len(p)]
                if len(remainder) >= _acronym_min_len(remainder):
                    term = remainder
                    changed = True
                    break
    return term


def lexical_term_groups(query):
    """질의의 낱말을 [조사 붙은 형태, 뗀 형태]씩 묶어서 돌려준다.

    묶는 이유: 한 낱말을 두 형태로 넣어 두고 각각 점수를 주면 같은 말을
    두 번 세게 된다. "DC형에서"(8.9) + "DC형"(7.8) = 16.7이 "위험자산"
    (5.6) 하나를 압도해서, 정작 위험자산 얘기가 없는 청크가 1등이 됐다.
    한 묶음은 한 낱말로 치고 한 번만 점수를 준다.

    trigram 색인은 3글자 미만을 못 찾으므로 3글자 이상만 남긴다(단, 조사를
    뗀 결과가 DC/DB처럼 영문 대문자 약어면 2글자도 남긴다 - lexical_search
    가 그런 짧은 말은 LIKE로 찾아서 별도로 처리한다)."""
    groups, seen = [], set()
    expansions = []
    for raw in _TERM_RE.findall(query or ""):
        raw = raw.strip(",.")
        stem = _strip_particles(raw)
        forms = (raw, stem)
        # 조사를 뗀 형태가 불용어면 붙은 형태도 같은 말이다("퍼센트까지").
        if any(f in LEXICAL_STOPWORDS for f in forms):
            continue
        variants = []
        for t in forms:
            is_short_acronym = len(t) == 2 and t.isascii() and t.isalpha() and t.isupper()
            if (len(t) < 3 and not is_short_acronym) or t in seen:
                continue
            seen.add(t)
            variants.append(t)
        if variants:
            groups.append(variants)
        # 문서가 쓰는 말로 넓힌다. 2글자 알맹이("세금")는 색인에서 못 찾아
        # 버려지므로, 조사를 다 뗀 형태로도 찾아본다.
        bare = raw
        for pcl in _PARTICLES:
            if bare.endswith(pcl) and len(bare) > len(pcl):
                bare = bare[: -len(pcl)]
                break
        for key in (raw, stem, bare):
            for syn in DOMAIN_EXPANSIONS.get(key, ()):
                if syn not in seen:
                    seen.add(syn)
                    expansions.append([syn])
    return groups + expansions


def _is_expansion(variants, query):
    """이 묶음이 질문에 없던 말(넓힌 말)인지."""
    return not any(v in (query or "") for v in variants)


def lexical_terms(query):
    """lexical_term_groups를 펼친 것 (수동 점검·디버깅용)."""
    return [t for g in lexical_term_groups(query) for t in g]


def lexical_search(query, k=5, doc_type=None, product_code=None, db_path=DEFAULT_DB_PATH):
    """청크 본문을 글자 그대로 찾는다 (FTS5 trigram + bm25).

    의미 검색과 짝을 이룬다. 의미 검색은 "비슷한 얘기"를 잘 찾지만 정확한
    용어를 놓치고("기타소득세"를 물었는데 ISA 전환 FAQ가 1등), 이쪽은 정확한
    용어를 놓치지 않는 대신 말을 바꿔 물으면 못 찾는다. 둘을 합쳐 쓴다."""
    product_code = _canonical_product_code(product_code)
    groups = lexical_term_groups(query)
    if not groups:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not total:
            return []

        # 낱말마다 몇 개 청크에 나오는지 세서 무게를 매긴다. 흔한 말일수록
        # 가볍다. 이게 없으면 OR로 묶인 질의에서 가장 흔한 낱말 하나만
        # 걸린 긴 청크가 1등으로 올라온다 - "DC형에서 위험자산은 최대 몇
        # 퍼센트"에 '위험자산'이 아예 없는 청크가 뽑히던 이유다.
        #
        # 한 묶음의 무게는 그 안에서 가장 가벼운 형태의 것을 쓴다. 조사가
        # 붙은 "DC형에서"는 조사 때문에 드물 뿐 "DC형"보다 알맹이가 더
        # 있는 말이 아니므로, 드물다고 무겁게 쳐 주면 안 된다.
        weighted, query_terms = [], []
        for variants in groups:
            found = []
            for t in variants:
                # trigram 색인은 3글자 미만을 못 찾는다. 그런 말("과세",
                # "세율")도 줄 세우기에는 쓸모가 있으므로, 찾는 데는 안 쓰고
                # 글자 그대로 세어 점수에만 반영한다.
                try:
                    if len(t) >= 3:
                        df = conn.execute(
                            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                            [f'"{t}"']).fetchone()[0]
                    else:
                        df = conn.execute(
                            "SELECT COUNT(*) FROM chunks WHERE text LIKE ?",
                            [f"%{t}%"]).fetchone()[0]
                except sqlite3.OperationalError:
                    continue
                if df:
                    found.append((t, df, math.log(1 + total / df)))
            if not found:
                continue
            t, df, w = min(found, key=lambda f: f[2])
            # 너무 흔한 말은 점수에서도 뺀다. 찾는 대상에서는 빼면서
            # 점수는 주고 있었는데 앞뒤가 안 맞는다 - "연금저축"(코퍼스의
            # 6.3%)만 걸린 청크가 그 2.82점으로 "중도해지"(6.07)가 든
            # 정답 청크를 밀어냈다.
            if df > total * COMMON_TERM_RATIO:
                continue
            names = [f[0] for f in found]
            if _is_expansion(names, query):
                w *= EXPANSION_WEIGHT
            weighted.append((w, names))
            query_terms.extend(n for n in names if len(n) >= 3)
        if not weighted:
            # 다 흔한 말뿐이면 그중 가장 드문 묶음 하나로라도 찾는다.
            for variants in groups:
                found = []
                for t in variants:
                    try:
                        df = conn.execute(
                            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                            [f'"{t}"']).fetchone()[0]
                    except sqlite3.OperationalError:
                        continue
                    if df:
                        found.append((t, df, math.log(1 + total / df)))
                if found:
                    _t, _df, w = min(found, key=lambda f: f[2])
                    weighted.append((w, [f[0] for f in found]))
            if not weighted:
                return []
            best = max(weighted, key=lambda g: g[0])
            weighted, query_terms = [best], list(best[1])

        # 낱말 묶음마다 따로 찾아서 후보를 모은다.
        #
        # 처음엔 낱말을 전부 OR로 묶어 한 번에 찾고 상위 100개만 받아서
        # 다시 줄 세웠는데, 흔한 낱말("DC형")에 걸린 청크가 100자리를 다
        # 채워서 정작 "위험자산"이 있는 청크는 후보에 들지도 못했다.
        # 묶음마다 자리를 따로 주면 어느 낱말도 밀려나지 않는다.
        rows, seen_ids = [], set()
        for _w, variants in weighted:
            if not any(t in query_terms for t in variants):
                continue
            # trigram 색인은 3글자 미만을 아예 못 찾는다(MATCH 질의에 넣어도
            # 조용히 0건) - DC/DB처럼 2글자 약어만 남은 낱말은 MATCH가 아니라
            # LIKE로 따로 찾는다. 나머지(3글자 이상)는 그대로 MATCH를 쓴다.
            long_terms = [t for t in variants if len(t) >= 3]
            short_terms = [t for t in variants if len(t) < 3]
            if long_terms:
                sql = """
                    SELECT c.id, c.chunk_id, c.doc_type, c.doc_id, c.source_doc,
                           c.product_code, c.page, c.text, bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    WHERE chunks_fts MATCH ?
                """
                params = [" OR ".join(f'"{t}"' for t in long_terms)]
                if doc_type:
                    sql += " AND c.doc_type = ?"
                    params.append(doc_type)
                if product_code:
                    sql += " AND c.product_code = ?"
                    params.append(product_code)
                sql += " ORDER BY rank LIMIT ?"
                params.append(max(k * 10, 50))
                try:
                    for r in conn.execute(sql, params):
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            rows.append(r)
                except sqlite3.OperationalError:
                    pass  # 색인이 없거나 질의 문법이 안 맞으면 조용히 넘어간다
            for t in short_terms:
                sql = """
                    SELECT c.id, c.chunk_id, c.doc_type, c.doc_id, c.source_doc,
                           c.product_code, c.page, c.text, 0 AS rank
                    FROM chunks c
                    WHERE c.text LIKE ?
                """
                params = [f"%{t}%"]
                if doc_type:
                    sql += " AND c.doc_type = ?"
                    params.append(doc_type)
                if product_code:
                    sql += " AND c.product_code = ?"
                    params.append(product_code)
                sql += " LIMIT ?"
                params.append(max(k * 10, 50))
                try:
                    for r in conn.execute(sql, params):
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            rows.append(r)
                except sqlite3.OperationalError:
                    continue
    finally:
        conn.close()

    scored = []
    for r in rows:
        text = r["text"]
        covered = sum(w for w, variants in weighted
                      if any(t in text for t in variants))
        if not covered:
            continue
        scored.append((covered, -r["rank"], r))
    scored.sort(key=lambda s: (-s[0], -s[1]))

    return [{
        "chunk_id": r["chunk_id"],
        "text": r["text"],
        "doc_type": r["doc_type"],
        "doc_id": r["doc_id"],
        "source_doc": r["source_doc"],
        "product_code": r["product_code"],
        "page": r["page"],
        # 의미 검색의 코사인 유사도와는 척도가 달라서 그대로 견주면 안 된다
        # (router에서 순위로만 쓴다).
        "lexical_score": covered,
        "bm25": bm,
    } for covered, bm, r in scored[:k]]


def table_search(keyword, k=5, doc_type=None, product_code=None, db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT t.doc_type, t.doc_id, t.source_doc, t.product_code, t.page,
               t.table_index, t.data_json, t.row_text,
               bm25(tables_fts) AS rank
        FROM tables_fts
        JOIN tables t ON t.id = tables_fts.rowid
        WHERE tables_fts MATCH ?
    """
    params = [keyword]
    if doc_type:
        sql += " AND t.doc_type = ?"
        params.append(doc_type)
    if product_code:
        sql += " AND t.product_code = ?"
        params.append(product_code)
    sql += " ORDER BY rank LIMIT ?"
    params.append(k)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        conn.close()
        raise ValueError(f"FTS5 쿼리 실패 (키워드 문법 확인): {e}") from e

    hits = []
    for r in rows:
        hits.append({
            "doc_type": r["doc_type"],
            "doc_id": r["doc_id"],
            "source_doc": r["source_doc"],
            "product_code": r["product_code"],
            "page": r["page"],
            "table_index": r["table_index"],
            "data": json.loads(r["data_json"]),
        })
    conn.close()
    return hits


def main():
    parser = argparse.ArgumentParser(description="연금 Agent 검색 인프라 수동 점검 CLI")
    parser.add_argument("--query", required=True)
    parser.add_argument("--mode", choices=["semantic", "table"], default="semantic")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--doc-type", choices=["institution", "products"], default=None)
    parser.add_argument("--product-code", default=None)
    args = parser.parse_args()

    if args.mode == "semantic":
        hits = semantic_search(args.query, k=args.k, doc_type=args.doc_type,
                                product_code=args.product_code)
    else:
        hits = table_search(args.query, k=args.k, doc_type=args.doc_type,
                             product_code=args.product_code)

    print(json.dumps(hits, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
