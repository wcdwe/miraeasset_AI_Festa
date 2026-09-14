"""질문 글자에서 상품(과 클래스)을 찾아낸다.

지금까지 상품 인식은 질문 안에 상품코드(KR5127420034)가 문자 그대로
들어 있을 때만 됐다(router.PRODUCT_CODE_RE). 그런데 실제 질문은
"미래에셋장기성장포커스 총보수 얼마야?"처럼 상품 이름으로 오기 때문에,
그 경로로는 구조화 DB(class_fees/class_returns)에 아예 닿지 못하고
텍스트 청크 검색으로 빠진다 - 정확한 숫자를 갖고 있으면서도 못 쓰는 셈.

상품명은 "미래에셋장기성장포커스증권자투자신탁1호(주식)"처럼 뒤에
상품 종류를 나타내는 상투적인 말이 길게 붙는다. 사람은 그 앞의
고유한 부분만 부르므로, 상투어를 걷어낸 "핵심 이름"으로 맞춘다.
"""

import difflib
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")

PRODUCT_CODE_RE = re.compile(r"KR[0-9A-Z]{10}", re.IGNORECASE)

# 상품 종류를 나타내는 상투어. 이 말들만으로 이뤄진 일치는 상품을
# 가리키지 못한다("증권투자신탁"은 100개 상품 대부분에 들어 있다).
# 뒤쪽 줄은 제도 질문에 흔히 나오는 말들이다 - "퇴직연금 중도인출은
# 어떤 경우에 가능한가요?"가 상품명에 '퇴직연금'이 든 펀드로 매칭돼
# 제도 질문이 상품 비교로 새는 걸 막는다.
GENERIC_WORDS = (
    "증권", "자투자신탁", "모투자신탁", "투자신탁", "투자회사", "집합투자기구",
    "주식형", "채권형", "혼합형", "주식", "채권", "혼합", "재간접", "파생형",
    "인덱스", "전환형", "단위형", "추가형", "개방형", "폐쇄형", "종류형",
    "국공채", "단기채", "제", "호",
    "퇴직연금", "개인연금", "연금저축", "연금", "펀드", "적립금",
    "확정급여", "확정기여", "금융투자협회", "펀드코드", "명칭",
)

# 짧은 조각을 이만큼 넘는 상품이 함께 갖고 있으면 상품을 가리키지 못한다.
# "미래에셋"(수십 개), "퇴직연금"(4개)처럼 상투어 목록에 없어도 여럿이
# 나눠 갖는 짧은 말은 식별력이 없다. 반대로 "솔로몬"(4개)이나 "한국투자
# 골드플랜연금"(4개)은 형제 펀드끼리만 나눠 갖는 진짜 이름이라 살려야
# 해서, 경계를 그 위에 둔다.
MAX_SHARED_PRODUCTS = 5
# 다만 긴 조각은 여럿이 공유해도 버리지 않는다. "한국투자골드플랜연금"은
# 형제 펀드 4개가 나눠 갖지만 사용자가 실제로 부른 상품 이름이다 - 버리면
# 아무것도 못 찾고, 남겨 두면 아래 echo 점수로 (채권)/(주식)을 가릴 수 있다.
SHARED_PIECE_MAX_LEN = 8
# 겹친 글자가 이만큼은 돼야 상품을 짚었다고 본다.
MIN_MATCH_SCORE = 4
# 이미 다른 후보가 설명한 자리 말고 새로 설명하는 글자가 이만큼은 있어야
# "상품을 하나 더 물은 것"으로 친다.
MIN_NEW_CHARS = 2

# "A클래스" / "종류C-e" / "클래스 C-P" 처럼 클래스를 지목하는 표현
CLASS_IN_QUERY_RE = re.compile(
    r"(?:종류|클래스)\s*([A-Za-z][A-Za-z0-9\-]{0,7})|([A-Za-z][A-Za-z0-9\-]{0,7})\s*클래스")


def _norm(s):
    return re.sub(r"[\s.,·ㆍ\-_()\[\]:]", "", s or "")


def _is_generic(text):
    """일치한 조각이 상투어만으로 이뤄졌는지. 그렇다면 상품을 못 가린다."""
    t = text
    for w in sorted(GENERIC_WORDS, key=len, reverse=True):
        t = t.replace(w, "")
    return len(t) < 2


def load_products(db_path=DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT product_code, product_name FROM product_master"
        ).fetchall()
    finally:
        conn.close()
    return [(code, name, _norm(name)) for code, name in rows if name]


_CACHE = None
_BACKGROUND_CACHE = {}


def _common_pieces(q, nname, min_size=2):
    """질문과 상품명에 함께 나오는 조각들을 순서에 매이지 않고 모은다.

    difflib이 주는 matching_blocks는 순서가 어긋나면 못 쓴다. 사람은
    이름의 말 순서를 바꿔 부르기 때문에("솔로몬 국공채 단기" <-> 원래
    이름 "솔로몬단기국공채") 그 제약이 그대로 실패가 된다. 긴 조각부터
    떼어 내고 남은 자리에서 또 찾는 식으로, 순서와 상관없이 겹치는 만큼
    모은다."""
    sm = difflib.SequenceMatcher(None, q, nname, autojunk=False)
    qsegs, nsegs = [(0, len(q))], [(0, len(nname))]
    pieces = []
    while qsegs and nsegs:
        best = None
        for qi, (qa, qb) in enumerate(qsegs):
            for ni, (na, nb) in enumerate(nsegs):
                m = sm.find_longest_match(qa, qb, na, nb)
                if m.size >= min_size and (best is None or m.size > best[0].size):
                    best = (m, qi, ni)
        if best is None:
            break
        m, qi, ni = best
        pieces.append((m.a, m.a + m.size, q[m.a:m.a + m.size]))
        qa, qb = qsegs.pop(qi)
        na, nb = nsegs.pop(ni)
        for s, e in ((qa, m.a), (m.a + m.size, qb)):
            if e - s >= min_size:
                qsegs.append((s, e))
        for s, e in ((na, m.b), (m.b + m.size, nb)):
            if e - s >= min_size:
                nsegs.append((s, e))
    return pieces


def _piece_is_background(piece):
    """상품을 짚어 주지 못하는 조각인가.

    두 가지다. (1) 상품 종류를 나타내는 상투어("채권", "국공채"),
    (2) 너무 많은 상품이 나눠 갖는 말("미래에셋"). 순서에 매이지 않고
    조각을 모으다 보니 이런 말이 여기저기서 조금씩 붙어 점수가 오르는
    일이 생겼다 - "미래에셋차세대Fun인덱스 위험등급" 질문에 엉뚱한
    펀드가 '미래에셋'(4) + '등급'(2)으로 따라 올라왔다."""
    if _is_generic(piece):
        return True
    # 숫자만으로 된 조각은 상품을 못 짚는다. "1,000만원 투자하면"의
    # '100'이 'KRX100' 펀드에 걸려 엉뚱한 상품이 따라붙은 적이 있다.
    if piece.isdigit():
        return True
    if len(piece) >= SHARED_PIECE_MAX_LEN:
        return False  # 긴 말은 여럿이 나눠 가져도 진짜 이름이다
    n = _BACKGROUND_CACHE.get(piece)
    if n is None:
        n = sum(1 for _, _, nn in _CACHE if piece in nn)
        _BACKGROUND_CACHE[piece] = n
    # 경계값 자체(정확히 MAX_SHARED_PRODUCTS개)도 배경어로 본다. "포커스"
    # (마케팅용 상투어, 상품 고유명이 아님)를 정확히 5개 상품이 나눠
    # 갖는데, ">"였을 때는 "5 > 5"가 거짓이라 식별 조각으로 잘못
    # 살아남았다(실측: "미래에셋장기성장포커스의 연금저축용 클래스는
    # 뭐야?"가 "포커스" 한 조각으로 무관한 "미래에셋고배당포커스연금
    # 저축..." 펀드까지 같이 찾아서 단일 상품 질문이 엉뚱하게 비교로
    # 샜다). "솔로몬"/"한국투자골드플랜연금"처럼 진짜 식별 조각은 4개
    # 상품만 나눠 가져 이 경계보다 낮으므로 그대로 살아남는다.
    return n >= MAX_SHARED_PRODUCTS


def _match_blocks(q, nname):
    """겹치는 조각들 -> (상품을 짚는 조각, 그러지 못하는 조각).

    뒤쪽도 버리지는 않는다. 형제 펀드를 가르는 데는 쓸모가 있어서
    (골드플랜 연금 (채권) vs (국공채)) 동점 처리에 쓴다."""
    solid, background = [], []
    for a, b, piece in _common_pieces(q, nname):
        (background if _piece_is_background(piece) else solid).append(
            (a, b, piece))
    solid.sort()
    background.sort()
    return solid, background


def _new_chars(blocks, covered):
    """이 후보가 질문에서 새로 설명하는 글자 수(이미 설명된 자리는 뺀다)."""
    n = 0
    for a, b, _piece in blocks:
        for i in range(a, b):
            if not any(sa <= i < sb for sa, sb in covered):
                n += 1
    return n


def find_products(question, db_path=DEFAULT_DB_PATH, limit=4):
    """질문에서 상품을 찾는다. 코드가 직접 적혀 있으면 그게 우선.

    처음엔 상품명에서 상투어("증권자투자신탁1호(주식)")를 지운 "핵심
    이름"이 질문에 통째로 들어 있는지로 맞췄는데, 상투어 목록에 있는
    "전환형"이 "목표전환형"의 일부라 잘려 나가는 등 단어를 깎는 방식
    자체가 취약했다(KCGI코리아목표전환형 -> KCGI코리아목표).

    다음엔 "가장 긴 공통 문자열" 하나로 점수를 매겼는데, 사람은 이름의
    말 순서를 바꿔 부른다. "솔로몬 국공채 단기"는 원래 이름
    "솔로몬단기국공채"와 통째로는 3글자('솔로몬')밖에 안 겹쳐서 아예
    못 찾았다. 그래서 지금은 겹치는 조각을 전부 모아 그 합으로 센다
    ('솔로몬' + '국공채' + '단기' = 8).

    후보를 추리는 규칙은 세 가지다.

    1. 상품을 못 짚는 조각은 점수로 세지 않는다 - 상품 종류를 나타내는
       상투어("채권")와, 너무 많은 상품이 나눠 갖는 말("미래에셋").
    2. 남은 점수가 모자라거나 3글자 넘는 조각이 하나도 없으면 버린다.
       2글자짜리가 우연히 몇 개 겹친 것으로는 상품을 짚었다고 못 한다.
    3. 질문에서 새로 설명하는 데가 없는 후보는 버린다. "미래에셋
       프리미엄크레딧알파" 질문에 "...크레딧초단기"가 같이 걸리지만,
       그 후보가 짚는 글자는 이미 1등이 다 짚은 자리라 상품을 하나 더
       물은 게 아니다. 반대로 "솔로몬 국공채 단기ㆍ중장기ㆍ장기"는
       후보마다 '단기'/'중장기'/'장기'라는 제 몫의 자리가 있으므로
       셋 다 남는다.

    돌려주는 것: [(product_code, product_name, 겹친 글자수)]"""
    global _CACHE
    if _CACHE is None:
        _CACHE = load_products(db_path)

    codes = list(dict.fromkeys(
        c.upper() for c in PRODUCT_CODE_RE.findall(question or "")))
    by_code = {c: n for c, n, _ in _CACHE}
    if codes:
        return [(c, by_code.get(c), 99) for c in codes if c in by_code][:limit]

    q = _norm(question)
    if len(q) < 3:
        return []

    cand = []
    for code, name, nname in _CACHE:
        blocks, background = _match_blocks(q, nname)
        if not blocks:
            continue
        score = sum(b - a for a, b, _ in blocks)
        # 3글자 이상 되는 "상품을 짚는" 조각이 하나는 있어야 한다. 2글자
        # 짜리나 배경어만 겹친 건 우연이다("미래에셋" + "등급").
        if max(b - a for a, b, _ in blocks) < 3:
            continue
        # 배경어도 겹친 자리로는 쳐 준다. 카탈로그 전체로 보면 흔한 말이라도
        # ("단기", "장기") 형제 펀드를 가르는 건 바로 그 말이다.
        all_blocks = sorted(blocks + background)
        echo = sum(b - a for a, b, _ in all_blocks)
        if echo < MIN_MATCH_SCORE:
            continue
        cand.append((score, echo, code, name, all_blocks))

    cand.sort(key=lambda c: (-c[0], -c[1], c[2]))
    out, covered = [], []
    norm_by_code = {c: nn for c, _n, nn in _CACHE}
    for score, _echo, code, name, blocks in cand:
        if _new_chars(blocks, covered) < MIN_NEW_CHARS:
            # 앞선 후보가 이미 짚은 자리만 짚었다. 다만 형제 펀드는 이름
            # 앞부분을 나눠 갖기 때문에("솔로몬 국공채" + 단기/중장기/장기)
            # 겹친 자리를 가리고 다시 맞춰 본다 - 질문의 다른 자리에 제 몫이
            # 있으면 그건 진짜로 하나 더 물은 것이다.
            masked = "".join(
                "\x00" if any(sa <= i < sb for sa, sb in covered) else ch
                for i, ch in enumerate(q))
            retry = list(_common_pieces(masked, norm_by_code[code]))
            if sum(b - a for a, b, _ in retry) < MIN_NEW_CHARS:
                continue
            blocks = sorted(retry)
        covered.extend((a, b) for a, b, _ in blocks)
        out.append((blocks[0][0], code, name, score))
        if len(out) >= limit:
            break
    # 사용자가 말한 순서대로 돌려준다. 점수 순으로 주면 "A랑 B 비교해줘"의
    # 답이 B부터 나와서 질문과 어긋나 읽힌다.
    out.sort()
    return [(code, name, size) for _a, code, name, size in out]


def find_class_code(question):
    """질문이 특정 클래스를 지목하면 그 코드. 없으면 None."""
    m = CLASS_IN_QUERY_RE.search(question or "")
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


if __name__ == "__main__":
    import sys
    for q in sys.argv[1:]:
        print(f"{q!r}")
        for code, name, n in find_products(q):
            print(f"   {code}  {name}  (맞은 글자 {n})")
        print(f"   클래스: {find_class_code(q)}")
