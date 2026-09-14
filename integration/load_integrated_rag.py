#!/usr/bin/env python3
"""Replace the runtime chunk index with the deduplicated integrated corpus."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "integrated" / "structured_store.db"
CHUNKS = ROOT / "data" / "integrated" / "chunks.jsonl"
PRODUCTS = ROOT / "data" / "integrated" / "fund_products.csv"

SCHEMA = """
DROP TABLE IF EXISTS chunks_fts;
DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS fund_products;
CREATE TABLE fund_products (
  product_code TEXT PRIMARY KEY,
  fund_id TEXT NOT NULL,
  canonical_product_code TEXT NOT NULL,
  source_doc_id TEXT,
  duplicate_group_id TEXT,
  is_canonical INTEGER NOT NULL
);
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chunk_id TEXT UNIQUE,
  doc_type TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  source_doc TEXT,
  product_code TEXT,
  product_codes_json TEXT,
  fund_id TEXT,
  section TEXT,
  heading TEXT,
  page TEXT,
  text TEXT NOT NULL,
  extraction_method TEXT,
  quality_status TEXT,
  chunk_hash TEXT,
  boilerplate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_chunks_doc_type ON chunks(doc_type);
CREATE INDEX idx_chunks_product_code ON chunks(product_code);
CREATE INDEX idx_chunks_fund_id ON chunks(fund_id);
CREATE INDEX idx_chunks_section ON chunks(section);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def main():
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    with PRODUCTS.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            conn.execute(
                "INSERT INTO fund_products VALUES (?,?,?,?,?,?)",
                (row["product_code"], row["fund_id"], row["canonical_product_code"],
                 row["source_doc_id"], row["duplicate_group_id"], row["is_canonical"] == "true"),
            )
    count = 0
    for line in CHUNKS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        conn.execute(
            """INSERT INTO chunks
            (chunk_id,doc_type,doc_id,source_doc,product_code,product_codes_json,
             fund_id,section,heading,page,text,extraction_method,quality_status,
             chunk_hash,boilerplate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["chunk_id"], row["doc_type"], row["doc_id"], row.get("source_file"),
             row.get("canonical_product_code"), json.dumps(row.get("product_codes", []), ensure_ascii=False),
             row.get("fund_id"), row.get("section"), row.get("heading"), str(row.get("page", "")),
             row["text"], row.get("extraction_method"), row.get("quality_status"),
             row.get("chunk_hash"), bool(row.get("boilerplate"))),
        )
        count += 1
    conn.commit()
    indexed = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    aliases = conn.execute("SELECT COUNT(*) FROM fund_products").fetchone()[0]
    conn.close()
    print({"chunks": count, "fts_rows": indexed, "product_aliases": aliases, "db": str(DB)})


if __name__ == "__main__":
    main()
