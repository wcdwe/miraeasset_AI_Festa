from __future__ import annotations

import re

from scripts.answer_llm import generate, verify_answer
from scripts import institution_facts
from scripts.search import lexical_search, semantic_search

from .product_resolver import resolve_product


_PRODUCT_DOCUMENT = re.compile(r"투자전략|투자목적|위험요인|주요\s*위험|원금보장|운용사")
_INSTITUTION_SIGNAL = re.compile(
    r"DB|DC|IRP|연금저축|퇴직연금|퇴직금|연금|세액공제|중도인출|"
    r"매수\s*취소|장외채권|예금\s*만기|재예치|유상청약",
    re.IGNORECASE,
)
_COVER_TOC = re.compile(r"\(표지\)|\(섹션\s*표지\)|목차|contents", re.IGNORECASE)
_BARE_TOC = re.compile(r"^\s*[1-9][.)]\s*[^\n:：>→]{1,30}$")
_FAQ_INDEX_LINE = re.compile(r"(?m)^\s*\d+(?:-\d+)?[.)]\s*.+\?\s*$")
_CONTACT_SIGNAL = re.compile(r"주소|연락처|홈페이지|www\.|전화", re.I)
_STRATEGY_SECTION = re.compile(r"투자전략|투자목적|투자방침|주요\s*투자대상")
_RISK_SECTION = re.compile(r"투자위험|주요\s*위험|원금손실|시장위험|가격변동")
_REQUEST_TERMS = {
    "어떻게", "어떻게해", "해주세요", "알려줘", "설명해줘", "되는", "거", "아니었나요",
    # 이 코퍼스가 대부분 FAQ 형식이라("~되나요?" 식 질문이 문서마다 즐비하다),
    # 아래 낱말들은 질문 어디에 붙어 있든 거의 모든 페이지에 코사인 유사도와
    # 무관하게 걸린다 - 실측(INST-05): "연금저축을 중도해지하면 세금이
    # 어떻게 되나요?"에서 정답과 전혀 무관한 페이지들이 "어떻게"/"되나요"
    # 때문에 정답 페이지(coverage=0)보다 coverage 점수가 높게 나왔다.
    "되나요", "됩니까", "됩니다", "인가요", "합니까", "습니까", "하나요",
    "그런가요", "가능한가요", "가능한가", "됐나요",
}

# 조사·흔한 의문형 어미가 명사에 그대로 붙은 원문 토큰("연금저축을",
# "중도해지하면", "세금이")은 정답 청크가 조사 없는 원형("연금저축",
# "중도해지", "세금")으로 적혀 있으면 그대로는 절대 안 걸린다. 형태소
# 분석기를 새로 넣는 대신, 흔한 접미사만 하나 떼어 정규화 토큰을 "추가로"
# 만든다 - 원문 토큰은 그대로 두고 검사 대상에 얹기만 한다.
#
# "도"(-도 조사) 같은 흔하고 애매한 접미사는 일부러 뺐다 - "위험도",
# "수수료도"처럼 조사가 아니라 단어 자체의 일부인 경우까지 잘못 잘라
# ("위험") 무관한 청크에 새로 걸릴 위험이 더 크다고 판단했다.
_NORMALIZE_SUFFIXES = sorted([
    "이라서", "라서", "이지만", "지만", "이라도", "라도",
    "됩니까", "됩니다", "되나요", "됐나요", "인가요", "합니까", "습니까", "하나요",
    "하려면", "하면은", "했나요",
    "하면", "해서", "하고", "하는", "했을", "인가",
    "에서", "에게", "한테", "까지", "부터", "으로",
    "이나", "이란", "란",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "만", "로",
], key=len, reverse=True)


def _normalized_token(token: str) -> str | None:
    """token 끝에서 접미사를 하나 떼어낸 정규화 형태. 어간이 2글자 미만으로
    남으면(잘못 자를 위험이 크다) None."""
    for suf in _NORMALIZE_SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 2:
            return token[: -len(suf)]
    return None


# 이 코퍼스는 "제도(대상) + 행위 -> 결과"를 묻는 질문이 대부분인데("연금저축을
# 중도해지하면 세금이 어떻게 되나요?" = 대상 연금저축 + 행위 중도해지 + 요구정보
# 세금), 그런데 정작 대상 낱말("연금저축", "퇴직연금")은 이 코퍼스 문서
# 대부분이 연금/퇴직연금 제도를 다루는 문서라 거의 모든 페이지에 등장한다 -
# 변별력이 없다. 반면 행위 낱말("중도해지")은 그 페이지가 실제로 그 절차를
# 다루는지를 정확히 가른다(실측: "연금저축"+"세금이"만 일치하는 무관한
# ISA 절세 페이지가, "중도해지"가 일치하는 정답 페이지보다 대상 낱말까지
# 덩달아 가중치를 받으면 오히려 더 높은 점수를 받는다). 그래서 행위
# 낱말에만 가중치를 높이고, 대상·요구정보 낱말은 원래 가중치(글자 수)를
# 그대로 둔다 - 길이로 짧은 낱말을 잘라내는 대신, 실제 변별력이 있는
# 낱말군만 무겁게 본다.
_ACTION_TERMS = {
    "해지", "중도해지", "인출", "중도인출", "이전", "전환", "신청", "가입",
    "수령", "개시", "만기", "연장", "환급", "환매", "감면", "승계", "이체", "납입",
}
_ANCHOR_WEIGHT_MULT = 3


def _term_weight(form: str) -> int:
    base = len(form)
    if form in _ACTION_TERMS:
        return base * _ANCHOR_WEIGHT_MULT
    return base


# "연금저축"이라고만 물으면 보통 세제적격 연금저축(현행, 세액공제)을
# 가리킨다. "(구)개인연금저축"은 이름에 "연금저축"을 부분열로 포함하고
# 있어 낱말 겹침만으로는 구분이 안 되지만, 실제로는 전혀 다른
# 상품이다(소득공제 방식, 1994년 이전 가입분) - institution_facts.py도
# 이 둘을 별도 subject로 분리해 뒀다(SUBJECT_ALIASES 참고). 질문에
# "개인연금저축"/"구형"/"소득공제"가 없는데 후보 문서가 (구)개인연금저축
# 얘기면 관련성을 깎는다 - 실측: "연금저축 중도해지 시 과세는?"에서
# 이 상품 얘기(doc25)가 진짜 정답(연금저축의 기타소득세 16.5%)을
# 낱말 겹침만으로 밀어냈다.
_OLD_PENSION_SAVINGS_MARKER = re.compile(r"개인연금저축|구형\s*연금저축")
_OLD_PENSION_SAVINGS_QUESTION_RE = re.compile(r"개인연금저축|구형|소득공제")
_OLD_PENSION_SAVINGS_PENALTY = 8


def topic_coverage(hit: dict, question: str) -> int:
    compact_text = re.sub(r"\s+", "", hit.get("text") or "").lower()
    tokens = [
        token.lower() for token in re.findall(r"[가-힣A-Za-z0-9]+", question or "")
        if len(token) >= 2 and token not in _REQUEST_TERMS
    ]
    total = 0
    for token in tokens:
        forms = {token}
        norm = _normalized_token(token)
        if norm and norm not in _REQUEST_TERMS:
            forms.add(norm)
        total += max(
            (_term_weight(form) for form in forms if form in compact_text),
            default=0,
        )
    if _OLD_PENSION_SAVINGS_MARKER.search(hit.get("text") or "") and not (
        _OLD_PENSION_SAVINGS_QUESTION_RE.search(question or "")
    ):
        total -= _OLD_PENSION_SAVINGS_PENALTY
    return total


def has_action_term_overlap(hit: dict, question: str) -> bool:
    """질문의 행위어(_ACTION_TERMS)가 hit 본문에 실제로 있는지만 본다.

    topic_coverage()는 대상·요구정보 낱말까지 다 더하는데, 관련성 "게이트"로
    쓰기엔 그게 오히려 독이 된다 - "퇴직연금"처럼 이 코퍼스 거의 모든
    페이지에 나오는 대상어까지 coverage>0을 만들어 버려서, 정작
    "중도인출"이라는 행위어가 전혀 없는 무관한 페이지도 게이트를
    통과해 버린다(실측: INST-06, api/server.py의 관련성 게이트에서
    이 문제로 doc27 p.4가 계속 이겼다). 그래서 게이트 전용으로는
    행위어 일치 여부만 이진으로 본다."""
    compact_text = re.sub(r"\s+", "", hit.get("text") or "").lower()
    tokens = [
        token.lower() for token in re.findall(r"[가-힣A-Za-z0-9]+", question or "")
        if len(token) >= 2 and token not in _REQUEST_TERMS
    ]
    for token in tokens:
        forms = {token}
        norm = _normalized_token(token)
        if norm:
            forms.add(norm)
        if any(form in _ACTION_TERMS and form in compact_text for form in forms):
            return True
    return False


def _usable(hit: dict) -> bool:
    text = (hit.get("text") or "").strip()
    try:
        page = int(hit.get("page"))
    except (TypeError, ValueError):
        return False
    hit["page"] = page
    if not text or not hit.get("doc_id") or page < 1:
        return False
    if hit.get("boilerplate") or _COVER_TOC.search(text) or _BARE_TOC.match(text):
        return False
    # 답 없이 질문 제목만 여러 개 나열한 FAQ 색인도 목차로 취급한다.
    if len(_FAQ_INDEX_LINE.findall(text)) >= 4 and not re.search(r"(?m)^\s*[-☞]\s*\S", text):
        return False
    if len(_CONTACT_SIGNAL.findall(text)) >= 3:
        return False
    # 1쪽의 짧은 제목은 표지일 가능성이 높다. 짧아도 콜론·문장 종결·수치가
    # 있으면 실제 사실일 수 있으므로 제외하지 않는다.
    if page == 1 and len(text) <= 45 and not re.search(r"[:：\d]|다[.!]?\s*$|요[.!]?\s*$", text):
        return False
    return True


# 사용자는 "중도해지"의 준말로 그냥 "해지"라고 쓰기도 한다("연금저축
# 해지 시 세금이 부과되나요?" - 실측). "해지"만으로는 이 코퍼스에 계약
# 해지·판매 해지 등 무관한 용례가 너무 많아 검색 자체가 정답 문서를
# 못 찾는다(중도해지/중도인출 같은 구체적 행위어가 있어야 검색도 되고
# 가중치도 붙는다 - has_action_term_overlap 참고). 원문을 대체하지
# 않고, 구체형을 덧붙인 검색을 "추가로" 한 번 더 돌려서 후보를
# 넓힌다 - 원문 그대로의 검색 결과도 그대로 유지된다.
_ACTION_TERM_EXPANSIONS = {"해지": "중도해지", "인출": "중도인출"}


def _action_term_expansions(question: str) -> list[str]:
    """"중도"가 이미 붙어 있으면(예: "중도에 해지") 확장하지 않는다 -
    실측: "연금저축펀드를 중도에 해지하면..."에 "중도해지"를 또 얹으면
    "중도"라는 조각이 이미 있는데도 겹쳐서 다른 subject(개인연금저축)
    문서의 우연한 "중도해지" 일치까지 덩달아 더 무겁게 만들어 역효과가
    났다. "중도"가 어디에도 없을 때만(=진짜 준말로 줄여 쓴 경우만)
    확장한다."""
    result = []
    for bare, rich in _ACTION_TERM_EXPANSIONS.items():
        prefix = rich[: len(rich) - len(bare)]
        if bare in question and prefix not in question:
            result.append(rich)
    return result


def retrieve_document_hits(question: str, doc_type: str,
                           product_code: str | None = None,
                           k: int = 10, fact_types: list[str] | None = None) -> list[dict]:
    # 문서에는 붙여 쓴 행위명("중도해지")이 많지만 사용자는
    # "중도에 해지"처럼 조사와 함께 자연스럽게 묻는다. 검색 직전에만
    # 같은 의미의 표준 행위명으로 정규화하고 사용자 원문은 보존한다.
    search_question = re.sub(r"중도\s*(?:에|로)?\s*해지", "중도해지", question or "")
    facts = set(fact_types or [])
    if "RISK_NARRATIVE" in facts:
        search_question += " 주요 투자위험 손실 위험요인"
    if "INVESTMENT_STRATEGY" in facts or "STRATEGY" in facts:
        search_question += " 투자목적 투자전략"
    expansions = _action_term_expansions(question)
    queries = [search_question] + [f"{search_question} {rich}" for rich in expansions]
    pooled: dict[tuple, dict] = {}
    for q in queries:
        semantic = semantic_search(q, k=max(k, 8), doc_type=doc_type, product_code=product_code)
        lexical = lexical_search(q, k=max(k, 8), doc_type=doc_type, product_code=product_code)
        for ranked in (semantic, lexical):
            for rank, hit in enumerate(ranked):
                key = (hit.get("doc_id"), hit.get("page"), hit.get("chunk_id"))
                current = pooled.setdefault(key, dict(hit, rrf=0.0))
                current["rrf"] = current.get("rrf", 0.0) + 1.0 / (61 + rank)
                current["score"] = max(current.get("score") or 0.0, hit.get("score") or 0.0)
    hits = [hit for hit in pooled.values() if _usable(hit)]
    # 확장어는 검색뿐 아니라 관련성 순위에도 반영한다.
    ranking_question = " ".join([search_question] + expansions)
    # 투자전략/위험 섹션 가산은 상품 투자설명서에만 적용한다. 제도 문서에
    # 적용하면 세제·절차 질문에서도 우연히 해당 표현이 든 페이지가 앞선다.
    section_pattern = None
    if doc_type == "product":
        section_pattern = _RISK_SECTION if "RISK_NARRATIVE" in facts else (
            _STRATEGY_SECTION if facts & {"INVESTMENT_STRATEGY", "STRATEGY"} else None)
    tax_result_needed = bool(
        re.search(r"세금|과세|소득세", search_question)
        and re.search(r"해지|인출|수령", search_question)
    )
    hits.sort(
        key=lambda hit: (
            1 if section_pattern and section_pattern.search(hit.get("text") or "") else 0,
            1 if tax_result_needed and re.search(
                r"기타소득세|연금소득세|퇴직소득세|이자소득세|배당소득세|분리과세",
                hit.get("text") or "",
            ) else 0,
            topic_coverage(hit, ranking_question), hit.get("rrf", 0.0), hit.get("score", 0.0),
        ),
        reverse=True,
    )
    # 같은 페이지의 인접 청크 반복을 막는다.
    pages, deduped = set(), []
    for hit in hits:
        page_key = (hit.get("doc_id"), hit.get("page"), hit.get("chunk_id"), hit.get("text"))
        if page_key in pages:
            continue
        pages.add(page_key)
        deduped.append(hit)
    return deduped[:k]


def _context(hits: list[dict]) -> str:
    return "\n\n---\n\n".join(
        f"[{hit['doc_type']}/{hit['doc_id']} p.{hit['page']}]\n{hit['text'][:500]}"
        for hit in hits
    )


# 질문이 특정 화제를 물으면, 그 화제의 "답변다운" 값 신호를 담은 문장
# 구간에 작은 가산점을 준다. 특정 정답 문장을 하드코딩한 게 아니라 화제
# 단위 신호다 - 세제 질문이면 세율·공제 같은 값 모양 낱말이 나오는
# 구간이 정답일 가능성이 높다는 일반 규칙이라, 다른 세제 질문에도 같이
# 적용된다. 필요하면 이 딕셔너리에 화제를 더 추가하면 된다.
_TOPIC_SIGNALS = {
    "tax": (
        re.compile(r"세금|과세|세액|소득세"),
        re.compile(r"과세|비과세|세율|소득세|세액공제|공제|%"),
    ),
}


def _topic_bonus(question: str, window: str) -> float:
    for question_pattern, answer_pattern in _TOPIC_SIGNALS.values():
        if question_pattern.search(question or "") and answer_pattern.search(window):
            return 3.0
    return 0.0


# 질문이 "어떤 경우에 가능한가"류(조건·사유를 묻는 질문)면, 정답은 보통
# 번호 매긴 목록(사유 1, 2, 3...)이다. 번호 목록이라고 무조건 가산하면
# 절차 안내나 무관한 목차도 다시 유리해지므로, 질문이 실제로 조건·사유를
# 묻을 때만, 그리고 그 목록이 여러 항목을 담고 있을 때만 가산한다.
_CONDITIONS_QUESTION_RE = re.compile(r"어떤\s*경우|어느\s*경우|무슨\s*경우|조건|사유|언제\s*가능")
_LIST_ITEM_RE = re.compile(r"(?:^|[\s\)\]])(?:\d{1,2}[.)]\s*|[-•▶]\s+)\S")
# 이 코퍼스의 FAQ 문서들이 답을 시작할 때 쓰는 표지. 대부분 "▶"를 쓰고
# (doc27/doc55 등), 일부는 "•"(doc14 계열)나 "☞"(doc9 등)를 쓴다 - 문서
# 하나에 맞춘 게 아니라 실제로 관찰된 여러 표지를 모은 것이다.
_ANSWER_MARKER_RE = re.compile(r"[▶☞]|(?:^|\n)\s*•")


def asks_for_conditions(question: str) -> bool:
    return bool(_CONDITIONS_QUESTION_RE.search(question or ""))


def list_item_count(window: str) -> int:
    return len(_LIST_ITEM_RE.findall(window))


def _split_sentences(text: str) -> list[str]:
    sentences = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        # "1. 무주택자인..."처럼 번호 목록 마침표 뒤에서 쪼개면 "1."과
        # 항목 내용이 서로 다른 "문장"으로 갈라져 list_item_count/
        # _LIST_ITEM_RE가 항목을 하나도 못 알아본다(실측) - 마침표
        # 바로 앞이 숫자면(=목록 번호) 그 자리에서는 안 쪼갠다.
        sentences.extend(
            p.strip() for p in re.split(r"(?<!\d[.!?])(?<=[.!?])\s+", line) if p.strip()
        )
    return sentences


def _sentence_windows(text: str, size: int = 3) -> list[str]:
    """문장 size개씩 겹치며 이어 붙인 구간들. 문장이 size개 이하면 통째로
    구간 하나."""
    sentences = _split_sentences(text)
    if not sentences:
        return [text or ""]
    if len(sentences) <= size:
        return [" ".join(sentences)]
    return [" ".join(sentences[i:i + size]) for i in range(len(sentences))]


def _list_block_start(sentences: list[str], from_idx: int) -> int:
    """from_idx 근처(±몇 문장)에서 목록 항목을 찾아, 그 목록이 실제로
    시작되는 문장 인덱스까지 거꾸로 되짚는다.

    관련성 점수로 고른 "가장 잘 맞는 구간"이 항상 목록의 첫 항목에서
    시작하지는 않는다 - 질문 표현에 따라 뒷부분 항목(예: 6, 7번)이 더
    높은 점수를 받을 수 있다(실측: "중도인출 사유가 뭐가 있나요?"는
    6·7번 사유가 있는 구간이 1등이었다). 이대로 앞으로만 이어 붙이면
    1~5번은 영영 안 나온다 - 목록의 실제 시작점부터 다시 모은다."""
    first_item = None
    for idx in range(from_idx, min(from_idx + 8, len(sentences))):
        if _LIST_ITEM_RE.search(sentences[idx]):
            first_item = idx
            break
    if first_item is None:
        return from_idx
    start = first_item
    while start > 0 and _LIST_ITEM_RE.search(sentences[start - 1]):
        start -= 1
    # 목록 항목 자체에는 "왜 이 목록인지"(예: "중도인출")가 안 적혀
    # 있을 수 있다 - 그 말은 보통 목록 바로 앞의 안내 문장에 있다
    # (실측: "• 일정 조건을 충족하면 중도인출이 가능합니다." 없이
    # 항목만 나오면 답변에 "중도인출"이라는 낱말 자체가 사라진다).
    # 목록 바로 앞 문장 최대 2개까지 안내문으로 함께 포함한다.
    start = max(0, start - 2)
    return start


def _extend_list_window(sentences: list[str], start: int, max_chars: int) -> str:
    """start부터 이어지는 번호 목록 항목을 끝까지(항목이 아닌 문장이
    두 개 연속 나올 때까지) 모아 붙인다. 고정 문장 개수(3/7문장)로는
    항목이 몇 개짜리 목록이든 상관없이 일부만 잘려 나가므로("사유
    7가지 중 2개만" - 실측 INST-06), 목록이 실제로 몇 항목이든 끝까지
    따라간다."""
    parts, total, saw_list_item, trailing_non_list = [], 0, False, 0
    for idx in range(start, len(sentences)):
        s = sentences[idx]
        is_list = bool(_LIST_ITEM_RE.search(s))
        if saw_list_item and not is_list:
            trailing_non_list += 1
            if trailing_non_list > 1:
                break
        else:
            trailing_non_list = 0
        if is_list:
            saw_list_item = True
        candidate_total = total + len(s) + 1
        if parts and candidate_total > max_chars:
            break
        parts.append(s)
        total = candidate_total
    return " ".join(parts)


def relevant_excerpt(question: str, text: str, max_chars: int) -> str:
    """청크에서 질문과 가장 관련 있는 문장 구간을 찾아 그 구간만 자른다.

    예전엔 청크 앞부분 max_chars를 그대로 잘랐는데("투자자 유의사항" 같은
    보일러플레이트가 앞에 있으면 그 청크가 실제로 담고 있는 정답 문장
    (예: "...중도해지...기타소득세(16.5%)")까지 못 가고 잘려 나갔다 -
    실측(INST-05). max_chars를 늘리는 건 근본 해결이 아니다(문서가 더
    길면 같은 문제가 반복된다) - 문장 단위로 나눠 질문과 가장 관련 있는
    구간을 고른다."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    windows = [(i, w) for i, w in enumerate(_sentence_windows(text, size=3))]
    asks_conditions = asks_for_conditions(question)
    if asks_conditions:
        # 조건·사유 목록은 보통 한 항목이 한 문장이라, 3문장 구간엔
        # 기껏해야 항목 1~2개만 걸린다. "몇 가지 조건 중 어떤 것들인가"를
        # 묻는 질문이면 더 넓은(7문장) 구간도 후보에 얹어서, 항목이
        # 여러 개 이어지는 구간이 통째로 뽑힐 수 있게 한다(둘 다 같은
        # 시작 문장 위치를 인덱스로 써서 "원래 순서" 우선순위가 흐트러
        # 지지 않게 한다).
        windows += [(i, w) for i, w in enumerate(_sentence_windows(text, size=7))]

    # 실측(INST-06): "퇴직연금 중도인출은 어떤 경우에 가능한가요?"에서
    # "중도인출"이 두 군데(진짜 사유 목록 직전, 그리고 전혀 다른 질문
    # 끝자락)에 걸려 coverage가 동점이 났는데, 예전 동점 규칙("?"로 안
    # 끝나면 우선, 그래도 같으면 늦은 구간 우선")이 사유 목록이 아닌
    # 쪽을 골랐다 - "늦은 구간 우선"은 근거 없는 추측이었다. 대신
    # 사람이 실제로 쓰는 순서로 동점을 가른다:
    #   1) 관련성 점수(coverage+화제 가산 - 조건 질문이면 목록 항목
    #      개수 가산도 여기 포함)
    #   2) 핵심 행위어가 이 구간에 실제로 있는가
    #   3) (조건 질문일 때) 목록 항목이 몇 개나 있는가 - 많을수록 우선
    #   4) 답변 표지(▶·☞·•)가 이 구간 안에 있는가 - "?"로 안 끝나는지를
    #      봤었는데, "#연금수령" 같은 해시태그 줄이 우연히 물음표 뒤에
    #      와서 "안 끝남"을 만족시켜 버리는 오탐이 있었다(실측 INST-04
    #      회귀). 이 코퍼스는 "▶"(대부분) 또는 "•"(doc14 계열)로 답을
    #      시작하므로, 그 표지가 실제로 있는지를 직접 본다.
    #   5) 그래도 같으면 문서에 나온 원래 순서(앞선 구간)
    def _rank(item):
        i, w = item
        list_count = list_item_count(w) if asks_conditions else 0
        score = topic_coverage({"text": w}, question) + _topic_bonus(question, w)
        if asks_conditions and list_count >= 2:
            score += 6.0
        return (
            score,
            has_action_term_overlap({"text": w}, question),
            list_count,
            bool(_ANSWER_MARKER_RE.search(w)),
            -i,
        )

    best_i, best = max(windows, key=_rank)
    ranked_best = _rank((best_i, best))
    best_score, best_list_count = ranked_best[0], ranked_best[2]
    if best_score <= 0:
        # 관련 신호를 하나도 못 찾았으면(코퍼스 전반에 걸친 일반 질문 등)
        # 예전 동작(앞부분 절단)으로 안전하게 되돌아간다.
        return text[:max_chars] + ("…" if len(text) > max_chars else "")
    # 조건·사유 목록 질문은 항목 하나만 보여주면 "어떤 경우들"에 대한
    # 답으로 부족하다(실측: INST-06 - 고정 문장 개수 구간으로는 사유
    # 7개 중 2~3개만 들어갔다). 목록이 실제로 여러 항목이면, 이 목록이
    # 끝날 때까지 이어 붙인다(항목 개수에 맞춰 늘어나므로 3개짜리
    # 목록이든 7개짜리 목록이든 같은 규칙으로 다 붙는다).
    if asks_conditions and best_list_count >= 2:
        sentences = _split_sentences(text)
        block_start = _list_block_start(sentences, best_i)
        grown = _extend_list_window(sentences, block_start, max_chars * 3)
        if len(grown) > len(best):
            best = grown
    effective_max = max_chars * 3 if (asks_conditions and best_list_count >= 2) else max_chars
    if len(best) > effective_max:
        return best[:effective_max] + "…"
    return best


def _fallback(hit: dict, question: str = "") -> str:
    excerpt = relevant_excerpt(question, hit["text"], 350)
    return f"검색된 근거({hit['doc_id']} p.{hit['page']})에 따르면:\n{excerpt}"


def _procedure_fallback(hits: list[dict], question: str = "") -> str:
    top = hits[0]
    adjacent = [
        hit for hit in hits
        if hit["doc_id"] == top["doc_id"] and abs(hit["page"] - top["page"]) <= 1
    ][:2]
    adjacent.sort(key=lambda hit: hit["page"])
    if len(adjacent) == 1:
        return _fallback(top, question)
    return "\n\n".join(
        f"검색된 근거({hit['doc_id']} p.{hit['page']}):\n"
        f"{relevant_excerpt(question, hit['text'], 450)}"
        for hit in adjacent
    )


def try_simple_product_document(question_id: str, question: str) -> dict | None:
    """명확한 단일 상품의 서술형 문서 질문을 Hybrid RAG로 처리한다."""
    if not _PRODUCT_DOCUMENT.search(question or ""):
        return None
    resolution = resolve_product(question)
    if resolution.status not in {"exact", "alias"} or len(resolution.candidates) != 1:
        return None

    candidate = resolution.candidates[0]
    # 통합 저장소의 실제 상품 doc_type 값은 단수형 `product`다.
    subqueries = []
    if re.search(r"투자전략|투자목적", question):
        subqueries.append(f"{candidate.product_name} 투자목적 투자전략 투자방침")
    if re.search(r"위험요인|주요\s*위험|원금", question):
        subqueries.append(f"{candidate.product_name} 주요 투자위험 원금손실 가격변동위험")
    subqueries = subqueries or [question]
    hits, seen = [], set()
    for subquery in subqueries:
        for hit in retrieve_document_hits(subquery, "product", candidate.product_code)[:2]:
            key = (hit.get("doc_id"), hit.get("page"))
            if key not in seen:
                seen.add(key)
                hits.append(hit)
    if not hits:
        return None
    context = _context(hits)
    answer, how = generate(question, context)
    fallback_reason = None
    if answer:
        answer = re.sub(
            r"\[product/([^\]\s]+)\s+p\.(\d+)\]",
            r"(출처: \1, p.\2)", answer,
        )
        problems = verify_answer(question, answer, context)
        cited = "p." in answer and any(hit["doc_id"] in answer for hit in hits)
        if not cited:
            problems.append("검색 결과에 있는 문서명·페이지가 답변에 연결되지 않음")
        if problems:
            fallback_reason = problems
            answer = None
        else:
            cited_pairs = {
                (doc, int(page))
                for doc, page in re.findall(r"출처:\s*([^,()]+),\s*p\.(\d+)", answer)
            }
            used = [
                hit for hit in hits
                if (str(hit.get("doc_id")), int(hit.get("page"))) in cited_pairs
            ]
            if used:
                hits = used
                context = _context(hits)
    if not answer:
        answer = _fallback(hits[0], question)
        how = how + "; Python 검증 후 근거 발췌 사용"

    return {
        "question_id": str(question_id),
        "question": str(question),
        "retrieved_context": context,
        "think_trace": (
            "1. Agent fallback: PRODUCT_RAG\n"
            f"2. 상품 식별: {resolution.status} - {candidate.product_code} "
            f"({candidate.product_name})\n"
            f"3. 상품 문서 Hybrid RAG: 사실별 검색, 비본문 제외, 실제 사용 근거 {len(hits)}건\n"
            f"4. 답변 생성·검증: {how}"
            + (f"\n   - 생성 답변 반려 사유: {fallback_reason}" if fallback_reason else "")
        ),
        "answer": answer,
        "route": "rag",
    }


def try_simple_institution_document(question_id: str, question: str) -> dict | None:
    """제도·절차 질문: 원자적 사실을 우선하고 없을 때만 RAG를 사용한다."""
    if not _INSTITUTION_SIGNAL.search(question or ""):
        return None
    # 상품 투자설명서 질문을 제도 문서로 잘못 보내지 않는다.
    if _PRODUCT_DOCUMENT.search(question or ""):
        return None

    summary, evidence = institution_facts.institution_facts_answer(question)
    if summary is not None:
        return {
            "question_id": str(question_id),
            "question": str(question),
            "retrieved_context": str(summary),
            "think_trace": (
                "1. Agent fallback: INSTITUTION_FACT\n"
                "2. institution_facts 원자적 사실 조회: HIT\n"
                f"3. Python 근거 검증: PASS (근거 {len(evidence)}건)\n"
                "4. 승인된 사실 템플릿 사용; LLM 호출 없음"
            ),
            "answer": str(summary),
            "route": "institution_facts",
        }

    hits = retrieve_document_hits(question, "institution")
    if not hits:
        return None
    context = _context(hits)
    answer, how = generate(question, context)
    fallback_reason = None
    if answer:
        problems = verify_answer(question, answer, context)
        cited = "p." in answer and any(hit["doc_id"] in answer for hit in hits)
        if not cited:
            problems.append("검색 결과에 있는 문서명·페이지가 답변에 연결되지 않음")
        if problems:
            fallback_reason = problems
            answer = None
    if not answer:
        answer = _procedure_fallback(hits, question)
        how = how + "; Python 검증 후 근거 발췌 사용"

    return {
        "question_id": str(question_id),
        "question": str(question),
        "retrieved_context": context,
        "think_trace": (
            "1. Agent fallback: INSTITUTION_RAG\n"
            "2. institution_facts 원자적 사실 조회: MISS\n"
            f"3. 제도·절차 Hybrid RAG: 표지·목차 제외, 페이지 중복 제거, 근거 {len(hits)}건\n"
            f"4. 답변 생성·검증: {how}"
            + (f"\n   - 생성 답변 반려 사유: {fallback_reason}" if fallback_reason else "")
        ),
        "answer": answer,
        "route": "rag",
    }
