"""
연금 Agent 과제 - 평가용 API 서버

주최측 스펙:
    GET {endpoint}/answer?question_id={id}&question={질의}
    -> 200 application/json
    {
        "question_id": "Q-001",
        "question": "평가 질의 원문",
        "retrieved_context": "답변 생성에 참고한 검색 문서",
        "think_trace": "사고, 추론, 도구 사용 과정",
        "answer": "최종 생성 답변"
    }
    (모든 필드는 string. 헤더/인증 없음. GET만 지원.)

현재 상태: 검색(라우팅 + semantic/table search)까지는 실제로 동작한다.
"answer"는 아직 HyperCLOVA X API 키가 없어서 실제 LLM 생성이 아니라
검색된 근거를 그대로 요약해서 보여주는 발췌형 스텁이다
(generate_answer()에 명시). 키가 발급되면 이 함수만 HCX 호출로
교체하면 되고, 그 외 라우팅/검색/응답 스키마는 그대로 재사용한다.

실행:
    uvicorn api.server:app --host 0.0.0.0 --port 8000
    (배포 시 표준 포트: HTTP 80 / HTTPS 443. 80 포트 바인딩은 root 권한 필요)

테스트:
    curl -G "http://127.0.0.1:8000/answer" \
        --data-urlencode "question_id=Q-001" \
        --data-urlencode "question=DC와 DB, 운용 주체가 어떻게 다른가요?"
"""

import os
import re
import sys

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

# router는 벡터 검색(chromadb)을 끌어오므로 모듈 로드 시점에 import하면
# 벡터 스토어가 없는 환경에서 서버가 아예 안 뜬다. 구조화 DB로 답하는
# 경로는 그것과 무관하게 동작해야 하므로 필요할 때 늦게 불러온다.
from compare_products import is_comparison_query, compare_products  # noqa: E402
from product_lookup import find_products, find_class_code  # noqa: E402
from product_facts import detect_intents, product_facts  # noqa: E402
import input_guard  # noqa: E402
import query_analyzer  # noqa: E402
import tax_calculator  # noqa: E402
import product_ranking  # noqa: E402
import institution_facts  # noqa: E402
from agent_v2.pre_router import pre_route  # noqa: E402
from agent_v2.anchor import extract_anchor  # noqa: E402
from agent_v2.document_path import (  # noqa: E402
    asks_for_conditions,
    has_action_term_overlap,
    list_item_count,
    relevant_excerpt,
    topic_coverage,
    try_simple_institution_document,
    try_simple_product_document,
)
from agent_v2.structured_path import try_fast_structured  # noqa: E402
from agent_v2.templates import build_policy_payload  # noqa: E402
from agent_v2.orchestrator import try_agent_payload  # noqa: E402
from agent_v2.filter_path import try_fast_filter  # noqa: E402
from agent_v2.comparison_path import try_fast_compare  # noqa: E402
from agent_v2.api_contract import ResponseCache, validate_api_response  # noqa: E402
from agent_v2.telemetry import reset_usage, usage_snapshot  # noqa: E402

app = FastAPI(title="연금 Agent 평가용 API")
response_cache = ResponseCache(max_size=256)

MAX_CONTEXT_CHUNKS = 6
MAX_TABLE_ROWS_SHOWN = 3
# 청크당 컨텍스트에 넣는 최대 글자 수 — 토큰(크레딧) 절약을 위해 chunk_text.py의
# 최대 청크 길이(500자)보다 더 짧게 자른다. LLM에 원문 전체를 통째로 넘기지 않고
# 필요한 만큼만 파싱해서 전달하는 "Parser" 패턴.
CONTEXT_CHUNK_CHAR_LIMIT = 350


def _rank_score(hit):
    """router가 매긴 최종 순위 점수. 의미 검색과 글자 검색을 순위로 합친
    rrf가 있으면 그걸 쓴다 - 코사인 유사도(score)로 다시 줄 세우면 글자
    검색으로만 올라온 청크가 도로 뒤로 밀려 합친 의미가 없어진다."""
    return hit.get("rrf", hit.get("score") or 0.0)


def _query_coverage(hit, query):
    text = hit.get("text", "")
    terms = {t for t in re.findall(r"[가-힣A-Za-z0-9]+", query or "") if len(t) >= 3}
    return sum(len(term) for term in terms if term in text)


def _dedupe_by_page(hits, query=""):
    """같은 (doc_id, page)에서 나온 청크가 여러 개면 가장 점수 높은 것만 남긴다.
    한 페이지 안의 인접 청크들이 겹치는 내용을 반복해서 토큰을 낭비하는 걸 막는다."""
    best = {}
    for hit in hits:
        key = (hit.get("doc_id"), hit.get("page"))
        hit_rank = (_query_coverage(hit, query), _rank_score(hit))
        best_rank = (_query_coverage(best[key], query), _rank_score(best[key])) if key in best else None
        if key not in best or hit_rank > best_rank:
            best[key] = hit
    return sorted(best.values(), key=_rank_score, reverse=True)


def _context_excerpt(text: str, query: str) -> str:
    """긴 청크는 질의와 맞닿은 부분을 중심으로 자른다."""
    if len(text) <= CONTEXT_CHUNK_CHAR_LIMIT:
        return text
    terms = sorted(
        {t for t in re.findall(r"[가-힣A-Za-z0-9]+", query or "") if len(t) >= 3},
        key=len,
        reverse=True,
    )
    positions = [(text.find(term), term) for term in terms if term in text]
    if not positions:
        return text[:CONTEXT_CHUNK_CHAR_LIMIT] + "…"
    pos, _term = max(positions, key=lambda item: len(item[1]))
    start = max(0, pos - CONTEXT_CHUNK_CHAR_LIMIT // 3)
    end = min(len(text), start + CONTEXT_CHUNK_CHAR_LIMIT)
    start = max(0, end - CONTEXT_CHUNK_CHAR_LIMIT)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def format_retrieved_context(route_result: dict, query: str = "") -> str:
    """근거 문서를 소스 태그와 함께 하나의 문자열로 합친다 (근거 표시 요구사항)."""
    parts = []
    deduped = _dedupe_by_page(route_result["semantic_hits"], query)
    for hit in deduped[:MAX_CONTEXT_CHUNKS]:
        tag = f"[{hit.get('doc_type')}/{hit.get('doc_id')} p.{hit.get('page')}]"
        text = _context_excerpt(hit["text"], query)
        parts.append(f"{tag}\n{text}")
    for hit in route_result["table_hits"]:
        tag = f"[{hit.get('doc_type')}/{hit.get('doc_id')} p.{hit.get('page')} 표]"
        rows = hit["data"][:MAX_TABLE_ROWS_SHOWN]
        row_text = "\n".join(" | ".join(cell for cell in row if cell) for row in rows)
        parts.append(f"{tag}\n{row_text}")
    if not parts:
        return "(검색된 근거 문서 없음)"
    return "\n\n---\n\n".join(parts)


def format_think_trace(query: str, route_result: dict) -> str:
    """질의 분류/검색 과정을 사고 과정으로 기록 (think_trace 필드)."""
    c = route_result["classification"]
    lines = [
        f"1. 질의 분류: {c['category']} (institution={c['use_institution']}, products={c['use_products']}, table_search={c['use_table_search']})",
    ]
    if c["matched_institution_keywords"]:
        lines.append(f"   - 매칭된 제도/세제 키워드: {c['matched_institution_keywords']}")
    if c["matched_product_keywords"]:
        lines.append(f"   - 매칭된 상품 키워드: {c['matched_product_keywords']}")
    if c["product_codes"]:
        lines.append(f"   - 인식된 상품코드: {c['product_codes']}")
    if c["ambiguous"]:
        lines.append("   - 키워드로 분류가 애매해 institution/products 양쪽 다 검색함")
    if route_result.get("retried"):
        lines.append(
            "   - 1차 검색 결과의 유사도가 낮아 검색 범위를 institution+products 양쪽으로 넓혀 재검색함"
        )
    lines.append(f"2. 의미 검색(semantic_search) 결과 {len(route_result['semantic_hits'])}건")
    for hit in route_result["semantic_hits"][:MAX_CONTEXT_CHUNKS]:
        lines.append(f"   - {hit.get('doc_id')} p.{hit.get('page')} (유사도 {hit.get('score'):.3f})")
    if route_result["table_hits"]:
        lines.append(f"3. 표 검색(table_search) 결과 {len(route_result['table_hits'])}건")
        for hit in route_result["table_hits"]:
            lines.append(f"   - {hit.get('doc_id')} p.{hit.get('page')}")
    return "\n".join(lines)


def _analysis_trace_line(analysis, analysis_how):
    """LLM 구조화 질의분석 결과를 think_trace 한 줄로 요약.

    missing_slots(질문에 안 밝혀진 조건)는 되묻지 않고(단일 턴 평가라
    되물으면 답을 못 받는다 - answer_llm.SYSTEM_PROMPT 규칙 6 참고),
    "이 조건이 안 밝혀져 있었다"는 근거로만 남긴다. 실제로 어떤 기본값을
    썼는지는 LLM이 답을 쓸 때 규칙 6에 따라 알아서 밝힌다."""
    if not analysis:
        return f"   - LLM 질의분석: {analysis_how}"
    line = (f"   - LLM 질의분석: intent={analysis.get('intent')}, "
            f"entities={analysis.get('entities')}")
    missing = analysis.get("missing_slots")
    if missing:
        line += f"\n   - 질문에 안 밝혀진 조건(되묻지 않고 무난한 기본값으로 답함): {missing}"
    return line


NO_EVIDENCE = (
    "가지고 있는 자료로는 이 질문에 답할 수 있는 근거를 찾지 못했습니다. "
    "질문을 더 구체적으로 말씀해 주시거나, 관련 제도나 상품명을 알려주시면 다시 찾아보겠습니다."
)


def compose_answer(question: str, context: str, fallback: str):
    """근거를 사람 말로 옮긴다. (답변, 어떻게 만들었는지 한 줄)

    LLM은 문장만 만든다. 숫자를 고르고 클래스를 가리고 근거 페이지를 다는
    일은 이미 앞에서 끝났고, 그 결과가 context다. LLM 답에 근거에 없는
    숫자가 섞이면 그 답은 버리고 fallback(=조회 결과 원문)을 내보낸다 -
    투박한 근거가 그럴듯한 오답보다 낫다(answer_llm.check_numbers).

    쓰지 못하는 상황(키 없음/호출 실패/검산 실패)을 조용히 넘기지 않고
    think_trace에 적는 이유는, 그때 나간 답이 LLM이 쓴 문장이 아니라
    조회 결과라는 걸 채점자가 알 수 있어야 하기 때문이다."""
    from answer_llm import generate  # HCX가 없어도 서버가 떠야 한다

    text, how = generate(question, context)
    if text:
        return text, how
    return fallback, how


# HCX가 없거나 실패했을 때 쓰는 fallback(=조회 결과 원문)이 표지·목차
# 줄을 답으로 내보내는 사고가 있었다("퇴직연금 장외채권 매수 신청
# 어떻게 해?" 실측: "퇴직연금 장외채권 매수 모바일 신청 가이드"(23자,
# 표지)가 유사도 1등(0.941)이고, 진짜 절차가 적힌 페이지("1. 금융상품
# 매매 메뉴...", 259자)는 0.638로 밀렸다 - 표지·목차처럼 짧고 질문
# 낱말만 고밀도로 담은 줄이, 낱말이 여러 문장에 흩어진 진짜 본문보다
# TF-IDF 유사도가 더 높게 나오기 쉽다).
#
# 처음엔 "글자 수 40 미만이면 제외"로 막았는데, 이것도 틀렸다 - 짧아도
# 정답인 문장("가입대상: 만 55세 이상", "위험자산 투자한도 70%")을
# 같이 버리고, 길어도 무관한 주의문·목차는 못 거른다. 그래서 "확실한
# 표지/목차 신호"만 하드 제외하고, 나머지는 "검색 관련성 + 정답이
# 실려 있을 만한 구조"로 점수를 매겨 고른다. 구조 신호 중 일부(번호
# 단계·메뉴경로 등)는 질문 유형에 따라 가산 여부가 갈린다 - 아래
# _INTENT_PATTERNS/_intent_bonus 참고.
_COVER_MARKER_RE = re.compile(r"\((?:섹션\s*)?표지\)|목차|Contents")
# "1. 퇴직연금 유상청약 신청"처럼 번호 + 명사구 한 줄로 끝나는 목차 줄의
# 모양(뒤에 콜론·화살표·구체값이 붙어 실제 설명으로 이어지면 아래
# _excerpt_score에서 목차로 안 본다).
_TOC_LINE_RE = re.compile(r"^\s*[1-9][.)]\s*[^\n:：>→]{1,30}$")
_SENTENCE_END_RE = re.compile(r"(다|요|니다|함|음|까)[.?!]\s*$")
_KEY_VALUE_RE = re.compile(r"[가-힣A-Za-z0-9()·/ ]{1,20}\s*[:：]\s*\S")
_MENU_PATH_RE = re.compile(r"\S\s*[>→]\s*\S")
_STEP_MARKER_RE = re.compile(r"(?:^|\n)\s*[1-9][.)]\s*\S{3,}")
_ACTION_WORD_RE = re.compile(
    r"선택|이동|검색|클릭|입력|신청|가능|조회|매수|매도|이체|해지|등록|접수")
_SPECIFIC_VALUE_RE = re.compile(r"\d+\s*(?:%|세|원|만원|억원|년|개월|회|건|등급)")
_EXCEPTION_MARKER_RE = re.compile(r"다만|단,|예외|제외하고|해당하지\s*않|아니면")
_RISK_ASSET_RE = re.compile(
    r"위험등급|\d\s*등급|주식형|채권형|혼합형|파생상품|MMF|자산유형")
# 비교 질문("DB와 DC 차이")에서, 실제로 비교 대상 둘 다 언급하는 문장에만
# 가산점을 준다 - 한쪽만 설명하는 문장은 "차이"에 대한 완전한 답이 못 된다.
_DUAL_SUBJECT_RE = re.compile(r"DB|DC|IRP|연금저축")

# 질문 유형 감지. 하나의 질문에 여러 유형이 동시에 걸릴 수 있다(복합
# 질문 - 아래 _pick_fallback_hits 참고). "제도 설명" 칸의 "대상·조건"도
# 여기 definition에 포함한다("가입대상이 뭐야?"는 절차가 아니라 제도
# 설명 질문이다).
_INTENT_PATTERNS = {
    "procedure": re.compile(
        r"어떻게|방법|절차|하는\s*법|신청|하려면|하는거|하려|해야\s*(?:하|되)"),
    "definition": re.compile(
        r"뭐야|무엇|이란|정의|뜻이|대상이|가입\s*대상|가입\s*조건|자격|어떤\s*사람"),
    "tax_limit": re.compile(r"세액공제|한도|얼마|몇\s*%|몇\s*퍼센트|공제"),
    "product_info": re.compile(r"위험등급|자산유형|클래스|어떤\s*상품|상품명"),
    "fee_return": re.compile(r"보수|수수료|수익률|비용"),
    "comparison": re.compile(r"차이|비교|보다|중\s*어디|어느\s*쪽|둘\s*다"),
    "recommendation": re.compile(r"추천|골라|뭐가\s*좋"),
}


def _detect_intents(question):
    q = question or ""
    return {name for name, pat in _INTENT_PATTERNS.items() if pat.search(q)}


def _intent_bonus(t, intent, has, question):
    """질문 유형별 가산점. has는 _excerpt_score가 이미 계산해 둔 공통
    신호 dict(문장종결/항목값/메뉴/단계/행동/구체값) - 유형마다 새로
    정규식을 돌리지 않고 재사용한다."""
    if intent == "procedure":
        b = 0.0
        if has["menu"]:
            b += 0.3
        if has["step"] or has["action"]:
            b += 0.3
        return b
    if intent == "definition":
        # "다만/예외" 같은 단서 절이 붙어 있으면 대상·조건·예외까지
        # 완전한 설명일 가능성이 높다.
        return 0.2 if _EXCEPTION_MARKER_RE.search(t) else 0.0
    if intent in ("tax_limit", "fee_return"):
        # 값 자체는 공통 점수에서 이미 +0.2를 받는다 - 세제·보수 질문
        # 에서는 그 값이 핵심이므로 추가로 더 얹는다.
        return 0.15 if has["value"] else 0.0
    if intent == "product_info":
        return 0.2 if _RISK_ASSET_RE.search(t) else 0.0
    if intent == "comparison":
        subs = set(_DUAL_SUBJECT_RE.findall((question or "").upper()))
        if len(subs) < 2:
            return 0.0
        present = sum(1 for s in subs if s in t.upper())
        return 0.3 if present >= 2 else 0.0
    if intent == "recommendation":
        # "사용자 조건과 상품 특성이 함께 있는 근거" - 항목:값과 구체
        # 수치가 같이 있으면 조건·특성이 둘 다 적힌 문장일 가능성이 있다.
        return 0.2 if (has["kv"] and has["value"]) else 0.0
    return 0.0


# 구조 점수만으로 고르면 검색 관련성을 사실상 무시하게 된다(무관한
# 수수료표가 항목:값+구체수치로 0.4를 받아, 정답 문장(문장종결 0.1)을
# 구조 점수만으로 이길 수 있었다). 그래서 검색 유사도(retrieval_score,
# 0~1대)를 기본값으로 깔고 구조 점수를 그 위에 더하는 보정으로 쓴다 -
# 관련성이 먼저고, 구조는 "관련성이 비슷한 후보들 중에 표지·목차를
# 밀어내고 실제 설명을 우선"시키는 재정렬용이다. 이 코퍼스의 검색
# 유사도는 관련 문서끼리도 0.4~0.9대로 흩어져 있어(실측), 0.4 근처를
# 문턱으로 잡으면 구조 보정 없이도 상당수가 걸러진다.
MIN_FALLBACK_SCORE = 0.35
_BARE_PHRASE_PENALTY = 0.25


def _excerpt_score(text, retrieval_score=0.0, page=None, intents=frozenset(), question=""):
    """이 히트를 fallback 발췌로 쓸 만한지 채점한다(검색 유사도 +
    공통 구조 보정 + 질문 유형별 가산). 확실히 표지/목차면 None(하드
    제외).

    "글자 수가 짧으면 표지"가 아니다 - PDF 추출에서 마침표가 곧잘
    사라져 "확정급여형은 회사가 적립금을 운용하는 제도"처럼 종결어미도
    콜론도 없는 짧은 정답 문장이 실제로 나온다. 그래서 이런 "벌거벗은
    명사구"는 감점만 하고 걸러내지는 않는다 - 검색 유사도가 충분히
    높으면(=질문과 실제로 관련 있으면) 감점을 받고도 살아남을 수
    있다.

    반면 "번호. 명사구" 목차 줄(예: "1. 퇴직연금 유상청약 신청")은
    감점이 아니라 그대로 하드 제외한다 - 이 모양은 "정답 문장에서
    마침표만 빠진 것"과 다르다, 애초에 절 제목을 가리키는 목차
    항목이라는 게 구조적으로 명확하고, 이 코퍼스에서 목차 줄은 질문
    낱말과 고밀도로 겹쳐 검색 유사도가 실제 정답 페이지보다도 높게
    나오기 쉬워서(실측) 감점만으로는 다 못 눌러낸다. 콜론·메뉴경로·
    구체값이 붙어 실제 설명으로 이어지면(예: "1. 가입대상: 만 55세
    이상") 이 하드 제외에서 빠진다.

    표지도 마찬가지로 명시적 마커, 그리고 "1쪽 + 벌거벗은 명사구"
    (문서 1쪽이 정확히 이 모양이면 표지 페이지일 확률이 매우 높다 -
    실측 "퇴직연금 장외채권 매수 모바일 신청 가이드", 1쪽)는 하드
    제외한다."""
    t = (text or "").strip()
    if not t:
        return None
    if _COVER_MARKER_RE.search(t):
        return None
    has = {
        "end": bool(_SENTENCE_END_RE.search(t)),
        "kv": bool(_KEY_VALUE_RE.search(t)),
        "menu": bool(_MENU_PATH_RE.search(t)),
        "step": bool(_STEP_MARKER_RE.search(t)),
        "action": bool(_ACTION_WORD_RE.search(t)),
        "value": bool(_SPECIFIC_VALUE_RE.search(t)),
    }

    single_line = "\n" not in t
    is_bare_phrase = (single_line and len(t) <= 35
                       and not (has["end"] or has["kv"] or has["menu"] or has["value"]))
    is_bare_toc = (single_line and _TOC_LINE_RE.match(t)
                   and not (has["kv"] or has["menu"] or has["value"]))

    if is_bare_toc:
        return None  # 목차 줄 - 감점이 아니라 하드 제외
    if is_bare_phrase and page == 1:
        return None  # 1쪽짜리 벌거벗은 명사구 - 표지일 확률이 매우 높다

    score = retrieval_score
    if has["end"]:
        score += 0.1
    if has["kv"]:
        score += 0.2
    if has["value"]:
        score += 0.2
    for intent in intents:
        score += _intent_bonus(t, intent, has, question)
    if is_bare_phrase:
        # 1쪽이 아니라 하드 제외는 피했지만, 그래도 벌거벗은 명사구는
        # 감점한다 - 검색 유사도가 충분히 높은 진짜 정답만 이 감점을
        # 견디고 살아남는다.
        score -= _BARE_PHRASE_PENALTY
    return score


def _best_scored_hit(hits, query, intents, exclude_ids=frozenset()):
    """intents 기준으로 채점해 최고점 히트 하나를 고른다. 문턱 미달이거나
    후보가 없으면 None. exclude_ids는 이미 다른 사실에 쓴 히트를
    복합질문 선택에서 다시 뽑지 않게 뺀다."""
    scored = []
    for rank, h in enumerate(hits):
        if id(h) in exclude_ids:
            continue
        s = _excerpt_score(h.get("text"), h.get("score", 0.0) or 0.0,
                            h.get("page"), intents, query)
        if s is not None:
            scored.append((s, -rank, h))
    if not scored:
        return None

    # 핵심 행위어 게이트: 문장종결·항목값 같은 구조 가산점은 "질문이
    # 실제로 묻는 낱말을 담은 문장"을 더 위로 올리기 위한 보정일 뿐인데,
    # 이 가산점만으로 그 낱말이 아예 없는 문장이 역전하는 사고가 있었다
    # (실측: "퇴직연금 중도인출은 언제 가능한가"에서 "중도인출"이라는
    # 글자가 전혀 없는 무관한 FAQ 문항이, 문장종결·항목값·구체수치
    # 가산점만으로 "중도인출" 사유가 실제로 나열된 정답 문항을 이겼다).
    # 처음엔 agent_v2.document_path._coverage()(대상·요구정보 낱말까지
    # 다 더하는 값)로 게이트를 걸었는데, "퇴직연금"처럼 이 코퍼스 거의
    # 모든 페이지에 나오는 대상어만으로도 coverage>0이 돼서 게이트가
    # 무력화됐다(실측). 그래서 게이트 전용으로는 행위어(중도인출·이전
    # 등) 일치만 보는 has_action_term_overlap()을 쓴다 - 이 후보군 안에
    # 행위어가 겹치는 후보가 하나라도 있으면, 안 겹치는 후보는 구조
    # 가산점이 아무리 높아도 제외한다. 질문에 인식된 행위어가 없거나
    # 후보 전체가 하나도 못 담고 있으면(코퍼스 recall 자체의 한계) 게이트를
    # 걸 근거가 없으므로 그대로 둔다.
    covered = [
        (s, rank, h) for s, rank, h in scored
        if has_action_term_overlap(h, query)
    ]
    if covered:
        scored = covered

    # 조건·사유 질문("어떤 경우에 가능한가")은 구조 가산점(문장종결·항목값
    # 등)이 화제 커버리지보다 앞서면 안 된다 - 실측: "중도인출 사유가
    # 뭐가 있나요?"에서 "중도인출"을 스쳐 지나가듯 한 번 언급한 무관한
    # 페이지(제도 변경 안내)가, 진짜 사유 목록이 있는 페이지(화제
    # 커버리지가 더 높음)를 구조 점수만으로 역전했다. 행위어 게이트만
    # 으로는 둘 다 "중도인출"을 담고 있어 못 갈랐다.
    #
    # topic_coverage만 1순위로 쓰면 또 다른 문제가 있다 - 사유 하나만
    # 다루는 개별 문서(doc50 등)의 도입부 boilerplate("퇴직연금제도는
    # ...중도인출이 허용됩니다")가 낱말 겹침만으로 사유 전체를 표로
    # 담은 문서(doc14 p.2)보다 근소하게 coverage가 더 높게 나온 적이
    # 있다(실측: 19 대 18). 그래서 목록 항목이 실제로 여러 개(2개
    # 이상)면 coverage에 가산해 - 근소한 차이로는 "사유 하나만 다루는
    # 문서"가 "여러 사유를 한 번에 답할 수 있는 문서"를 못 이기게
    # 한다. 다른 질문 유형은 기존 동작을 그대로 둔다(회귀 위험을
    # 조건 질문 범위로 좁힌다).
    def _conditions_rank(item):
        _, _, h = item
        cov = topic_coverage(h, query)
        if list_item_count(h.get("text") or "") >= 2:
            cov += 6
        return (cov, item[0], item[1])

    if asks_for_conditions(query):
        scored = sorted(scored, key=_conditions_rank, reverse=True)
    else:
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, _, best_hit = scored[0]
    # 부동소수점 오차(0.7-0.25가 정확히 0.45가 아니라 0.44999...로 나오는
    # 등)로 문턱 바로 위 점수가 억울하게 탈락하지 않도록 아주 작은
    # 여유(epsilon)를 둔다.
    if best_score < MIN_FALLBACK_SCORE - 1e-9:
        return None
    return best_hit


def _pick_fallback_hits(hits, query="", max_chunks=3):
    """표지·목차를 하드 제외하고, 남은 것 중 (검색 유사도+구조+질문
    유형) 점수가 가장 높은 히트를 고른다. 문턱 미달이거나 후보가 아예
    없으면 빈 리스트 - 관련성 낮은 아무 문장이나 억지로 답인 척 내보내지
    않는다.

    질문이 서로 다른 사실을 두 개 이상 요구하면("IRP 가입 대상과
    중도인출 방법을 알려줘" - 가입대상=definition, 중도인출 방법=
    procedure) 청크 하나로 다 답하려 하지 않는다 - 사실(=감지된 질문
    유형)마다 제일 잘 맞는 청크를 따로 골라 최대 max_chunks개까지
    모은다. 한 사실도 문턱을 못 넘으면 그 사실은 건너뛴다."""
    intents = _detect_intents(query)
    if len(intents) >= 2:
        picked, seen = [], set()
        for intent in sorted(intents):
            hit = _best_scored_hit(hits, query, {intent}, seen)
            if hit is not None:
                picked.append(hit)
                seen.add(id(hit))
            if len(picked) >= max_chunks:
                break
        if picked:
            return picked
        # 사실별로 하나도 못 골랐으면(전부 문턱 미달) 아래 단일 선택
        # 경로로 넘어간다 - 그래도 감지된 의도는 전부 가산점에 반영한다.
    hit = _best_scored_hit(hits, query, intents)
    return [hit] if hit is not None else []


def generate_answer(query: str, route_result: dict):
    """검색(rag) 경로의 답변. (답변, 생성 방식 한 줄)"""
    hits = route_result["semantic_hits"]
    if not hits:
        return NO_EVIDENCE, "근거가 없어 정보한계로 답함"
    context = format_retrieved_context(route_result, query)
    picked = _pick_fallback_hits(hits, query)
    if not picked:
        # 표지가 확실하거나(1쪽 벌거벗은 명사구 등), 남은 후보의 점수가
        # 다 MIN_FALLBACK_SCORE에 못 미치면(=검색 관련성부터 낮으면)
        # 그 문장을 억지로 답인 척 내보내지 않는다 - 근거 부족을
        # 정직하게 말하는 쪽이 낫다.
        return NO_EVIDENCE, "상위 검색 결과 중 관련성 있는 근거를 찾지 못해 정보한계로 답함"
    if len(picked) == 1:
        top = picked[0]
        # 청크 앞 300자를 그대로 자르면, 실제로 골라 둔 정답 청크라도
        # 앞부분이 무관한 안내문이면 진짜 답 문장까지 못 간다(INST-05와
        # 같은 문제가 여기서도 그대로 재현됐다 - 실측 INST-06: doc14 p.2를
        # 정확히 골랐는데도 앞 300자가 IRP 이체 안내라 "중도인출 사유"
        # 표는 잘려 나갔다). document_path.relevant_excerpt()로 질문과
        # 가장 관련 있는 문장 구간만 자른다.
        fallback = (f"검색된 근거({top.get('doc_id')} p.{top.get('page')})에 따르면:\n"
                    f"{relevant_excerpt(query, top['text'], 300)}")
    else:
        # 복합 질문 - 사실별로 고른 청크를 각자 출처를 단 채로 이어붙인다.
        parts = [f"[{h.get('doc_id')} p.{h.get('page')}] "
                 f"{relevant_excerpt(query, h['text'], 300)}"
                 for h in picked]
        fallback = "질문에 필요한 사실을 근거별로 나눠 찾았습니다:\n\n" + "\n\n".join(parts)
    return compose_answer(query, context, fallback)


def answer_payload(question_id: str, question: str) -> dict:
    from agent_v2.runtime import answer_payload as unified_answer_payload
    return unified_answer_payload(question_id, question)




@app.get("/answer")
def answer(question_id: str, question: str):
    cached = response_cache.get(question_id, question)
    if cached is not None:
        return JSONResponse(content=cached)

    reset_usage()
    try:
        body = dict(answer_payload(question_id, question))
        body.pop("route", None)  # 주최측 응답 스펙에 없는 필드라 빼고 내보낸다
        usage = usage_snapshot()
        body["think_trace"] = str(body.get("think_trace", "")) + (
            "\n8. LLM 사용량(문자 기반 추정): "
            f"호출 {usage.calls}회, 실패 {usage.failed_calls}회, "
            f"입력 약 {usage.estimated_input_tokens}토큰, "
            f"출력 약 {usage.estimated_output_tokens}토큰"
            f"; HTTP 시도 {usage.http_attempts}회; 실제 usage 수신 {usage.actual_usage_responses}회"
            f" (수신분 입력 {usage.actual_input_tokens}, 출력 {usage.actual_output_tokens})"
            f"; 단계별 호출 {dict(usage.calls_by_stage)}"
        )
        validated = validate_api_response(body)
    except Exception as exc:
        import logging
        logging.getLogger("agent_v2.audit").error("API failure: %s", type(exc).__name__)
        # 내부 경로·키·원문 데이터는 응답에 노출하지 않는다. 5xx를 반환해
        # 평가 서버가 일시 장애로 판단하고 재시도할 수 있게 한다.
        raise HTTPException(status_code=503, detail="일시적인 처리 오류입니다.") from exc

    response_cache.put(validated)
    return JSONResponse(content=validated)


@app.get("/health")
def health():
    return {"status": "ok"}
