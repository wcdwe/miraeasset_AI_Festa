"""답변 검증 세트를 실제 답변 경로로 돌려 본다.

지금까지 "데이터를 정확히 뽑았는가"는 A/B 비교와 정합성 검사로 계속
확인해 왔는데, "그래서 질문에 제대로 답하는가"는 몇 개를 손으로 쳐 보는
것 말고는 확인한 적이 없다. 평가가 자유 질의응답으로 이뤄지므로,
질문 -> 답변까지 통째로 돌려 보고 틀리는 걸 찾아내는 판이 필요하다.

eval/questions.jsonl의 각 줄:
    id, category, q            질문
    route                      기대 경로 (rag / single_product / comparison)
    code                       기대 상품코드 (있을 때만)
    must                       사용자에게 보이는 답변에 반드시 있어야 할 문자열
    must_answer                (선택) must를 대신할 때 - 의미는 must와 같다.
                                둘 다 있으면 이쪽을 쓴다.
    must_not                   답변에 있으면 안 되는 문자열
    expected_evidence          (선택) [{"document_id":"docNN","page":N}, ...] -
                                이 문항의 정답을 실제로 뒷받침하는 문서·페이지.
                                있는 문항만 retrieval_pass/citation_pass를 엄격히
                                가릴 수 있다(아래 설명).
    note                       사람이 눈으로 볼 때 참고할 메모

answer_pass는 반드시 사용자가 실제로 받는 payload["answer"]만 본다.
예전엔 answer+retrieved_context+think_trace를 합쳐서 검사했는데, 그러면
"근거 어딘가에 정답 단어가 있었다"와 "사용자가 정답을 받았다"가 같은
걸로 취급된다 - 실측(2026-09-06): 이렇게 재본 결과 INST-05는 답변이
"과학기술인공제회 FAQ"(사망·MP플랜)로 완전히 딴 얘기를 하는데도, 근거
청크 어딘가에 있던 "기타소득세"란 글자 때문에 "통과"로 나왔었다.

must(_answer)의 문자열 포함 검사도 숫자류는 그냥 substring이 아니라
숫자 경계·단위까지 맞춰서 본다 - 실측(2026-09-06): INST-09의 must가
"70"이었는데, 답변이 엉뚱한 근거를 냈어도 그 근거 안의 "MP70"(상품
포트폴리오 이름, 정답과 무관)의 "70" 때문에 통과로 잘못 나왔었다.
_answer_contains()가 숫자류는 앞뒤로 다른 숫자/영문자가 안 붙어 있을
때만(그리고 %가 붙은 형태라면 % 뒤에도 아무것도 안 붙을 때만) 인정한다.

evidence_pass(근거를 찾았는가)는 원래 retrieved_context에 must 글자가
있는지로만 봤는데, 이것도 같은 함정에 걸린다 - "그 근거가 진짜 정답을
뒷받침하는 문서인지"가 아니라 "글자가 어쩌다 겹쳤는지"만 본다. 대신
expected_evidence가 있는 문항은 다음 두 신호로 쪼갠다.
    retrieval_pass   검색 후보군(retrieved_context+think_trace)에
                     expected_evidence의 (document_id, page) 쌍이 하나라도
                     있는가 - 애초에 후보에 정답 문서가 걸렸는가.
    citation_pass    최종 답변(answer)이 실제로 인용한 (document_id, page)가
                     expected_evidence 중 하나인가 - 후보에 있었어도 답변
                     조립 단계에서 그 문서를 골랐는가.
expected_evidence가 없는 문항(대부분)은 예전처럼 부분 문자열 기반의
느슨한 evidence_hint만 참고용으로 남긴다 - 근거 문서를 아직 하나하나
확인 못 한 문항까지 전부 강한 기준을 요구할 수는 없다.

    answer_pass=True                              정상
    answer_pass=False, retrieval_pass=False        검색 실패 - 후보에도 없었다
    answer_pass=False, retrieval_pass=True,
                        citation_pass=False         답변조립 실패 - 후보엔
                                                     있었는데 다른 근거를 인용함
    answer_pass=False, retrieval_pass=True,
                        citation_pass=True           맞는 근거를 인용했는데도
                                                      must_answer 불통과 -
                                                      발췌·서술 자체의 문제

기계로 확인할 수 있는 건 경로·상품코드·숫자 포함 여부까지다. 제도 질문의
답이 실제로 맞는 말인지는 자동으로 못 가리므로, 근거를 같이 찍어서 사람이
읽을 수 있게 한다(--show).

실행:
    python3 scripts/eval_answers.py            # 요약만
    python3 scripts/eval_answers.py --show     # 실패한 것의 답변 전문
    python3 scripts/eval_answers.py --show-all # 전부 다
    python3 scripts/eval_answers.py --only INST  # id 앞글자로 추리기
"""

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from api.server import answer_payload  # noqa: E402

QUESTIONS_PATH = os.path.join(REPO_ROOT, "eval", "questions.jsonl")


def load_questions(path=QUESTIONS_PATH):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# must(_answer)의 항목이 이 모양이면 "숫자류"로 보고 경계 검사를 적용한다
# (정수/소수, 천단위 콤마, 끝에 % 선택). "KR5111420047"처럼 문자가 섞인
# 상품코드나 "기타소득세" 같은 일반 문자열은 여기 안 걸려 예전처럼 단순
# substring 검사로 간다.
_NUMERIC_NEEDLE_RE = re.compile(r"^-?\d[\d,]*(?:\.\d+)?%?$")
_DOC_PAGE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*\d[A-Za-z0-9_]*)\s*p\.\s*(\d+)")


def _answer_contains(text, needle):
    """숫자류 needle은 앞뒤로 다른 숫자/영문자가 안 붙어 있을 때만 인정한다.

    "70"이 "MP70"(포트폴리오 이름, 정답과 무관)의 일부로 붙어 있으면
    통과시키면 안 된다 - 실측(INST-09, 2026-09-06) 참고."""
    if not _NUMERIC_NEEDLE_RE.match(needle):
        return needle in text
    core = needle.rstrip("%")
    escaped = re.escape(core)
    if needle.endswith("%"):
        pattern = rf"(?<![A-Za-z0-9]){escaped}(?:\.0+)?\s*%(?![A-Za-z0-9])"
    else:
        # %를 안 붙이고 그냥 "70"이라고만 적은 기존 문항도 있으니, 뒤에 %가
        # 붙은 형태("70%")까지는 같이 인정한다 - 안 붙어도(순수 숫자) 통과.
        pattern = rf"(?<![A-Za-z0-9,]){escaped}(?:\.0+)?\s*%?(?![A-Za-z0-9])"
    return re.search(pattern, text) is not None


def _cited_doc_pages(text):
    """"docNN p.N" 모양(따옴표·괄호·슬래시 등에 둘러싸여 있어도)을 전부 뽑는다.
    retrieved_context의 "[institution/doc14 p.2]", think_trace의
    "- doc14 p.2 (유사도 ...)", 폴백 답변의 "검색된 근거(doc27 p.4)에
    따르면" 등 현재 코드가 실제로 쓰는 모든 표기 형태를 이 하나로 커버한다."""
    return {(m.group(1), int(m.group(2))) for m in _DOC_PAGE_RE.finditer(text or "")}


def check(case, payload):
    """(통과 여부, 실패 사유 목록, 진단 dict)

    진단 dict: {"answer_pass": bool, "retrieval_pass": bool|None,
    "citation_pass": bool|None, "failure_type": str|None}.
    retrieval_pass/citation_pass는 case에 expected_evidence가 있을 때만
    채워진다(없으면 None - 아직 근거 문서를 확인 안 한 문항). failure_type은
    answer_pass가 False일 때만 채워진다 - "retrieval"(근거 문서 자체가
    후보에 없었음) / "answer_assembly"(후보엔 있었는데 답변이 다른 근거를
    인용했거나, 맞는 근거를 인용하고도 서술에서 빠뜨림) / "routing"(경로·
    상품코드 자체가 틀림, must/must_not과 무관) 중 하나."""
    fails = []
    answer_text = str(payload.get("answer", ""))
    evidence_text = "\n".join(
        str(payload.get(k, "")) for k in ("retrieved_context", "think_trace"))

    routing_fail = False

    want_route = case.get("route")
    if want_route and payload.get("route") != want_route:
        fails.append(f"경로: {want_route} 기대 -> {payload.get('route')}")
        routing_fail = True

    want_code = case.get("code")
    if want_code and want_code not in payload.get("think_trace", ""):
        fails.append(f"상품코드 {want_code}를 못 찾음")
        routing_fail = True

    # 제도 질문에 상품이 잘못 걸리는 건 경로가 맞아도 결함이다.
    # 근거 글자에 "■"가 있는지로 보면 원문 청크에도 그 글자가 있어서
    # 헛발질한다 - 상품을 인식했는지 자체로 본다.
    if case.get("no_product") and "인식된 상품" in payload.get("think_trace", ""):
        fails.append("상품이 걸리면 안 되는 질문인데 상품을 인식함")
        routing_fail = True

    must = case.get("must_answer", case.get("must", []))
    missing_in_answer = [s for s in must if not _answer_contains(answer_text, s)]
    for s in missing_in_answer:
        fails.append(f"'{s}' 답변에 없음")
    for s in case.get("must_not", []):
        if s in answer_text:
            fails.append(f"'{s}' 있으면 안 됨")

    # must_answer 한 단어만 있으면 "정답 단어가 어쩌다 들어간 딴 얘기"도
    # 통과한다 - 실측(INST-06): "중도인출"이 답에 있기만 하면 통과였는데,
    # "IRP로 입금할 수 있나요? 네, 가능합니다"처럼 질문이 실제로 물은
    # 여러 사유(주택구입/전세보증금/요양/파산 등) 중 아무것도 없어도
    # 통과해 버렸다. must_any_groups는 "이 항목들 중 하나는 있어야
    # 한다"는 그룹을 여러 개 두고, 그룹을 전부 만족해야 통과시킨다 -
    # 그룹마다 그 범주의 동의어 몇 개를 두므로(예: 무주택/주택구입/
    # 전세보증금) 표현이 조금 달라도 오탐하지 않는다.
    missing_groups = [
        group for group in case.get("must_any_groups", [])
        if not any(_answer_contains(answer_text, s) for s in group)
    ]
    for group in missing_groups:
        fails.append(f"{group} 중 어느 것도 답변에 없음")

    answer_pass = (
        not missing_in_answer and not missing_groups
        and not any(s in answer_text for s in case.get("must_not", []))
    )

    expected_evidence = case.get("expected_evidence")
    retrieval_pass = citation_pass = None
    if expected_evidence:
        expected_pairs = {(e["document_id"], e["page"]) for e in expected_evidence}
        retrieval_pass = bool(expected_pairs & _cited_doc_pages(evidence_text))
        citation_pass = bool(expected_pairs & _cited_doc_pages(answer_text))
        if not answer_pass:
            if not retrieval_pass:
                fails.append("정답 근거 문서가 검색 후보군에 없음(retrieval_pass=False)")
            elif not citation_pass:
                fails.append("정답 근거 문서가 후보엔 있었지만 답변이 다른 근거를 인용함"
                              "(citation_pass=False)")

    failure_type = None
    if routing_fail:
        failure_type = "routing"
    elif not answer_pass:
        if expected_evidence:
            failure_type = "retrieval" if not retrieval_pass else "answer_assembly"
        else:
            # expected_evidence가 없는 문항은 예전 방식(부분 문자열 기반)의
            # 느슨한 힌트로만 가른다 - 근거 문서를 아직 확인 못 했으므로
            # 강한 기준(retrieval_pass/citation_pass)을 적용할 근거가 없다.
            evidence_hint = all(s in evidence_text for s in must)
            failure_type = "answer_assembly" if evidence_hint else "retrieval"

    diag = {"answer_pass": answer_pass, "retrieval_pass": retrieval_pass,
            "citation_pass": citation_pass, "failure_type": failure_type}
    return (not fails), fails, diag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="실패한 항목의 답변 전문")
    ap.add_argument("--show-all", action="store_true", help="모든 항목의 답변 전문")
    ap.add_argument("--only", default=None, help="id가 이 글자로 시작하는 것만")
    args = ap.parse_args()

    cases = load_questions()
    if args.only:
        cases = [c for c in cases if c["id"].startswith(args.only)]

    passed, failed, errored = [], [], []
    for c in cases:
        try:
            payload = answer_payload(c["id"], c["q"])
        except Exception as e:  # 답변 경로가 터지는 것 자체가 가장 큰 결함
            errored.append((c, f"{type(e).__name__}: {e}"))
            print(f"[터짐] {c['id']} {c['q']}\n    {type(e).__name__}: {e}")
            continue
        ok, fails, diag = check(c, payload)
        (passed if ok else failed).append((c, payload, fails, diag))
        mark = "통과" if ok else "실패"
        detail = ""
        if not ok and diag["retrieval_pass"] is not None:
            detail = (f" retrieval_pass={diag['retrieval_pass']}"
                      f" citation_pass={diag['citation_pass']}")
        tag = "" if ok else f" [{diag['failure_type']}]{detail}"
        print(f"[{mark}]{tag} {c['id']} ({c.get('category')}) {c['q']}"
              + (f"\n    - " + "\n    - ".join(fails) if fails else ""))
        if args.show_all or (args.show and not ok):
            print("    --- 답변 ---")
            for line in str(payload.get("answer", "")).splitlines():
                print("    " + line)
            if c.get("note"):
                print(f"    [메모] {c['note']}")
            print()

    print(f"\n통과 {len(passed)} / 실패 {len(failed)} / 터짐 {len(errored)}"
          f" (전체 {len(cases)})")
    if failed:
        by_type = {}
        for c, _, _, diag in failed:
            by_type.setdefault(diag["failure_type"], []).append(c["id"])
        print("실패:", ", ".join(c["id"] for c, _, _, _ in failed))
        for ftype, ids in by_type.items():
            label = {"routing": "경로/상품코드 오인식",
                      "answer_assembly": "근거는 찾았는데 답변에 안 씀",
                      "retrieval": "근거 자체를 못 찾음"}.get(ftype, ftype)
            print(f"  - {label}: {', '.join(ids)}")
    if errored:
        print("터짐:", ", ".join(c["id"] for c, _ in errored))
    return 0


if __name__ == "__main__":
    sys.exit(main())
