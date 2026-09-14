"""이 펀드가 무엇에 얼마나 투자하고 있는지 뽑는다 ("다. 집합투자기구의 자산구성 현황").

    "이 펀드 뭐에 투자해요?"
    "주식 비중이 얼마나 돼요?"

지금은 못 답한다. 상품명·보수·수익률은 다 있는데 정작 "이게 뭘 담고
있는 펀드인가"가 없다. 위험등급만으로는 주식형인지 채권형인지도 흐릿하다.

문서에는 이렇게 실려 있다.

    다. 집합투자기구의 자산구성 현황 (기준일 : 2025년 05월 02일, 단위: 백만원, %)
    통화별 구분 | 증권                          | 파생상품  | ... | 자산총액
               | 주식   채권  어음  집합투자증권 | 장내 장외 |
    KRW(한국)  | 5,715  -     -     -           | -    -    | ... | 5,856
               | 97.59  -     -     -           | -    -    | ... | 100
    합계       | 5,715  -     -     -           | -    -    | ... | 5,856
               | 97.59  -     -     -           | -    -    | ... | 100

금액 줄 바로 아래가 비율 줄이다. 비율을 괄호로 싸는 문서도 있다((66.40)).

제목으로 찾지 않는다
--------------------
"자산구성"이라는 글자로 표를 찾으면 42개 문서밖에 안 걸린다. 제목이
표 밖에 있거나 다른 표에 들어가 있기 때문이다. 표의 모양으로 찾으면
64개다 - 이 표는 "통화별"과 "자산총액"과 "주식/채권"이 한 표 안에
같이 있는 유일한 표라 모양만으로 확실히 특정된다. 제목 대신 모양을
보는 건 class_returns에서 이미 같은 이유로 택한 방식이다.

표로 안 잡히는 문서
------------------
글자가 한 자씩 떨어져 나오는 문서가 있다("통 화 별 구 분" -
KR5111450067 실측). 그런 페이지는 pdfplumber 기본 설정으로 표가 아예
안 잡혀서(0개), 가로줄을 글자 줄에서 잡는 설정으로 다시 읽는다.

실행:
    python3 scripts/extract_asset_mix.py
    python3 scripts/extract_asset_mix.py --check
"""

import argparse
import glob
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "asset_mix.json")
DATA_DIR = os.path.join(REPO_ROOT, "data", "products")

# 이 표를 다른 표와 가르는 낱말들. 다 있어야 한다. "자산총액"은 한
# 글자로 두지 않고 "자산"/"총액"으로 나눠 둔다 - 머리글이 물리적으로
# 세 줄에 걸쳐 있는 문서가 있어서(KR5116501001 42쪽 실측: "자산"은
# "단기대출및" 칸의 줄에, "총액"은 그보다 두 줄 아래인 "기타" 칸의
# 줄에 있다) 표를 줄 순서로 이어붙이면 그 사이에 다른 칸 이름들이
# 끼어들어 "자산총액"이 붙어 있지 않다. "통화"만으로도 이 표를 다른
# 표와 가르기엔 충분히 특이해서(다른 표에는 안 쓰는 말이다), 나머지
# 네 낱말이 어디에 있든 다 있기만 하면 이 표로 본다.
SHAPE_WORDS = ("통화", "자산", "총액", "주식", "채권")

# 큰 묶음 이름(윗줄)과 그 아래 세부 이름(아랫줄)
GROUP_WORDS = ("증권", "파생상품", "부동산", "특별자산",
               "단기대출및예금", "단기대출", "기타", "자산총액")
SUB_WORDS = ("주식", "채권", "어음", "집합투자증권", "장내", "장외",
             "실물자산", "기타")

# 이 표는 금융투자협회 표준 서식이라 값 열의 순서가 문서마다 같다.
# 머리글은 세 줄에 걸쳐 쪼개지고 글자가 한 자씩 떨어져 나오기도 해서
# ("집 합 투 자 증" + "권"이 서로 다른 줄, 다른 칸에 있다 -
# KR5111450067 실측) 이름으로 열을 맞추면 제일 큰 자산을 통째로
# 놓친다. 열 개수가 맞으면 순서로 붙이고, 머리글은 확인용으로만 쓴다.
CANONICAL_COLUMNS = (
    "주식", "채권", "어음", "집합투자증권",
    "파생상품(장내)", "파생상품(장외)", "부동산",
    "특별자산(실물자산)", "특별자산(기타)",
    "단기대출및예금", "기타", "자산총액",
)
# "파생결합증권" 칸을 하나 더 두는 서식도 있다(KR5114420016 실측).
CANONICAL_COLUMNS_13 = (
    "주식", "채권", "어음", "집합투자증권", "파생결합증권",
    "파생상품(장내)", "파생상품(장외)", "부동산",
    "특별자산(실물자산)", "특별자산(기타)",
    "단기대출및예금", "기타", "자산총액",
)
CANONICAL_BY_LEN = {len(CANONICAL_COLUMNS): CANONICAL_COLUMNS,
                    len(CANONICAL_COLUMNS_13): CANONICAL_COLUMNS_13}

RE_NUM = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
RE_PCT_ROW_HINT = re.compile(r"^\(?\d+(?:\.\d+)?\)?$")
# "(기준일 : 2025년 05월 02일" / "[2025.05.02 현재" 둘 다 쓴다.
RE_AS_OF = re.compile(
    r"(20\d{2})\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})")
# "단위: 백만원" 같은 캡션에서 금액 단위를 뽑는다. 문서마다 단위이
# 다르다(실측: KR5127450117은 억원, KR5129420025는 백만원) - 단위 없이
# amount/total_amount만 내보내면 두 상품을 그대로 비교했을 때 100배
# 차이가 나는 걸 놓친다.
#
# "단위" 바로 뒤에 금액단위가 오지 않는 문서가 많다(실측: "단위 : %,
# 백만, 2024.12.31 기준" - 퍼센트가 먼저 나온다). 그래서 "단위" 뒤
# 일정 구간(_UNIT_WINDOW)을 통째로 보고 그 안에서 찾는다. 또 "원"
# 없이 "백만"/"억"만 쓰는 문서도 있다(실측: "단위 : %, 백만," -
# "백만원"이 아니라 "백만"만). 이런 짧은 말은 뒤에 다른 한글이 곧장
# 이어지면 안 받는다 - 안 그러면 캡션과 무관한 데서 "억"/"원" 한
# 글자가 우연히 걸릴 위험이 있다(쉼표/괄호/퍼센트/숫자/문자열 끝처럼
# 캡션에서 실제로 단위 뒤에 오는 것들만 받는다).
_UNIT_WINDOW = 30
_UNIT_PATTERNS = [
    (re.compile(r"백만원"), "백만원"),
    (re.compile(r"억원"), "억원"),
    (re.compile(r"천원"), "천원"),
    (re.compile(r"백만(?=[,\)%0-9]|$)"), "백만원"),
    (re.compile(r"억(?=[,\)%0-9]|$)"), "억원"),
    (re.compile(r"(?<![가-힣])원(?=[,\)%0-9]|$)"), "원"),
]


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def _num(v):
    """숫자 칸을 float으로.

    비율을 괄호로 싸는 문서가 많은데((66.40)) 그 괄호는 음수 표시가
    아니라 그냥 감싼 것이다. 음수는 괄호 밖에 따로 붙는다 - "-(17.58)"
    (KR5127420034 실측). 그래서 괄호는 어디 있든 지우고 본다. 처음엔
    양끝만 떼다가 "-(17.58"이 남아 그 칸을 통째로 잃었고, 자산 비율
    합이 117%가 됐다."""
    t = _squash(v).replace("(", "").replace(")", "").replace("△", "-")
    if not t or t == "-":
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _is_shape(rows):
    # 머리글이 아무리 여러 줄로 쪼개져도(KR555202013M 실측: 다섯 줄)
    # 첫머리 몇 줄 안에는 다 들어 있다. 표 전체를 다 보면 상관없는
    # 큰 표(투자한도·투자제한 설명 등)가 "자산"/"총액"/"주식"/"채권"
    # 같은 흔한 낱말을 본문 어딘가에서 우연히 다 담고 있어 잘못 걸린다
    # (KR5153451009 35쪽 실측 - 69행짜리 투자대상 설명표가 이 검사를
    # 통과했다). 앞 8줄만 보면 진짜 표는 놓치지 않으면서 이런 오탐은
    # 막는다.
    flat = _squash(" ".join((c or "") for r in rows[:8] for c in r))
    return all(w in flat for w in SHAPE_WORDS)


def _header_labels(rows):
    """머리글 두 줄을 겹쳐 열마다의 자산 이름을 만든다.

    윗줄은 큰 묶음("증권", "파생상품"), 아랫줄은 세부("주식", "장내")다.
    묶음 칸은 여러 열에 걸쳐 있고 그 열들엔 빈 칸으로 나오므로, 나온
    묶음 이름을 오른쪽으로 끌고 간다. 돌려주는 것은 (열 -> 이름, 머리글
    마지막 줄 번호)."""
    gi = si = -1
    for i, row in enumerate(rows[:6]):
        flat = _squash(" ".join((c or "") for c in row))
        if gi < 0 and "통화" in flat and "자산총액" in flat:
            gi = i
        elif gi >= 0 and si < 0 and any(w in flat for w in ("주식", "장내")):
            si = i
            break
    if gi < 0:
        return {}, -1

    groups, cur = {}, None
    for j, cell in enumerate(rows[gi]):
        name = _squash(cell)
        if name in GROUP_WORDS or name.startswith("단기대출"):
            cur = name
        groups[j] = cur

    labels = {}
    subs = rows[si] if si >= 0 else []
    for j in range(max(len(rows[gi]), len(subs))):
        sub = _squash(subs[j]) if j < len(subs) else ""
        group = groups.get(j)
        if sub and sub not in SUB_WORDS:
            sub = ""
        if not sub and not group:
            continue
        if group in ("파생상품", "특별자산") and sub:
            labels[j] = f"{group}({sub})"
        elif sub:
            labels[j] = sub
        elif group and group != "통화별구분":
            labels[j] = group
    return labels, max(gi, si)


def _cell_with_wrap(rows, i, j, span=2):
    """한 칸의 글자가 위아래 줄로 쪼개진 경우 이어 붙여 읽는다.

    KR518101012M 실측: 기타 비율 "(-25.47)"이 세 줄에 걸쳐 "(-" / 빈칸 /
    "25.47)"로 나뉘어 있다. 그대로 두면 그 칸만 빠져 자산 비율 합이
    125%가 된다.

    값이 제대로 읽히는 칸에는 쓰지 않는다 - 데이터 줄이 빈 줄 없이
    붙어 있는 표에서는 위아래를 이어 붙이면 다른 자산의 값이 섞인다."""
    parts = []
    for r in range(max(0, i - span), min(len(rows), i + span + 1)):
        if j < len(rows[r]):
            parts.append(_squash(rows[r][j]))
    return _num("".join(parts))


def _numeric_rows(rows, start):
    """값이 두 칸 이상 든 줄만 순서대로 (줄번호, 줄)."""
    out = []
    for i in range(start, len(rows)):
        cols = [j for j, c in enumerate(rows[i]) if j and _num(c) is not None]
        if len(cols) >= 2:
            out.append((i, rows[i]))
    return out


def _amount_and_pct_rows(rows, start):
    """합계(또는 통화) 금액 줄과 그 아래 비율 줄을 고른다.

    두 줄이 딱 붙어 있지 않은 문서가 있다. 줄 앞머리가 다음 줄로
    넘어가면서 빈 줄이 끼기 때문이다(KR5111450067 실측: "대 한 민" /
    "국" / 빈 줄 / 비율 줄). 그래서 바로 다음 줄이 아니라 "값이 든
    다음 줄"을 짝으로 본다.

    비율 줄을 아예 안 싣고 금액만 적는 문서도 있다(KR5114420016 실측:
    KRW/USD/합계 세 줄에 금액만). 그럴 땐 비율 줄 없이 금액 줄만
    돌려준다 - 비율은 자산총액으로 나눠서 만든다.

    합계 줄이 따로 있으면 그쪽을 쓴다 - 통화가 여럿인 펀드는 통화별
    줄만 보면 전체 비중이 안 나온다."""
    numeric = _numeric_rows(rows, start)
    amounts = []          # 비율 줄이 없을 때 쓸 후보
    for k, (i, row) in enumerate(numeric):
        # 이름표 칸이 통화 이름 자리가 밀려 첫 칸이 아니라 둘째 칸에
        # 오는 문서가 있다(KR5153451009 실측 - "합계"가 col1에 있다).
        # 첫 두 칸을 다 본다.
        head = _squash("".join(
            "".join((rows[r][c] or "") for c in (0, 1) if c < len(rows[r]))
            if rows[r] else ""
            for r in range(i, numeric[k + 1][0]
                            if k + 1 < len(numeric) else i + 1)))
        # 이 줄 자신이 비율 줄이면 금액 후보가 아니다.
        last = next((v for v in (_num(c) for c in reversed(row))
                     if v is not None), None)
        if last is not None and abs(last - 100) <= 1.0:
            continue
        amounts.append((k, i, row, head))

    best = None
    for k, i, amt, head in amounts:
        pct = None
        if k + 1 < len(numeric):
            nxt = numeric[k + 1][1]
            # 어느 줄이 비율 줄인지는 맨 오른쪽 "자산총액" 칸이 말해
            # 준다 - 거기가 100이면 비율 줄이다. 처음엔 "100을 넘는
            # 값이 없으면 비율 줄"로 봤는데 그러면 레버리지를 쓰는
            # 펀드를 통째로 잃는다(KR5113420069 실측: 채권 101.32%,
            # 기타 -33.95%로 자산총액만 100이다).
            last_pct = next((v for v in (_num(c) for c in reversed(nxt))
                             if v is not None), None)
            # 맨 오른쪽(자산총액) 칸 자체가 통째로 빈 문서가 있다
            # (KR5156450026 실측) - 그러면 비율 줄의 "맨 오른쪽 값"도
            # 100이 아니라 그 앞 칸(기타 등)의 값이 되어 위 신호를 못
            # 쓴다. 비율 줄은 칸을 괄호로 감싸는 게 이 표의 관례이므로
            # (docstring 참고), 값 있는 칸 대부분이 괄호로 싸여 있으면
            # 그것도 비율 줄로 본다.
            wrapped = sum(1 for c in nxt if (c or "").strip().startswith("("))
            if (last_pct is not None and abs(last_pct - 100) <= 1.0) or wrapped >= 2:
                pct = nxt
        if "합계" in head or "합" in head:
            return amt, pct
        if best is None:
            best = (amt, pct)
    return best if best else (None, None)


def _as_of(rows, page_text=""):
    flat = _squash(" ".join((c or "") for r in rows for c in r)) + _squash(page_text)
    m = RE_AS_OF.search(flat)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _unit(rows, page_text=""):
    flat = _squash(" ".join((c or "") for r in rows for c in r)) + _squash(page_text)
    # "단위" 글자 자체가 없는 캡션도 있다(실측: "집합투자기구의
    # 자산구성 현황 (2025.03.31 기준, 억원)" - "단위:" 없이 괄호 안에
    # 기준일과 같이 바로 적는다). "단위" 근처에서 못 찾으면 "자산구성"
    # 캡션 근처로 한 번 더 찾는다 - 이 캡션은 _is_shape가 이미
    # 요구하므로 항상 있다.
    #
    # "자산구성" 글자가 한 문서 안에 두 번(표 캡션 + 파이차트 캡션)
    # 나오는 경우가 있는데, 파이차트 쪽은 퍼센트만 있고 단위가 없다
    # (실측: KR5153420022 - 진짜 캡션 "(단위 : %, 백만, ...)"은 이전
    # 쪽 끝에 있고, 표가 있는 이 쪽엔 단위 없는 파이차트 캡션만 있다).
    # find()로 첫 번째 자리만 보면 그 단위 없는 자리에서 멈춰 버려
    # 뒤에 이어붙은 이전 쪽 텍스트의 진짜 단위를 못 본다 - 모든 자리를
    # 훑어 하나라도 단위를 찾으면 그걸 쓴다.
    for anchor in ("단위", "자산구성"):
        start = 0
        while True:
            idx = flat.find(anchor, start)
            if idx == -1:
                break
            window = flat[idx: idx + _UNIT_WINDOW]
            for pat, label in _UNIT_PATTERNS:
                if pat.search(window):
                    return label
            start = idx + len(anchor)
    return None


# 잎 이름 -> 답변에 쓸 이름
LEAF_RENAME = {"장내": "파생상품(장내)", "장외": "파생상품(장외)",
               "실물자산": "특별자산(실물자산)"}
LEAF_WORDS = set(SUB_WORDS) | {"부동산", "단기대출및예금", "자산총액"}


def _leaf_sequence(rows, hdr_end):
    """머리글에서 잎 이름만 왼쪽부터 순서대로 뽑는다.

    빈 칸을 잔뜩 끼워 넣은 표가 있다(KR5131420025 실측: 28열인데 값은
    9열뿐이고 나머지는 빈 칸). 열 번호로 이름을 맞추면 묶음 이름("증권")이
    잎 자리로 새어 들어와 자산 이름이 "증권"으로 두 번 나온다. 이름도
    값과 같은 순서로 놓이므로 순서로 맞춘다.

    묶음 이름(증권/파생상품/특별자산)은 잎이 아니라서 뺀다 - 그 아래
    주식·채권·장내 같은 잎이 따로 있다.

    머리글이 세 줄인 문서도 있다(KR5131420007 실측: 1행 "증권", 2행
    "집합투자"(잘림), 3행 "증권" - "집합투자"와 "증권"이 서로 다른
    줄에 있어야 "집합투자증권"이 완성된다). _header_labels는 "주식"이나
    "장내"가 나오는 줄까지만 보고 멈추므로 이런 3행째를 놓친다. 그
    다음 줄에 숫자가 하나도 없으면(진짜 데이터 줄이 아니면) 머리글의
    이어지는 줄로 보고 마저 합친다.

    머리글이 다섯 줄까지 늘어나는 문서도 있다(KR555202013M 실측 -
    _header_labels가 gi 자체를 못 찾아 hdr_end=-1로 시작한다). 정해진
    줄 수만큼만 더 보면 이런 문서를 또 놓치니, 숫자 있는 줄(진짜 데이터)
    이 나올 때까지는 계속 이어붙인다 - 머리글이 아닌 글줄이 섞여도
    LEAF_WORDS로 걸러지니 손해가 없다."""
    end = hdr_end
    for i in range(hdr_end + 1, len(rows)):
        if any(_num(c) is not None for c in rows[i]):
            break
        end = i
    joined = {}
    for row in rows[:end + 1]:
        for j, c in enumerate(row):
            t = _squash(c)
            if t:
                joined[j] = joined.get(j, "") + t
    # "증권"이 묶음칸(여러 열에 걸쳐야 할 자리)인데 첫 열에만 찍혀 잎
    # 칸("주식")과 같은 열에 겹치는 표도 있다(KR555202013M 실측: "증권주식"
    # 으로 합쳐진다). 반면 "집합투자"+"증권"처럼 같은 잎 이름을 완성하는
    # 데 "증권"이 정말 필요한 경우도 있다(KR5131420007). 안 맞는 것부터
    # 접두어 "증권"/"파생상품"을 떼고 다시 맞춰본다 - 진짜 필요한 경우는
    # 이미 LEAF_WORDS와 맞아떨어져 여기까지 안 온다.
    for j, t in list(joined.items()):
        if t not in LEAF_WORDS and not t.startswith("단기대출"):
            for prefix in ("증권", "파생상품"):
                if t.startswith(prefix) and t[len(prefix):] in LEAF_WORDS:
                    joined[j] = t[len(prefix):]
                    break
    seq = []
    for j in sorted(joined):
        t = joined[j]
        if t in LEAF_WORDS or t.startswith("단기대출"):
            seq.append(LEAF_RENAME.get(t, t))
    return seq


def _column_names(amt_row, rows, hdr_end):
    """값이 든 열 -> 자산 이름.

    표준 서식대로 값 열이 12개(또는 13개)면 순서로 붙인다. 그게 아니면
    머리글의 잎 이름을 순서로 맞춘다. 둘 다 안 되면 포기한다 - 이름을
    잘못 붙이느니 이 상품을 비워 두는 편이 낫다."""
    value_cols = [j for j, c in enumerate(amt_row) if j and _num(c) is not None]
    canon = CANONICAL_BY_LEN.get(len(value_cols))
    if canon:
        return dict(zip(value_cols, canon))
    # 맨 끝 "자산총액" 칸 자체가 빈 채로 뽑히는 문서가 있다(KR5156450026
    # 실측: 표 안 다른 칸은 다 있는데 합계 칸만 통째로 비어, 값 열이
    # 표준(12개)보다 하나 적게 나온다). 개별 비중은 이미 100으로 맞아
    # 떨어지므로(parse_asset_table의 검산이 잡는다) 자산총액 이름표 없이
    # 나머지 칸만 순서로 붙인다 - parse_asset_table이 자산총액 금액을
    # 개별 금액 합으로 채운다.
    for base in (CANONICAL_COLUMNS, CANONICAL_COLUMNS_13):
        if len(value_cols) == len(base) - 1:
            return dict(zip(value_cols, base[:-1]))
    leaves = _leaf_sequence(rows, hdr_end)
    if len(leaves) == len(value_cols) and len(set(leaves)) == len(leaves):
        return dict(zip(value_cols, leaves))
    # 표준 12/13칸이 아닌(특별자산·부동산 칸이 아예 없는) 문서에서도
    # 자산총액 칸만 빌 수 있다(KR555202013M 실측 - 잎 이름은 9개인데
    # 값 열은 8개, 마지막 잎 이름이 "자산총액"이다). 위와 같은 이유로
    # 총액 이름표 없이 나머지만 순서로 붙인다.
    if (leaves and leaves[-1] == "자산총액"
            and len(leaves) - 1 == len(value_cols)
            and len(set(leaves[:-1])) == len(leaves) - 1):
        return dict(zip(value_cols, leaves[:-1]))
    # 마지막으로 머리글의 열 번호를 그대로 쓴다. 안 담은 자산을 "-"로
    # 비워 둔 문서는 값 열이 서너 개뿐이라 위 두 길에 안 걸린다.
    labels = {j: n for j, n in _header_labels(rows)[0].items() if j in value_cols}
    if labels and len(set(labels.values())) == len(labels):
        return labels
    return {}


def parse_asset_table(rows, page_text=""):
    """자산구성 표 하나 → {items, total_amount, as_of}. 아니면 None."""
    if not _is_shape(rows):
        return None
    # 머리글은 시작 줄을 잡는 데만 쓴다. 열 이름은 표준 서식의 순서로
    # 붙이므로 머리글을 못 찾아도 진행한다 - "통화별"과 "자산총액"이
    # 서로 다른 줄에 놓인 문서가 있어서(KR5120451001 실측) 머리글을
    # 요구하면 그런 문서를 통째로 잃는다. 잘못 읽는 건 아래 자산총액
    # 100% 검산이 막는다.
    _labels, hdr_end = _header_labels(rows)
    amt_row, pct_row = _amount_and_pct_rows(rows, max(hdr_end + 1, 0))
    pct_idx = next((i for i, r in enumerate(rows) if r is pct_row), -1)
    if amt_row is None:
        return None
    names = _column_names(amt_row, rows, max(hdr_end, 0))
    if not names:
        return None

    # 맨 오른쪽은 보통 자산총액이고 그 비율은 100이어야 한다. 아니면 열을
    # 잘못 짚은 것이니 이 표는 쓰지 않는다 - 답변에 "주식 97.6%"처럼
    # 바로 나가는 값이라 한 칸만 밀려도 그대로 틀린 답이 된다.
    #
    # 자산총액 칸 자체가 표에서 통째로 비어 이름표를 못 붙인 경우도
    # 있다(_column_names 실측 - 개별 자산 이름만 순서로 붙고 "자산총액"은
    # 없다). 이때는 검산을 건너뛰고 총액을 개별 금액의 합으로 만든다 -
    # 아래에서 개별 비중 합이 100인지 다시 검산하므로 안전하다.
    last = max(names)
    has_total_col = names[last] == "자산총액"
    total_amount = None
    if has_total_col:
        total_amount = _num(amt_row[last]) if last < len(amt_row) else None
        if pct_row is not None:
            total_pct = _num(pct_row[last]) if last < len(pct_row) else None
            if total_pct is None or abs(total_pct - 100) > 1.0:
                return None
        elif not total_amount:
            # 비율 줄이 없으면 자산총액으로 나눠 만들어야 하는데, 그 값이
            # 없으면 만들 수가 없다.
            return None
    elif pct_row is None:
        # 자산총액 칸도 없고 비율 줄도 없으면 비율을 만들 방법이 없다.
        return None

    out, derived = [], False
    for j, name in sorted(names.items()):
        if name == "자산총액":
            continue
        amount = _num(amt_row[j]) if j < len(amt_row) else None
        if pct_row is not None:
            pct = _num(pct_row[j]) if j < len(pct_row) else None
            if pct is None and amount:
                # 금액은 있는데 비율 칸만 비었다 - 글자가 위아래로
                # 쪼개진 경우다.
                pct = _cell_with_wrap(rows, pct_idx, j)
        elif amount is not None and total_amount:
            # 문서가 비율을 안 실은 경우. 자산총액으로 나눠 만든 값이라
            # 문서에 그대로 적힌 숫자가 아니다 - derived로 표시한다.
            pct = round(amount / total_amount * 100, 2)
            derived = True
        else:
            pct = None
        if pct is None and amount is None:
            continue
        # 안 담고 있는 자산은 답변에 낼 필요가 없다.
        if (pct or 0) == 0 and (amount or 0) == 0:
            continue
        out.append({"asset": name, "amount": amount, "pct": pct})
    if not out:
        return None
    # 자산별 비중은 서로 더하면 100이어야 한다. 안 맞으면 칸을 잘못
    # 읽었거나 하나를 빠뜨린 것이다 - 답변에 "채권 112.7%"처럼 그대로
    # 나가는 값이라, 맞출 수 없으면 이 상품은 비워 둔다.
    got = sum(i["pct"] for i in out if i["pct"] is not None)
    if abs(got - 100) > 1.0:
        return None
    if not has_total_col:
        # 자산총액 칸이 없던 경우, 위 검산을 통과했으니(비중 합이 100)
        # 개별 금액을 더해 총액을 만든다.
        amounts = [i["amount"] for i in out if i["amount"] is not None]
        total_amount = round(sum(amounts), 2) if amounts else None
    return {"items": out, "total_amount": total_amount,
            "pct_derived": derived,
            "unit": _unit(rows, page_text),
            "as_of": _as_of(rows, page_text)}


# "통화별" 칸 자체를 안 쓰고 파이차트 옆에 "금액"/"비중" 두 줄만 싣는
# 문서가 있다(KR5172450019 실측: "주식 집합투자증권 단기대출 및 예금
# 기타 자산 총액" / "금액 47,142 24 227 1,013 48,406" / "비중 97.39
# 0.05 0.47 2.09 100.00"). "통화별"이 아예 없어 _is_shape를 못 지나고,
# 표로도 안 잡혀(그림 옆 캡션 글자라 pdfplumber가 표로 안 본다) 표
# 칸(rows) 기반 파서로는 손을 못 댄다. 페이지 글자를 직접 읽는다.
_LEAF_NAME_ALTS = sorted([
    "주식", "채권", "어음", "집합투자증권",
    r"파생상품\s*\(\s*장내\s*\)", r"파생상품\s*\(\s*장외\s*\)",
    r"특별자산\s*\(\s*실물자산\s*\)", r"특별자산\s*\(\s*기타\s*\)",
    "부동산", r"단기대출\s*및\s*예금",
    "기타", r"자산\s*총액",
], key=len, reverse=True)
RE_LEAF_NAME = re.compile("|".join(_LEAF_NAME_ALTS))
RE_SIMPLE_CAPTION = re.compile(r"자산\s*구성\s*현황")
RE_NUM_TOKEN = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse_simple_asset_text(text):
    """페이지 글자에서 "자산구성 현황" 캡션 뒤 "금액"/"비중" 두 줄을
    찾아 읽는다. 표 칸이 없어 값을 검산할 다른 수가 없으므로, 이름·
    금액·비율 개수가 셋 다 같고 맨 끝이 "자산총액"이며 비율 합이
    100인지를 평소보다 더 엄격히 확인한다."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if not RE_SIMPLE_CAPTION.search(line):
            continue
        window = lines[i: i + 30]
        amt_i = next((j for j, ln in enumerate(window)
                      if ln.strip().startswith("금액")), None)
        pct_i = next((j for j, ln in enumerate(window)
                      if ln.strip().startswith("비중")), None)
        if amt_i is None or pct_i is None or amt_i == 0:
            continue
        header = window[amt_i - 1]
        names = [re.sub(r"\s+", "", m.group())
                 for m in RE_LEAF_NAME.finditer(header)]
        amounts = [_num(v) for v in RE_NUM_TOKEN.findall(window[amt_i])]
        pcts = [_num(v) for v in RE_NUM_TOKEN.findall(window[pct_i])]
        if not (names and len(names) == len(amounts) == len(pcts)):
            continue
        if names[-1] != "자산총액" or pcts[-1] is None or abs(pcts[-1] - 100) > 1.0:
            continue
        out = []
        for name, amount, pct in zip(names[:-1], amounts[:-1], pcts[:-1]):
            if (pct or 0) == 0 and (amount or 0) == 0:
                continue
            out.append({"asset": name, "amount": amount, "pct": pct})
        if not out:
            continue
        got = sum(x["pct"] for x in out if x["pct"] is not None)
        if abs(got - 100) > 1.0:
            continue
        return {"items": out, "total_amount": amounts[-1],
                "pct_derived": False, "unit": _unit([], text),
                "as_of": _as_of([], text)}
    return None


def _simple_tables_from_pdf(conn, code):
    """_parse_simple_asset_text가 쓸 페이지 글자를 후보 쪽마다 넘긴다."""
    import pdfplumber
    import pdf_words

    pdfs = glob.glob(os.path.join(DATA_DIR, code, "*.pdf"))
    pages = _candidate_pages(conn, code)
    if not pdfs or not pages:
        return
    with pdfplumber.open(pdfs[0]) as pdf:
        for pno in pages:
            if pno < 1 or pno > len(pdf.pages):
                continue
            text = pdf_words.extract_text(pdf.pages[pno - 1]) or ""
            rec = _parse_simple_asset_text(text)
            if rec:
                yield pno, rec


def _has_total_row(rows):
    # "합계" 이름표가 항상 첫 칸에 있는 건 아니다(KR5153451009 실측:
    # 통화 이름 칸이 빈 채로 한 칸 밀려 "합계"가 둘째 칸에 있다).
    # 이름표가 든 칸(보통 첫 두 칸 중 하나)을 다 본다. "자산합계"처럼
    # 앞에 다른 말이 붙는 문서도 있다(KR5129420031 실측) - 꽉 찬 낱말이
    # 아니라 "합계"가 들어만 있으면 된다. 정확히 "합계"만 요구하면
    # (KR5129420031 실측) 이미 합계 줄이 있는데도 없다고 보고 계속
    # 뒤 페이지를 이어 붙이다 엉뚱한 문단(회사연혁 등)의 날짜를
    # 기준일로 잘못 줍는다.
    return any(r and any("합계" in _squash(c or "") for c in r[:2])
               for r in rows)


def _is_mother_fund_table(text):
    """이 표가 상품 자신이 아니라, 상품이 투자하는 모투자신탁 하나의
    참고용 자산구성표인가(KR5157450090 실측: "다. 집합투자기구의
    자산구성" 절 안에 모투자신탁 두 개(마이다스 우량채권/마이다스
    거북이)의 표가 먼저 나오고, 상품 자신("...자투자신탁...(운용)")의
    표가 맨 뒤에 나온다 - 페이지 순서로 첫 표를 집으면 모투자신탁
    쪽을 담아 총액이 완전히 다른 값(691,895)이 된다). 표 바로 위
    캡션 줄("<이름> 모투자신탁(...) [<날짜> / 단위 : ...]")에만 있는
    낱말을 본다 - "모투자신탁"이 다른 문단(클래스 이름 등)에서 그냥
    스쳐 지나가는 것과 구분하기 위해 "단위"가 같은 줄에 있을 때만
    본다."""
    return any("모투자신탁" in line and "단위" in line
               for line in (text or "").splitlines())


def _tables_from_db(conn, code):
    entries = []
    for page, dj in conn.execute(
            "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
            (code,)):
        try:
            rows = json.loads(dj)
        except (ValueError, TypeError):
            continue
        entries.append((page, rows))

    found = []
    i = 0
    while i < len(entries):
        page, rows = entries[i]
        i += 1
        if not _is_shape(rows):
            continue
        # 통화가 여럿인 펀드는 "합계" 줄이 다음 쪽 표 조각에 떨어져
        # 있을 수 있다(KR5153451009 실측: AUD~KRW가 47쪽 표 조각에,
        # TWD~합계가 48쪽 표 조각에 있다 - pdfplumber가 표를 쪽마다
        # 따로 잡는다). 합계 줄 없이 첫 조각만 보면 통화 하나(AUD)의
        # 값만 쓰게 된다. 합계 줄이 나올 때까지(또는 새 머리글이
        # 나올 때까지, 페이지가 몇 장 넘어갈 때까지) 뒤 조각을 이어
        # 붙인다.
        rows = list(rows)
        ncols = max((len(r) for r in rows if r), default=0)
        while (not _has_total_row(rows) and i < len(entries)
               and entries[i][0] - page <= 3):
            _npage, nrows = entries[i]
            if _is_shape(nrows):
                break
            # 이어 붙는 조각이 원래 표와 칸 수가 다른 문서가 있다
            # (KR5153451009 실측: 48쪽 조각이 빈 칸 하나가 왼쪽에 더
            # 있어 47쪽 조각보다 칸이 하나 많다 - pdfplumber가 쪽마다
            # 표 칸 경계를 따로 잡아서 생긴다). 왼쪽에 남는 빈 칸을
            # 떼어 칸 수를 맞춘다 - 안 맞추면 값이 한 칸씩 밀려 엉뚱한
            # 자산에 붙는다.
            data_rows = [r for r in nrows
                         if r and any(_num(c) is not None for c in r)]
            extra = max((len(r) for r in nrows if r), default=0) - ncols
            if extra > 0 and data_rows and all(
                    not any((r[c] or "").strip() for c in range(extra))
                    for r in data_rows):
                nrows = [r[extra:] if r else r for r in nrows]
            rows.extend(nrows)
            i += 1
        # 기준일("(2025년 04월 16일 기준 / ...)")은 표 자신의 칸이 아니라
        # 표 바로 위 캡션 문장으로만 찍히는 문서가 많다 - 표 칸(rows)만
        # 보면 기준일을 아예 못 찾는다(KR5111420047 실측: as_of가 통째로
        # 빠짐). 같은 페이지의 본문 청크(chunks)를 이어붙여 같이 넘긴다.
        # 캡션이 표보다 한 쪽 앞서 나오는 문서도 있다(KR5127450215 실측:
        # "다. 집합투자기구의 자산 구성 현황(...기준일: 2025년 05월
        # 17일)"이 39쪽 맨 끝에, 정작 표는 40쪽에 있다). 앞쪽 한 쪽의
        # 글도 뒤에 이어 붙여 같이 넘긴다 - _as_of는 첫 매치만 쓰므로
        # 이 페이지 글을 먼저 두어, 이 페이지 자체에 날짜가 있으면 그걸
        # 우선하고 없을 때만 앞쪽 페이지의 날짜로 넘어가게 한다.
        cur_text = " ".join(
            t for (t,) in conn.execute(
                "SELECT text FROM chunks WHERE doc_id = ? AND page = ?",
                (code, page))
        )
        prev_text = " ".join(
            t for (t,) in conn.execute(
                "SELECT text FROM chunks WHERE doc_id = ? AND page = ?",
                (code, page - 1))
        )
        page_text = f"{cur_text} {prev_text}"
        found.append((page, rows, page_text, cur_text))

    # 모투자신탁 참고표는 나중에 준다 - 상품 자신의 표가 있으면 그게
    # 먼저 골라지도록 한다(extract()는 첫 성공을 그대로 쓴다). 이
    # 판정은 반드시 이 페이지 자신의 글(cur_text)만 본다 - 기준일
    # 보강용으로 앞쪽 페이지 글까지 합친 page_text로 보면, 바로 앞
    # 페이지가 모투자신탁 표였을 때 그 캡션이 넘어와 지금 페이지까지
    # 덩달아 모투자신탁으로 잘못 찍힌다(KR5157450090 67쪽 실측 - 상품
    # 자신의 표인데 66쪽 모투자신탁 캡션이 앞서 붙어 있었다).
    own = [f for f in found if not _is_mother_fund_table(f[3])]
    mother = [f for f in found if _is_mother_fund_table(f[3])]
    for page, rows, page_text, _cur_text in own + mother:
        yield page, rows, page_text


def _candidate_pages(conn, code):
    """이 표가 있을 만한 페이지 번호. 본문·표 어디든 "자산구성"이나
    "통화별"이 적힌 쪽과 그 다음 쪽을 후보로 본다(제목과 표가 페이지
    경계로 갈리는 문서가 있다). "자산 구성"처럼 낱말 사이에 띄어쓰기가
    낀 문서도 있다(KR5172450019 실측 - "통화별" 칸 자체가 없는 표라
    "자산총액"도 "자산 총액"으로 띄어 쓴다)."""
    pages = set()
    for sql in ("SELECT page FROM chunks WHERE doc_id = ? AND "
                "(text LIKE '%자산구성%' OR text LIKE '%자산 구성%' "
                "OR text LIKE '%통화별%' OR text LIKE '%자산총액%' "
                "OR text LIKE '%자산 총액%')",
                "SELECT page FROM tables WHERE doc_id = ? AND "
                "(row_text LIKE '%자산구성%' OR row_text LIKE '%자산 구성%' "
                "OR row_text LIKE '%통화별%' OR row_text LIKE '%자산총액%' "
                "OR row_text LIKE '%자산 총액%')"):
        for (pg,) in conn.execute(sql, (code,)):
            pages.add(pg)
            pages.add(pg + 1)
    return sorted(pages)


def _tables_from_pdf(conn, code):
    """DB의 표로 못 찾은 문서를 PDF에서 다시 읽는다.

    두 가지가 걸린다. 글자가 한 자씩 떨어져 나오는 문서는 기본 설정으로
    표가 아예 안 잡히고(KR5111450067 58쪽 실측: 0개), 표가 한 칸으로
    뭉뚱그려 잡히는 문서도 있다(KR5116501001 43쪽: 2행 1열). 둘 다
    가로줄을 글자 줄에서 잡으면 칸이 살아난다.

    페이지는 DB에서 미리 추린다 - 전 쪽을 여는 건 느리기도 하지만,
    글자가 흩어진 문서는 페이지 글자로 걸러 봐야 "통화별"이 붙어 있지도
    않아서 소용이 없다."""
    import pdfplumber
    import pdf_words

    pdfs = glob.glob(os.path.join(DATA_DIR, code, "*.pdf"))
    pages = _candidate_pages(conn, code)
    if not pdfs:
        return
    # "lines/text"가 못 잡는 문서가 있다(KR555202013M 35쪽 실측: 그
    # 설정으론 표 왼쪽 "통화별" 칸과 오른쪽 "자산총액" 칸이 통째로
    # 떨어져 나간 반쪽짜리 표만 잡히는데, 기본 설정으로는 그 두 칸을
    # 포함한 온전한 표가 잡힌다). 반대로 기본 설정으로는 아예 표가 안
    # 잡히는 문서도 있다(KR5111450067). 그래서 둘 다 시도한다 - 값
    # 검산(parse_asset_table)이 있어 잘못 잡은 표는 어차피 걸러진다.
    settings_variants = (
        {"vertical_strategy": "lines", "horizontal_strategy": "text"},
        None,  # pdfplumber 기본 설정
    )
    with pdfplumber.open(pdfs[0]) as pdf:
        if not pages:
            # 회전 잡음이 본문 글자 자체를 깨서 DB에 저장된 chunks/tables
            # 텍스트에 "자산구성"/"통화별"이 아예 안 남는 문서가 있다
            # (KR5172450019/KR555202013M 실측 - build_structured_store.py가
            # 쓰는 기본 extract_text()로는 못 읽지만 pdf_words의 회전
            # 보정판으로는 읽힌다). DB 후보가 하나도 없을 때만 전 쪽을
            # pdf_words로 다시 훑는다 - 느려서 평소엔 안 쓴다.
            pages = [
                pno for pno, page in enumerate(pdf.pages, start=1)
                if any(w in _squash(pdf_words.extract_text(page) or "")
                       for w in ("자산구성", "통화별", "자산총액"))
            ]
            pages = sorted({p for pno in pages for p in (pno, pno + 1)})
        if not pages:
            return
        for pno in pages:
            if pno < 1 or pno > len(pdf.pages):
                continue
            page = pdf.pages[pno - 1]
            # 회전 잡음으로 글자가 한 자씩 흩어지는 문서가 있다(KR5120420091
            # 실측: "다.집합투자기구의자산구성현황"만 멀쩡하고 기준일
            # 캡션은 "통\n대\n자\n(주\n..."처럼 통째로 깨진다 - 기본
            # extract_text()로는 기준일을 못 찾는다). extract_class_fees.py
            # 등에서 이미 검증된 보정 함수를 그대로 쓴다 - 정상 문서는
            # 결과가 완전히 같다.
            #
            # 캡션이 표보다 한 쪽 앞서기도 한다(KR5127450215 실측 -
            # _tables_from_db와 같은 이유). 이 쪽에 날짜가 없을 때만
            # 앞쪽 쪽의 날짜로 넘어가도록 이 페이지 글을 먼저 둔다.
            prev_text = (pdf_words.extract_text(pdf.pages[pno - 2]) or ""
                         if pno > 1 else "")
            text = (pdf_words.extract_text(page) or "") + " " + prev_text
            for settings in settings_variants:
                tabs = (page.find_tables(table_settings=settings)
                        if settings else page.find_tables())
                for t in tabs:
                    rows = t.extract()
                    if rows:
                        yield pno, rows, text


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute("SELECT product_code FROM product_master")]

    out, fallback = [], []
    for code in codes:
        got = None
        for page, rows, page_text in _tables_from_db(conn, code):
            rec = parse_asset_table(rows, page_text)
            if rec:
                got = dict(rec, product_code=code, page=page, method="cell_grid")
                break
        if got is None:
            for page, rows, text in _tables_from_pdf(conn, code):
                rec = parse_asset_table(rows, text)
                if rec:
                    got = dict(rec, product_code=code, page=page,
                               method="pdf_text_rows")
                    fallback.append(code)
                    break
        if got is None:
            for page, rec in _simple_tables_from_pdf(conn, code):
                got = dict(rec, product_code=code, page=page,
                           method="pdf_simple_text")
                fallback.append(code)
                break
        if got:
            out.append(got)
    conn.close()
    return out, fallback


def report(rows, fallback):
    print(f"자산구성 {len(rows)}개 상품")
    if fallback:
        print(f"  PDF 재읽기로 건진 문서: {len(fallback)}개 {fallback[:6]}")
    for r in rows[:4]:
        items = ", ".join(f"{i['asset']} {i['pct']}%" for i in r["items"])
        print(f"  [{r['product_code']} p{r['page']} {r.get('as_of') or '기준일?'}] {items}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows, fallback = extract(args.db)
    report(rows, fallback)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
