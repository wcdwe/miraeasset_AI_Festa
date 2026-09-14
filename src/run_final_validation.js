// The canonical final validator reads every generated row instead of keeping
// a second, hard-coded list of extractors that becomes stale as companies are added.
//
// Scope: this only validates data/processed/ (the original JS extraction
// baseline). It does NOT touch data/integrated/ or the structured_store.db
// that api/server.py actually queries at answer time - a clean run here does
// not mean the live service data is correct. For that, run
// `npm run validate:integration` (checks data/integrated) and
// `npm run validate:runtime` (checks structured_store.db, built from
// data/staging/suhyeon via `npm run integrate:runtime`).
require('./refresh_validation_summary');
