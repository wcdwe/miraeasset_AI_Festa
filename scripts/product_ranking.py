"""여러 상품을 조건으로 걸러 정렬해서 답한다 (랭킹/다중조건검색 질의).

지금까지 구조화 DB 조회 경로는 상품 하나(product_facts)나 이름으로
지목된 둘(compare_products)만 다뤘다. "1년 수익률 상위 5개", "총보수가
가장 낮은 상품 5개", "위험등급 4 이하이고 총보수가 낮은 상품", "채권형
펀드 중에서 수익률이 제일 좋은 것" 같은 질문은 지목된 상품이 없고 전체
상품 100개를 조건으로 걸러 정렬해야 답이 나오는데, 그런 질의 경로
자체가 없어서 router의 텍스트 검색(RAG)으로 빠졌다 - 정렬·필터링은
텍스트 청크 몇 개로 할 수 있는 일이 아니라서 엉뚱한 답이 나갔다.
이 모듈이 그 경로를 만든다.

원칙(product_facts.py/compare_products.py와 같다):
- 값은 DB에서 그대로 가져온다(해석하지 않는다).
- 보수는 일반 고객이 가입 가능한 클래스만 본다(기관/고액/랩 전용 제외) -
  안 그러면 살 수도 없는 클래스가 "제일 싼 상품"으로 뽑힌다.
- 조건에 안 걸리는 상품은 그냥 뺀다. 값이 없어 순위를 못 매긴 상품이
  있으면 몇 개 뺐는지 밝힌다(있는 척하지 않는다).

detect()는 "이 질문이 랭킹/필터 질의인가"만 판단한다. 그냥 "주식형
펀드가 뭐예요?" 같은 설명 질문까지 받아버리면 랭킹 5개만 뚝 떼어
보여주는 엉뚱한 답이 되므로, 정렬 지표(보수/수익률/설정액) 또는 명시적
비교 조건(위험등급 N 이하 등)이 있을 때만 랭킹 질의로 본다.

사용법(CLI, 수동 점검용):
    python3 scripts/product_ranking.py --ask "총보수가 가장 낮은 상품 5개 알려줘"
"""

import argparse
import re
import sqlite3

from build_product_facts_db import DEFAULT_DB_PATH

# 한 번에 보여줄 상품 수 상한. "상위 100개"라고 물어도 답이 안 읽히므로 자른다.
MAX_RESULTS = 10
DEFAULT_RESULTS = 5

# 사용자가 말하는 분류 -> DB의 실제 asset_type 값들. product_master를
# 직접 조회해 보면 코퍼스 100개 상품은 "채권"/"주식"/"주식-파생형"/
# "주식혼합-재간접형"/"국공채" 다섯 값뿐이다("혼합형"/"채권형"처럼 "-형"이
# 안 붙는다) - 사용자 말과 DB 표기가 다르므로 매핑이 필요하다. TDF는
# 이 코퍼스에 실제로 없어서 빈 튜플로 두고, 매칭되면 "0건"이라고 정직하게
# 답한다(있는 척 다른 상품을 끼워 넣지 않는다).
ASSET_TYPE_GROUPS = {
    "주식형": ("주식", "주식-파생형"),
    "주식": ("주식", "주식-파생형"),
    "채권형": ("채권", "국공채"),
    "채권": ("채권", "국공채"),
    "국공채": ("국공채",),
    "혼합형": ("주식혼합-재간접형",),
    "혼합": ("주식혼합-재간접형",),
    "tdf": (),
    "TDF": (),
}

RETURN_PERIOD_COLUMNS = [
    ("1년", "return_1y"), ("2년", "return_2y"), ("3년", "return_3y"),
    ("5년", "return_5y"), ("설정후", "return_since_inception"),
    ("설정이후", "return_since_inception"),
]

# 답변에 열 이름(return_1y)을 그대로 노출하면 답변이 "return_1y 12.3%"처럼
# 사람 말이 아니게 된다. answer_llm.check_question_coverage가 "수익률"
# 낱말이 답에 있는지도 보므로, 자연어 라벨을 반드시 같이 낸다.
RETURN_COL_LABELS = {
    "return_1y": "1년 수익률", "return_2y": "2년 수익률", "return_3y": "3년 수익률",
    "return_5y": "5년 수익률", "return_since_inception": "설정후 수익률",
}

LOW_WORDS = ("낮은", "낮게", "적은", "싼", "저렴", "최저", "작은")
HIGH_WORDS = ("높은", "높게", "많은", "큰", "비싼", "최고", "최대", "우수")

TOPN_RE = re.compile(r"(?:상위|하위|top|bottom)\s*(\d+)\s*(?:개|위)?", re.IGNORECASE)
N_RE = re.compile(r"(\d+)\s*개")
RISK_COND_RE = re.compile(r"위험\s*등급\s*(?:이)?\s*(\d)\s*(?:등급)?\s*(이하|미만|이상|초과)")
FEE_COND_RE = re.compile(r"(?:총보수|보수)\s*(?:가|는|이)?\s*([\d.]+)\s*%\s*(이하|미만|이상|초과)")

_OPS = {
    "이하": lambda v, n: v <= n,
    "미만": lambda v, n: v < n,
    "이상": lambda v, n: v >= n,
    "초과": lambda v, n: v > n,
}


def _direction(qn, default):
    if any(w in qn for w in LOW_WORDS):
        return "asc"
    if any(w in qn for w in HIGH_WORDS):
        return "desc"
    return default


def detect(question):
    """랭킹/필터 질의로 보이면 조건 dict, 아니면 None.

    정렬 지표(수익률/총보수/설정액/위험등급)나 명시적 비교 조건이 하나도
    없으면 None - "OO 펀드가 뭐예요" 같은 설명 질문까지 여기서 가로채면
    안 된다."""
    q = question or ""
    qn = q.replace(" ", "")

    risk_filter = None
    m = RISK_COND_RE.search(qn)
    if m:
        risk_filter = (int(m.group(1)), m.group(2))

    fee_filter = None
    m = FEE_COND_RE.search(qn)
    if m:
        fee_filter = (float(m.group(1)), m.group(2))

    asset_types = None
    for kw, vals in ASSET_TYPE_GROUPS.items():
        if kw in qn:
            asset_types = vals
            break

    # 정렬 지표: 명시적 비교 조건으로 이미 걸린 지표는 필터로만 쓰고
    # 정렬 후보에서 뺀다("위험등급 4 이하" 자체를 다시 정렬 기준으로
    # 삼으면 뜻이 겹친다).
    #
    # "총보수"/"수익률" 낱말이 있다는 것만으로 정렬 지표를 잡으면 안 된다
    # - "미래에셋솔로몬단기국공채 총보수 얼마야?"(상품 하나를 지목한
    # 질문)에도 "총보수"가 들어 있고, 하필 상품명에 "국공채"까지 들어
    # 있어서 분류 필터까지 잘못 걸릴 뻔했다. "낮은/비싼" 같은 방향어도
    # 신호로 못 쓴다 - "하나IT코리아 보수가 비싼 편인가요?"(상품 하나의
    # 성격을 묻는 질문, PROD-14)에도 "비싼"이 그대로 들어 있어서 방향어만
    # 믿으면 이 질문까지 랭킹으로 가로챈다(실제로 이 회귀가 잡혔다).
    # "여럿 중에서 줄 세워 달라"는 확실한 신호(개수/최상급/"~중에서"/명시적
    # 비교 조건)가 있을 때만 랭킹 질의로 본다 - 없으면 find_products
    # 경로가 처리하도록 None을 돌려준다.
    m = TOPN_RE.search(qn) or N_RE.search(qn)
    has_topn = bool(m)
    has_superlative = "가장" in qn or "제일" in qn or "순위" in qn
    # "중에" 단독은 안 쓴다 - "A과 B 중에 뭐가 더 싸?"(상품 두 개를 직접
    # 지목한 비교 질문, CMP-01)에도 "중에"가 들어 있어서 비교 질문을
    # 랭킹으로 가로챌 뻔했다. "중에서"/"들중"은 "카테고리 안에서" 꼴로만
    # 쓰이므로 안전하다.
    has_among = "중에서" in qn or "들중" in qn
    # 명시적 비교 조건(위험등급 4 "이하", 총보수 2% "이하")이 이미 있으면
    # 그 자체로 "여럿을 걸러야 하는 질문"이 확정된다 - "위험등급 4
    # 이하이고 총보수가 낮은 상품"처럼 그 뒤에 방향어("낮은")만 붙는
    # 경우까지 정렬 지표로 받아야 하므로 신호에 포함한다. asset_types
    # 매칭은 상품명 부분 일치로도 걸릴 수 있어(예: 상품명에 "국공채"가
    # 들어간 단일 상품 질문) 신호로 못 쓴다.
    rank_signal = (has_topn or has_superlative or has_among
                   or risk_filter is not None or fee_filter is not None)

    sort_metric = None
    sort_direction = "desc"
    return_col = "return_1y"

    has_return_kw = any(w in qn for w in ("수익률", "성과", "수익"))
    has_fee_kw = ("총보수" in qn or "보수" in qn or "수수료" in qn) and fee_filter is None
    has_aum_kw = any(w in qn for w in ("설정액", "순자산", "규모", "자산총액"))
    has_risk_kw = (any(w in qn for w in ("위험등급", "위험", "안전")) and risk_filter is None)

    if has_return_kw and rank_signal:
        sort_metric = "return"
        sort_direction = _direction(qn, default="desc")
        for label, col in RETURN_PERIOD_COLUMNS:
            if label in qn:
                return_col = col
                break
    elif has_fee_kw and rank_signal:
        sort_metric = "fee"
        sort_direction = _direction(qn, default="asc")
    elif has_aum_kw and rank_signal:
        sort_metric = "aum"
        sort_direction = _direction(qn, default="desc")
    elif has_risk_kw and rank_signal:
        sort_metric = "risk"
        # 위험등급 숫자와 실제 위험 정도는 방향이 반대다(1등급이 가장
        # 위험, 등급 숫자가 클수록 안전 - 실측: 100% 주식형인 "미래에셋
        # 코어테크"가 1등급, 단기채권형이 6등급). "등급"이 붙은 표현은
        # 등급 숫자 자체를 묻는 것이라 숫자 오름차순이 "낮은 순"이 맞다.
        # 하지만 "위험도가 가장 낮은 상품"/"가장 안전한 상품"처럼 "등급"
        # 없이 실제 위험을 묻는 표현은 그 반대다 - 그대로 오름차순을
        # 쓰면 "가장 안전한 상품"을 물었는데 가장 위험한(1등급) 상품을
        # 돌려주는 정반대의 오답이 된다(실측 재현: "위험도가 가장 낮은
        # 상품은?"이 위험등급 1등급 상품을 답했었다).
        if "등급" in qn:
            sort_direction = _direction(qn, default="asc")
        else:
            wants_safe = "안전" in qn or any(w in qn for w in LOW_WORDS)
            sort_direction = "desc" if wants_safe else "asc"

    if sort_metric is None and risk_filter is None and fee_filter is None:
        return None

    limit = DEFAULT_RESULTS
    if has_topn:
        limit = min(int(m.group(1)), MAX_RESULTS)
    elif "가장" in qn or "제일" in qn:
        limit = 1

    return {
        "sort_metric": sort_metric,
        "sort_direction": sort_direction,
        "return_col": return_col,
        "risk_filter": risk_filter,
        "fee_filter": fee_filter,
        "asset_types": asset_types,
        "limit": limit,
    }


def _fee_map(conn):
    """상품코드 -> (일반 고객이 가입 가능한 클래스 중 가장 낮은 총보수, 그 클래스코드).

    기관/고액/랩 전용 클래스는 살 수 없으므로 뺀다(product_facts._fee_lines와
    같은 이유)."""
    out = {}
    for r in conn.execute(
            "SELECT cf.product_code, cf.class_code, cf.total_fee, cf.page "
            "FROM class_fees cf JOIN class_meaning cm "
            "ON cm.product_code = cf.product_code AND cm.class_code = cf.class_code "
            "WHERE cf.total_fee IS NOT NULL AND cm.retail = 1"):
        cur = out.get(r["product_code"])
        if cur is None or r["total_fee"] < cur[0]:
            out[r["product_code"]] = (r["total_fee"], r["class_code"], r["page"])
    return out


def _return_map(conn, col):
    """상품코드 -> (대표 클래스의 수익률, 클래스코드, page).

    한 상품 안에서도 클래스마다 값이 조금씩 다르다(product_facts.py와
    같은 문제). 연금 계좌로 살 수 있는 클래스를 우선하고, 그중 신뢰도가
    가장 높은 것을 대표로 쓴다."""
    rows = conn.execute(
        f"SELECT cr.product_code, cr.class_code, cr.{col} AS val, cr.confidence, "
        "cm.account_type, cr.page "
        "FROM class_returns cr JOIN class_meaning cm "
        "ON cm.product_code = cr.product_code AND cm.class_code = cr.class_code "
        f"WHERE cr.row_kind = 'class_return' AND cr.{col} IS NOT NULL AND cm.retail = 1")
    by_product = {}
    for r in rows:
        by_product.setdefault(r["product_code"], []).append(r)
    out = {}
    for code, rs in by_product.items():
        rs.sort(key=lambda r: (0 if r["account_type"] else 1, -(r["confidence"] or 0)))
        best = rs[0]
        out[code] = (best["val"], best["class_code"], best["page"])
    return out


def _aum_map(conn):
    out = {}
    for r in conn.execute(
            "SELECT product_code, net_asset_latest, unit, page FROM fund_aum "
            "WHERE net_asset_latest IS NOT NULL"):
        out[r["product_code"]] = (r["net_asset_latest"], r["unit"], r["page"])
    return out


def rank_products(conditions, db_path=DEFAULT_DB_PATH, named_codes=None):
    """조건에 맞는 상품을 정렬한 (요약 텍스트, 근거 목록).

    named_codes: find_products()가 질문에서 이름으로 직접 지목한 상품
    코드 목록(2개 이상). 주어지면 전체 100개가 아니라 이 상품들 안에서만
    정렬한다 - "솔로몬 단기·중장기·장기 국공채 중 위험도가 가장 낮은
    상품은?"처럼 "여러 상품 중에서"가 카테고리가 아니라 사용자가 직접
    이름을 댄 상품들을 가리키는 경우다. 이 경우 asset_type 필터는 이미
    사용자가 상품을 특정했으므로 의미가 없어 건너뛴다."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        products = {r["product_code"]: dict(r) for r in conn.execute(
            "SELECT product_code, product_name, asset_type, risk_level FROM product_master")}

        pool = set(named_codes) & set(products) if named_codes else set(products)
        excluded_notes = []

        if named_codes is None and conditions["asset_types"] is not None:
            allowed = set(conditions["asset_types"])
            pool = {c for c in pool if products[c]["asset_type"] in allowed}
            if not allowed:
                # 매칭된 분류 키워드가 있으나 이 코퍼스엔 해당 asset_type이 없다(TDF 등).
                pool = set()

        if conditions["risk_filter"] is not None:
            n, op = conditions["risk_filter"]
            fn = _OPS[op]
            pool = {c for c in pool
                    if products[c]["risk_level"] is not None and fn(products[c]["risk_level"], n)}

        fee_map = None
        if conditions["fee_filter"] is not None or conditions["sort_metric"] == "fee":
            fee_map = _fee_map(conn)
        if conditions["fee_filter"] is not None:
            n, op = conditions["fee_filter"]
            fn = _OPS[op]
            before = len(pool)
            pool = {c for c in pool if c in fee_map and fn(fee_map[c][0], n)}
            missing = before - len(pool) if before else 0

        return_map = None
        if conditions["sort_metric"] == "return":
            return_map = _return_map(conn, conditions["return_col"])
            before = len(pool)
            have = pool & set(return_map)
            if before - len(have) > 0:
                excluded_notes.append(
                    f"{before - len(have)}개 상품은 해당 기간 수익률 자료가 없어 제외")
            pool = have

        if conditions["sort_metric"] == "fee":
            before = len(pool)
            have = pool & set(fee_map)
            if before - len(have) > 0:
                excluded_notes.append(
                    f"{before - len(have)}개 상품은 일반 고객용 클래스의 총보수 자료가 없어 제외")
            pool = have

        aum_map = None
        if conditions["sort_metric"] == "aum":
            aum_map = _aum_map(conn)
            before = len(pool)
            have = pool & set(aum_map)
            if before - len(have) > 0:
                excluded_notes.append(f"{before - len(have)}개 상품은 설정액 자료가 없어 제외")
            pool = have

        codes = list(pool)
        metric = conditions["sort_metric"]
        reverse = conditions["sort_direction"] == "desc"
        if metric == "return":
            codes.sort(key=lambda c: return_map[c][0], reverse=reverse)
        elif metric == "fee":
            codes.sort(key=lambda c: fee_map[c][0], reverse=reverse)
        elif metric == "aum":
            codes.sort(key=lambda c: aum_map[c][0], reverse=reverse)
        elif metric == "risk":
            codes.sort(key=lambda c: products[c]["risk_level"], reverse=reverse)
        else:
            codes.sort()

        total_matched = len(codes)
        codes = codes[: conditions["limit"]]

        lines = []
        cond_bits = []
        if named_codes:
            cond_bits.append(f"질문에서 지목한 상품 {len(set(named_codes) & set(products))}개 중에서")
        elif conditions["asset_types"] is not None:
            cond_bits.append("분류: " + ("/".join(conditions["asset_types"]) or "해당 분류 상품 없음"))
        if conditions["risk_filter"] is not None:
            n, op = conditions["risk_filter"]
            cond_bits.append(f"위험등급 {n} {op}")
        if conditions["fee_filter"] is not None:
            n, op = conditions["fee_filter"]
            cond_bits.append(f"총보수 {n}% {op}")
        metric_label = {"fee": "총보수",
                        "return": RETURN_COL_LABELS[conditions["return_col"]],
                        "aum": "설정액", "risk": "위험등급", None: None}[metric]
        if metric_label:
            cond_bits.append(f"정렬: {metric_label} {'낮은' if not reverse else '높은'} 순")
        lines.append("■ 조건: " + (", ".join(cond_bits) if cond_bits else "(없음)"))
        lines.append(f"  조건에 맞는 상품 {total_matched}개 중 {len(codes)}개 표시")
        if metric == "risk" or conditions["risk_filter"] is not None:
            # LLM이 이 근거만 보고 "6등급이 제일 위험하다"처럼 등급 방향을
            # 거꾸로 답하지 않도록, 방향을 매번 근거에 직접 적는다 - LLM이
            # 이 도메인 상식을 스스로 알고 있다고 기대하지 않는다.
            lines.append("  (참고: 위험등급은 숫자가 작을수록 위험이 크고 "
                         "1등급이 가장 위험합니다. 숫자가 큰 등급일수록 "
                         "안전한 편입니다)")

        evidence = []
        if not codes:
            lines.append("  조건에 맞는 상품을 찾지 못했습니다.")
        for i, code in enumerate(codes, 1):
            p = products[code]
            bits = [f"  {i}. {p['product_name'] or code} ({code})"]
            if p.get("asset_type"):
                bits.append(f"분류 {p['asset_type']}")
            if p.get("risk_level") is not None:
                bits.append(f"위험등급 {p['risk_level']}등급")
            if fee_map is not None and code in fee_map:
                fee, cc, page = fee_map[code]
                bits.append(f"총보수 {fee}%({cc})")
                evidence.append({"table": "class_fees", "product_code": code,
                                  "class_code": cc, "page": page})
            if return_map is not None and code in return_map:
                val, cc, page = return_map[code]
                label = RETURN_COL_LABELS[conditions["return_col"]]
                bits.append(f"{label} {val}%({cc})")
                evidence.append({"table": "class_returns", "product_code": code,
                                  "class_code": cc, "page": page})
            if aum_map is not None and code in aum_map:
                v, unit, page = aum_map[code]
                bits.append(f"설정액 {v}{unit or ''}")
                evidence.append({"table": "fund_aum", "product_code": code, "page": page})
            lines.append(" | ".join(bits))

        for note in excluded_notes:
            lines.append(f"  ※ {note}")

        return "\n".join(lines), evidence
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", required=True)
    args = ap.parse_args()
    cond = detect(args.ask)
    if cond is None:
        print("랭킹/필터 질의로 인식되지 않음")
        return
    print(cond)
    summary, evidence = rank_products(cond)
    print(summary)
    print("\n근거:", evidence[:6], "...")


if __name__ == "__main__":
    main()
