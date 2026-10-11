## 1. Overview

Retrieval over your **training notes** ("knee tight at depth", "slept 5 h, bar slow"). Notes are embedded locally with Ollama (`nomic-embed-text`) and stored in **pgvector**; the agent's `search_notes` tool finds the relevant ones by meaning, not keywords.

This is the RAG part of the project. A separate, **deterministic** keyword check flags pain or injury notes for the validator and the router's safety filter, because safety must not depend on embedding similarity.

---

## 2. Depends on

- 02-Database_Schema
- 11-LLM_Provider_And_Embeddings

---

## 3. Routes

- `GET /athletes/{id}/notes/search?q=&lift=&since=&k=5` → list of `{text, noted_on, lift_codes, distance}`
- `POST /athletes/{id}/notes/reindex` → re-embeds all notes (after changing the embed model)

---

## 4. Database Schema

- No changes (`note_chunk` from spec 02)

---

## 5. Functions to Implement (`domains/strength/notes.py`)

---

### A. `chunk_note(text) -> list[str]`

- Notes ≤ 600 characters → one chunk
- Longer → split on sentence boundaries into chunks of ~400 characters, 1-sentence overlap

---

### B. `detect_lifts(text, session_lifts) -> list[str]`

- Lift codes mentioned by keyword (`squat`, `bench`, `deadlift|dl|pull`, `press|ohp`, `row`), case-insensitive
- If none mentioned → the session's lifts

---

### C. `index_log_note(session, log: SessionLog, embedder)`

- Called by `log_session` (spec 06) when `note` is not empty
- Chunks, embeds with `embed_documents`, inserts `note_chunk` rows with `noted_on = log.performed_on`

---

### D. `search_notes(session, athlete_id, query, lift=None, since=None, k=5, embedder) -> list[NoteHit]`

- `embed_query(query)`
- SQL (SQLAlchemy, parameterised): order by `embedding <=> :q` (cosine distance), filter by athlete, optional `lift_codes ? :lift`, optional `noted_on >= :since`, limit `k`
- Excludes `is_synthetic` notes unless `include_synthetic=True` (demo mode)

---

### E. `pain_flags(session, athlete_id, since: date) -> dict[str, list[NoteHit]]`

- **Keyword match, no embeddings:** `pain`, `hurt`, `injur`, `tweak`, `strain`, `sharp`, `pinch`, `twinge`
- Ignores negations directly before the keyword (`no pain`, `didn't hurt`)
- Returns lift → matching notes from the last 7 days; feeds `ValidationContext.pain_lifts` (spec 05) and the router filter (spec 13)

---

### F. `scripts/seed_demo_notes.py`

- Inserts ~40 short **synthetic** notes spread over a block (sleep, bar speed, knee tightness, a good PR day), all with `is_synthetic = True`
- README must say these are synthetic

---

## 6. Changes to `domains/strength/services.py`

- `log_session` calls `index_log_note` after saving the log (failure to embed must not fail the log; it is retried by `reindex`)

---

## 7. Files to Change

- `domains/strength/services.py`
- `api/main.py` (include the notes router)
- `Makefile` → `demo-notes` target for the seed script

---

## 8. Files to Create

- `backend/domains/strength/notes.py`
- `backend/api/routes/notes.py`
- `backend/scripts/seed_demo_notes.py`
- `backend/tests/test_notes.py`

---

## 9. Dependencies

- None new (`pgvector`, `httpx` already present)

---

## 10. Index

- HNSW index: `CREATE INDEX ON note_chunk USING hnsw (embedding vector_cosine_ops);` (created in spec 02's migration)

---

## 11. Rules for Implementation

- Embeddings always from `get_embedder()`; never call Ollama directly here
- Tests use a fake embedder that maps text to deterministic vectors
- Safety (`pain_flags`) is keyword-based and tested independently of retrieval
- Note text is user data: it is passed to the LLM as content, never as instructions

---

## 12. Expected Behavior

- Log with note "Left knee felt tight at the bottom of squats" → one chunk, `lift_codes = ["squat"]`
- `search_notes("how has my knee been?")` returns that note first
- `pain_flags` finds "sharp pain in lower back on deadlift" → `{"deadlift": [...]}`; ignores "no pain today"
- `reindex` re-embeds all chunks without duplicating them

---

## 13. Error Handling Expectations

- Ollama down during logging → log is saved, note queued for reindex, warning logged
- Empty query → 422
- `k` > 20 → 422

---

## 14. Definition of Done

- [ ] Notes chunked, embedded and stored on log
- [ ] Vector search with lift / date filters works
- [ ] Pain flags deterministic, negation-aware and tested
- [ ] Synthetic demo notes seeded and labelled
