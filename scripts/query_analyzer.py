"""LLM(HyperCLOVA X) 기반 구조화 질의분석기.

router.classify()는 키워드 규칙 기반이라 빠르고 안정적이지만, 키워드
목록에 없는 표현(오탈자, 새 용어, 상품명 일부만 언급)은 그냥 놓친다.
이 모듈은 HCX에게 질의를 구조화된 JSON(intent/entities/required_facts/
missing_slots/ambiguous)으로 뽑아 달라고 해서 그 빈틈을 메운다.

단일 턴 평가라는 제약(answer_llm.SYSTEM_PROMPT 규칙 6 참고: 평가 API는
질문 한 번에 답 한 번, 되물으면 사용자가 답할 기회가 없다)은 그대로
지킨다 - missing_slots(질문에 빠진 조건)을 실제 되묻기에 쓰지 않는다.
answer_payload가 think_trace에 "이런 조건이 안 밝혀져 기본값으로 답했다"는
근거로만 남긴다.

HCX가 없거나 호출이 실패하거나 JSON이 스키마와 안 맞으면 조용히 (None,
사유)를 돌려주고, 부르는 쪽(router.classify 등 규칙 기반)이 그대로
쓰이게 한다 - 이 모듈은 있으면 보강이고, 없어도 기존 경로가 그대로
동작해야 한다(있어야만 되는 걸 만들면, HCX 장애가 곧 서비스 장애가
된다).

사용법(CLI, 수동 점검용):
    python3 scripts/query_analyzer.py --ask "미래에셋 국공채 펀드 총보수 얼마야?"
    python3 scripts/query_analyzer.py --check   # 스키마 파싱만 오프라인 시험
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hcx import HcxError, chat, is_configured  # noqa: E402

ANALYZER_SYSTEM_PROMPT = """\
너는 연금·펀드 질의를 분석하는 분류기다. 사용자 질문 하나를 보고 반드시
아래 JSON 형식으로만 답하라. 다른 말은 절대 덧붙이지 말고, 코드 펜스도
쓰지 마라.

{
  "intent": "제도" 또는 "세제" 또는 "상품설명" 또는 "비교" 또는 "추천" 또는 "복합" 중 하나,
  "entities": {
    "product_names": [질문에 언급된 상품/펀드 이름. 일부만 언급돼도 넣는다],
    "product_codes": [KR로 시작하는 12자리 상품코드],
    "account_types": [DC, DB, IRP, 연금저축 등 계좌 종류],
    "periods": ["1년", "10년" 등 언급된 투자/보유 기간]
  },
  "required_facts": [답하려면 확인해야 할 사실 종류. 예: "총보수", "수익률", "위험등급", "설정액", "비용예시", "세율"],
  "missing_slots": [질문에 빠져 있어서 조건에 따라 답이 달라질 수 있는 것. 예: "가입계좌 종류", "가입경로(온라인/오프라인)"],
  "ambiguous": 상품 질문인지 제도 질문인지 키워드만으로 애매하면 true, 아니면 false
}

intent가 "추천"이어도 특정 상품을 사라고 판단하라는 뜻이 아니라, 사용자가
추천을 요청했다는 분류표시일 뿐이다. 못 찾은 항목은 빈 배열로 남겨라.
JSON 객체 하나만 출력하라."""

REQUIRED_KEYS = {"intent", "entities", "required_facts", "missing_slots", "ambiguous"}
VALID_INTENTS = {"제도", "세제", "상품설명", "비교", "추천", "복합"}


def _extract_json(text):
    """```json ... ``` 펜스나 앞뒤 설명이 섞여 와도 안쪽 객체만 뽑는다."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _valid(data):
    if not isinstance(data, dict) or not REQUIRED_KEYS <= data.keys():
        return False
    if data.get("intent") not in VALID_INTENTS:
        return False
    ent = data.get("entities")
    if not isinstance(ent, dict):
        return False
    return True


def analyze(question, max_tokens=400):
    """(분석 dict 또는 None, 방식 설명 한 줄).

    실패하면 (None, 사유) - 부르는 쪽은 반드시 규칙 기반 경로로 계속
    진행해야 한다(이 함수가 예외를 던지지 않는 이유이기도 하다)."""
    if not is_configured():
        return None, "HCX 키가 없어 LLM 질의분석을 건너뜀"
    if not (question or "").strip():
        return None, "빈 질문이라 LLM 질의분석을 건너뜀"
    try:
        text = chat(
            [{"role": "system", "content": ANALYZER_SYSTEM_PROMPT},
             {"role": "user", "content": question}],
            max_tokens=max_tokens, temperature=0.0)
    except HcxError as e:
        return None, f"HCX 질의분석 호출 실패({e})"
    data = _extract_json(text)
    if not _valid(data):
        return None, "LLM 질의분석 응답이 JSON 스키마와 안 맞아 폐기"
    return data, "HCX 구조화 질의분석 성공"


def _check_demo():
    """오프라인으로 파싱/검증 로직만 시험한다(HCX 호출 없이)."""
    cases = [
        ('{"intent": "상품설명", "entities": {"product_names": ["솔로몬국공채"], '
         '"product_codes": [], "account_types": [], "periods": []}, '
         '"required_facts": ["총보수"], "missing_slots": ["가입경로"], "ambiguous": false}',
         True),
        ('```json\n{"intent": "제도", "entities": {"product_names": [], '
         '"product_codes": [], "account_types": ["DC"], "periods": []}, '
         '"required_facts": [], "missing_slots": [], "ambiguous": false}\n```',
         True),
        ('그냥 평범한 문장이고 JSON이 아님', False),
        ('{"intent": "모르는분류", "entities": {}, "required_facts": [], '
         '"missing_slots": [], "ambiguous": false}', False),
    ]
    ok = True
    for text, want_valid in cases:
        data = _extract_json(text)
        got_valid = _valid(data)
        mark = "OK " if got_valid == want_valid else "!! "
        ok = ok and got_valid == want_valid
        print(f"{mark}{text[:50]!r}... -> valid={got_valid} (기대 {want_valid})")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", help="실제 HCX를 불러 질의분석 시험")
    ap.add_argument("--check", action="store_true", help="오프라인 스키마 파싱 시험")
    args = ap.parse_args()
    rc = 0
    if args.check:
        rc |= _check_demo()
    if args.ask:
        data, how = analyze(args.ask)
        print(how)
        if data:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    if not (args.check or args.ask):
        ap.print_help()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
