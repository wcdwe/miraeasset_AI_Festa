"""
연금 Agent 과제 - 질의 유형 분류 및 검색 라우팅

자연어 질의를 (제도/세제 institution) vs (상품 products) vs (복합)으로 분류하고,
scripts/search.py의 semantic_search / table_search를 적절히 호출해서
근거 후보(retrieved context)를 만든다.

HyperCLOVA X API 키가 아직 없어서, 분류는 지금은 키워드 규칙 기반이다.
키가 생기면 이 모듈의 classify()만 HCX 기반 의도분류로 교체하고,
route_search()의 인터페이스(반환 스키마)는 그대로 재사용하면 된다.

사용법(CLI, 수동 점검용):
    python scripts/router.py --query "DC와 DB, 운용 주체가 어떻게 다른가요?"
"""

import argparse
import json
import re

from search import semantic_search, lexical_search, table_search

# ---------------------------------------------------------------------------
# 키워드 규칙 (HCX 의도분류로 교체 전까지의 임시 휴리스틱)
# ---------------------------------------------------------------------------

INSTITUTION_KEYWORDS = [
    "db", "dc", "irp", "연금저축", "퇴직연금", "퇴직금", "개인연금",
    "세액공제", "연금소득세", "퇴직소득세", "기타소득세", "과세", "비과세", "세율",
    "중도인출", "해지", "수령", "개시", "이전", "이체", "승계", "가입", "인출",
    "부득이한 사유", "수령한도", "연금수령", "제도", "소득세법", "근로자퇴직급여보장법",
    "디폴트옵션", "명퇴", "퇴직", "연금계좌", "이연퇴직소득", "종합과세", "분리과세",
]

PRODUCT_KEYWORDS = [
    "펀드", "etf", "상품", "위험등급", "보수", "수수료", "수익률", "클래스",
    "리츠", "인프라펀드", "추천", "비교", "tdf", "mp", "포트폴리오", "종목",
    "투자설명서", "투자전략", "자산배분", "원리금보장", "국공채", "채권형",
    "주식형", "혼합형", "운용사", "설정액", "판매사",
]

TAX_KEYWORDS = [
    "세액공제", "연금소득세", "퇴직소득세", "기타소득세", "과세", "비과세",
    "세율", "종합과세", "분리과세", "한도",
]

# 세제뿐 아니라 "표로 정리된 명확한 사실값"을 묻는 질의는 table_search도 같이
# 태워야 근거가 잡힌다. 세액공제/한도 같은 세제 키워드에 더해, 위험등급/보수/
# 수수료/수익률처럼 상품 표에 그대로 나오는 수치성 키워드를 포함한다.
TABLE_FACT_KEYWORDS = TAX_KEYWORDS + [
    "위험등급", "보수", "수수료", "수익률", "설정액",
]

PRODUCT_CODE_RE = re.compile(r"KR[0-9A-Z]{10}", re.IGNORECASE)


def _build_fts_query(classification: dict) -> str | None:
    """자연어 문장을 그대로 FTS5 MATCH에 넘기면 (묵시적 AND라) 거의 안 걸린다.
    매칭된 키워드들을 OR로 묶어서 표 검색에 쓸 질의를 만든다."""
    terms = list(dict.fromkeys(
        classification["matched_institution_keywords"]
        + classification["matched_product_keywords"]
        + classification["product_codes"]
    ))
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def classify(query: str) -> dict:
    """질의를 규칙 기반으로 분류. institution/products 검색 여부와 세제 여부를 판단."""
    q = query.lower()

    inst_hits = [kw for kw in INSTITUTION_KEYWORDS if kw in q]
    prod_hits = [kw for kw in PRODUCT_KEYWORDS if kw in q]
    tax_hits = [kw for kw in TAX_KEYWORDS if kw in q]
    table_fact_hits = [kw for kw in TABLE_FACT_KEYWORDS if kw in q]
    product_codes = PRODUCT_CODE_RE.findall(query)

    use_institution = bool(inst_hits) or bool(tax_hits)
    use_products = bool(prod_hits) or bool(product_codes)

    # 둘 다 안 걸리면(짧은 질의, 애매한 질의 등) 폭넓게 양쪽 다 검색 -
    # 정보가 부족한 채로 좁혀서 검색하면 근거 누락 위험이 더 크다고 판단.
    ambiguous = not use_institution and not use_products
    if ambiguous:
        use_institution = True
        use_products = True

    if use_institution and use_products:
        category = "복합"
    elif use_institution:
        category = "제도/세제"
    elif use_products:
        category = "상품"
    else:
        category = "복합"  # unreachable given ambiguous fallback above, kept for clarity

    return {
        "category": category,
        "use_institution": use_institution,
        "use_products": use_products,
        "use_table_search": bool(table_fact_hits),
        "product_codes": product_codes,
        "matched_institution_keywords": inst_hits,
        "matched_product_keywords": prod_hits,
        "ambiguous": ambiguous,
    }


# 이 점수 밑이면 "근거로 쓰기엔 부실하다"고 보고 검색 범위를 넓혀 재시도한다.
# tfidf/LSA 코사인 유사도 기준 임시값 — 실제 질의로 튜닝된 값은 아니라 조정 여지 있음.
RELEVANCE_RETRY_THRESHOLD = 0.30

# RRF(reciprocal rank fusion)의 완충 상수. 60은 이 방식에서 흔히 쓰는 값으로,
# 1등과 2등의 점수 차이를 너무 벌리지 않아 한쪽 검색이 헛짚어도 다른 쪽이
# 만회할 수 있게 한다.
RRF_K = 60

# 화제별 보조 검색어. 정답 문서가 표 제목처럼 짧고 건조한 문장이라
# 자연어 질문과 어휘가 겹치지 않는 화제만 등록한다(실측: doc58 p.2
# "퇴직연금 적립금의 70%까지만 투자가능한 운용방법" - "위험자산 투자한도가
# 어떻게 되나요?"로는 top 60 안에도 안 들지만, "위험자산 투자한도 70%"
# 같은 키워드형 질의로는 잡힌다). 질문 문장을 대체하지 않고 이 보조
# 질의를 추가로 더 검색해서 후보를 넓힌다.
_QUERY_EXPANSIONS = [
    (re.compile(r"위험자산.*(?:한도|비중|퍼센트|%|담을)"), "위험자산 투자한도 70%"),
]


def _topic_expansion_queries(query: str) -> list[str]:
    return [aux for pattern, aux in _QUERY_EXPANSIONS if pattern.search(query or "")]


def route_search(query: str, k: int = 5) -> dict:
    """분류 결과에 따라 semantic_search / table_search를 호출하고 결과를 합쳐서 반환.

    1차 검색 결과의 최고 유사도가 너무 낮으면(RELEVANCE_RETRY_THRESHOLD 미만),
    분류가 좁게(institution만 또는 products만) 잡았을 가능성을 의심하고
    양쪽 다 검색하는 재시도를 한 번 한다 — "검색 결과가 질문과 안 맞으면
    쿼리 범위를 바꿔서 재시도" 패턴.

    반환값:
        {
            "classification": classify()의 결과,
            "semantic_hits": [...],
            "table_hits": [...],
            "retried": bool,
        }
    """
    classification = classify(query)

    def _search(doc_types):
        """의미 검색과 글자 검색을 각각 돌린 뒤 순위로 합친다.

        의미 검색만 쓰면 "연금저축 중도해지하면 세금이 어떻게 되나요?"에
        ISA 전환 FAQ가 1등으로 올라온다(검증 세트에서 잡힘). 정답인
        "기타소득세 16.5%" 청크는 코퍼스에 있는데도 TF-IDF를 256차원으로
        줄이는 과정에서 그 용어의 신호가 뭉개져 밀려난다.

        두 점수는 척도가 달라서(코사인 유사도 vs bm25) 직접 더할 수 없다.
        각자의 순위만 가지고 1/(60+순위)를 더하는 방식(RRF)을 쓴다 - 어느
        한쪽에서만 1등이어도 위로 올라오고, 양쪽에서 다 높으면 더 위로 간다.

        일부 화제(예: 위험자산 투자한도)는 정답이 표 제목처럼 짧고
        건조한 문장이라("퇴직연금 적립금의 70%까지만 투자가능한
        운용방법") 자연어 질문("위험자산 투자한도가 어떻게 되나요?")과
        어휘가 너무 달라 그 질문 그대로는 검색 후보에도 못 든다(실측:
        top 60 안에도 없었다). _topic_expansion_queries()가 이런 화제를
        알아보면 키워드형 보조 질의를 추가로 함께 검색한다."""
        pooled = {}
        queries = [query] + _topic_expansion_queries(query)
        for doc_type in doc_types:
            product_code = classification["product_codes"][0] if (
                doc_type == "products" and classification["product_codes"]
            ) else None
            for q in queries:
                sem = semantic_search(q, k=k, doc_type=doc_type, product_code=product_code)
                lex = lexical_search(q, k=k, doc_type=doc_type, product_code=product_code)
                for ranked in (sem, lex):
                    for rank, hit in enumerate(ranked):
                        key = (hit.get("doc_id"), hit.get("page"), hit.get("chunk_id"))
                        cur = pooled.get(key)
                        if cur is None:
                            cur = dict(hit)
                            cur.setdefault("score", 0.0)
                            cur["rrf"] = 0.0
                            pooled[key] = cur
                        else:
                            # 의미 검색 쪽 유사도는 살려 둔다(재검색 판단에 쓴다)
                            if hit.get("score") is not None:
                                cur["score"] = max(cur.get("score") or 0.0, hit["score"])
                            if hit.get("bm25") is not None:
                                cur["bm25"] = hit["bm25"]
                        cur["rrf"] += 1.0 / (RRF_K + rank + 1)

        # 사용자가 괄호 안에 정식 명칭처럼 긴 표현을 직접 적었다면 그
        # 표현을 실제로 포함한 근거를 우선한다. RRF 동점 때문에 같은
        # 페이지의 목차/인접 FAQ가 정식 정의 청크를 밀어내는 일을 막는다.
        anchors = [
            term for term in re.findall(r"[가-힣A-Za-z0-9]+", query or "")
            if len(term) >= 6
        ]
        if anchors:
            longest = max(anchors, key=len)
            for hit in pooled.values():
                if longest in hit.get("text", ""):
                    hit["rrf"] += 1.0 / (RRF_K + 1)
        hits = sorted(pooled.values(), key=lambda h: h["rrf"], reverse=True)
        return hits

    doc_types = []
    if classification["use_institution"]:
        doc_types.append("institution")
    if classification["use_products"]:
        doc_types.append("products")

    semantic_hits = _search(doc_types)

    retried = False
    both_types = {"institution", "products"}
    # 순위 합산(RRF) 뒤에는 1등이 글자 검색으로만 올라온 청크일 수 있어서
    # hits[0]의 유사도만 보면 안 된다. 결과 전체의 최고 유사도로 판단한다.
    def _top_score(hits):
        return max((h.get("score") or 0.0) for h in hits) if hits else 0.0

    top_score = _top_score(semantic_hits)
    if top_score < RELEVANCE_RETRY_THRESHOLD and set(doc_types) != both_types:
        retried = True
        doc_types = ["institution", "products"]
        retry_hits = _search(doc_types)
        # 재시도 결과가 원래 결과보다 나을 때만 채택 (더 나쁘면 원래 결과 유지)
        if retry_hits and _top_score(retry_hits) > top_score:
            semantic_hits = retry_hits

    table_hits = []
    if classification["use_table_search"]:
        fts_query = _build_fts_query(classification)
        if fts_query:
            product_code = classification["product_codes"][0] if classification["product_codes"] else None
            for doc_type in doc_types:
                try:
                    # 상품코드가 특정됐으면 그 상품 표만 우선 검색 (다른 상품의
                    # 변경이력 표 등 무관한 표가 키워드만 겹쳐서 섞여 들어오는 걸 방지)
                    scoped_hits = table_search(
                        fts_query, k=k, doc_type=doc_type,
                        product_code=product_code if doc_type == "products" else None,
                    )
                    if product_code and doc_type == "products" and not scoped_hits:
                        scoped_hits = table_search(fts_query, k=k, doc_type=doc_type)
                    table_hits.extend(scoped_hits)
                except ValueError:
                    pass  # FTS5 쿼리 문법에 안 맞으면 표 검색 스킵

    return {
        "classification": classification,
        "semantic_hits": semantic_hits[: max(k, 5)],
        "table_hits": table_hits[:k],
        "retried": retried,
    }


def main():
    parser = argparse.ArgumentParser(description="질의 분류 + 라우팅 검색 수동 점검 CLI")
    parser.add_argument("--query", required=True)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    result = route_search(args.query, k=args.k)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
