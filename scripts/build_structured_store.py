"""
연금 Agent 과제 - 표 데이터 구조화 스크립트

extracted/{institution,products}/*_tables.json 을 모아 SQLite로 만든다.
표 형태가 문서마다 제각각(헤더 위치, 병합 셀 등)이라 완전한 컬럼 정규화 대신,
각 표를 "레코드 하나"로 저장하고 행을 이어붙인 검색 텍스트(row_text)를 같이
넣어 키워드/FTS 검색과 근거 표시(doc_id + page + table_index)를 모두
지원하는 절충안을 쓴다. 세액공제 한도, 위험등급, 총보수처럼 숫자가 명확한
표는 이 row_text 검색만으로도 질의에 필요한 행을 특정할 수 있다.

테이블:
- tables(id, doc_type, doc_id, source_doc, product_code, page, table_index,
         rows, cols, data_json, row_text)
- tables_fts: row_text 위 FTS5 가상 테이블 (구조화 검색용)

사용법:
    python scripts/build_structured_store.py
    python scripts/build_structured_store.py --output structured_store.db
"""

import argparse
import json
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED_DIR = os.path.join(REPO_ROOT, "extracted")
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "integrated", "structured_store.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    source_doc TEXT,
    product_code TEXT,
    page INTEGER,
    table_index INTEGER,
    rows INTEGER,
    cols INTEGER,
    data_json TEXT NOT NULL,
    row_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tables_doc_id ON tables(doc_id);
CREATE INDEX IF NOT EXISTS idx_tables_product_code ON tables(product_code);
CREATE INDEX IF NOT EXISTS idx_tables_doc_type ON tables(doc_type);

CREATE VIRTUAL TABLE IF NOT EXISTS tables_fts USING fts5(
    row_text,
    content='tables',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS tables_ai AFTER INSERT ON tables BEGIN
    INSERT INTO tables_fts(rowid, row_text) VALUES (new.id, new.row_text);
END;
"""

# 청크 본문 위의 글자 단위 검색 색인.
#
# 왜 필요한가: 의미 검색은 문자 n-gram TF-IDF를 256차원으로 줄여 쓰는데,
# 그 과정에서 "기타소득세", "사전지정운용방법" 같은 정확한 용어의 신호가
# 뭉개진다. 검증 세트를 돌려 보니 답이 코퍼스에 분명히 있는데도(기타소득세
# 10곳, 사전지정운용 3곳) 엉뚱한 FAQ 청크가 1등으로 올라왔다. 연금 질문은
# 제도 이름·세율·한도 같은 정확한 말이 답을 가르는 경우가 많아서, 의미
# 검색만으로는 부족하고 글자 그대로 찾는 검색이 함께 있어야 한다.
#
# 토크나이저로 trigram을 쓰는 이유: 기본 unicode61은 공백으로만 끊어서
# "기타소득세는"이 "기타소득세"로 안 걸린다. 형태소 분석기 없이 한국어
# 부분 문자열을 잡으려면 trigram이 가장 간단하다.
CHUNK_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT,
    doc_type TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    source_doc TEXT,
    product_code TEXT,
    page INTEGER,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc_type ON chunks(doc_type);
CREATE INDEX IF NOT EXISTS idx_chunks_product_code ON chunks(product_code);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def rows_to_text(data):
    """표의 각 행을 ' | '로 이어붙이고 행끼리는 줄바꿈으로 구분한 검색용 텍스트."""
    lines = []
    for row in data:
        cells = [c for c in row if c]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def iter_table_records():
    for doc_type in ("institution", "products"):
        d = os.path.join(EXTRACTED_DIR, doc_type)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith("_tables.json"):
                continue
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                tables = json.load(f)
            for t in tables:
                row_text = rows_to_text(t.get("data", []))
                if not row_text.strip():
                    continue  # 빈/깨진 표는 구조화 저장소에서 제외 (검색 노이즈만 늘림)
                yield {
                    "doc_type": t.get("doc_type", doc_type),
                    "doc_id": t.get("doc_id", name.replace("_tables.json", "")),
                    "source_doc": t.get("source_doc"),
                    "product_code": t.get("product_code"),
                    "page": t.get("page"),
                    "table_index": t.get("table_index"),
                    "rows": t.get("rows"),
                    "cols": t.get("cols"),
                    "data_json": json.dumps(t.get("data", []), ensure_ascii=False),
                    "row_text": row_text,
                }


def iter_chunk_records():
    for doc_type in ("institution", "products"):
        d = os.path.join(EXTRACTED_DIR, doc_type)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith("_chunks.json"):
                continue
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                chunks = json.load(f)
            for c in chunks:
                text = (c.get("text") or "").strip()
                if not text:
                    continue
                yield {
                    "chunk_id": c.get("chunk_id"),
                    "doc_type": c.get("doc_type", doc_type),
                    "doc_id": c.get("doc_id", name.replace("_chunks.json", "")),
                    "source_doc": c.get("source_doc"),
                    "product_code": c.get("product_code"),
                    "page": c.get("page"),
                    "text": text,
                }


def build(db_path):
    # 예전엔 파일을 통째로 지우고 시작했는데, 같은 파일에 상품 사실
    # 테이블(product_master/class_fees/...)을 쓰는 build_product_facts_db.py가
    # 생기면서 이 스크립트를 나중에 돌리면 그쪽을 다 날려 버렸다.
    # 자기가 만드는 테이블만 지운다.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "DROP TABLE IF EXISTS tables_fts; DROP TABLE IF EXISTS tables;"
        "DROP TABLE IF EXISTS chunks_fts; DROP TABLE IF EXISTS chunks;")
    conn.executescript(SCHEMA)
    conn.executescript(CHUNK_SCHEMA)

    n = 0
    for rec in iter_table_records():
        conn.execute(
            """
            INSERT INTO tables
                (doc_type, doc_id, source_doc, product_code, page, table_index,
                 rows, cols, data_json, row_text)
            VALUES (:doc_type, :doc_id, :source_doc, :product_code, :page,
                    :table_index, :rows, :cols, :data_json, :row_text)
            """,
            rec,
        )
        n += 1

    m = 0
    for rec in iter_chunk_records():
        conn.execute(
            """
            INSERT INTO chunks
                (chunk_id, doc_type, doc_id, source_doc, product_code, page, text)
            VALUES (:chunk_id, :doc_type, :doc_id, :source_doc, :product_code,
                    :page, :text)
            """,
            rec,
        )
        m += 1

    conn.commit()
    conn.close()
    print(f"{n}개 표 레코드, {m}개 청크 → {db_path}")


def main():
    parser = argparse.ArgumentParser(description="표 데이터를 SQLite(FTS5)로 구조화")
    parser.add_argument("--output", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
