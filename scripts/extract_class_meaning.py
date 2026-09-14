"""클래스 코드가 무슨 뜻인지 문서에서 뽑는다.

왜 필요한가
-----------
지금 답변은 "총보수(C클래스) 0.544%"처럼 클래스 코드를 그대로 내보낸다.
고객은 C가 뭔지 모른다. 그런데 클래스를 안 밝히면 답이 아예 틀린 게 되는데,
한 펀드 안에서 총보수가 중앙값 0.4%p, 최대 1.5%p(0.7% <-> 2.2%)까지 벌어지기
때문이다.

그렇다고 "대표 클래스"를 정해 둘 수도 없다. 상품 100개의 클래스 구성이
70가지로 제각각이라, 어떤 코드를 대표로 잡아도 최소 30개 상품에는 그 코드가
아예 없다.

무엇보다 코드의 뜻이 운용사마다 다르다:

    미래에셋   C-P  = 개인연금        C-P2 = 퇴직연금
    교보악사   CP   = 퇴직연금        C-P2 = 개인연금     (정반대)
    한국투자   C    = 개인연금        C-R  = 퇴직연금

코드로 뜻을 짐작하면 연금계좌에 못 담는 클래스를 담을 수 있다고 답하게 되고,
비교할 때도 개인연금과 보수체감을 나란히 놓고 "같은 클래스끼리 비교했다"고
표시하게 된다. 그래서 문서가 직접 적어 둔 이름표를 그대로 읽는다.

이름표는 투자설명서 앞부분 "집합투자기구의 명칭" 표에 있고, 형식이
`수수료방식-판매경로[-속성...]` 로 표준화되어 있다:

    수수료선취-오프라인(A)
    수수료미징구-온라인-개인연금(C-Pe)
    수수료미징구-오프라인-퇴직연금,기관(C-RF)
    종류C1(수수료미징구-오프라인-보수체감)      <- 순서가 뒤집힌 형식도 있다

이걸 뽑아 두면 세 가지가 한꺼번에 풀린다.
1. 코드 대신 말로 답할 수 있다("연금저축·창구 가입 기준").
2. 일반 고객이 못 사는 클래스(기관/고액/랩)를 답에서 뺄 수 있다. 교보악사
   Tomorrow장기우량은 제일 싼 A(0.1195%)가 고액자산가 전용이라, 이걸 모르면
   살 수도 없는 걸 "제일 싸다"고 안내하게 된다.
3. 비교를 코드가 아니라 뜻으로 맞출 수 있다.

실행:
    python3 scripts/extract_class_meaning.py
    python3 scripts/extract_class_meaning.py --check   # 커버리지만 확인
"""

import argparse
import collections
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")
OUTPUT_JSON = os.path.join(REPO_ROOT, "class_meaning.json")

FEE_TYPES = ("선취", "미징구", "후취")

# 판매경로. 긴 것부터 찾아야 "온라인슈퍼"가 "온라인"으로 잘리지 않는다.
# "온라인직접판매"(운용사 홈페이지 직판)와 "디폴트옵션"(사전지정운용제도
# 전용)도 판매경로 자리에 들어간다. 안 넣었더니 그 자리에 있는 클래스는
# 이름표를 통째로 못 읽었다 - KR5129420031의 J-e/J-Pe/J-RPe 세 개와
# KR5122420005의 O가 그랬다.
CHANNELS = ("온라인직접판매", "온라인슈퍼", "디폴트옵션", "오프라인", "온라인", "직판")

# 속성 중 "이 계좌라야 살 수 있다"를 뜻하는 것
ACCOUNT_TYPES = ("개인연금", "퇴직연금", "주택마련", "금전신탁")

# 일반 개인 고객이 살 수 없는 클래스를 가려내는 말. 이게 붙어 있으면
# 보수가 아무리 싸도 답변에서 빼야 한다(살 수가 없으므로).
# "펀드"/"펀드등"은 이 펀드에 투자하는 다른 펀드(모집합투자기구)용이라는
# 뜻이라 개인이 살 수 없다. 빼먹었더니 미래에셋솔로몬중장기국공채의
# C-PI(0.13%)가 일반 클래스로 잡혀, 형제 펀드 비교에서 혼자 3분의 1 값으로
# 나왔다. 가입자격 원문도 "…자집합투자기구로 두고 있는 모집합투자기구"라고
# 적고 있다.
RESTRICTED = ("기관", "고액", "랩", "펀드", "금전신탁", "임직원", "협회",
              # "고유재산" - 집합투자업자·계열금융회사가 펀드 안정 운용을
              # 위해 매입하는 클래스(KR5117420097 실측: "집합투자업자,
              # 집합투자업자의 계열금융회사 등이 매입하는 집합투자기구").
              # "집합투자업자(형)" - "당해 집합투자기구의 집합투자업자"
              # 전용(KR514X450008/KR5169950018 실측). 둘 다 일반 고객은
              # 애초에 살 수 없는 자리라 retail:false여야 하는데, 이
              # 목록에 없어서 일반 클래스로 잘못 잡히고 있었다.
              "고유재산", "집합투자업자")

# raw_label이 "기타"처럼 뜻 없는 낱말이라 RESTRICTED 키워드로는 못 잡는
# 제한 클래스. 실제 제한 근거는 class_charges.json의 가입자격 원문에만
# 있다(이 스크립트는 그 필드를 안 읽는다) - KR5125450023/Cy 실측:
# raw_label "수수료미징구-오프라인-기타"라 retail:true로 잘못 잡혔지만,
# 가입자격 원문은 "사원복지연기금(근로자의 복리후생을 위해 현직
# 임직원에 한하여 매월 지급되는 일정액) 수령자"로 일반 고객이 못 사는
# 클래스다. 전체 코퍼스에서 이 1건만 해당(다른 "기타" 클래스는 문제 없음).
_KNOWN_RETAIL_OVERRIDES = {
    ("KR5125450023", "Cy"): {
        "retail": False,
        "description": "사원복지연기금 수령자 전용 · 창구",
    },
}

# 같은 말이 "기관/기관형/기관등", "개인연금/개인연금형"처럼 조금씩 다르게
# 적혀 있어서 정확히 같은지로 보면 17개쯤을 놓친다. 놓치면 기관 전용
# 클래스가 "일반 가입 가능"으로 표시되므로 부분 일치로 본다.
#
# 그런데 부분 일치로 "찾기"만 하고 원문 글자는 그대로 저장했더니
# account_type 칸에 "개인연금"과 "개인연금형"이 같은 뜻인데 다른
# 문자열로 섞여 나왔다(KR5122420005 등 5개 상품, 24건 실측). 계좌
# 종류만 골라 "형"을 떼는 좁은 정규화를 둔다 - "무권유저비용형"처럼
# 진짜 다른 뜻인 속성까지 건드리면 안 되므로, ACCOUNT_TYPES에 "형"이
# 그대로 붙은 정확한 문자열일 때만 뗀다.
def _norm_account_attr(a):
    if a.endswith("형") and a[:-1] in ACCOUNT_TYPES:
        return a[:-1]
    return a


# 코드 자체의 괄호 속성("(연금저축)")이 계좌 종류를 말하는데, 이름표
# 문구(fee_type-channel-attrs)엔 그 속성이 안 붙는 문서가 있다
# (KR5129420025 실측: "C-Pu(연금저축)"의 이름표는 "수수료미징구-오프
# 라인-전환가능"뿐이라 attributes에 "개인연금"이 없다 - 그런데 8쪽은
# 이 클래스를 C-P(연금저축)/C-Pe(연금저축)와 나란히 "연금저축계좌의
# 세제" 대상으로 묶어 부른다). "연금저축"은 개인연금의 다른 말이다.
_CODE_ACCOUNT_SYNONYMS = {"연금저축": "개인연금"}


def _account_type_from_code(code):
    """코드 자체의 맨 뒤 괄호에서 계좌 종류를 읽는다. 이름표 문구에
    이미 있으면 이건 안 쓴다(_resolve와 겹쳐 부르는 쪽에서 가린다)."""
    m = re.search(r"\(([^()]*)\)\s*$", code or "")
    if not m:
        return None
    tail = m.group(1)
    for word in ACCOUNT_TYPES:
        if word in tail:
            return word
    for word, canon in _CODE_ACCOUNT_SYNONYMS.items():
        if word in tail:
            return canon
    return None


def _match_any(attr, words):
    return any(w in attr for w in words)

# 공백을 다 지운 뒤 찾는다. PDF에서 라벨이 줄바꿈으로 잘리기 때문이다
# ("수수료미징구-오프라인-보\n수체감"). 라벨 안에는 원래 띄어쓰기가 없다.
#
# 그런데 표에서는 라벨이 숫자 칸에 통째로 끊기기도 한다:
#     수수료선취-  납입금액의0.17%이내  0.1736 0.10 ...  오프라인(A)
# 그래서 "수수료방식-채널(코드)"를 한 덩어리로 보는 정규식으로는 못 잡는다.
# 수수료방식을 찾은 뒤 그 뒤쪽을 훑어 채널을 만나면 거기서 코드를 읽는다.
RE_FEE = re.compile(r"수수료(선취|미징구|후취)-")
# 판매경로 뒤에 "형"을 붙여 쓰는 문서가 있다("수수료미징구-오프라인형(C)").
# 이걸 안 넘기면 코드를 못 읽어 그 문서의 이름표를 통째로 놓친다.
RE_CHANNEL = re.compile(r"(" + "|".join(CHANNELS) + r")형?")

# 이름표를 붙임표가 아니라 띄어쓰기로 적은 문서가 있다.
#     C-E 수수료미징구 온라인 개인연금 BG326      (KR5174420011 10쪽)
# 공백을 다 지우면 "수수료미징구온라인개인연금"이 되어 RE_FEE가 요구하는
# 붙임표가 사라진다. 그래서 공백을 지우기 전에, 아는 낱말 사이의 공백만
# 붙임표로 바꾼다. 모르는 낱말 사이의 공백은 그대로 둔다 - 아무 공백이나
# 붙임표로 바꾸면 본문 문장이 이름표처럼 보이게 된다.
_CH_ALT = "|".join(CHANNELS)
_ATTR_ALT = "|".join(ACCOUNT_TYPES + RESTRICTED)
RE_SPACE_FEE_CH = re.compile(
    r"(수수료(?:선취|미징구|후취))\s+(?=(?:" + _CH_ALT + r"))")
RE_SPACE_CH_ATTR = re.compile(
    r"((?:" + _CH_ALT + r")형?)\s+(?=(?:" + _ATTR_ALT + r"))")

# 속성이 괄호에 들어간 형식: "수수료미징구-오프라인-퇴직연금(고액)".
# 이걸 속성으로 안 먹으면 그 뒤의 (코드)를 못 읽는다. 고액/기관 전용
# 클래스가 여기 몰려 있어서, 놓치면 일반 고객이 살 수 없는 클래스를
# 이름표 없이(=일반 가입 가능으로) 내보내게 된다.
#
# "직판"은 예외다 - 코드 자체가 "(직판)"으로 순수 한글인 문서가 있어서
# (KR5114420016 실측: "수수료미징구-직판(직판)"), 이 정규식이 먼저
# "(직판)"을 속성으로 삼켜 버리면 그 뒤에 남는 게 없어 코드 자리
# (RE_CODE_AFTER)까지 아예 못 간다. 속성 자리에 "직판"이 오는 문서는
# 없어서 빼도 안전하다.
RE_PAREN_ATTR = re.compile(r"^\((?!직판\))([가-힣][가-힣,]{0,9})\)")

# 이름표 앞에 코드가 "종류" 없이 그냥 놓인 형식:
#     C-Pi    수수료미징구-오프라인-퇴직연금(고액) EA921  (KR5118420006 9쪽)
#     S-P(퇴직) 수수료미징구-온라인슈퍼-퇴직연금 D9392     (KR5118201004 9쪽)
# 뒤엣것처럼 코드에 괄호 설명이 붙기도 한다. 같은 문서에 S-P(개인연금,
# 총보수 0.2757)와 S-P(퇴직)(0.2657)이 따로 있어서, 괄호를 안 읽으면
# 퇴직연금 클래스가 통째로 뜻 없는 코드가 된다.
# 이건 아무 글자나 코드로 읽을 위험이 있어서, 보수표에 실제로 있는
# 코드일 때만 받아들인다(known_codes).
RE_CODE_BEFORE_PLAIN = re.compile(
    r"(?:^|[^A-Za-z0-9가-힣])"
    r"([A-Za-z][A-Za-z0-9]{0,3}(?:-[A-Za-z0-9]{1,4})?(?:\([^()]{1,10}\))?)$")
# 속성은 1글자짜리도 있다("랩"). {2,8}로 잡았더니 "-랩,펀드등"이 통째로
# 떨어져 나가서, 랩 전용인 F클래스가 "일반 고객도 가입 가능"으로 표시됐다.
#
# 상한을 8로 뒀더니 이름표가 아니라 서술형 문장을 잘못 먹는 사고가 났다
# (KR5160420009 실측: "수수료미징구-온라인-판매회사의온라인(On-line)을
# 통하여 가입하며..."에서 "판매회사의온라인"이 구분자 없이 8자라 통째로
# "속성"으로 삼켜지고, 그 뒤 문장 속 영어 괄호 "(On-line)"을 클래스
# 코드로 잘못 읽었다 - "On-line"이라는 있지도 않은 클래스가 생겼다.
# 코퍼스 전체에서 진짜 속성 중 제일 긴 것이 7자("무권유저비용형",
# "사원복지연기금")라서, 8자였던 상한을 7로 낮추면 진짜 속성은 하나도
# 안 잃으면서 이 문장만 더 안 삼킨다(그러면 뒤 텍스트가 "("로 시작하지
# 않게 되어 코드 자리로 안 넘어간다).
RE_ATTRS = re.compile(r"^((?:[-,][가-힣]{1,7})*)")
# 채널 뒤에 바로 붙는 (코드) -> 라벨이 먼저 나오는 형식
#
# 코드 자체가 순수 한글(운용사 직판 채널)인 문서가 있다("수수료미징구-
# 직판(직판)", "수수료미징구-직판-기관(직판f)" - KR5114420016/027
# 실측). "직판"만 좁게 예외로 둔다 - 아무 한글이나 코드로 받으면
# 서술형 문장의 괄호를 코드로 오인하는 사고가 난다(RE_ATTRS 8→7 글자수
# 상한을 고친 이유와 같은 위험이라 폭을 넓히지 않는다).
RE_CODE_AFTER = re.compile(
    r"^\((직판[A-Za-z0-9]{0,3}|[A-Za-z][A-Za-z0-9\-]{0,12}(?:\([^)]{1,10}\))?)\)")
# 코드를 감싼 바깥 괄호가 안 닫힌 문서가 있다(KR5129420031 11쪽 실측:
# "...고액(Ci-RP(퇴직연금) E3898" - 안쪽 "(퇴직연금)"은 닫혔는데 바깥
# "(Ci-RP...)"의 닫는 괄호 자체가 원문에 없다. 같은 문서 바로 위 줄의
# "Cf-RP(퇴직연금))"은 정상적으로 닫혀 있어 이 한 곳만 결함이다).
# RE_CODE_AFTER는 바깥 닫는 괄호까지 요구해서 이 경우 못 잡으므로,
# 안쪽 괄호까지만 요구하는 느슨한 짝을 폴백으로 둔다.
RE_CODE_AFTER_UNCLOSED = re.compile(
    r"^\(([A-Za-z][A-Za-z0-9\-]{0,12}\([^)]{1,10}\))")
# 코드가 먼저 나오는 형식: "종류C1(수수료...오프라인)" / "A(수수료...오프라인)"
# 앞이 한글이면 코드가 아니라 펀드 이름의 꼬리다. 이걸 안 막았더니
# "..._직판f(수수료미징구-직판-기관)"이라는 자펀드 목록 줄에서 f를
# 클래스로 만들어 냈다(KR5114420027 60쪽). "종류"/"Class"는 코드 앞에
# 붙는 머리말이므로 예외로 둔다.
# 코드 뒤에 "형"을 붙이는 문서도 26개 있다("A형(수수료선취-오프라인)",
# "C-G형(...)" - 신영자산운용 계열 문서군 실측). 괄호 바로 앞에서만
# 허용하므로 임의의 한글 뒤 괄호를 코드로 오인할 위험은 없다.
#
# 코드는 항상 영문자로 시작한다(진짜 클래스 코드 중 숫자로 시작하는 게
# 코퍼스 전체에 하나도 없다). 첫 글자를 숫자까지 허용했더니, 테두리 없는
# 표 전체가 셀 하나로 뭉쳐진 문서에서 "2025/05/25C(수수료미징구..." 같은
# 날짜 뒤 숫자를 코드로 잘못 읽었다(KR5125450023 4쪽 실측: "25C"라는
# 있지도 않은 클래스가 생겼다 - RE_ATTRS 8→7, "On-line" 사고와 같은
# 종류의 위험이다).
RE_CODE_BEFORE = re.compile(
    r"(?:종류|Class|^|[^가-힣A-Za-z0-9])([A-Za-z][A-Za-z0-9\-]{0,12})형?\($")
# 괄호가 아예 없는 형식: "종류C2수수료미징구-오프라인-보수체감98295"
# (투자설명서 앞쪽 "집합투자기구의 명칭" 표가 이 모양이다)
# "종류직판F"(KR5153420105 실측)처럼 "직판"으로 시작하는 코드도 있다.
RE_CODE_BEFORE_BARE = re.compile(
    r"종류(직판[A-Za-z0-9]{0,3}|[A-Za-z][A-Za-z0-9\-]{0,12})$")
# 코드 앞에 붙는 머리말("종류C1", "Class S-R")
RE_CODE_PREFIX = re.compile(r"^(?:종류|Class)")

# 수수료방식과 채널 사이에 끼어들 수 있는 글자 수. 표에서 숫자 칸이
# 통째로 들어오므로 넉넉히 잡되, 다음 클래스 라벨까지 넘어가지 않게 제한한다.
_GAP = 150


def _squash(text):
    # 붙임표를 일반 하이픈(-, U+002D) 대신 en dash/em dash/마이너스 기호로
    # 쓰는 문서가 3개 있다("수수료미징구 –\n온라인형(Ce)" - KR514X450008
    # 실측). RE_FEE 등 이 파일의 모든 정규식이 일반 하이픈만 찾으므로,
    # 안 바꾸면 그 줄의 이름표를 통째로 못 읽는다("Ae"/"Ce"가 aka로도
    # 안 남고 사라졌었다).
    t = (text or "").translate(str.maketrans("–—−", "---"))
    t = RE_SPACE_FEE_CH.sub(r"\1-", t)
    t = RE_SPACE_CH_ATTR.sub(r"\1-", t)
    return re.sub(r"\s+", "", t)


def _split_label(body):
    """'오프라인-퇴직연금,기관' -> ('오프라인', ['퇴직연금', '기관'])"""
    channel = None
    for ch in CHANNELS:
        if body.startswith(ch):
            channel = ch
            body = body[len(ch):]
            break
    rest = [p for p in re.split(r"[-,]", body) if p]
    return channel, rest


def _parse(text, known_codes=()):
    """한 덩어리 글에서 (코드, 수수료방식, 판매경로, 속성들)을 모두 찾는다.

    known_codes는 그 상품의 보수표에 실제로 있는 클래스 코드다. 코드가
    "종류" 같은 머리말 없이 이름표 앞에 그냥 놓인 형식을 읽을 때만 쓴다 -
    그 형식은 아무 글자나 코드로 오인할 수 있어서, 보수표가 이미 아는
    코드일 때만 받아들인다."""
    t = _squash(text)
    found = {}

    for fee in RE_FEE.finditer(t):
        fee_type = fee.group(1)
        tail = t[fee.end(): fee.end() + _GAP]
        ch = RE_CHANNEL.search(tail)
        if not ch:
            continue
        channel = ch.group(1)
        after = tail[ch.end():]
        attrs_raw = RE_ATTRS.match(after).group(1)
        rest = after[len(attrs_raw):]
        # 괄호에 든 속성을 마저 먹는다("-퇴직연금(고액)"). 먹고 나면
        # 그 뒤에 (코드)가 이어진다.
        paren_attrs = []
        while True:
            pa = RE_PAREN_ATTR.match(rest)
            if not pa:
                break
            paren_attrs.append(pa.group(1))
            rest = rest[pa.end():]

        code = None
        m = RE_CODE_AFTER.match(rest) or RE_CODE_AFTER_UNCLOSED.match(rest)
        if m:
            code = m.group(1)
        elif rest.startswith(")"):
            # 코드가 앞에 있는 형식: "종류C1(수수료미징구-오프라인-보수체감)"
            m = RE_CODE_BEFORE.search(t[: fee.start()])
            if m:
                code = m.group(1)
        else:
            # 괄호 없는 형식: "종류C2수수료미징구-오프라인-보수체감"
            m = RE_CODE_BEFORE_BARE.search(t[: fee.start()])
            if m:
                code = m.group(1)
            elif known_codes:
                # 머리말 없이 코드만 앞에 놓인 형식. 보수표가 아는
                # 코드일 때만 받는다.
                m = RE_CODE_BEFORE_PLAIN.search(t[: fee.start()])
                if m and m.group(1) in known_codes:
                    code = m.group(1)
        if not code:
            continue
        # 정규식은 왼쪽부터 맞추므로 "ClassS-R("에서 머리말까지 통째로
        # 코드로 집는다. 머리말은 코드가 아니니 떼어 낸다.
        code = RE_CODE_PREFIX.sub("", code).strip("-")
        if not code or code in found:
            continue

        attrs = [_norm_account_attr(p) for p in re.split(r"[-,]", attrs_raw) if p]
        attrs.extend(_norm_account_attr(a) for pa in paren_attrs for a in re.split(r"[,]", pa) if a)
        body = channel + attrs_raw + "".join(f"({a})" for a in paren_attrs)
        found[code] = {
            "class_code": code,
            "fee_type": fee_type,
            "channel": channel,
            "attributes": attrs,
            "raw_label": f"수수료{fee_type}-{body}",
        }
    return found


# 코드만 든 칸("C", "C-Pe", "종류A")을 알아보기 위한 모양. 코드 뒤에
# 괄호 속성이 붙는 칸도 있다("S-P(퇴직)" - KR5118420062 8쪽 실측: 코드
# 칸과 라벨 칸이 나뉜 표에서, 괄호를 안 받아 주니 "S-P(퇴직)"이 통째로
# 코드칸 매칭에서 빠져 그 클래스 자체가 사라졌다).
RE_BARE_CODE = re.compile(
    r"^(?:종류)?([A-Za-z][A-Za-z0-9가-힣\-]{0,12}(?:\([^()]{1,10}\))?)$")


def _parse_label_only(text):
    """코드 없이 이름표만 든 칸을 읽는다("수수료미징구-오프라인- 기관")."""
    t = _squash(text)
    fee = RE_FEE.search(t)
    if not fee:
        return None
    ch = RE_CHANNEL.search(t[fee.end(): fee.end() + _GAP])
    if not ch or ch.start() != 0:
        return None
    channel = ch.group(1)
    after = t[fee.end() + ch.end():]
    attrs_raw = RE_ATTRS.match(after).group(1)
    rest = after[len(attrs_raw):]
    paren_attrs = []
    while True:
        pa = RE_PAREN_ATTR.match(rest)
        if not pa:
            break
        paren_attrs.append(pa.group(1))
        rest = rest[pa.end():]
    # 이름표 칸이면 뒤에 다른 말이 붙지 않는다. 붙어 있으면 다른 칸이
    # 섞인 것이므로 쓰지 않는다.
    if rest.strip():
        return None
    attrs = [p for p in re.split(r"[-,]", attrs_raw) if p]
    attrs.extend(a for pa in paren_attrs for a in re.split(r"[,]", pa) if a)
    body = channel + attrs_raw + "".join(f"({a})" for a in paren_attrs)
    return {
        "fee_type": fee.group(1),
        "channel": channel,
        "attributes": attrs,
        "raw_label": f"수수료{fee.group(1)}-{body}",
    }


def _parse_row(cells, known_codes=()):
    """한 줄의 칸들에서 이름표를 읽는다.

    줄을 통째로 이어 붙이면 안 된다. 명칭표가 한 줄에 (코드, 라벨,
    펀드코드) 쌍을 두 벌씩 싣는 문서가 있어서, 앞 쌍의 펀드코드가 뒤
    쌍의 클래스 코드에 들러붙는다("...오프라인A8183S-P..." -> 코드를
    "A8183S-P"로 읽음). 칸 짝으로 읽어야 정확하다."""
    out = {}
    cells = [(c or "") for c in cells]
    for i, cell in enumerate(cells):
        # 칸 하나에 라벨과 코드가 다 든 경우
        for cc, rec in _parse(cell, known_codes).items():
            out.setdefault(cc, rec)
        # 라벨 칸 + 바로 앞 코드 칸
        label = _parse_label_only(cell)
        if not label or i == 0:
            continue
        for j in range(i - 1, -1, -1):
            prev = _squash(cells[j])
            if not prev:
                continue
            m = RE_BARE_CODE.match(prev)
            if m:
                code = m.group(1).strip("-")
                if code:
                    out.setdefault(code, dict(label, class_code=code))
            break
    return out


def _describe(rec):
    """고객에게 보여 줄 말. 코드 대신 이걸 쓴다."""
    account = next((a for a in rec["attributes"] if _match_any(a, ACCOUNT_TYPES)), None)
    channel = {"오프라인": "창구", "온라인": "온라인",
               "온라인슈퍼": "온라인슈퍼", "직판": "운용사 직판",
               "온라인직접판매": "운용사 직판(온라인)",
               "디폴트옵션": "디폴트옵션(사전지정운용)"}[rec["channel"]]
    if account and "개인연금" in account:
        account = "연금저축"
    elif account and "퇴직연금" in account:
        account = "퇴직연금(DC/IRP)"
    parts = [p for p in (account, channel) if p]
    # 계좌 종류도 제한도 아닌 속성(보수체감/무권유저비용 등)도 붙인다.
    # 안 붙이면 C1~C4가 전부 "창구"로 똑같이 보여서, 보수가 왜 다른지
    # 알 수 없는 답이 된다.
    others = [a for a in rec["attributes"]
              if not _match_any(a, ACCOUNT_TYPES) and not _match_any(a, RESTRICTED)
              and a != rec["channel"]]
    parts.extend(others)
    limits = [a for a in rec["attributes"] if _match_any(a, RESTRICTED)]
    if limits:
        parts.append("/".join(limits) + " 전용")
    return " · ".join(parts)


# 가입자격 원문에서 "이 사람만 살 수 있다"를 분명히 말하는 구절.
# 애매한 말은 일부러 넣지 않았다 - 여기 걸리면 "전용"이라고 단정하게
# 되므로, 틀리느니 아무 말도 안 하는 편이 낫다.
#
# 짧은 속성 토큰(2번째 자리)도 같이 둔다 - 예전엔 description(긴 문구)만
# 만들고 attributes는 빈 채로 남겨서, 이 자리로 채워지는 클래스(이름표가
# 아예 없는 클래스)는 구조화된 속성이 하나도 없었다(KR5153420063 C-F/I
# 실측). RESTRICTED가 쓰는 짧은 낱말과 같은 어휘를 쓴다.
ELIGIBILITY_ONLY = (
    # "집합투자업자가 판매회사로서 직접 판매하는 수익증권" - 운용사가
    # 중개 판매사 없이 직접 파는 클래스(KR5153420063 C-F 실측).
    ("직접판매", "직판", "운용사 직판 전용"),
    ("랩어카운트", "랩", "랩·일임 계좌 전용"),
    ("일임·자문", "랩", "랩·일임 계좌 전용"),
    ("금전신탁", "금전신탁", "금전신탁 전용"),
    ("전문투자자", "전문투자자등", "전문투자자 전용"),
    ("집합투자기구", "펀드", "펀드(모집합투자기구) 전용"),
    ("고액", "고액", "고액 전용"),
)
RE_NO_LIMIT = re.compile(r"제한\s*없[음습]")


def _canon_key(code):
    """붙임표·괄호·대소문자만 지운 열쇠(merge_class_spelling.py와 같은 것).
    "같은 클래스다"가 아니라 "따져 볼 후보다"라는 뜻일 뿐이다."""
    return re.sub(r"\(.*?\)", "", code or "").replace("-", "").upper()


def _channel_from_eligibility(conn, product_code, code):
    """가입자격 원문에서 기본 판매경로(오프라인/온라인) 단어를 읽는다.
    "온라인슈퍼"/"온라인직접판매"처럼 "온라인"을 부분 문자열로 포함하는
    복합 채널과 안 헷갈리려고, 여기서는 기본 두 채널만 본다 - 이 함수를
    부르는 쪽도 라벨 채널이 이 둘 중 하나일 때만 부른다(호출부 참고).
    단어가 정확히 하나만 나올 때만 돌려준다 - 여럿이거나 하나도 없으면
    라벨 쪽 채널을 못 믿을 근거가 없다는 뜻이라 손대지 않는다(None)."""
    try:
        row = conn.execute(
            "SELECT eligibility FROM class_charges "
            "WHERE product_code = ? AND class_code = ?",
            (product_code, code)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    flat = re.sub(r"\s+", "", row[0])
    found = {ch for ch in ("오프라인", "온라인") if ch in flat}
    return next(iter(found)) if len(found) == 1 else None


def _meaning_from_eligibility(conn, product_code, code):
    """이름표가 없는 클래스를, 가입자격 원문만으로 설명한다.

    문서가 종류형 명칭을 아예 안 붙이고 "-"(없음)로 적어 두는 경우가 있다
    (KR5153420063 8쪽: 종류C-F와 종류I만 명칭 칸이 "-"다). 뽑기에 실패한
    게 아니라 문서에 없는 것이라, 이름표 쪽에서는 더 할 수 있는 게 없다.

    그런데 같은 문서 23쪽 가입자격표는 이 둘이 각각 랩·일임·금전신탁
    전용과 기관·전문투자자 전용이라고 분명히 적고 있다. 이름표가 없다고
    "가입 조건 모름"으로 두면 일반 고객에게 못 사는 클래스를 섞어 보이게
    되므로, 가입자격이 분명히 말하는 것만 받아 적는다.

    분명하지 않으면 아무것도 돌려주지 않는다 - 모른다고 두는 편이 낫다."""
    try:
        row = conn.execute(
            "SELECT eligibility, page FROM class_charges "
            "WHERE product_code = ? AND class_code = ?",
            (product_code, code)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    text, page = row[0], row[1]
    flat = re.sub(r"\s+", "", text)

    limits, attrs = [], []
    for word, attr, name in ELIGIBILITY_ONLY:
        if word in flat:
            if name not in limits:
                limits.append(name)
            if attr not in attrs:
                attrs.append(attr)
    if limits:
        return {
            "product_code": product_code, "class_code": code,
            "fee_type": None, "channel": None, "account_type": None,
            "attributes": attrs, "retail": False, "aka": [],
            "description": " · ".join(limits),
            "raw_label": f"종류형 명칭 없음(문서 표기 \"-\"), 가입자격: {text}",
            "page": page,
        }
    if RE_NO_LIMIT.search(flat):
        return {
            "product_code": product_code, "class_code": code,
            "fee_type": None, "channel": None, "account_type": None,
            "attributes": [], "retail": True, "aka": [],
            "description": "가입 제한 없음",
            "raw_label": f"종류형 명칭 없음(문서 표기 \"-\"), 가입자격: {text}",
            "page": page,
        }
    return None


# 「집합투자기구의 명칭(종류형 명칭)」 표가 자기 머리에 적어 둔 이름.
# 코드 개수가 같은 표가 여럿일 때만 쓴다(_Vote 참고).
NAMING_TABLE_TITLES = ("집합투자기구의명칭", "종류형명칭", "집합투자기구명칭")


# 표를 그릴 때 칸 안의 줄바꿈까지 별도 "행"으로 쪼개는 문서가 있다
# (KR5127420034 21쪽 실측: 논리적으로 한 행("C-퇴직연금" / "수수료미징구-
# 오프라인-퇴직연금" / "퇴직연금 가입자...")인데, 셀 안에서 줄이 바뀐
# 지점("금" / "연금")이 다음 '행'으로 떨어져 나와 코드가 "C-퇴직연",
# 속성이 "퇴직"으로 통째로 잘려 보였다 - 문서 결함이 아니라 표 읽기
# 쪽 문제였다). 이런 조각 행은 칸 전부가 한글 1~3자뿐이라, 정상 행과
# 안 헷갈리고 가려낼 수 있다.
RE_WRAP_FRAGMENT = re.compile(r"^[가-힣]{1,3}$")


def _looks_like_wrap_continuation(row):
    """이 행이 새 행이 아니라 윗 행 칸 글자의 줄바꿈 조각인가.

    칸 전부(빈 칸 제외)가 한글 1~3자뿐이어야 한다 - 숫자·영문·붙임표가
    하나라도 섞이면 진짜 행(수익률 숫자 행 등)일 수 있으므로 손대지
    않는다. "직판"은 그 자체로 진짜 코드라 예외로 둔다."""
    cells = [(c or "").strip() for c in row]
    non_empty = [c for c in cells if c]
    if not non_empty:
        return False
    for c in non_empty:
        if c == "직판" or not RE_WRAP_FRAGMENT.match(c):
            return False
    return True


def _stitch_wrapped_rows(rows):
    """조각 행을 윗 행에 칸별로 이어 붙인다(사이에 아무 글자도 안 넣는다 -
    줄바꿈이 곧 이어 붙일 자리이므로)."""
    out = []
    for row in rows:
        if out and _looks_like_wrap_continuation(row):
            prev = out[-1]
            width = max(len(prev), len(row))
            out[-1] = [
                (prev[i] or "" if i < len(prev) else "") +
                (row[i] or "" if i < len(row) else "")
                for i in range(width)
            ]
            continue
        out.append(list(row))
    return out


# 본문(표가 아닌 문장) 줄바꿈이 코드 중간(붙임표 뒤)에서 끊기면 뒷 줄만
# 봐서는 코드가 마지막 글자 하나로 잘못 읽힌다(KR5118420062 34쪽 실측:
# "...예상투자기간이 해당 시점 이전이라면 종류C-" 줄 다음 "e(수수료
# 미징구-온라인)에 가입하시는..." 줄 - 뒷 줄만 보면 코드가 "e"가 된다.
# 진짜 온라인 미징구 클래스는 "C-e"로 이미 따로 있다). 앞 줄이
# "종류CODE-"로 끝나고 뒷 줄이 코드 조각처럼 보이는 글자로 시작하면
# 이어 붙인다.
RE_LINE_CODE_TAIL = re.compile(r"종류([A-Za-z][A-Za-z0-9]{0,3})-$")
RE_LINE_CODE_HEAD = re.compile(r"^[A-Za-z0-9]{1,3}\(")


def _stitch_wrapped_lines(lines):
    out = []
    for line in lines:
        if out and RE_LINE_CODE_TAIL.search(out[-1].rstrip()) \
                and RE_LINE_CODE_HEAD.match(line.lstrip()):
            out[-1] = out[-1].rstrip() + line.lstrip()
            continue
        out.append(line)
    return out


def _tables_in_order(conn, doc_id):
    """이 문서의 표를 쪽 번호 순으로. ORDER BY가 없으면 DB가 돌려주는
    대로라, 근거 페이지가 다시 쌓을 때마다 달라질 수 있었다."""
    out = []
    for page, dj in conn.execute(
            "SELECT page, data_json FROM tables WHERE doc_id = ? ORDER BY page",
            (doc_id,)):
        try:
            rows = json.loads(dj)
        except (ValueError, TypeError):
            continue
        out.append((page, _stitch_wrapped_rows(rows)))
    return out


def _meaning_key(rec):
    return (rec["fee_type"], rec["channel"], tuple(rec["attributes"]))


class _Vote:
    """한 클래스의 이름표를 문서 전체에서 모아, 제일 많이 나온 것을 고른다.

    왜 "먼저 나온 것"이면 안 되나
    ----------------------------
    같은 클래스의 이름표가 한 문서에 열 번 넘게 나오는데, 그중 몇 곳이
    틀려 있는 문서가 있다(KR5194450018 실측).

        수수료미징구-온라인슈퍼-개인연금(S-P)   ... 12곳
        수수료미징구-오프라인-퇴직연금(S-P)     ...  2곳(6쪽 수익률표)

    6쪽 수익률표는 앞 행의 이름표가 다음 행까지 그대로 이어져 있다.
    예전 코드는 "먼저 찾은 것"을 썼는데, 맞는 쪽을 집은 건 판단이 아니라
    DB가 돌려준 순서 덕이었다. 쪽 순서로 바꾸든 표 제목으로 바꾸든,
    "어느 하나를 먼저 본다"로는 이런 걸 못 가른다.

    문서가 되풀이해서 말하는 쪽을 따른다. 표에서 한 번이라도 봤으면
    표에서만 센다 - 본문은 줄이 섞일 여지가 있어서 표가 있는데 굳이
    섞을 이유가 없다. 같은 수로 갈리면 앞쪽 페이지를 쓴다.

    근거 페이지는 「집합투자기구의 명칭(종류형 명칭)」 표가 있는 쪽으로
    단다. 그 표가 이 문서가 클래스를 정의한 자리이기 때문이다. 요약
    투자비용표에도 이름표가 곁다리로 적혀 있지만, 거기를 근거로 대면
    "이 클래스가 무슨 뜻이냐"의 출처로는 약하다.

    명칭표를 알아보는 건 제목이 아니라 구조로 한다. 제목으로 해 봤더니
    투자자유의사항 문구 안에 "집합투자기구의 명칭"이 들어 있는 표가
    명칭표로 잡혔고, 하필 그게 이름표가 틀린 수익률표였다. 명칭표는
    정의상 그 펀드의 클래스를 다 싣기 때문에, 클래스 코드를 제일 많이
    담은 표가 명칭표다(KR510902511M 실측: 명칭표 14개 / 요약표 6개).

    같은 수인 표가 여럿이면 그때만 제목을 곁들인다. 이때는 안전한데,
    후보가 이미 "클래스 코드를 최대로 담은 표"로 좁혀져 있어서 코드를
    하나도 안 담은 안내문 표는 아예 후보가 못 되기 때문이다. 제목도
    없으면 앞쪽 페이지를 쓴다.

    단, 이긴 이름표가 실제로 적혀 있는 쪽만 후보다. 다른 뜻으로 적힌
    페이지를 근거로 달면 고객이 열어 봤을 때 우리 답과 다르다."""

    def __init__(self):
        self.table = collections.Counter()
        self.chunk = collections.Counter()
        self.rec = {}
        self.page = {}
        self.rank = {}
        self.order = {}
        self._n = 0
        self.ever_titled = False

    def add(self, rec, page, from_table, n_codes=0, titled=False):
        key = _meaning_key(rec)
        (self.table if from_table else self.chunk)[key] += 1
        if titled:
            self.ever_titled = True
        rank = (n_codes, titled, -page)
        if key not in self.rec:
            self.rec[key] = rec
            self.page[key] = page
            self.rank[key] = rank
            self.order[key] = self._n
            self._n += 1
        elif rank > self.rank[key]:
            self.page[key] = page
            self.rank[key] = rank

    def candidates(self):
        counts = self.table or self.chunk
        return _fold_truncations(counts) if counts else counts

    def pick(self, counts):
        best = max(counts, key=lambda k: (counts[k], -self.page[k],
                                          -self.order[k]))
        return self.rec[best], self.page[best]


def _fold_spelling(merged, pages, known_codes):
    """같은 뜻인데 표기만 다른 이름표를 한 줄로 모은다.

    문서가 같은 클래스를 표마다 다르게 적는다(KR5110501016 실측).

        3쪽 요약표   수수료선취-온라인(A-e)
        9쪽 명칭표   수수료선취-온라인(Ae)

    둘 다 담으면 15개짜리 펀드가 16개로 보인다. 실제로 이 문서에서
    그렇게 세고 있었다.

    합치는 조건은 하나다: 뜻(수수료방식·판매경로·속성)이 완전히 같을 것.
    붙임표만 다른데 뜻이 다른 짝이 코퍼스에 10쌍 실재하므로
    (C-P=개인연금 / Cp(퇴직연금)=퇴직연금) 표기만 보고 합치면 안 된다.

    남길 표기는 보수표가 쓰는 것이다. 다른 표들이 전부 그 표기로 이어
    붙기 때문이다. 보수표에 둘 다 없으면 앞쪽 페이지 것을 남긴다.
    지운 표기는 버리지 않고 aka로 남겨 둔다."""
    groups = collections.defaultdict(list)
    for cc in merged:
        groups[_canon_key(cc)].append(cc)
    aka = {}

    def _take(codes):
        if len(codes) < 2:
            return
        if len({_meaning_key(merged[c]) for c in codes}) != 1:
            return  # 뜻이 다르면 손대지 않는다
        keep = next((c for c in sorted(codes) if c in known_codes), None)
        if keep is None:
            keep = min(sorted(codes), key=lambda c: pages[c])
        for c in codes:
            if c != keep:
                aka.setdefault(keep, []).append(c)
                del merged[c]
                del pages[c]

    for codes in list(groups.values()):
        _take(codes)

    # 글자 순서만 뒤바뀐 표기도 있다(KR5111450067 실측). 11쪽 명칭표는
    # "C-P2E", 43쪽 비용예시표는 "C-PE2"라고 적는다 - 붙임표를 지운
    # 글자가 같은데(P,2,E) 순서만 다르다. _canon_key로는 안 걸려서
    # (다른 문자열이라 다른 그룹이 됨) 정렬한 글자로 한 번 더 묶는다.
    # 위와 똑같이 뜻이 완전히 같을 때만 합친다 - 자릿수가 같은 두
    # 코드가 우연히 애너그램이 될 확률은 낮고, 그마저도 뜻이 다르면
    # 안 건드리므로 위험이 크지 않다.
    remaining = collections.defaultdict(list)
    for cc in merged:
        remaining["".join(sorted(_canon_key(cc)))].append(cc)
    for codes in remaining.values():
        _take(codes)

    # 코드와 뜻이 함께 잘린 표기도 있다 - 문서 자체의 글자 결락이다(실측:
    # KR5127420034/039 21쪽. "퇴직" 뒤 "금" 글자가 PDF 콘텐츠 스트림에
    # 아예 없다 - 지운 게 아니라 원본에 없다). 그러면 코드도
    # "C-퇴직연"(정확히는 "C-퇴직연금"이어야 함), 속성도 "퇴직"(정확히는
    # "퇴직연금")으로 코드·뜻이 같이 잘린다. 뜻만 보고 합치면 "C-P"(개인
    # 연금)와 "C-P2"(코퍼스에 실재하는 별도 클래스)처럼 진짜 다른 클래스를
    # 잘못 묶을 위험이 있으므로, 코드 문자열 자체도 canon_key 접두 관계일
    # 때만 합친다 - "짧은 코드의 뜻이 긴 코드 뜻의 앞부분과 같고, 짧은
    # 코드 글자 자체도 긴 코드 글자의 앞부분과 같다"는 두 조건이 함께
    # 성립하는 경우는 "글자가 잘렸다"는 가설 말고는 달리 설명하기 어렵다.
    for c_short in list(merged):
        if c_short not in merged:
            continue
        ck_short = _canon_key(c_short)
        for c_full in list(merged):
            if c_full == c_short or c_full not in merged or c_short not in merged:
                continue
            ck_full = _canon_key(c_full)
            if ck_full == ck_short or not ck_full.startswith(ck_short):
                continue
            if not _is_truncation_of(_meaning_key(merged[c_short]), _meaning_key(merged[c_full])):
                continue
            aka.setdefault(c_full, []).append(c_short)
            del merged[c_short]
            del pages[c_short]
            break

    # 코드 앞쪽 글자(클래스 계열 접두, "C-" 등)가 표 하나에서만 통째로
    # 빠지는 경우도 있다 - 뜻(수수료방식-판매경로-속성)은 완전히 같은데
    # 코드만 짧다(KR5160420009 41쪽 실측: 수익률표에서만 "C-P2e"가
    # "P2e"로, "C-W"가 "W"로 적혀 있다. 이 문서의 다른 모든 표는 전부
    # "C-" 접두를 그대로 쓴다).
    #
    # 뜻이 같다는 것만으로는 부족하다 - 파서가 못 잡아낸 속성 차이 때문에
    # 뜻(fee_type·channel·attributes)이 같게 읽혀도 실제로는 다른 클래스인
    # 경우가 있다(KR5153420105 실측: "I"와 "C-PI"가 파싱상 뜻은 똑같이
    # "미징구-오프라인-기관"이지만, 보수표엔 총보수 0.19%/0.17%로 각각
    # 따로 실려 있는 별개 클래스다). 그래서 둘 다 보수표(known_codes)에
    # 독자적으로 등재돼 있으면 - 보수표가 이미 "둘은 별개"라고 말하는
    # 것이므로 - 합치지 않는다. 한쪽만 등재돼 있을 때만(등재 안 된 쪽이
    # 어느 한 표에서만 접두가 빠진 조각일 가능성이 높을 때만) 합친다.
    for c_short in list(merged):
        if c_short not in merged:
            continue
        ck_short = _canon_key(c_short)
        for c_full in list(merged):
            if c_full == c_short or c_full not in merged or c_short not in merged:
                continue
            ck_full = _canon_key(c_full)
            if ck_full == ck_short or not ck_full.endswith(ck_short):
                continue
            if c_short in known_codes and c_full in known_codes:
                continue
            _take([c_short, c_full])
            break

    return aka


def _resolve(votes):
    """{코드: _Vote} -> {코드: (이름표, 근거쪽)}

    남는 자리가 하나 있다. 문서가 두 클래스에 똑같은 이름표를 붙여 둔
    경우다(KR5194450018 실측).

        명칭표(9쪽)  수수료선취-오프라인(A)    AY120
                    수수료선취-오프라인(A-e)  AY121   <- 같은 이름
        보수표(5쪽)  수수료선취-오프라인(A)    선취 1%
                    수수료선취-온라인(A-e)     선취 0.5%

    종류형 명칭은 클래스를 가르라고 있는 것이니 명칭표 쪽이 틀렸다.
    그래서 "겹치는 이름표는 빼고 고른다"를 넣어 봤는데, 그건 못 쓴다.
    이름표가 정당하게 겹치는 문서가 있기 때문이다 - 같은 문서의
    C1/C2/C3/C4가 넷 다 "수수료미징구-오프라인-보수체감"이다(보유기간
    으로 갈리는 클래스라 이름이 같은 게 맞다). 그 규칙을 넣었더니
    C1에서 6표짜리 맞는 이름표를 빼고 1표짜리 틀린 것을 골랐고,
    KR5160420009 A-e에서는 "수수료미징구-온라인-수수료선취"라는 말도
    안 되는 이름표를 골랐다.

    그래서 겹침은 손대지 않는다. 문서가 자기 안에서 어긋난 자리는
    되풀이해서 말하는 쪽을 따르고 그 근거 페이지를 단다 - 어느 쪽이
    맞는지 우리가 정하는 게 아니라, 문서가 뭐라고 했는지를 확인할 수
    있게 두는 것이다. 위 A-e는 이 규칙에서 9쪽(오프라인)으로 간다."""
    out = {}
    for cc, v in votes.items():
        counts = v.candidates()
        if counts:
            out[cc] = v.pick(counts)
    return out


def _is_truncation_of(short, full):
    """short가 full의 잘린 조각인가.

    PDF에서 이름표가 줄바꿈이나 칸 경계에서 잘리면 뒷부분이 날아간다.

        "수수료미징구-온라인슈퍼(S-P)"  ->  "…온라인(S-P)"      (KR516702010M 23쪽)
        "…오프라인-개인연금(C-P)"       ->  "…오프라인-개인(C-P)" (KR515302022M 31쪽)

    이건 다른 뜻이 아니라 같은 말의 조각이라 따로 세면 안 된다. 실제로
    "온라인"이 "온라인슈퍼"를 표 수로 이겨서 온라인슈퍼 전용 클래스가
    그냥 온라인으로 바뀌는 일이 있었다.

    반대로 속성이 하나 더 붙은 건(온라인 vs 온라인-보수체감) 조각이
    아니라 다른 뜻이다 - 옆줄에서 새어 들어온 것일 수도 있으므로
    합치지 않고 표 수로 겨루게 둔다."""
    s_fee, s_ch, s_attrs = short
    f_fee, f_ch, f_attrs = full
    if s_fee != f_fee or len(s_attrs) != len(f_attrs):
        return False
    if not f_ch.startswith(s_ch):
        return False
    if not all(f.startswith(s) for s, f in zip(s_attrs, f_attrs)):
        return False
    return short != full


def _fold_truncations(counts):
    """잘린 조각의 표를 원래 말 쪽으로 옮긴다. 원래 말 후보가 둘 이상이면
    어느 쪽인지 모르므로 손대지 않는다."""
    out = collections.Counter(counts)
    for short in list(counts):
        fulls = [f for f in counts if _is_truncation_of(short, f)]
        if len(fulls) == 1:
            out[fulls[0]] += out.pop(short, 0)
    return out


def _single_class_fund(conn, product_code, known_codes):
    """종류형이 아닌 펀드인가. 맞으면 그렇다고 적은 한 줄을 돌려준다.

    상품 100개 중 1개(KR5123365001)는 「종류형 명칭」 표가 아예 없다.
    이름표를 못 뽑은 게 아니라 나눌 클래스가 없는 펀드라서다 - 7쪽
    "특수형태 표시"에 모자형만 있고 종류형이 없고, 문서 어디에도
    "종류형"이라는 말이 나오지 않는다. 그래서 보수 요약표의 클래스 칸에도
    코드 대신 "투자신탁"이라고 적혀 있다.

    이걸 "뜻을 모르는 클래스"로 두면 답변이 "이 클래스가 무엇인지 문서에
    없습니다"라고 나가는데, 사실은 그 반대다 - 클래스를 고를 필요가 없는
    펀드이고, 보수는 하나뿐이다. 그대로 말해 주는 편이 맞다.

    "종류형"이라는 낱말이 문서에 한 번도 안 나오는 것으로 가른다. 100개 중
    이 조건에 걸리는 문서는 이 하나뿐이고, 나머지 99개는 모두 여러 번
    나온다 - 경계가 애매하지 않다."""
    if len(known_codes) != 1:
        return None
    seen, form_page, form_line = False, None, None
    for page, text in conn.execute(
            "SELECT page, text FROM chunks WHERE doc_id = ? ORDER BY page",
            (product_code,)):
        for line in (text or "").splitlines():
            if "종류형" in line:
                seen = True
            if form_line is None and "집합투자기구의 종류 및 형태" in line \
                    and "투자신탁" in line:
                form_page, form_line = page, " ".join(line.split())
    if seen:
        return None
    return {
        "product_code": product_code,
        "class_code": next(iter(known_codes)),
        "fee_type": None,
        "channel": None,
        "account_type": None,
        "attributes": [],
        "retail": True,
        "aka": [],
        "description": "클래스 구분 없는 단일 펀드",
        "raw_label": form_line or "종류형 표시 없음",
        "page": form_page,
    }


def extract(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM class_fees WHERE product_code IS NOT NULL")]
    known = {}
    for pc, cc in conn.execute(
            "SELECT product_code, class_code FROM class_fees "
            "WHERE class_code IS NOT NULL"):
        known.setdefault(pc, set()).add(cc)

    out = []
    for code in codes:
        merged, pages = {}, {}
        known_codes = known.get(code, set())
        # 표는 "줄 단위"로 읽는다. 표 전체를 한 덩어리 글로 평탄화하면
        # 아랫줄의 칸이 윗줄 라벨에 붙어 엉뚱하게 짝지어진다. 실제로
        # KR515302022M의 CI가 "수수료미징구-오프라인-고액"인데 옆줄과
        # 섞여 "수수료선취-오프라인"으로 읽혔다 - 고액 전용 클래스를
        # 일반 고객이 살 수 있는 것으로 답하게 되는 오류다.
        #
        # 한 줄 안에서는 붙여도 된다. 코드 칸과 라벨 칸이 나뉜 표가
        # 있어서(["종류A", "수수료선취-오프라인", "98292"]) 칸 하나씩만
        # 보면 그런 표를 통째로 놓친다.
        votes = collections.defaultdict(_Vote)
        for page, rows in _tables_in_order(conn, code):
            # 표 하나가 한 클래스에 한 표만 준다. 병합된 칸 때문에 같은
            # 이름표가 여러 줄에 걸쳐 나오는 표가 있어서, 줄마다 세면
            # 그 표가 여러 번 투표한 셈이 된다.
            found = {}
            for row in rows:
                for cc, rec in _parse_row(row, known_codes).items():
                    found.setdefault(cc, rec)
            if not found:
                continue
            flat = _squash(" ".join((c or "") for r in rows for c in r))
            titled = any(t in flat for t in NAMING_TABLE_TITLES)
            for cc, rec in found.items():
                votes[cc].add(rec, page, True, len(found), titled)
        # 본문 청크도 본다(줄 단위로). 표에서 하나도 못 찾았을 때만
        # 보던 것을 항상 보도록 바꿨다 - "종류형 명칭" 표가 테두리 없이
        # 글자로만 놓인 문서가 있는데(KR5139420015 2쪽 실측: 16개 클래스가
        # "수수료미징구-오프라인-개인연금(C-p) AI180" 꼴로 줄줄이 적혀
        # 있다), 그런 문서도 다른 표에서 몇 개는 잡히는 바람에 폴백이
        # 안 걸려 나머지를 통째로 잃고 있었다(16개 중 11개).
        #
        # 표에서 찾은 것이 우선이다 - 표는 코드 칸과 라벨 칸이 나뉘어
        # 있어 짝이 확실하고, 본문은 줄이 섞일 여지가 있다(_Vote 참고).
        for page, text in conn.execute(
                "SELECT page, text FROM chunks WHERE doc_id = ? ORDER BY page",
                (code,)):
            for line in _stitch_wrapped_lines((text or "").splitlines()):
                for cc, rec in _parse(line, known_codes).items():
                    votes[cc].add(rec, page, False)

        for cc, (rec, page) in _resolve(votes).items():
            merged[cc] = rec
            pages[cc] = page

        # 이름표가 여러 표에 반복돼도, 그 반복이 전부 같은 최초 입력
        # 오타의 복붙이면 "많이 나왔다"는 다수결로는 못 가른다(KR5194450018
        # 실측: "수수료선취-오프라인(A-e)"이 명칭표·비용표·요약표 9곳에서
        # 나오는데, 가입자격 원문은 "판매사의 온라인을 통해 가입"이라고
        # 분명히 말하고, 협회 펀드코드(AY121)·판매수수료율(0.5%)도 다른
        # 표의 "수수료선취-온라인(A-e)"와 완전히 같다 - 반복 횟수가 아니라
        # 가입자격이라는 별도 출처가 진실이다). 가입자격 원문에 판매경로
        # 단어가 하나만, 그것도 라벨과 다르게 적혀 있으면 그쪽을 따른다.
        for cc in list(merged):
            cur_channel = merged[cc]["channel"]
            # "온라인슈퍼"/"온라인직접판매" 등 복합 채널은 대상에서 뺀다 -
            # 가입자격 원문의 "온라인"이 그 복합 채널의 일부일 수 있어
            # (S-P 실측: "온라인 판매시스템"이 "온라인슈퍼"를 가리킴)
            # 기본 채널로 잘못 덮어쓸 위험이 있다.
            if cur_channel not in ("오프라인", "온라인"):
                continue
            elig_ch = _channel_from_eligibility(conn, code, cc)
            if elig_ch and elig_ch != cur_channel:
                merged[cc] = dict(merged[cc], channel=elig_ch)

        aka = _fold_spelling(merged, pages, known_codes)

        # 티어(보수체감) 계열 전체를 대표하는 벌거벗은 글자가 곁다리로
        # 남는 문서가 있다 - 보수표엔 C1~C5(또는 C1~C4)만 실제 클래스로
        # 있고 "C" 자체는 없는데, <1,000만원 투자시...> 비용예시표가
        # C1~C5를 한 줄로 몰아 "C"라는 대표 라벨을 쓴다(KR5172450019
        # 27쪽, KR5194450018 38쪽 실측 - KR5194450018은 각주로도 "C는
        # 이연보수체계를 적용하여 계산한 수치"라고 명시). 보수표에 자기
        # 몫의 보수 행이 없고, 같은 접두의 숫자-티어 계열이 2개 이상
        # 이미 실재할 때만 지운다 - 그래야 진짜 벌거벗은 클래스(A, C
        # 등 티어가 아예 없는 문서)는 안 건드린다.
        for cc in list(merged):
            if cc in known_codes:
                continue
            tiers = sum(1 for kc in known_codes if kc.startswith(cc)
                        and kc[len(cc):].isdigit())
            if tiers >= 2:
                del merged[cc]
                del pages[cc]
                aka.pop(cc, None)

        # 이 상품이 실제로 파는 클래스가 아닌데 이름표만 걸리는 경우가
        # 있다 - 환매수수료 안내문처럼 "이 운용사 계열 펀드가 두루 쓰는
        # 표준 클래스 목록"을 그대로 실어 둔 보조 표(KR5194450018 27쪽
        # 환매수수료표: 클래스I "수수료미징구-오프라인-고액"이 실렸는데,
        # 정작 이 상품의 명칭표(9쪽)와 보수·비용표(31~34쪽) 어디에도
        # I클래스가 없다 - 이 상품은 I클래스를 발행한 적이 없다). 그런
        # 클래스는 보수표 근거(known_codes)도 없고, 진짜 「종류형 명칭」
        # 표에도 한 번을 안 걸린다(ever_titled=False) - 둘 다 없으면 이
        # 상품 소속이 아니라고 보고 뺀다. 위 _fold_spelling·티어 정리가
        # 끝난 뒤에 하는 마지막 단계다 - 표기만 다른 진짜 클래스(예:
        # "P2e"가 "C-P2e"로 접힘)까지 여기서 먼저 지워버리면 그 병합
        # 기회를 뺏는다. known_codes와의 대조는 대소문자·붙임표 흔들림도
        # 같은 클래스로 보도록 _canon_key로 한다.
        known_canon = {_canon_key(k) for k in known_codes}
        for cc in list(merged):
            if _canon_key(cc) not in known_canon and not votes[cc].ever_titled:
                del merged[cc]
                del pages[cc]
                aka.pop(cc, None)

        if not merged:
            single = _single_class_fund(conn, code, known_codes)
            if single:
                out.append(single)
                continue

        # 문서가 이름표를 안 붙인 클래스는 가입자격만이라도 읽는다.
        # 다만 붙임표·괄호만 다른 같은 클래스가 이미 이름표를 갖고 있으면
        # 건드리지 않는다 - 그건 이름표가 없는 게 아니라 표기가 갈린
        # 것이고, merge_class_spelling.py가 뒤에서 한 행으로 합친다.
        # 여기서 억지로 뜻을 붙이면 그 합치기가 막힌다.
        labeled_keys = {_canon_key(c) for c in merged}
        for cc in sorted(known_codes - set(merged)):
            if _canon_key(cc) in labeled_keys:
                continue
            rec = _meaning_from_eligibility(conn, code, cc)
            if rec:
                out.append(rec)

        for cc, rec in sorted(merged.items()):
            account_type = next(
                (a for a in rec["attributes"]
                 if _match_any(a, ACCOUNT_TYPES)), None)
            attrs = rec["attributes"]
            if account_type is None:
                inferred = _account_type_from_code(cc)
                if inferred:
                    account_type = inferred
                    attrs = attrs + [inferred]
            rec = dict(rec, attributes=attrs)
            override = _KNOWN_RETAIL_OVERRIDES.get((code, cc))
            retail = not any(_match_any(a, RESTRICTED) for a in attrs)
            description = _describe(rec)
            if override:
                retail = override.get("retail", retail)
                description = override.get("description", description)
            out.append({
                "product_code": code,
                "class_code": cc,
                "fee_type": rec["fee_type"],
                "channel": rec["channel"],
                "account_type": account_type,
                "attributes": attrs,
                "retail": retail,
                # 같은 클래스를 문서가 달리 적은 표기(뜻이 같을 때만).
                # 버리지 않고 남겨 둬야 그 표기로 물었을 때도 찾을 수 있다.
                "aka": aka.get(cc, []),
                "description": description,
                "raw_label": rec["raw_label"],
                "page": pages.get(cc),
            })
    conn.close()
    return out


def report(rows, db_path=DEFAULT_DB_PATH):
    """뽑은 이름표가 실제 class_fees의 클래스를 얼마나 덮는지."""
    conn = sqlite3.connect(db_path)
    have = {}
    for pc, cc in conn.execute("SELECT product_code, class_code FROM class_fees"):
        have.setdefault(pc, set()).add(cc)
    conn.close()

    got = {}
    for r in rows:
        got.setdefault(r["product_code"], set()).add(r["class_code"])

    total = matched = 0
    no_label = []
    for pc, classes in have.items():
        total += len(classes)
        m = len(classes & got.get(pc, set()))
        matched += m
        if not got.get(pc):
            no_label.append(pc)
    print(f"이름표 {len(rows)}건 / 상품 {len(got)}개")
    print(f"class_fees의 클래스 {total}개 중 뜻을 아는 것: {matched}개 "
          f"({matched * 100 // max(total, 1)}%)")
    if no_label:
        print(f"이름표를 하나도 못 찾은 상품: {len(no_label)}개 {no_label[:6]}")
    restricted = [r for r in rows if not r["retail"]]
    print(f"일반 고객이 살 수 없는 클래스: {len(restricted)}건 "
          f"(예: {[(r['product_code'], r['class_code'], r['description']) for r in restricted[:3]]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--check", action="store_true", help="저장하지 않고 커버리지만")
    args = ap.parse_args()

    rows = extract(args.db)
    report(rows, args.db)
    if args.check:
        return
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
