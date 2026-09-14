"""매입·환매할 때 어느 날 기준가격이 적용되고 돈이 언제 들어오는지 뽑는다.

    "17시 50분에 환매 청구하면 어떻게 되나요?"
    "판 돈은 언제 들어와요?"

고객이 실제로 많이 하는 질문인데 지금은 아예 답하지 못한다. 문서에는
89/100, 88/100에 들어 있다.

    (2) 매입청구시 적용되는 기준가격
    (가) 15시 30분 이전에 자금을 납입한 경우 : 납입일로부터 제2영업일에
         공고되는 기준가격을 적용
    (나) 15시 30분 경과 후 자금을 납입한 경우 : 납입일로부터 제3영업일에
         공고되는 기준가격을 적용

    (3) 환매청구시 적용되는 기준가격
    (가) 15시 30분 이전에 환매를 청구한 경우 : 환매청구일로부터 제2영업일에
         공고되는 기준가격을 적용하여 제4영업일에 관련세금등을 공제한 후
         환매대금을 지급합니다.

숫자로 바꾸지 않고 문장을 통째로 담는다. "제2영업일"만 뽑으면 그게 몇 시
기준인지, 지급은 언제인지가 날아간다. 기준시각도 문서마다 다르다
(15시 30분 / 오후 5시 / 17시). 조건문을 값 하나로 줄이면 틀린 답이 된다.

실행:
    python3 scripts/extract_trade_rules.py
    python3 scripts/extract_trade_rules.py --check
"""

import argparse
import functools
import glob
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "trade_rules.json")

# 뽑을 절과 그 절을 여는 말. 문서마다 표현이 조금씩 다르다 - "청구"가
# 아예 없는 문서도 있고("환매 시 적용되는 기준가격" - KR555202013M 20쪽
# 실측: "(2) 환매 시 적용되는 기준가격", "청구"라는 말 자체가 없다),
# "매입"/"환매"와 "청구" 사이에 공백이 낀 문서도 있다(KR5174420011 22쪽
# 실측: "(2) 환매 청구시 적용되는 기준가격" - "환매청구"가 아니라 "환매
# 청구"). "청구"를 통째로 선택 요소로 두고 "적용" 뒤 "되는"도 있어도
# 없어도 받아준다 - 안 그러면 절 제목 자체를 못 찾아 규칙이 통째로 빠진다.
SECTIONS = (
    ("매입기준가", (r"매입\s*(?:청구\s*)?시?\s*적용(?:되는)?\s*기준가격",)),
    ("환매기준가", (r"환매\s*(?:청구\s*)?시?\s*적용(?:되는)?\s*기준가격",)),
)

# 절이 끝나는 자리. 다음 번호 항목이나 새 제목이 나오면 거기까지다.
# "가."/"나." 같은 항목 표시는 앞에 공백이 와야 한다. 그 조건이 없으면
# "지급합니다. (나) ..."의 "다."가 절 제목으로 잡혀서 (나) 항목이 통째로
# 잘려 나간다 - 15시 30분 경과 후에 어떻게 되는지가 사라진다.
RE_SECTION_END = re.compile(
    r"\([0-9]\)|(?<=\s)[나-하]\.\s|제\s*\d+\s*부|■|◆|◇|【")

# 항목 표시 뒤가 시각 조건이면 그건 새 절이 아니라 같은 규칙의 두 번째
# 갈래다. 매입/환매를 "가./나."로 나눠 놓고 그 안의 시각 갈래도 다시
# "가./나."로 쓰는 문서가 있어서(KR5144420020 실측), 표시만 보고 자르면
# "5시 넘겨서 넣으면 어떻게 되나"가 통째로 사라진다.
RE_TIME_BRANCH = re.compile(r"\s*(?:오전|오후)?\s*\d+\s*시")

# 쪽 아래 머리글이 규칙 뒤에 딸려 오는 문서가 있다
# ("... 환매대금을 지급합니다. 30 NH-Amundi Asset Management").
# 우리말 규칙 문장 뒤에 쪽번호와 로마자만 남으면 그건 머리글이다.
# 쪽번호만 덩그러니 남기도 한다("... 기준가격을 적용 24"). 규칙 문장이
# 숫자로 끝나는 일은 없으니(늘 "적용/매입/지급합니다"로 끝난다) 떼도 된다.
RE_PAGE_FOOTER = re.compile(
    r"\s\d{1,3}(?:\s+[A-Za-z][\w.\-]*){0,5}\s*$")

# 매입/환매 흐름을 그린 타임라인 그림의 칸 이름들(KR5156450026 실측:
# "제1영업일(D) 제2영업일(D+1) 제3영업일(D+2) 제4영업일(D+3) 환매청구일
# (15시30분이전) 기준가 적용일 환매대금지급일 환매청구일(15시30분이후)
# 기준가 적용일 환매대금지급일" - 진짜 조건문(①/② 문장) 뒤에 그대로
# 이어 붙는다). 아래 _drop_diagram_junk에서 이런 칸 이름이 3개 이상
# 연달아 나오면 걷어낸다.
_DIAGRAM_DAY_CELL = r"제\d\s*영업일\s*\([^)]{0,10}\)"
_DIAGRAM_REQ_CELL = r"(?:환매|매입)청구일\s*\([^)]{0,20}\)"
# 매입 흐름을 그린 또 다른 모양의 그림도 있다(KR5127420034 26쪽 실측:
# "17시(오후 5시) 기준가격 적용일 자금납입일 이전 매입시 집합투자증권
# 교부일 17시(오후 5시) 기준가격 적용일 자금납입일 경과 후 매입시
# 집합투자증권 교부일" - 이미 다 끝난 두 갈래 문장 뒤에 그대로 이어
# 붙는다). "기준가\s*적용일"만으로는 "격"이 낀 "기준가격 적용일"을 못
# 잡고, "이전 매입시"/"경과 후 매입시"·"집합투자증권 교부일" 칸 이름도
# 새로 나온 것들이라 따로 더한다. 표 칸이 "D D+1일"처럼 갈라져 "일" 한
# 글자만 뚝 떨어져 나오는 문서도 있다(KR5113420069 26쪽 실측: "일
# 자금납입일 수익증권매입일" - "일"은 흔한 글자라 혼자 두면 아무 문장에나
# 걸리니, 바로 뒤에 저 두 칸 이름이 붙어 있을 때만 칸 이름으로 본다.
_DIAGRAM_LABEL_CELL = (
    r"기준가\s*적용일|기준가격\s*적용일|환매대금지급일|매입대금납입일|"
    r"수익증권매입일|자금납입일|집합투자증권\s*교부일|"
    r"(?:이전|경과\s*후)\s*매입시|일(?=\s*(?:자금납입일|수익증권매입일))"
)
RE_DIAGRAM_CELLS = re.compile(
    r"(?:(?:" + _DIAGRAM_DAY_CELL + r"|" + _DIAGRAM_REQ_CELL + r"|"
    + _DIAGRAM_LABEL_CELL + r")\s*){3,}"
)

# 같은 타임라인 그림인데 칸 이름이 "D/D+1/D+2"처럼 짧고 "제N영업일" 꼴이
# 아닌 문서도 있다(KR5118420006 26쪽 실측: "D D+1 자금납입 수익증권매입
# (17시 이전) 기준가격 적용"). "D"/"D+N"는 너무 흔한 토막이라 혼자서는
# 걸러낼 신호가 못 되지만, 이 칸 이름들과 3개 이상 연달아 나올 때만
# 걷어낸다 - 평범한 문장이 이런 낱말만 죽 나열할 일은 없다.
_DIAGRAM_D_CELL = r"D(?:\s*\+\s*\d+)?"
_DIAGRAM_FLOW_LABEL = (
    r"자금납입|수익증권매입|기준가격\s*적용|"
    r"\((?:17시|오후\s*\d+\s*시)\s*이(?:전|후)\)"
)
RE_TIMELINE_DIAGRAM = re.compile(
    r"(?:(?:" + _DIAGRAM_D_CELL + r"|" + _DIAGRAM_FLOW_LABEL + r")\s*){3,}"
)


def _drop_diagram_junk(body):
    """도표 안 글자가 한 자씩 흩어져 딸려 오는 문서가 있다(KR5118420006
    실측: "기준가격 ( 자 1 7 금 시 D 납 이 입 전 ) 수 기 익 준 D 증 가"
    - 매입 흐름을 그린 그림의 글자다). 한 글자 토막이 대여섯 개 넘게
    이어지면 문장이 아니라 도표 잔해다."""
    out, run = [], []
    for tk in body.split():
        if len(tk) == 1:
            run.append(tk)
            continue
        if len(run) <= 5:
            out.extend(run)
        run = []
        out.append(tk)
    if len(run) <= 5:
        out.extend(run)
    body = " ".join(out)
    # 글자가 한 자씩 안 흩어지고 "제1영업일(D) 제2영업일(D+1) ... 환매
    # 청구일(15시30분이전) 기준가 적용일 환매대금지급일"처럼 도표 칸
    # 이름이 온전한 단어째로 죽 나열되는 문서도 있다(KR5156450026 실측
    # - 진짜 조건문(①/② 문장) 뒤에 타임라인 그림의 칸 이름이 그대로
    # 이어 붙었다). 이런 칸 이름이 3개 이상 연달아 나오면(정상 문장은
    # 이런 명사만 죽 나열하지 않는다) 도표 잔해로 보고 걷어낸다.
    body = RE_DIAGRAM_CELLS.sub(" ", body)
    return RE_TIMELINE_DIAGRAM.sub(" ", body)


# 표 칸 경계나 다음 청크에서 잘리면 각주가 "*"/"※"만 열어놓고 못 끝맺은
# 채로 꼬리에 남는다(KR5118420006 실측: "...기준가격 적용 * 판매회사의"
# - "*" 뒤로 각주가 시작만 하고 안 끝난다). 뒤에서부터 마지막 "*"나 "※"를
# 찾아, 그 뒤에 문장을 맺는 말("다"/"니다"/"음"/마침표)이 하나도 없으면
# 못 끝맺은 각주로 보고 그 앞까지만 남긴다. 갈래 문장 자체(①/② 등) 안에는
# "*"/"※"가 나올 일이 없으니 잘라도 규칙 본문은 안 다친다.
RE_UNFINISHED_FOOTNOTE_TAIL = re.compile(r"[*※][^*※]{0,60}$")


def _trim(body):
    # 앞머리의 "·"/"-"는 갈래를 여는 글머리표라 남긴다 - 떼면 첫 갈래만
    # 표시가 없어져 둘째 갈래와 짝이 안 맞아 보인다.
    body = RE_PAGE_FOOTER.sub("", body)
    body = _drop_diagram_junk(body)
    m = RE_UNFINISHED_FOOTNOTE_TAIL.search(body)
    if m and not re.search(r"(?:다|니다|음)\.?\s*$", m.group(0)):
        body = body[: m.start()]
    return body.rstrip(" :：-·").strip()


def _running_headers(texts):
    """쪽마다 되풀이되는 머리글(문서 제목 줄)을 찾는다.

    쪽을 넘어가는 규칙을 이어 붙이면 이 머리글이 문장 한가운데로 끼어든다
    ("... 환매대금을 지급합니다. NH-Amundi 필승 코리아 증권투자신탁[주식]
    나. 오후 3시 30분 경과 후 ..."). 여러 조각의 첫 줄에 똑같이 나오는
    줄이 곧 머리글이다."""
    seen = {}
    for t in texts:
        line = (t or "").strip().split("\n")[0].strip()
        if 4 <= len(line) <= 60:
            seen[line] = seen.get(line, 0) + 1
    return {ln for ln, n in seen.items() if n >= 3}


def _strip_furniture(text, heads):
    """조각 하나에서 머리글·쪽번호를 덜어낸다(이어 붙이기 전에)."""
    flat = _flat(text)
    for h in heads:
        flat = flat.replace(_flat(h), " ")
    return RE_PAGE_FOOTER.sub("", _flat(flat)).strip()


def _join(a, b, max_overlap=200):
    """조각끼리 끝과 앞이 겹쳐 있는 문서가 있다. 그냥 이으면 같은 문장이
    두 번 나온다 - 겹치는 만큼 덜고 잇는다."""
    if not a:
        return b
    if not b:
        return a
    for n in range(min(len(a), len(b), max_overlap), 7, -1):
        if a[-n:] == b[:n]:
            return a + b[n:]
    return f"{a} {b}"

# 이만큼 넘으면 절 하나가 아니라 여러 절을 삼킨 것이다.
MAX_SECTION_CHARS = 600
# 청크 경계에서 잘린 규칙을 되붙일 때 한 번에 이어 보는 조각 수. 절 제목부터
# 그 절이 실제로 맺음말로 끝나는 자리까지가 조각 3개 폭을 넘는 문서가 있다
# (KR5113420069 실측 - 제목에서 "나." 경계까지 필요한 조각이 4~5개). 조각
# 폭이 모자라면 그 안에서 아무리 찾아도 맺음말 자리 자체가 안 들어 있어
# _snap_to_clause_end로도 못 살린다.
CHUNK_WINDOW = 5
# 온전한 규칙에는 기준시각과 며칠 뒤인지가 둘 다 있다. 하나라도 없으면
# 청크 경계에서 잘린 것이다("15시 30분 이전에 자금을 납입한 경우 :
# 납입일로부터" 하고 끊긴 것을 담고 있었다). 그럴 땐 다른 데서 다시 찾는다.
RE_HAS_TIME = re.compile(r"\d+\s*시")
RE_HAS_DAYS = re.compile(r"영업일|D\s*\+\s*\d")


def _is_complete(body):
    return bool(RE_HAS_TIME.search(body) and RE_HAS_DAYS.search(body))


# 단을 갈라 읽으면 줄이 바뀐 자리에 공백이 생긴다("제3영 업일에").
# 흔한 모양만 되붙인다.
RE_WRAPPED_DAY = re.compile(r"제\s*(\d+)\s*영\s+업일")


def _flat(text):
    out = " ".join((text or "").split())
    return RE_WRAPPED_DAY.sub(r"제\1영업일", out)


def _section(text, patterns):
    """절을 열고 그 안의 글을 다음 절 직전까지 잘라 온다."""
    flat = _flat(text)
    for pat in patterns:
        m = re.search(pat, flat)
        if not m:
            continue
        # 절 제목이 문장의 주어인 문서가 있다("매입청구시 적용되는
        # 기준가격은 아래와 같으며, ..."). 제목 뒤부터만 담으면 "은 아래와
        # 같으며,"로 시작하는 토막이 된다 - 그럴 땐 제목까지 같이 담는다.
        start = m.end()
        if flat[start: start + 1] in ("은", "는", "이", "가"):
            start = m.start()
        # 절 경계("나." 등)를 찾는 범위는 최종 길이 한도(MAX_SECTION_CHARS)
        # 보다 넉넉히 잡는다. 이어붙이기(_join) 방식은 청크 짝에 따라
        # 겹치는 만큼을 다르게 덜어내므로, 같은 내용이라도 title 매치
        # 자리에서 진짜 경계("나.")까지의 거리가 시도마다 달라진다(KR
        # 5113420069 실측 - 어떤 조합에서는 545자, 다른 조합에서는 600자
        # 밖으로 밀려난다). 범위를 600자로 좁혀 놓으면 경계가 밖으로 밀린
        # 시도에서는 아예 못 보고 엉뚱한 자리(각주 한가운데)에서 끊는다.
        window = flat[start: start + MAX_SECTION_CHARS * 2]
        pos = 4  # 바로 뒤 "(가)"는 넘긴다
        found_end = False
        while True:
            end = RE_SECTION_END.search(window, pos)
            if not end:
                break
            if RE_TIME_BRANCH.match(window, end.end()):
                pos = end.end()
                continue
            window = window[: end.start()]
            found_end = True
            break
        # 다음 절 표시(RE_SECTION_END)를 못 찾으면 그때 비로소 최종
        # 길이 한도를 적용한다. 한도에 걸린 게 확실할 때만(다음 절
        # 표시를 못 찾은 채 끝까지 채워졌을 때) 마지막 낱말 경계(공백)
        # 까지만 남긴다 - 규칙 문장 자체(두 갈래)는 이미 그 앞에서 다
        # 끝난 뒤라 잘려도 손해가 없다.
        if not found_end:
            window = window[:MAX_SECTION_CHARS]
            if len(window) >= MAX_SECTION_CHARS - 5:
                cut = window.rfind(" ")
                if cut > len(window) * 0.7:
                    window = window[:cut]
        body = window
        # 두 갈래가 다 갖춰진 첫 조각을 찾으면 거기서 검색을 멈추는데
        # (extract()의 "이미 둘 다 갖췄으면 더 안 본다" 규칙), 청크가
        # 하필 그 직후 각주 한가운데서 끊기면 그 잘린 채로 굳어버린다
        # (KR5156450026 환매기준가 실측: "...경과 후...지급합니다."까지
        # 두 갈래는 다 끝났는데, 그 뒤에 이어지는 각주가 "...주기적으로
        # 수익증"에서 끊긴 조각이 먼저 걸려 그대로 채택됐다). 두 갈래
        # 자체는 이미 끝난 뒤라 그 뒤에 붙는 도표·각주는 있으나 없으나
        # 규칙 정보에 손해가 없다 - 아래 _snap_to_clause_end가 마지막
        # 맺음자리까지만 남기고 걷어낸다.
        body = _trim(body)
        # 두 단(매입/환매)을 표 밖 글자로 잘못 이어 붙이면 시각+영업일
        # 정보는 다 있어도 꼬리가 조사 하나로 매달린 채 끝난다
        # (KR5113420069 실측 - _is_complete()는 정보 유무만 보지 문장이
        # 온전히 맺혔는지는 안 본다). 마지막으로 온전히 끝난 절 자리까지
        # 되돌린다.
        body, trimmed = _snap_to_clause_end(body)
        if len(body) >= 20 and _is_complete(body):
            return body, trimmed
    return None, None


# 매입/환매 규칙을 좌우 2단 표로 싣는 문서가 18개 있다. 본문 글자로
# 읽으면 두 단이 한 줄씩 번갈아 섞여서 알아볼 수가 없다.
#
#   ㆍ오후 5시 이전 자금을 납입한 경우 : 자금   ㆍ오후5시 이전 환매를 청구한
#   을 납입한 영업일의 다음 영업일(D+1)에      경우 : 환매를 청구한 날로부터
#
# 표의 칸으로 보면 제대로 나뉘어 있다.
#
#   ['매입 방법', '<매입 규칙>', '환매 방법', '<환매 규칙>']
CELL_LABELS = {"매입방법": "매입기준가", "환매방법": "환매기준가",
               "매입": "매입기준가", "환매": "환매기준가"}


# 두 갈래 중 뒤엣것("경과 후"/"이후"/"초과")이 있는지. 앞 갈래만 담고
# 끝내면 "17시 50분에 청구하면요?"에 엉뚱한 답을 하게 된다.
RE_SECOND_BRANCH = re.compile(r"경과\s*후|이\s*후|초과")


def _has_both_branches(body):
    # 둘째 갈래를 열어 놓고 며칠 뒤인지는 안 적힌 채 끝나는 조각이 있다
    # (KR5118420006 실측: "... 매입 오후 5시 경과 후 자금을 납입한 경우"
    # 에서 끊긴다). 여는 말만 있는 건 없느니만 못하다 - 열었으면 며칠
    # 뒤인지까지 있어야 온전한 규칙이다.
    m = RE_SECOND_BRANCH.search(body)
    return bool(m and RE_HAS_DAYS.search(body, m.end()))


def _done(found):
    """두 절을 다 찾았고 둘 다 갈래가 온전하며 맺음자리에서 끝났는가.

    갈래 정보(_has_both_branches)만 갖췄다고 바로 확정 지으면, 절 제목부터
    진짜 경계까지가 조각 폭보다 넓은 문서에서 못 끝맺은 조각이 그대로
    굳어버린다(KR5113420069 실측). found[kind][2](끝까지 온전히 읽었는지,
    _snap_to_clause_end가 깎아낸 적 없는지)까지 같이 걸어야 그런 조각을
    넘기고 계속 찾는다."""
    return all(kind in found and _has_both_branches(found[kind][0])
               and found[kind][2]
               for kind, _pats in SECTIONS)


def _continuation(rows, ri, col, body):
    """규칙의 두 번째 갈래가 아랫줄 칸에 따로 놓인 표가 있다
    (KR5174420011 실측: '17시 이전...'과 '17시 경과 후에...'가 라벨 없는
    두 줄로 나뉘어 있어 앞 갈래만 담고 있었다). 같은 열의 다음 줄이
    시각 조건으로 시작하면 같은 규칙의 이어지는 갈래로 본다."""
    if _has_both_branches(body):
        return body
    for row in rows[ri + 1:]:
        cells = [(x or "").strip() for x in row]
        if any(CELL_LABELS.get(_flat(c).replace(" ", "")) for c in cells):
            break
        nxt = _flat(cells[col]) if col < len(cells) else ""
        if not nxt:
            continue
        if RE_TIME_BRANCH.match(nxt) and RE_HAS_DAYS.search(nxt):
            return f"{body} {nxt}"
        break
    return body


# 대부분 문서는 "시각별로 며칠 뒤 기준가"라는 갈래 구조지만, 그런 갈래
# 없이 "최초설정일에 공고되는 기준가격"처럼 통짜 문장 하나로 끝나는
# 매입 규칙도 있다(KR5147430065 실측 - 목표전환형이라 최초설정 이후엔
# 매입을 안 받는 구조로 보인다). _is_complete(시각+영업일 둘 다 요구)로
# 걸러지면 이 규칙 자체가 통째로 사라진다. 표 칸은 청크 텍스트와 달리
# 페이지 경계에서 잘릴 일이 없는 완결된 단위이므로, 칸에서 읽을 때는
# "시각+영업일"이 없어도 문장이 딱 맺음말로 끝나면(조사·접속사로 안
# 끝나면) 그대로 받아들인다 - 조각난 문장이라면 대개 "...의"/"...에"
# 처럼 다음 말을 기다리는 조사로 끝난다.
RE_DANGLING_END = re.compile(
    r"(?:의|에|로|과|와|은|는|이|가|을|를|하|되|:|：)$")

# 시각+영업일이 몸통 어딘가에 다 있으면 _is_complete()가 True를 주는데,
# 그건 "글이 온전히 끝났는지"가 아니라 "정보가 있는지"만 본다. 표 두
# 단을 잘못 이어 붙이면(_continuation) 정보는 다 있어도 꼬리가 조사
# 하나로 매달린 채 끝난다(KR5113420069 실측: "...4영업일(D+3)에"에서
# 끝남 - "지급합니다" 등 맺음말이 없다). "지급"/"적용"/"매입"/"다." 중
# 마지막으로 온전히 끝난 자리까지 되돌린다 - 이 표들의 규칙 문장은
# 예외 없이 이 네 가지 중 하나로 끝난다.
#
# "적용"/"지급"/"매입"은 문장을 맺기도 하지만("...기준가격을 적용"),
# "적용하여 ...을 지급합니다"처럼 뒤에 "하여/하고"가 붙어 문장 중간을
# 잇는 접속형으로도 아주 흔히 쓰인다(KR5113420069/KR5127450473 실측 -
# "적용하여"에서 바로 잘랐더니 정작 뒤에 이어지는 "...관련세금 등을
# 공제한 후 환매대금을 지급합니다"라는 진짜 맺음말을 놓쳤다). 뒤에
# "하"가 바로 붙으면(접속형) 그 자리는 건너뛰고 다음 후보를 본다 -
# "합니다"가 붙은 경우나 마침표·공백으로 바로 끝나는 경우만 진짜
# 맺음자리로 본다.
#
# 표 칸이 좁아 "지급"/"적용"/"매입" 두 글자가 줄바꿈으로 갈라지는 문서가
# 있다(KR5113420012 실측: "환매대금을 지\n급" - _flat()이 줄바꿈을 공백
# 하나로 바꾸면서 "지 급"이 된다). 글자 사이에 공백 하나가 낄 수 있다고
# 보고 받아준다 - 안 그러면 이미 온전히 끝난 셀 문장도 "맺음자리를 못
# 찾음"으로 오판해 굳이 본문 절 탐색으로 넘어가 더 지저분한 결과로
# 갈아치운다.
RE_CLAUSE_END = re.compile(
    r"다\s*\.|(?:지\s*급|적\s*용|매\s*입)\s*합니다\.?"
    r"|(?:지\s*급|적\s*용|매\s*입)(?!\s*하)\.?")


def _snap_to_clause_end(body):
    """(body, trimmed)를 돌려준다. trimmed=True는 원문이 절 중간에서 잘려
    맺음자리까지 되돌려 깎아냈다는 뜻이다 - 결과 글자만 보면 멀쩡해 보여도
    그 깎아낸 자리 바로 뒤에 있었을 세부 내용(KR5113420069 실측: 둘째
    갈래의 지급 시점)이 통째로 사라졌을 수 있다. 그래서 trimmed=True인
    결과는 "다 찾았다"고 바로 확정 지어선 안 되고, 더 나은 조각을 계속
    찾아봐야 한다(extract()의 _done/guard가 이 값을 쓴다).

    "끝 글자가 조사면 못 끝맺은 것"이라는 식으로 끝 모양만 블랙리스트로
    걸러내면(RE_DANGLING_END), 낱말 중간에서 잘렸는데 하필 그 마지막
    글자가 조사가 아닌 경우를 놓친다(KR5113420069 실측: "...매입 또는
    환매청"에서 잘렸는데 "청"은 조사가 아니라서 안 걸렸다). 그 대신 몸통
    안의 마지막 맺음자리(RE_CLAUSE_END)가 문자열 끝과 정확히 맞아떨어지는지
    직접 확인한다 - 안 맞으면 그 뒤에 뭔가 잘려나간 채 덧붙어 있다는 뜻이니
    마지막 맺음자리까지 되돌린다. 맺음자리 표시가 몸통 어디에도 없으면
    (KR5147430065처럼 갈래 구조 자체가 없는 통짜 문장) 되돌릴 기준이 없으니
    그대로 둔다."""
    stripped = body.rstrip()
    matches = list(RE_CLAUSE_END.finditer(stripped))
    if not matches:
        return body, False
    if matches[-1].end() == len(stripped):
        return stripped, False
    return stripped[: matches[-1].end()], True


def _is_complete_cell(body):
    if _is_complete(body):
        return True
    return len(body) >= 10 and not RE_DANGLING_END.search(body.rstrip())


def _from_cells(rows):
    """표의 '매입 방법 | 규칙' 짝에서 규칙을 읽는다."""
    out = {}
    for ri, row in enumerate(rows):
        cells = [(x or "").strip() for x in row]
        for i, cell in enumerate(cells):
            kind = CELL_LABELS.get(_flat(cell).replace(" ", ""))
            if not kind or kind in out:
                continue
            for j in range(i + 1, len(cells)):
                if not cells[j].strip():
                    continue
                body = _trim(_flat(cells[j]))
                if len(body) >= 20 and _is_complete_cell(body):
                    full = _continuation(rows, ri, j, body)[:MAX_SECTION_CHARS]
                    out[kind] = _snap_to_clause_end(full)
                break
    return out


# 매입/환매를 좌우 2단으로 싣되 표로도 안 잡히는 문서가 8개 있다(KR5127 계열).
# 글자만 보면 두 단이 줄 단위로 번갈아 나오고 한쪽이 두 줄로 쪼개져서,
# 어느 줄이 어느 단의 이어지는 부분인지 짐작할 수가 없다. 짐작으로 붙였더니
# "제3영" + "4영업일에" = "제3영4영업일에" 같은 엉터리가 나왔다.
#
# 좌표를 보면 답이 분명하다. 매입은 x 112~260, 환매는 x 383~540에 있다.
# "환매방법" 라벨의 x를 경계로 삼아 두 단을 갈라 읽는다.
LABEL_X = {"매입방법": "매입기준가", "환매방법": "환매기준가"}
# 좌표로 단을 갈라 읽으면 위아래 다른 절(투자위험 설명 등)도 딸려 온다.
# 시각 조건으로 시작해서 "매입" 또는 "대금 지급"으로 끝나는 대목만 추린다.
RE_RULE_SPAN = re.compile(
    r"\d+\s*시(?:\s*\d+\s*분)?\s*(?:이전|경과\s*후|이후)\s*:?.{0,70}?"
    r"(?:으로\s*매입|대금\s*지급)")
# 라벨 줄에서 위아래로 이만큼 안에 있는 글자만 본다(다른 절을 삼키지 않게).
COLUMN_ROW_WINDOW = 80


def _from_pdf_columns(pdf_path):
    """2단으로 놓인 매입/환매 규칙을 x좌표로 갈라 읽는다."""
    import pdfplumber

    out = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if "매입방법" not in text or "환매방법" not in text:
                continue
            words = page.extract_words()
            labels = {}
            for w in words:
                key = w["text"].replace(" ", "")
                if key in LABEL_X and key not in labels:
                    labels[key] = w
            if len(labels) < 2:
                continue
            split_x = labels["환매방법"]["x0"]
            base_top = labels["매입방법"]["top"]

            cols = {"매입기준가": [], "환매기준가": []}
            for w in words:
                if abs(w["top"] - base_top) > COLUMN_ROW_WINDOW:
                    continue
                if w["text"].replace(" ", "") in LABEL_X:
                    continue
                kind = "매입기준가" if w["x0"] < split_x else "환매기준가"
                cols[kind].append(w)

            for kind, ws in cols.items():
                rows = {}
                for w in ws:
                    rows.setdefault(round(w["top"]), []).append(w)
                body = " ".join(
                    " ".join(x["text"] for x in sorted(v, key=lambda x: x["x0"]))
                    for _t, v in sorted(rows.items()))
                spans = RE_RULE_SPAN.findall(_flat(body))
                if not spans:
                    continue
                body = " / ".join(_flat(x) for x in spans)
                if _is_complete(body) and kind not in out:
                    out[kind] = (body[:MAX_SECTION_CHARS], pno)
            if len(out) == len(LABEL_X):
                break
    return out


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]

    out = []
    for code in codes:
        found = {}
        # 표의 칸을 먼저 본다. 예전엔 본문 글자를 먼저 봤는데, 같은 상품을
        # 두 방식으로 뽑아 견줘 보니 글자 쪽이 조건 하나를 통째로 빠뜨리고
        # 도표 잔해까지 딸려 오는 경우가 있었다.
        #
        #   글자: "(1) 오후 5시 이전에 ... 2영업일 ... T일 T+1일 자금납입일
        #          집합투자증권 매입일 (매입청구일) (기준가적용일)"
        #          <- 경과 후 조건이 없고 도표 글자가 섞였다
        #   셀  : "· 17시 이전 : 2영업일 기준가 매입
        #          · 17시 경과 후 : 3영업일 기준가 매입"
        #
        # 값이 서로 어긋나는 건 아니었지만(영업일 숫자는 같다) 셀 쪽이
        # 더 온전하고 짧아서 답변에 그대로 쓰기 좋다.
        for page, dj in conn.execute(
                "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
                (code,)):
            try:
                rows = json.loads(dj)
            except (ValueError, TypeError):
                continue
            for kind, (body, trimmed) in _from_cells(rows).items():
                if kind not in found:
                    found[kind] = (body, page, not trimmed)
            if len(found) == len(SECTIONS):
                break

        # 표로 안 잡힌 문서는 본문 절을 읽는다(원래 서술형인 문서가 있다).
        # 칸에서 읽어 놓은 규칙이 갈래 하나뿐이면 그것도 여기서 다시 찾는다
        # - 갈래 하나만 담은 규칙은 없느니만 못하다.
        if not _done(found):
            for sql, col in (("SELECT page, text FROM chunks WHERE doc_id = ? ORDER BY page", 1),
                             ("SELECT page, row_text FROM tables WHERE doc_id = ? ORDER BY page", 1)):
                seq = list(conn.execute(sql, (code,)))
                heads = _running_headers([r[col] for r in seq])
                clean = [_strip_furniture(r[col], heads) for r in seq]
                for i, row in enumerate(seq):
                    # 규칙이 청크 경계에서 잘리는 문서가 있다(KR5144420020
                    # 실측: "나. 오후 5시 경과"에서 끊기고 나머지 "후에
                    # 자금을 납입한 경우..."는 다음 청크에 있다). 조각만
                    # 담으면 "5시 넘겨서 넣으면?"에 답할 수 없다. 이어지는
                    # 조각까지 붙여 놓고 읽는다. 조각들이 서로 겹쳐 있어서
                    # 규칙이 두 번째 조각에서 시작할 수도 있으니 둘이 아니라
                    # 셋을 붙인다.
                    text = functools.reduce(_join, clean[i: i + CHUNK_WINDOW])
                    for kind, patterns in SECTIONS:
                        if (kind in found and _has_both_branches(found[kind][0])
                                and found[kind][2]):
                            continue
                        body, trimmed = _section(text, patterns)
                        if not body:
                            continue
                        if kind not in found or _has_both_branches(body):
                            found[kind] = (body, row[0], not trimmed)
                if _done(found):
                    break

        if not _done(found):
            pdfs = glob.glob(os.path.join(
                REPO_ROOT, "data", "products", code, "*.pdf"))
            for pdf_path in pdfs:
                try:
                    for kind, (body, page) in _from_pdf_columns(pdf_path).items():
                        if (kind in found and _has_both_branches(found[kind][0])
                                and found[kind][2]):
                            continue
                        if kind not in found or _has_both_branches(body):
                            # 이 경로는 _snap_to_clause_end를 안 거친다 -
                            # RE_RULE_SPAN 자체가 이미 "...매입"/"...지급"
                            # 으로 끝나는 대목만 골라 담는다.
                            found[kind] = (body, page, True)
                except Exception:
                    pass  # PDF를 못 읽으면 그냥 없는 대로 둔다
                if _done(found):
                    break

        for kind, (body, page, _clean) in found.items():
            out.append({
                "product_code": code,
                "kind": kind,
                "text": body,
                "page": page,
            })
    conn.close()
    _apply_known_fixes(out)
    return out


# PDF 원문을 직접 대조해서 확인한, 위 자동 추출로는 못 잡는 두 건.
# 전수 재검사가 아니라 이 두 상품만 직접 확인한 결과이므로 새로 추가할
# 때는 반드시 원문을 다시 대조한다.
_KNOWN_TRUNCATION_FIXES = {
    # KR5113420069 34쪽 실측: "...공고되는 수익증권의" 뒤에서 두 갈래
    # 다 "기준가격 적용"이 통째로 빠진 채 끊겼다(다음 갈래·각주로 바로
    # 이어짐). 표 칸이 줄바꿈된 자리의 낱말이 통째로 안 걸린 것으로
    # 보인다 - 자동 추출 로직을 고치는 대신, 원문을 직접 옮겨 넣는다.
    ("KR5113420069", "매입기준가"): (
        "1) 17시[오후5시] 이전에 자금을 납입한 경우 : 자금을 납입한 영업일로부터 "
        "2영업일(D+1)에 공고되는 수익증권의 기준가격 적용  "
        "2) 17시[오후5시] 경과 후에 자금을 납입한 경우 : 자금을 납입한 영업일로부터 "
        "3영업일(D+2)에 공고되는 수익증권의 기준가격 적용  "
        "※ 수익증권의 판매회사는 전산시스템에 따라 매입 또는 환매업무를 처리한 경우에는 "
        "거래전표에 표시된 시점을 매입 또는 환매를 청구한 시점으로 봅니다. 다만, 수익자의 "
        "개별적인 매입 또는 환매청구 없이 사전약정에 따라 주기적으로 수익증권의 매입 또는 "
        "환매업무가 처리되는 경우에는 매입 또는 환매의 기준시점 이전에 매입 또는 환매청구가 "
        "이루어진 것으로 봅니다. ※ 모투자신탁 수익증권의 매수 집합투자업자는 투자자가 이 "
        "투자신탁 수익증권의 취득을 위하여 판매회사에 자금을 납입한 경우 달리 운용하여야 할 "
        "특별한 사유가 없는 한 자금을 납입한 당일에 모투자신탁 수익증권의 매수를 청구하여야 "
        "합니다."
    ),
    # KR5156450026 26쪽 실측: 마지막 문단("환매대금은 투자신탁재산으로
    # ...")이 다음 쪽(27쪽)까지 이어지는데 그쪽은 모투자신탁 관련
    # 예외조항(환매기준가 규칙과 무관)이라 안 가져온다 - 대신 그
    # 문단 앞의, 환매기준가 규칙 자체는 이미 온전히 맺은 자리
    # ("...이루어진 것으로 봅니다.")에서 끊는다.
    ("KR5156450026", "환매기준가"): (
        "① 오후 3시30분(15시30분) 이전에 환매를 청구한 경우: 환매를 청구한 날로부터 "
        "제2영업일(D+1)에 공고되는 수익증권의 기준가격을 적용하여 제4영업일(D+3)에 "
        "관련세금 등을 공제한 후 환매대금을 지급합니다. "
        "② 오후 3시30분(15시30분) 경과 후에 환매를 청구한 경우: 환매를 청구한 날로부터 "
        "제3영업일(D+2)에 공고되는 수익증권의 기준가격을 적용하여 제4영업일(D+3)에 "
        "관련세금 등을 공제한 후 환매대금을 지급합니다.  "
        "※수익증권의 판매회사 전자시스템에 따라 매입 또는 환매업무를 처리한 경우에는 "
        "거래전표에 표시된 시점을 매입 또는 환매를 청구한 시점으로 봅니다. 다만, 수익자의 "
        "개별적인 매입 또는 환매청구 없이 사전약정에 따라 주기적으로 수익증권의 매입 또는 "
        "환매업무가 처리되는 경우에는 매입 또는 환매의 기준시점 이전에 매입 또는 환매청구가 "
        "이루어진 것으로 봅니다."
    ),
}


def _apply_known_fixes(rows):
    for r in rows:
        fix = _KNOWN_TRUNCATION_FIXES.get((r["product_code"], r["kind"]))
        if fix:
            r["text"] = fix


def report(rows):
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], set()).add(r["product_code"])
    print(f"규칙 {len(rows)}건")
    for kind, codes in sorted(by_kind.items()):
        print(f"  {kind}: {len(codes)}개 상품")
    both = set.intersection(*by_kind.values()) if len(by_kind) > 1 else set()
    print(f"  둘 다 있는 상품: {len(both)}개")
    for r in rows[:3]:
        print(f"\n  [{r['product_code']} {r['kind']} p{r['page']}]\n    {r['text'][:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = extract(args.db)
    report(rows)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
