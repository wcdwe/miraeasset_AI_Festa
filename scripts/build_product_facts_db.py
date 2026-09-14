"""
연금 Agent 과제 - 상품 팩트(product_master/class_fees/class_returns)를
SQLite 표로 적재

product_name/asset_type/risk_level/총보수/수익률처럼 "정답이 하나로 정해진
숫자·분류"를 텍스트 재검색 없이 바로 조회할 수 있게 하는 게 목적이다.
특히 "A상품이랑 B상품 총보수 비교해줘" 같은 비교 질의에서, 문서 텍스트
청크를 여러 개 긁어와 LLM에 던지는 대신 이 표에서 숫자만 뽑아 짧게
답할 수 있어 토큰을 크게 아낄 수 있다 (scripts/compare_products.py 참고).

기존 structured_store.db(표 전문검색용 tables/tables_fts)와 같은 DB 파일에
추가한다 - 근거 문서 표시(page 등)까지 한 곳에서 조회 가능하게.

confidence 필드에 대한 주의: 이 값은 "이 행의 모든 필드가 다 맞다"는
뜻이 아니다("다 제대로 뽑았어야 1이어야 하는 거 아니냐"는 지적을 받고
정리함). class_fees는 "class_code(클래스 이름표)를 다른 클래스와 헷갈릴
위험 없이 찾았는가"만, fund_aum은 "자산총계/부채총계를 운용사 자체
재무제표가 아니라 이 펀드 것으로 확신할 수 있는가"만 본다 - 그 외
필드(총보수 숫자, 판매수수료 문구, unit 판별 등)는 서로 다른 이유로
틀릴 수 있어 하나의 점수로 합칠 근거가 없다(실제로 이번 세션에서 고친
버그 대부분이 class_code는 처음부터 confidence 1.0이었던 행에서
나왔다). "행이 실제로 맞는지"는 confidence가 아니라 각 extract_*.py
실행 후 매번 돌리는 전수 이상치 검사(1y>500/total_fee>10/class_code
중복 등, class_fees용 - README 참고)가 실질적으로 그 역할을 한다.

사용법:
    python scripts/build_product_master.py     # product_master.json 생성
    python scripts/extract_class_fees.py        # class_fees.json 생성
    python scripts/extract_class_returns.py     # class_returns.json 생성
    python scripts/build_product_facts_db.py    # 위 3개를 SQLite로 적재
"""

import argparse
import json
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")

SCHEMA = """
DROP TABLE IF EXISTS product_master;
CREATE TABLE product_master (
    product_code TEXT PRIMARY KEY,
    product_name TEXT,
    product_name_confidence REAL,
    asset_type TEXT,
    asset_type_confidence REAL,
    risk_level INTEGER,
    risk_level_confidence REAL
);

DROP TABLE IF EXISTS class_fees;
CREATE TABLE class_fees (
    product_code TEXT NOT NULL,
    class_code TEXT NOT NULL,
    sales_commission_desc TEXT,
    total_fee REAL,
    distribution_fee REAL,
    peer_avg_fee REAL,
    total_fee_and_cost REAL,
    cost_1y INTEGER,
    cost_2y INTEGER,
    cost_3y INTEGER,
    cost_5y INTEGER,
    cost_10y INTEGER,
    -- 보수는 시점에 따라 바뀌는 값이라 "언제 기준"인지 없이 숫자만
    -- 내보내면 틀린 답이 된다(간이투자설명서 자체가 작성기준일을 찍는다).
    as_of TEXT,
    -- 운용전환일 전/후로 보수가 나뉘는 상품(목표전환형)의 전환 후 값.
    -- 전환일이 날짜가 아니라 "목표기준가격 도달"이라는 조건이라
    -- conversion_trigger_nav_price(원)와 함께 본다.
    total_fee_after_conversion REAL,
    conversion_trigger_nav_price INTEGER,
    page INTEGER,
    confidence REAL,
    PRIMARY KEY (product_code, class_code),
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX idx_class_fees_product ON class_fees(product_code);

-- 같은 값을 문서의 두 표가 다르게 적을 때, 어느 표가 무엇이라고 했는지
-- (extract_class_fees.py).
--
-- 간이투자설명서는 앞쪽 "요약정보"와 뒤쪽 "13. 보수 및 수수료"에 같은
-- 값을 두 번 싣는데, 총보수·비용은 두 곳이 어긋나는 문서가 있다.
--
--   KR5110501016 종류A
--   3쪽(요약표)  총보수·비용 0.31   = 총보수 + 0.01 (전 클래스 일괄)
--   27쪽(상세표) 총보수·비용 0.30   = 총보수 + 그 클래스 기타비용("-")
--
-- 어느 쪽이 맞다고 판정할 근거가 없다. 한 쪽을 골라 담으면 그건 문서에
-- 없는 판단을 우리가 하는 것이고, 고객이 다른 쪽 페이지를 열면 틀린
-- 답이 된다. 그래서 둘 다 담고 각각 어느 쪽에서 왔는지 남긴다.
--
-- class_fees는 "답변에 쓰는 한 줄"로 그대로 두고, 이 표는 그 한 줄이
-- 어느 표의 값인지와 다른 표는 뭐라고 했는지를 보여 준다. 답변 규칙은
-- "한 클래스의 값은 그 클래스가 실려 있는 표 한 곳에서만 가져오고
-- 근거 페이지도 그 표로 단다" - 그래야 고객이 근거 페이지를 열었을 때
-- 우리가 말한 숫자가 거기 있다.
DROP TABLE IF EXISTS class_fee_sources;
CREATE TABLE class_fee_sources (
    product_code TEXT NOT NULL,
    class_code TEXT NOT NULL,
    field TEXT NOT NULL,        -- total_fee / distribution_fee / ...
    source TEXT NOT NULL,       -- 요약표 / 상세표
    value TEXT,
    page INTEGER,
    PRIMARY KEY (product_code, class_code, field, source),
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX IF NOT EXISTS idx_class_fee_sources_product
    ON class_fee_sources(product_code);

-- id를 그대로 둔다(복합키로 못 바꾼다) - row_kind가 fund/benchmark/
-- volatility인 행은 클래스 그룹(개인연금/퇴직연금 등)별로 여러 개가
-- 나올 수 있어 class_code가 NULL인 채로 같은 (product_code, row_kind)
-- 조합이 여러 번 나온다(실측: KR5114420027이 benchmark 3건, fund
-- 3건 - 클래스 그룹마다 하나씩). 그래서 아래 부분 유니크 인덱스는
-- class_code가 실제로 있는(row_kind='class_return') 행에만 건다 -
-- 이 행들만 확인해 보니 중복이 0건이었다(build_product_facts_db.py
-- 작성 시점 실측, structured_store.db에 대해 직접 조회로 확인).
DROP TABLE IF EXISTS class_returns;
CREATE TABLE class_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT,
    row_kind TEXT,
    class_code TEXT,
    inception_date TEXT,
    return_1y REAL,
    return_2y REAL,
    return_3y REAL,
    return_5y REAL,
    return_since_inception REAL,
    page INTEGER,
    confidence REAL,
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX idx_class_returns_product ON class_returns(product_code);
CREATE UNIQUE INDEX idx_class_returns_unique_class
    ON class_returns(product_code, class_code, row_kind)
    WHERE class_code IS NOT NULL;

-- 참고용: 운용전문인력 표의 "운용규모"는 이 상품 하나의 AUM이 아니라
-- 해당 운용역/운용사가 운용하는 전체 펀드 합산 규모다(is_product_aum=0
-- 고정). 6축 정답(product_master/class_fees/class_returns)과 섞이지
-- 않도록 별도 테이블로 분리한다.
DROP TABLE IF EXISTS manager_info;
CREATE TABLE manager_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT,
    name TEXT,
    birth_year INTEGER,
    manager_fund_count INTEGER,
    manager_aum_100m_won INTEGER,
    is_product_aum INTEGER,
    career TEXT,
    page INTEGER,
    confidence REAL,
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX idx_manager_info_product ON manager_info(product_code);

-- 6축 중 AUM(시장잔고): 펀드 자체 재무상태표의 자산총계-부채총계 =
-- 순자산총계를 이 상품의 실제 AUM으로 취급한다(manager_info와 달리
-- is_product_aum 플래그 없음 - 이건 진짜 이 상품의 값).
DROP TABLE IF EXISTS fund_aum;
CREATE TABLE fund_aum (
    product_code TEXT PRIMARY KEY,
    unit TEXT,
    net_asset_latest REAL,
    net_asset_won REAL,
    page INTEGER,
    confidence REAL,
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);

-- 클래스 코드가 무슨 뜻인지 (extract_class_meaning.py).
-- 코드로 뜻을 짐작할 수 없어서(운용사마다 C-P가 개인연금이기도 퇴직연금이기도
-- 하다) 문서가 적어 둔 이름표를 그대로 담는다. retail=0이면 기관·고액·랩
-- 전용이라 일반 고객이 살 수 없는 클래스이므로 답변에서 빼야 한다.
DROP TABLE IF EXISTS class_meaning;
CREATE TABLE class_meaning (
    product_code TEXT NOT NULL,
    class_code TEXT NOT NULL,
    fee_type TEXT,          -- 선취 / 미징구 / 후취
    channel TEXT,           -- 오프라인 / 온라인 / 온라인슈퍼 / 직판
    account_type TEXT,      -- 개인연금 / 퇴직연금 / NULL
    attributes TEXT,        -- 쉼표로 이은 원문 속성
    retail INTEGER,         -- 1이면 일반 고객이 가입 가능
    description TEXT,       -- 고객에게 보여 줄 말 ("연금저축 · 온라인")
    raw_label TEXT,         -- 문서 원문 이름표
    page INTEGER,
    PRIMARY KEY (product_code, class_code),
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX IF NOT EXISTS idx_class_meaning_product ON class_meaning(product_code);

-- 투자자가 직접 부담하는 수수료와 가입자격 (extract_class_charges.py).
-- 값을 숫자로 줄이지 않고 문장을 그대로 담는다. "90일미만 이익금의 30%.
-- 다만 2013년1월17일 이후 환매 청구하는 경우에는 부과하지 않음"을 숫자로
-- 줄이면 안 떼는 수수료를 뗀다고 답하게 된다.
-- "없음"은 문서가 없다고 적은 것이고, NULL은 우리가 모르는 것이다.
DROP TABLE IF EXISTS class_charges;
CREATE TABLE class_charges (
    product_code TEXT NOT NULL,
    class_code TEXT NOT NULL,
    eligibility TEXT,       -- "제한 없음" / "온라인 투자자" / "1년이상 종류C1가입자"
    front_load_fee TEXT,    -- 선취판매수수료
    back_load_fee TEXT,     -- 후취판매수수료
    redemption_fee TEXT,    -- 환매수수료
    switch_fee TEXT,        -- 전환수수료
    page INTEGER,
    PRIMARY KEY (product_code, class_code),
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX IF NOT EXISTS idx_class_charges_product ON class_charges(product_code);

-- 펀드 전체에 적용되는 환매수수료 문장. 클래스별 표가 없는 문서라도
-- "이 투자신탁은 환매수수료를 부과하지 않습니다" 한 줄이면 답이 된다.
-- 매입/환매 기준가격 적용과 환매대금 지급시기 (extract_trade_rules.py).
-- "17시 50분에 환매 청구하면?", "돈 언제 들어와요?"에 답하기 위한 것.
-- 조건문이라 문장을 그대로 담는다. 기준시각이 문서마다 다르고
-- (15시 30분 / 오후 5시 / 17시) 시각별로 적용일이 갈리기 때문에,
-- "제2영업일" 하나만 뽑으면 틀린 답이 된다.
-- 해마다의 수익률 (extract_yearly_returns.py).
-- class_returns의 연평균(누적)과는 다른 값이다. "최근 3년 -31.08%"는
-- 3년을 묶은 값이고, "작년에 얼마 벌었나"는 여기 있다.
-- period를 함께 담는 이유: "3년차"가 몇 년 몇 월부터인지는 문서마다
-- 다르다(24.01.01~24.12.31 / 24.05.20~25.05.19). 년차만 말하면 어느
-- 기간인지 알 수 없다.
DROP TABLE IF EXISTS yearly_returns;
CREATE TABLE yearly_returns (
    product_code TEXT NOT NULL,
    row_kind TEXT NOT NULL,     -- class_return / fund / benchmark
    class_code TEXT,
    year_rank INTEGER NOT NULL, -- 최근 N년차
    period TEXT,
    return_pct REAL,
    page INTEGER,
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX IF NOT EXISTS idx_yearly_returns_product ON yearly_returns(product_code);
-- class_returns와 달리 이 표는 (product_code, row_kind, class_code,
-- year_rank) 조합이 class_code가 NULL인 fund/benchmark 행까지 포함해도
-- 실측 결과 중복이 0건이었다(연도별 표는 클래스 그룹별로도 항상
-- year_rank가 갈리기 때문으로 보인다) - 그래서 부분 인덱스 없이 그대로
-- 유니크로 건다.
CREATE UNIQUE INDEX idx_yearly_returns_unique
    ON yearly_returns(product_code, row_kind, class_code, year_rank);

DROP TABLE IF EXISTS trade_rules;
CREATE TABLE trade_rules (
    product_code TEXT NOT NULL,
    kind TEXT NOT NULL,     -- 매입기준가 / 환매기준가
    text TEXT NOT NULL,
    page INTEGER,
    PRIMARY KEY (product_code, kind),
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);

-- 이 펀드가 무엇에 얼마나 투자하고 있는지 (extract_asset_mix.py).
-- "이 펀드 뭐에 투자해요?"에 답하기 위한 것. 위험등급만으로는 주식형인지
-- 채권형인지도 흐릿하다. 비중은 시점에 따라 바뀌는 값이라 as_of 없이
-- 숫자만 내보내면 틀린 답이 된다.
DROP TABLE IF EXISTS asset_mix;
CREATE TABLE asset_mix (
    product_code TEXT NOT NULL,
    asset TEXT NOT NULL,        -- 주식 / 채권 / 파생상품(장내) / 단기대출및예금 ...
    amount REAL,
    -- amount/total_amount의 단위. 문서마다 다르다(실측: KR5127450117은
    -- 억원, KR5129420025는 백만원) - unit 없이 숫자만 비교하면 100배
    -- 차이 나는 걸 놓친다. 단위를 못 찾은 문서는 NULL - 그때는 금액
    -- 비교에 쓰면 안 되고 pct(비중, 단위 무관)만 써야 한다.
    unit TEXT,
    pct REAL,
    total_amount REAL,
    -- 문서가 비율을 안 싣고 금액만 적은 경우 자산총액으로 나눠 만든
    -- 값이다. 문서에 그대로 적힌 숫자가 아니라서 구분해 둔다.
    pct_derived INTEGER,
    as_of TEXT,
    page INTEGER,
    PRIMARY KEY (product_code, asset),
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
CREATE INDEX IF NOT EXISTS idx_asset_mix_product ON asset_mix(product_code);

DROP TABLE IF EXISTS product_charges;
CREATE TABLE product_charges (
    product_code TEXT PRIMARY KEY,
    redemption_note TEXT,
    FOREIGN KEY (product_code) REFERENCES product_master(product_code)
);
"""


def to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def load_product_master(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            """
            INSERT INTO product_master
                (product_code, product_name, product_name_confidence,
                 asset_type, asset_type_confidence, risk_level, risk_level_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["product_name"]["value"],
                r["product_name"]["confidence"],
                r["asset_type"]["value"],
                r["asset_type"]["confidence"],
                r["risk_level"]["value"],
                r["risk_level"]["confidence"],
            ),
        )
        n += 1
    return n


def load_class_fees(conn, path):
    # class_fees.json의 "fee_breakdown"(상세표 보강으로 채워진 클래스에만
    # 있음 - 집합투자업자보수/신탁업자보수/기타비용 등 세부 항목)은 6축
    # 숫자 비교 질의엔 안 쓰여서 SQL 스키마에 안 넣는다 - JSON 파일에서
    # 그대로 조회하면 된다(README "class fee의 역할" 참고: 스키마 밖
    # 데이터라고 버리는 게 아니라 JSON에 그대로 보존하는 것).
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        cp = r.get("cost_projection_per_10m", {})
        conn.execute(
            """
            INSERT INTO class_fees
                (product_code, class_code, sales_commission_desc, total_fee,
                 distribution_fee, peer_avg_fee, total_fee_and_cost,
                 cost_1y, cost_2y, cost_3y, cost_5y, cost_10y,
                 as_of, total_fee_after_conversion, conversion_trigger_nav_price,
                 page, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["class_code"],
                r["sales_commission_desc"],
                to_float(r["total_fee"]),
                to_float(r["distribution_fee"]),
                to_float(r["peer_avg_fee"]),
                to_float(r["total_fee_and_cost"]),
                to_int(cp.get("1y")),
                to_int(cp.get("2y")),
                to_int(cp.get("3y")),
                to_int(cp.get("5y")),
                to_int(cp.get("10y")),
                r.get("as_of"),
                to_float(r.get("total_fee_after_conversion")),
                r.get("conversion_trigger_nav_price"),
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def load_asset_mix(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        for it in r.get("items") or []:
            conn.execute(
                "INSERT OR REPLACE INTO asset_mix (product_code, asset, amount,"
                " unit, pct, total_amount, pct_derived, as_of, page)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["product_code"], it["asset"], to_float(it.get("amount")),
                 r.get("unit"),
                 to_float(it.get("pct")), to_float(r.get("total_amount")),
                 1 if r.get("pct_derived") else 0,
                 r.get("as_of"), r.get("page")))
            n += 1
    return n


def load_class_fee_sources(conn, path):
    """class_fees.json의 각 행이 들고 있는 "어느 표가 뭐라고 했는지"를
    편다. 값이 한 표에서만 나온 클래스도 그대로 담는다 - 나중에 "이
    숫자는 몇 쪽에서 왔나"를 물을 때 필요하다."""
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        for s in r.get("value_sources") or []:
            conn.execute(
                "INSERT OR REPLACE INTO class_fee_sources "
                "(product_code, class_code, field, source, value, page) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (r["product_code"], r["class_code"], s["field"],
                 s["source"], s.get("value"), s.get("page")))
            n += 1
    return n


_CANON = None
_MEANING = None


def _canon_key(code):
    """표기 차이만 지운 열쇠. "A-e"와 "Ae"가 같은 열쇠로 모인다.
    이건 "같은 클래스인지"의 답이 아니라 "따져 볼 후보"를 모으는 것뿐이다."""
    return re.sub(r"\(.*?\)", "", code or "").replace("-", "").upper()


def canonical_code(conn, product_code, class_code):
    """같은 클래스를 표마다 다르게 적은 것을 보수표 표기로 맞춘다.

    같은 문서 안에서도 보수표는 "A-e", 뒤쪽 수익률 상세표는 "Ae"로
    적는 일이 있다(KR5110501016 실측). 그대로 두면 A-e를 물었을 때
    수수료는 나오는데 수익률은 안 나온다 - 한 클래스가 둘로 쪼개진다.

    그런데 표기만 보고 맞추면 안 된다. 붙임표를 지워 보면 같아 보이는데
    실제로는 서로 다른 클래스인 경우가 있다(KR5114420027 실측).

        C-P          수수료미징구-오프라인-개인연금
        Cp(퇴직연금)  수수료미징구-오프라인-퇴직연금

    처음엔 표기만 맞췄다가 퇴직연금 행이 개인연금 행을 덮어써서, 개인연금
    클래스가 통째로 퇴직연금으로 둔갑했다. 연금 상품에서 이보다 나쁜
    오류가 없다. 그래서 문서가 적어 둔 이름표로 같은 클래스임을 확인한
    뒤에만 맞춘다.

      - 보수표에 같은 열쇠의 코드가 둘 이상이면 손대지 않는다.
        어느 쪽으로 맞춰야 할지 문서가 말해 주지 않는다
        (KR5120420039 실측: 보수표에 C-E와 CE가 나란히 있다).
        위 KR5114420027의 Cp도 여기서 걸린다 - 열쇠 CP를 쓰는 보수표
        코드가 C-P, Cp, Cp(퇴직연금) 셋이라 후보가 하나로 안 좁혀진다.
      - 양쪽 다 이름표가 있으면, 이름표가 완전히 같을 때만 맞춘다.
      - 한쪽에 이름표가 아예 없으면(=문서의 「종류형 명칭」 표가 그
        표기를 모른다) 그건 클래스가 아니라 표기 차이다. 상대에 이름표가
        있고 후보가 하나뿐일 때만 맞춘다.

        이 갈래를 안 두면 문서가 두 표에서 코드를 다르게 적은 클래스가
        쪼개진 채로 남는다(6건 실측). 명칭표는 C(장마)라고 적어 뒀는데
        수익률표는 ClassC(장마)를 "C"로 읽히게 적어 두는 식이라,
        C(장마)를 물으면 보수는 나오는데 수익률이 안 나온다.

            KR5120450015  수익률표 C      <- 명칭표·보수표 C(장마)
            KR5120420091  수익률표 C-P    <- 명칭표·보수표 C-P(연금)
            KR5120420039  수익률표 Ai     <- 명칭표·보수표 A-i

        "이름표가 없다"가 근거가 되는 이유는 이름표가 보수표 클래스를
        빠짐없이 덮고 있기 때문이다(verify_data의 "뜻을 모르는 클래스"
        0건). 문서가 이름 붙인 클래스라면 이름표에 있어야 한다."""
    global _CANON, _MEANING
    if _CANON is None:
        by_key = {}
        for pc, cc in conn.execute(
                "SELECT product_code, class_code FROM class_fees "
                "WHERE class_code IS NOT NULL"):
            by_key.setdefault((pc, _canon_key(cc)), set()).add(cc)
        _CANON = {k: next(iter(v)) for k, v in by_key.items() if len(v) == 1}
        _MEANING = {}
        for pc, cc, ft, ch, at, attrs in conn.execute(
                "SELECT product_code, class_code, fee_type, channel, "
                "account_type, attributes FROM class_meaning"):
            _MEANING[(pc, cc)] = (ft, ch, at, attrs)
    if not class_code:
        return class_code
    target = _CANON.get((product_code, _canon_key(class_code)))
    if target is None or target == class_code:
        return class_code
    mine = _MEANING.get((product_code, class_code))
    theirs = _MEANING.get((product_code, target))
    if theirs is None:
        # 맞출 상대가 무슨 클래스인지 문서가 말해 주지 않는다.
        return class_code
    if mine is None:
        # 이 표기는 문서의 명칭표에 없다 = 같은 클래스를 다르게 적은 것.
        return target
    return target if mine == theirs else class_code


def load_class_returns(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        v = r.get("values", {})
        conn.execute(
            """
            INSERT INTO class_returns
                (product_code, row_kind, class_code, inception_date,
                 return_1y, return_2y, return_3y, return_5y, return_since_inception,
                 page, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["row_kind"],
                canonical_code(conn, r["product_code"], r["class_code"]),
                r.get("inception_date"),
                to_float(v.get("1y")),
                to_float(v.get("2y")),
                to_float(v.get("3y")),
                to_float(v.get("5y")),
                to_float(v.get("since_inception")),
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def load_manager_info(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            """
            INSERT INTO manager_info
                (product_code, name, birth_year, manager_fund_count,
                 manager_aum_100m_won, is_product_aum, career, page, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["name"],
                r["birth_year"],
                r["manager_fund_count"],
                r.get("manager_aum_100m_won"),
                0,
                r.get("career"),
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def load_fund_aum(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    unit_multiplier = {"원": 1, "천원": 1_000, "백만원": 1_000_000}
    n = 0
    for r in records:
        won = r["net_asset_latest"] * unit_multiplier.get(r["unit"], 1)
        conn.execute(
            """
            INSERT INTO fund_aum
                (product_code, unit, net_asset_latest, net_asset_won, page, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["unit"],
                r["net_asset_latest"],
                won,
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def load_class_meaning(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            """
            INSERT OR REPLACE INTO class_meaning
                (product_code, class_code, fee_type, channel, account_type,
                 attributes, retail, description, raw_label, page)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                # class_meaning은 이름표의 출처라서 표기를 손대지 않는다.
                # (product_code, class_code)가 기본키라 표기를 맞추면 뜻이
                # 다른 두 행이 서로를 덮어쓴다 - 실제로 KR5114420027의
                # 개인연금 행이 퇴직연금 행에 덮여 사라진 적이 있다.
                r["product_code"], r["class_code"], r.get("fee_type"),
                r.get("channel"), r.get("account_type"),
                ",".join(r.get("attributes") or []),
                1 if r.get("retail") else 0,
                r.get("description"), r.get("raw_label"), r.get("page"),
            ),
        )
        n += 1
    return n


def load_class_charges(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            """
            INSERT OR REPLACE INTO class_charges
                (product_code, class_code, eligibility, front_load_fee,
                 back_load_fee, redemption_fee, switch_fee, page)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (r["product_code"], canonical_code(conn, r["product_code"], r["class_code"]), r.get("eligibility"),
             r.get("front_load_fee"), r.get("back_load_fee"),
             r.get("redemption_fee"), r.get("switch_fee"), r.get("page")),
        )
        n += 1
    return n


def load_product_charges(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            "INSERT OR REPLACE INTO product_charges (product_code, redemption_note)"
            " VALUES (?, ?)", (r["product_code"], r.get("redemption_note")))
        n += 1
    return n


def load_trade_rules(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            "INSERT OR REPLACE INTO trade_rules (product_code, kind, text, page)"
            " VALUES (?, ?, ?, ?)",
            (r["product_code"], r["kind"], r["text"], r.get("page")))
        n += 1
    return n


def load_yearly_returns(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            "INSERT INTO yearly_returns (product_code, row_kind, class_code,"
            " year_rank, period, return_pct, page) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["product_code"], r["row_kind"], canonical_code(conn, r["product_code"], r.get("class_code")),
             r["year_rank"], r.get("period"), r.get("return_pct"), r.get("page")))
        n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description="상품 팩트 3종을 SQLite로 적재")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--product-master", default=os.path.join(REPO_ROOT, "product_master.json"))
    parser.add_argument("--class-fees", default=os.path.join(REPO_ROOT, "class_fees.json"))
    parser.add_argument("--class-returns", default=os.path.join(REPO_ROOT, "class_returns.json"))
    parser.add_argument("--manager-info", default=os.path.join(REPO_ROOT, "manager_info.json"))
    parser.add_argument("--fund-aum", default=os.path.join(REPO_ROOT, "fund_aum.json"))
    parser.add_argument("--class-meaning",
                        default=os.path.join(REPO_ROOT, "class_meaning.json"))
    parser.add_argument("--class-charges",
                        default=os.path.join(REPO_ROOT, "class_charges.json"))
    parser.add_argument("--yearly-returns",
                        default=os.path.join(REPO_ROOT, "yearly_returns.json"))
    parser.add_argument("--trade-rules",
                        default=os.path.join(REPO_ROOT, "trade_rules.json"))
    parser.add_argument("--product-charges",
                        default=os.path.join(REPO_ROOT, "product_charges.json"))
    parser.add_argument("--asset-mix",
                        default=os.path.join(REPO_ROOT, "asset_mix.json"))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    # 이 스크립트는 매번 표를 전부 지우고 새로 만든다. FK를 켠 채로
    # DROP TABLE product_master를 실행하면, 이전 실행에서 이미 만들어진
    # class_fees 등이 그 표를 참조하고 있어서 재실행 시(=이 표들이 이미
    # 있는 두 번째 실행부터) "FOREIGN KEY constraint failed"로 죽는다
    # (실측: 처음 한 번은 통과하고 다시 돌리면 바로 죽었다). DROP/CREATE
    # 하는 동안은 꺼 두고, 실제 데이터를 넣기 시작하기 전에 켠다 - SQLite
    # 공식 문서가 스키마를 바꿀 때 권하는 순서 그대로다.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")

    n1 = load_product_master(conn, args.product_master)
    n2 = load_class_fees(conn, args.class_fees)
    # class_meaning이 먼저다 - 표기가 갈린 클래스를 맞출지 말지를
    # 이름표로 판단하기 때문에(canonical_code 참고), 이름표가 DB에
    # 들어와 있어야 한다.
    n6 = load_class_meaning(conn, args.class_meaning)
    n3 = load_class_returns(conn, args.class_returns)
    n4 = load_manager_info(conn, args.manager_info)
    n5 = load_fund_aum(conn, args.fund_aum)
    n7 = load_class_charges(conn, args.class_charges)
    n8 = load_product_charges(conn, args.product_charges)
    n9 = load_trade_rules(conn, args.trade_rules)
    n10 = load_yearly_returns(conn, args.yearly_returns)
    n11 = load_class_fee_sources(conn, args.class_fees)
    n12 = load_asset_mix(conn, args.asset_mix)

    conn.commit()
    conn.close()
    print(
        f"product_master {n1}건, class_fees {n2}건, class_returns {n3}건, "
        f"manager_info(참고용) {n4}건, fund_aum {n5}건, class_meaning {n6}건, class_charges {n7}건, product_charges {n8}건, trade_rules {n9}건, yearly_returns {n10}건, class_fee_sources {n11}건, asset_mix {n12}건 → {args.db}"
    )


if __name__ == "__main__":
    main()
