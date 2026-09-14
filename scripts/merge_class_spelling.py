"""한 클래스를 표마다 다르게 적은 문서에서, 갈라진 보수 행을 하나로 모은다.

왜 필요한가
-----------
같은 문서 안에서도 요약표와 상세 보수표가 클래스 코드를 다르게 적는다.

    KR514X450008   요약표(5쪽) "(Ae)"        상세 보수표(34쪽) "A-e"
    KR5114420027   요약표(7쪽) "(Cp(퇴직연금))"  상세 보수표(35쪽) "Cp(퇴직연금)"
                   -> 요약표 쪽에서 중첩 괄호가 잘려 "Cp"로 읽힌다

그대로 두면 한 클래스가 두 행으로 쪼개진다. 그러면
- 클래스 목록이 실제보다 많아진다(16개짜리 펀드가 14개인데 16개로 보인다),
- "Ae"를 물으면 요약표 값만, "A-e"를 물으면 상세표 값만 나온다,
- 이름표(class_meaning)는 문서의 「종류형 명칭」 표 표기 하나만 아니까,
  나머지 한쪽은 뜻을 모르는 클래스가 된다.

무엇을 근거로 합치나
--------------------
표기가 비슷하다는 이유만으로 합치면 안 된다. 붙임표를 지우면 같아 보이는데
서로 다른 클래스인 경우가 있다(KR5114420027 실측).

    C-P          수수료미징구-오프라인-개인연금   총보수 0.43
    Cp(퇴직연금)  수수료미징구-오프라인-퇴직연금   총보수 0.35

한 번 이걸 표기로만 맞췄다가 퇴직연금 행이 개인연금 행을 덮어써서 개인연금
클래스가 통째로 퇴직연금으로 둔갑한 적이 있다. 연금 상품에서 이보다 나쁜
오류가 없다. 그래서 두 가지 근거를 요구한다.

  ① 문서의 「종류형 명칭」 표(class_meaning)가 그 표기를 쓰고 있을 것.
     합치는 방향은 늘 "명칭표가 쓴 표기" 쪽이다. 명칭표에 없는 표기끼리는
     건드리지 않는다.
  ② 어느 쪽으로 합칠지 문서가 분명히 말해 줄 것. 둘 중 하나다.
     - 그 열쇠(붙임표·괄호를 지운 표기)를 쓰는 이름표가 그 상품에 하나뿐이면
       짝이 하나로 정해진다.
     - 이름표가 둘 이상이면 값으로 가른다. 총보수·판매보수·총보수비용·
       동종유형보수 중 양쪽이 다 가진 항목이 전부 같은 이름표가 정확히
       하나일 때만 합친다. 위 C-P/Cp 예에서 Cp(0.35)는 Cp(퇴직연금)(0.35)와만
       맞고 C-P(0.43)와는 안 맞으므로 짝이 정해진다.

한쪽이라도 확인이 안 되면 쪼개진 채로 둔다. 잘못 합치는 것보다 낫다.

실행:
    python3 scripts/merge_class_spelling.py            # class_fees.json 갱신
    python3 scripts/merge_class_spelling.py --check    # 무엇을 합칠지만 출력
"""

import argparse
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASS_FEES_JSON = os.path.join(REPO_ROOT, "class_fees.json")
CLASS_MEANING_JSON = os.path.join(REPO_ROOT, "class_meaning.json")

# 값으로 가를 때 보는 항목. 문서가 "-"(없음)로 적은 칸은 숫자가 아니라서
# 저절로 빠진다 - 없음끼리 같다고 보고 합치면 근거가 너무 얇다.
COMPARE_FIELDS = ("total_fee", "distribution_fee",
                  "peer_avg_fee", "total_fee_and_cost")


def canon_key(code):
    """표기 차이만 지운 열쇠. "A-e"와 "Ae"가 같은 열쇠로 모인다.
    이건 "같은 클래스인지"의 답이 아니라 "따져 볼 후보"를 모으는 것뿐이다."""
    return re.sub(r"\(.*?\)", "", code or "").replace("-", "").upper()


def _nums(row):
    out = {}
    for f in COMPARE_FIELDS:
        try:
            out[f] = round(float(row.get(f)), 4)
        except (TypeError, ValueError):
            pass
    return out


def _agree(a, b):
    """양쪽이 다 가진 항목이 하나라도 있고, 그게 전부 같은가."""
    common = set(a) & set(b)
    return bool(common) and all(a[f] == b[f] for f in common)


def plan(fee_rows, meaning_rows):
    """{(상품, 지금 표기): 명칭표 표기} 와 근거 한 줄."""
    fees, labels = {}, {}
    for r in fee_rows:
        fees.setdefault(r["product_code"], {})[r["class_code"]] = r
    for r in meaning_rows:
        labels.setdefault(r["product_code"], set()).add(r["class_code"])

    moves, why = {}, {}
    for pc, rows in fees.items():
        groups = {}
        for c in rows:
            groups.setdefault(canon_key(c), {"fee": set(), "lab": set()})["fee"].add(c)
        for c in labels.get(pc, ()):
            groups.setdefault(canon_key(c), {"fee": set(), "lab": set()})["lab"].add(c)

        for g in groups.values():
            unlabeled = sorted(g["fee"] - g["lab"])
            if not unlabeled or not g["lab"]:
                continue
            for src in unlabeled:
                cands = sorted(g["lab"] - {src})
                if not cands:
                    continue
                if len(g["lab"]) == 1 and len(unlabeled) == 1:
                    # 짝이 하나로 정해진다. 그래도 상대에게 보수 행이 있으면
                    # 값이 맞는지 한 번 더 본다.
                    tgt = cands[0]
                    other = fees[pc].get(tgt)
                    if other is not None and not _agree(_nums(rows[src]), _nums(other)):
                        continue
                    moves[(pc, src)] = tgt
                    why[(pc, src)] = "명칭표에 이 표기가 하나뿐"
                    continue
                ok = [t for t in cands
                      if t in fees[pc] and _agree(_nums(rows[src]), _nums(fees[pc][t]))]
                if len(ok) == 1:
                    moves[(pc, src)] = ok[0]
                    why[(pc, src)] = f"값 일치(후보 {', '.join(cands)})"
    return moves, why


def _fold(dst, src):
    """src 행의 내용을 dst 행에 채워 넣는다. dst에 이미 있는 값은 안 건드린다."""
    for k, v in src.items():
        if k in ("product_code", "class_code"):
            continue
        if k in ("source_pages",):
            dst[k] = sorted(set(dst.get(k) or []) | set(v or []))
        elif k == "value_sources":
            seen = {json.dumps(s, sort_keys=True, ensure_ascii=False)
                    for s in (dst.get(k) or [])}
            merged = list(dst.get(k) or [])
            for s in v or []:
                j = json.dumps(s, sort_keys=True, ensure_ascii=False)
                if j not in seen:
                    seen.add(j)
                    merged.append(s)
            dst[k] = merged
        elif k == "field_source_pages":
            fsp = dict(v or {})
            fsp.update(dst.get(k) or {})
            dst[k] = fsp
        elif k == "confidence":
            # 두 표가 같은 값을 말했다는 게 확인된 행이다. 낮출 이유가 없다.
            dst[k] = max(dst.get(k) or 0, v or 0)
        elif k == "evidence":
            a, b = dst.get(k), v
            dst[k] = a if a == b else " ⧺ ".join(x for x in (a, b) if x)
        elif dst.get(k) in (None, "", [], {}):
            dst[k] = v
    return dst


def apply_moves(fee_rows, moves):
    by_key, order = {}, []
    for r in fee_rows:
        k = (r["product_code"], r["class_code"])
        by_key[k] = r
        order.append(k)

    merged, renamed = 0, 0
    for (pc, src), tgt in moves.items():
        row = by_key.pop((pc, src), None)
        if row is None:
            continue
        order.remove((pc, src))
        row = dict(row, class_code=tgt)
        row["merged_from_spelling"] = src
        if (pc, tgt) in by_key:
            _fold(by_key[(pc, tgt)], row)
            by_key[(pc, tgt)]["merged_from_spelling"] = src
            merged += 1
        else:
            by_key[(pc, tgt)] = row
            order.append((pc, tgt))
            renamed += 1
    return [by_key[k] for k in order], merged, renamed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--class-fees", default=CLASS_FEES_JSON)
    ap.add_argument("--class-meaning", default=CLASS_MEANING_JSON)
    ap.add_argument("--check", action="store_true", help="저장하지 않고 계획만")
    args = ap.parse_args()

    with open(args.class_fees, "r", encoding="utf-8") as f:
        fee_rows = json.load(f)
    with open(args.class_meaning, "r", encoding="utf-8") as f:
        meaning_rows = json.load(f)

    moves, why = plan(fee_rows, meaning_rows)
    for (pc, src), tgt in sorted(moves.items()):
        print(f"{pc}  {src!r} → {tgt!r}   ({why[(pc, src)]})")
    if not moves:
        print("맞출 표기 없음")

    rows, merged, renamed = apply_moves(fee_rows, moves)
    print(f"{len(fee_rows)}건 → {len(rows)}건 "
          f"(한 행으로 합침 {merged}건, 표기만 바꿈 {renamed}건)")
    if args.check:
        return
    with open(args.class_fees, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"→ {args.class_fees}")


if __name__ == "__main__":
    main()
