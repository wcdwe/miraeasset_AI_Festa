"""
연금 Agent 과제 - 상품 비교 질의 처리 (토큰 절약용 구조화 조회)

"OO상품이랑 XX상품 총보수 비교해줘" 같은 질의는 이미 확실한 숫자가
product_master/class_fees/class_returns 표(structured_store.db)에 있는데,
이걸 semantic_search로 텍스트 청크를 여러 개 긁어와 LLM에 던지면 (1) 토큰을
많이 쓰고 (2) 정작 필요한 숫자가 청크 경계에 걸려 잘릴 수도 있다.

비교 대상 상품(코드 또는 이름)이 질의에 2개 이상 있으면, DB에서 필요한
필드만 직접 조회해서 짧은 텍스트로 반환한다. 클래스를 지정하지 않으면
그 상품에서 confidence가 가장 높은 클래스 1개만 대표로 보여준다(전체
클래스를 다 보여주면 오히려 길어져서 토큰 절약 취지에 안 맞음).

사용법(CLI, 수동 점검용):
    python scripts/compare_products.py --codes KR5127420034,KR5127420039
"""

import argparse
import re
import sqlite3

# 예전엔 router에서 상품코드 정규식을 가져왔는데, router가 벡터 검색
# (chromadb)을 끌어와서 벡터 스토어가 없는 환경에선 이 모듈까지 못 쓰게
# 된다. 구조화 DB 조회는 벡터 검색과 무관하므로 끊어 둔다.
from product_lookup import PRODUCT_CODE_RE  # noqa: E402
from build_product_facts_db import DEFAULT_DB_PATH
from product_facts import (  # noqa: E402
    _return_caveat, _is_known_source_error, KNOWN_SOURCE_ERROR_NOTE)

# 나란히 견줄 조건 줄 수. 조건마다 상품 수만큼 줄이 늘어난다.
MAX_COMPARE_CONDITIONS = 2

COMPARISON_KEYWORDS = ["비교", "차이", "어느", "어디가", "더 낮", "더 높", "vs", "대비"]


def extract_product_codes(query: str) -> list:
    return list(dict.fromkeys(PRODUCT_CODE_RE.findall(query)))


def is_comparison_query(query: str, product_codes: list) -> bool:
    if len(product_codes) >= 2:
        return True
    return len(product_codes) >= 1 and any(k in query for k in COMPARISON_KEYWORDS)


def _fetch_product(conn, code):
    row = conn.execute(
        "SELECT product_code, product_name, asset_type, risk_level FROM product_master WHERE product_code = ?",
        (code,),
    ).fetchone()
    return row


def _fetch_best_class_fee(conn, code, class_code=None):
    sql = "SELECT class_code, total_fee, distribution_fee, peer_avg_fee, total_fee_and_cost, confidence FROM class_fees WHERE product_code = ?"
    params = [code]
    if class_code:
        sql += " AND class_code = ?"
        params.append(class_code)
    sql += " ORDER BY confidence DESC, total_fee ASC LIMIT 1"
    return conn.execute(sql, params).fetchone()


# 수익률 값이 하나라도 있는 행만 쓴다. 예전엔 confidence만 보고 골라서
# 값이 전부 비어 있는 행(클래스만 있고 수익률은 안 실린 행)이 뽑혔고,
# 답변에 "최근1년 수익률(C1클래스) None%"가 그대로 나갔다.
_RETURN_HAS_VALUE = ("(return_1y IS NOT NULL OR return_3y IS NOT NULL "
                     "OR return_since_inception IS NOT NULL)")


def _fetch_best_class_return(conn, code, class_code=None):
    sql = (
        "SELECT class_code, return_1y, return_3y, return_since_inception, confidence "
        "FROM class_returns WHERE product_code = ? AND row_kind = 'class_return' "
        "AND " + _RETURN_HAS_VALUE
    )
    params = [code]
    if class_code:
        sql += " AND class_code = ?"
        params.append(class_code)
    sql += " ORDER BY confidence DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _classes_with_meaning(conn, code):
    """이 상품의 클래스 -> (뜻, 총보수, 수익률행). 뜻을 모르는 건 뺀다.

    코드가 같으면 같은 클래스라고 보면 안 된다. 미래에셋의 C-P는
    개인연금이고 교보악사의 CP는 퇴직연금이며, 한국투자는 그냥 C가
    개인연금이다. 코드로 맞추면 개인연금과 퇴직연금을 나란히 놓고
    "같은 클래스끼리 비교했다"고 표시하게 된다."""
    out = {}
    for m in conn.execute(
            "SELECT * FROM class_meaning WHERE product_code = ?", (code,)):
        if not m["retail"]:
            continue  # 기관/고액/랩 전용은 일반 고객이 살 수 없다
        out[m["class_code"]] = {
            "condition": (m["account_type"], m["channel"]),
            "description": m["description"],
        }
    for f in conn.execute(
            "SELECT class_code, total_fee FROM class_fees "
            "WHERE product_code = ? AND total_fee IS NOT NULL", (code,)):
        if f["class_code"] in out:
            out[f["class_code"]]["total_fee"] = f["total_fee"]
    for r in conn.execute(
            "SELECT * FROM class_returns WHERE product_code = ? "
            "AND row_kind = 'class_return' AND " + _RETURN_HAS_VALUE, (code,)):
        if r["class_code"] in out:
            out[r["class_code"]]["ret"] = dict(r)
    return {k: v for k, v in out.items() if "total_fee" in v}


def _fee_range(classes):
    fees = [v["total_fee"] for v in classes.values()]
    return (min(fees), max(fees)) if fees else (None, None)


def _condition_name(cond):
    account, channel = cond
    account = {"개인연금": "연금저축", "퇴직연금": "퇴직연금(DC/IRP)"}.get(
        account, account)
    channel = {"오프라인": "창구", "온라인": "온라인",
               "온라인슈퍼": "온라인슈퍼", "직판": "운용사 직판",
               "온라인직접판매": "운용사 직판(온라인)",
               "디폴트옵션": "디폴트옵션(사전지정운용)"}.get(channel, channel)
    return " · ".join(p for p in (account, channel) if p)


def _common_conditions(per_product):
    """모든 상품이 함께 가진 가입 조건. 없으면 빈 리스트."""
    sets = [{v["condition"] for v in cls.values()} for cls in per_product.values()]
    if not sets:
        return []
    common = set.intersection(*sets)
    # 연금 계좌로 살 수 있는 조건을 앞에 둔다(연금 상품을 견주는 자리이므로).
    return sorted(common, key=lambda c: (c[0] is None, str(c[0]), str(c[1])))


def _pick(classes, cond):
    """그 조건에 해당하는 클래스 중 총보수가 가장 낮은 것."""
    hits = [(cc, v) for cc, v in classes.items() if v["condition"] == cond]
    return min(hits, key=lambda h: h[1]["total_fee"]) if hits else None


def compare_products(product_codes, db_path=DEFAULT_DB_PATH, fields=None):
    """product_codes: ['KR...', 'KR...'] (2개 이상)
    fields: {"fee", "return", "risk"} 중 관심 있는 것만 (None이면 전부)
    반환: (요약 텍스트, 근거 목록)

    상품마다 총보수 범위를 먼저 내고, 모든 상품이 함께 가진 가입 조건이
    있으면 그 조건으로 나란히 견준다. 공통 조건이 없으면 없다고 말한다 -
    조건이 다른 숫자를 나란히 놓으면 상품 차이가 아니라 가입 방법 차이를
    보여주게 된다.
    """
    fields = fields or {"fee", "return", "risk"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    lines, evidence = [], []
    per_product, names = {}, {}
    for code in product_codes:
        product = _fetch_product(conn, code)
        if not product:
            lines.append(f"[{code}] 상품 정보를 찾지 못함")
            continue
        names[code] = product["product_name"] or code
        classes = _classes_with_meaning(conn, code)
        per_product[code] = classes

        parts = [f"[{code}] {names[code]}"]
        if "risk" in fields and product["risk_level"] is not None:
            parts.append(f"위험등급 {product['risk_level']}등급")
        if product["asset_type"]:
            parts.append(f"({product['asset_type']})")
        lo, hi = _fee_range(classes)
        if lo is None:
            parts.append("총보수 정보를 찾지 못함")
        elif lo == hi:
            parts.append(f"총보수 {lo}%")
        else:
            parts.append(f"총보수 {lo}% ~ {hi}%")
        lines.append(" | ".join(parts))
        evidence.append({"product_code": code, "type": "class_fees"})

    usable = {c: v for c, v in per_product.items() if v}
    if len(usable) >= 2:
        common = _common_conditions(usable)
        if not common:
            lines.append("※ 이 상품들이 함께 가진 가입 조건이 없어 같은 "
                         "기준으로 나란히 견줄 수 없습니다. 위 범위로 비교해 주세요.")
        for cond in common[:MAX_COMPARE_CONDITIONS]:
            lines.append(f"— {_condition_name(cond)} 기준")
            for code, classes in usable.items():
                got = _pick(classes, cond)
                if not got:
                    continue
                cc, v = got
                bits = [f"    {names[code]}: 총보수 {v['total_fee']}%"]
                r = v.get("ret")
                if "return" in fields and r:
                    got_r = [(lbl, r[col]) for lbl, col in
                             (("최근1년", "return_1y"), ("최근3년", "return_3y"),
                              ("설정후", "return_since_inception"))
                             if r[col] is not None]
                    if got_r:
                        bits.append("수익률 " + ", ".join(
                            f"{lbl} {x}%{_return_caveat(x)}" for lbl, x in got_r))
                        if _is_known_source_error(code, cc):
                            bits.append(KNOWN_SOURCE_ERROR_NOTE)
                lines.append(" | ".join(bits))
                evidence.append({"product_code": code, "type": "class_fees",
                                 "class_code": cc})

    conn.close()
    return "\n".join(lines), evidence


def main():
    parser = argparse.ArgumentParser(description="상품 비교 조회 수동 점검 CLI")
    parser.add_argument("--codes", help="쉼표로 구분한 상품코드 (예: KR..,KR..)")
    parser.add_argument("--query", help="자연어 질의에서 상품코드를 직접 추출")
    args = parser.parse_args()

    if args.query:
        codes = extract_product_codes(args.query)
        print(f"추출된 상품코드: {codes}")
    else:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    summary, evidence = compare_products(codes)
    print(summary)
    print("\n근거:", evidence)


if __name__ == "__main__":
    main()
