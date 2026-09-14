"""API 진입점 입력 방어층.

평가자가 던지는 question은 신뢰할 수 없는 입력이다. 검색·LLM 호출까지 가기
전에 걸러야 하는 것 세 가지:

1. 길이 폭탄 - 너무 긴 질문은 임베딩·LLM 호출 비용만 태우고 답도 산으로 간다.
2. 프롬프트 공격 - "이전 지시 무시하고..." 류로 SYSTEM_PROMPT의 규칙(근거만
   쓰기/추천 금지/숫자 검산 등)을 우회하려는 지시.
3. 개인정보 - 주민등록번호·전화번호·계좌번호가 질문에 섞여 들어오면, 그걸
   그대로 retrieved_context/think_trace/answer에 되돌려주지 않아야 한다
   (평가 응답이 곧 로그로 남는다).

여기서 걸리면 검색·LLM 없이 고정 답변으로 짧게 끝낸다 - 걸러야 할 입력에
근거 검색까지 태우는 건 낭비고, 프롬프트 공격 문구를 그대로 컨텍스트에
넣고 LLM을 부르면 공격이 실제로 프롬프트에 도달해 버린다.

무관 질의(질문이 연금/펀드 도메인과 아예 상관없는 경우, 예: "오늘 날씨
어때")는 여기서 차단하지 않는다 - 도메인 키워드 매칭으로 판단하면 표현이
살짝 다른 정상 질문(오탈자, 약어, 새 용어)까지 잘못 막을 위험이 크고,
그런 질문은 검색기가 근거를 못 찾아 NO_EVIDENCE로 정직하게 답하는 기존
경로로도 충분히 걸러진다 - 정직한 "모른다"가 성급한 차단보다 안전하다.
"""

import re

# 임베딩 1건에 보통 수백 자 안팎이면 충분하다. 이보다 훨씬 길면 정상적인
# 질문이 아니라 뭔가를 욱여넣으려는 시도로 본다. 복합 비교 질문("A랑 B
# 총보수·수익률·위험등급 다 비교해서 세제까지 정리해줘" 류)도 여유롭게
# 통과하도록 500자보다 넉넉하게 잡는다.
MAX_QUESTION_LEN = 1000

# "이전 지시를 무시" 류 한국어 변형과, ignore previous instructions 류
# 영어 변형을 같이 잡는다. 정상적인 연금 질문에는 나올 일이 없는 표현이라
# 오탐 위험이 낮다.
RE_PROMPT_INJECTION = re.compile(
    r"(이전|위|앞선)\s*(지시|명령|프롬프트|규칙)[을를]?\s*(무시|잊|취소)"
    r"|(시스템|system)\s*(프롬프트|지시|메시지)[을를이가]?\s*(보여|알려|출력|공개)"
    r"|너는?\s*이제부터"
    r"|당신은\s*이제부터"
    r"|ignore\s+(the\s+)?(above|previous|prior|all)\s+instructions?"
    r"|disregard\s+(the\s+)?(above|previous|prior)\s+"
    r"|reveal\s+(your\s+)?(system\s+)?prompt"
    r"|jailbreak",
    re.IGNORECASE,
)

# 한국 주민등록번호(생년월일6자리-성별포함7자리), 휴대전화번호, 계좌번호
# 모양(자릿수 구분자 있는 긴 숫자열)을 본다. 완벽한 정규식은 아니지만
# (오탐/누락 둘 다 있을 수 있다), "숫자 그대로 안 돌려준다"는 목적에는
# 넉넉히 걸리는 쪽이 안전하다.
RE_RRN = re.compile(r"\d{6}[-\s]?[1-4]\d{6}")
RE_PHONE = re.compile(r"01[0-9][-\s]?\d{3,4}[-\s]?\d{4}")
RE_ACCOUNT = re.compile(r"\d{2,6}-\d{2,6}-\d{2,6}(-\d{1,4})?")


def detect_pii(question):
    """질문에 섞인 개인정보로 보이는 항목 이름 목록. 비어 있으면 없음."""
    hits = []
    if RE_RRN.search(question):
        hits.append("주민등록번호로 보이는 숫자")
    if RE_PHONE.search(question):
        hits.append("휴대전화번호로 보이는 숫자")
    if RE_ACCOUNT.search(question):
        hits.append("계좌번호로 보이는 숫자")
    return hits


def detect_prompt_injection(question):
    """프롬프트를 우회하려는 지시로 보이면 True."""
    return bool(RE_PROMPT_INJECTION.search(question or ""))


def check(question_id, question):
    """입력 방어 결과.

    통과하면 None. 걸리면 /answer 응답 스키마 그대로(question_id/question/
    retrieved_context/think_trace/answer) 채운 dict를 돌려준다 - 검색·LLM을
    타지 않고 여기서 바로 응답을 끝낸다."""
    text = question or ""

    if len(text) > MAX_QUESTION_LEN:
        return _blocked(
            question_id, question,
            f"질문 길이 {len(text)}자가 상한({MAX_QUESTION_LEN}자)을 넘음",
            "질문이 너무 깁니다. 궁금한 내용을 간단히 요약해서 다시 질문해 주세요.")

    if detect_prompt_injection(text):
        return _blocked(
            question_id, question,
            "프롬프트 우회 지시로 보이는 표현 감지",
            "죄송하지만 그 요청은 답변드릴 수 없습니다. 연금 제도나 상품에 "
            "관한 질문을 해 주시면 도와드리겠습니다.")

    pii = detect_pii(text)
    if pii:
        return _blocked(
            question_id, question,
            f"개인정보로 보이는 값 감지: {pii}",
            "질문에 개인정보로 보이는 값이 포함되어 있어 그대로 처리하지 "
            "않았습니다. 개인정보를 빼고 다시 질문해 주세요.")

    return None


def _blocked(question_id, question, reason, answer):
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": "(입력 방어층에서 차단되어 검색을 수행하지 않음)",
        "think_trace": f"0. 입력 방어층: {reason} - 검색/LLM 호출 없이 고정 답변으로 응답",
        "answer": answer,
        "route": "blocked",
    }
