# Pension integration

This directory combines the existing validated pension dataset with the
Python extraction/search pipeline from the `suhyeon` branch.

## Safety contract

- `data/processed` remains the immutable baseline.
- Incoming JSON is preserved under `data/staging/suhyeon`.
- Generated combined data is written only under `data/integrated`.
- Value disagreements are written to
  `data/validation/integration_conflicts.csv` for audit. For class details and
  quantitative runtime fields, the suhyeon value is authoritative.
- Exact duplicate PDFs are indexed once, while all product-code aliases remain
  searchable through `fund_products.csv`.

## Build order

Run the reproducible integration pipeline with `npm run integrate`. The Node
launcher selects the project Python environment (or `PENSION_PYTHON` when set).

Detailed build order:

1. `integration/materialize_source_views.py` (only when re-running suhyeon extractors)
2. Existing Node extraction and validation
3. Suhyeon detailed extractors into staging
4. `integration/build_integrated_store.py`
5. `integration/build_integrated_rag.py`
6. `integration/validate_integration.py`
7. Build SQLite/FTS and the flat semantic index from `data/integrated`
8. Run the legacy and integration evaluation suites

The source views use hard links and are disposable. They do not duplicate or
modify the original documents.

## Runtime authority

- Pension remains authoritative for fund IDs, duplicate-document groups, and
  source-quality records.
- Suhyeon is authoritative for class details, fees, returns, AUM, asset mix,
  RAG content, search, and API behavior.
- `data/processed` remains untouched as a rollback/audit baseline; runtime
  reads the regenerated `data/integrated` store.

## Updating from suhyeon

Fetch `suhyeon-source/suhyeon`, inspect its diff from the recorded source
commit, merge code changes, regenerate only affected staging data, and rerun
steps 4-8. Generated databases are never treated as source-of-truth files.

`npm run integrate:all` rebuilds data, RAG, SQLite/FTS, and the portable flat
semantic index. Chroma can still be built with `npm run integrate:vector`, but
the runtime automatically falls back to the flat NumPy index if a persisted
HNSW index cannot be opened (observed on some OneDrive-backed workspaces).
