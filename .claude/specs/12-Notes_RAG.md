## 1. Overview

Retrieval over your **training notes** ("knee tight at depth", "slept 5 h, bar slow"). Notes are embedded locally with Ollama (`nomic-embed-text`) and stored in **pgvector**; the agent's `search_notes` tool finds the relevant ones by meaning, not keywords.

This is the RAG part of the project. A separate, **deterministic** keyword check flags pain or injury notes for the validator and the router's safety filter, because safety must not depend on embedding similarity.

---

## 2. Depends on

- 02-Database_Schema
- 11-LLM_Provider_And_Embeddings

---

## 3. Routes

- `GET /athletes/{id}/notes/search?q=&lift=&since=&k=5&include_synthetic=false` → list of `{text, noted_on, lift_codes, distance, is_synthetic}`. `is_synthetic` is added to §3's field list: ADR-0004 makes it the authoritative "is this real?" and ADR-0007 rule 5 requires it on the wire
- `q` is stripped before its length check, so a query of only spaces is the same 422 as an empty one. It must be declared `Annotated[SearchQuery, Query(...)]` — the `q: SearchQuery = Query(...)` form silently drops the constraint and lets a blank query reach the service, where it raises (a 500)
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

- Lift codes mentioned by keyword (`squat`, `bench`, `deadlift|dl|pulls`, `press|ohp|overhead`, `row`), case-insensitive and word-boundary anchored, so `pullover` is not a deadlift and `impressive` is not a press
- If none mentioned → the session's lifts
- **If the note reports pain → the union of both.** "Mention wins" is right for retrieval and unsafe for screening, and the same stored `lift_codes` feed both: on a squat day, "knee hurt in the warm ups, bench felt fine" mentioned only bench, so it flagged the lift that was fine and cleared the lift that hurt
- `bench press` is folded to `bench` before matching, or `press` matches inside it and a shoulder hurt on the bench blocks every overhead press session
- `pull` keeps only its plural: "pulled a muscle during squats" is a squat note. The cost is "pulled 180 today", which a pain note recovers from the session's lifts

---

### C. `index_log_note(session, log: SessionLog, embedder)`

- Called by `log_session` (spec 06) when `note` is not empty
- Chunks, embeds with `embed_documents`, inserts `note_chunk` rows with `noted_on = log.performed_on`

---

### D. `search_notes(session, athlete_id, query, lift=None, since=None, k=5, embedder) -> list[NoteHit]`

- `embed_query(query)`
- SQL (SQLAlchemy, parameterised): order by cosine distance, filter by athlete, optional lift, optional `noted_on >= :since`, limit `k`
- The lift filter is `lift_codes @> '["squat"]'` (`.contains([lift])`) rather than the `?` operator. Same result for a string element in a JSONB array, and `?` cannot be confused with a driver placeholder
- Excludes `is_synthetic` notes unless `include_synthetic=True` (demo mode)
- **May return fewer than `k`.** HNSW is searched before the athlete and date filters are applied, so a narrow filter can leave fewer neighbours than asked for
- `k` is bounded 1..20 and an empty query is refused, both in the function and (as a 422) on the route

---

### E. `pain_flags(session, athlete_id, since: date) -> dict[str, list[NoteHit]]`

- **Keyword match, no embeddings:** `pain`, `hurt`, `injur`, `tweak`, `strain`, `sharp`, `pinch`, `twinge`. All **starting values**, as are the negation list, the 7-day window and the chunk sizes — labelled as such in the code per CLAUDE.md
- Ignores a negation within the two tokens before the keyword (`no pain`, `didn't hurt`, `without pain`), **bounded by clause**: a negation may not reach across `, ; : . ! ?`, or "no energy, pain in the knee" reads as *no pain*. The window stays narrow inside the clause because widening it reintroduces the same miss without a comma ("no sleep and my knee has pain")
- A quantifier straight after the negation preserves the report: "not much pain" is reduced pain, not absent pain
- Takes `include_synthetic=False`, like `search_notes`. A seeded demo note must not be able to block a real session, which is the direction that matters because a pain flag's consequence is a refusal
- The pure matcher is split out as `is_pain_text(text) -> bool`, so the safety decision is testable with no database
- Returns lift → matching notes since `since`; `pain_lifts(session, athlete_id, today)` reduces it to the `set[str]` the validator wants, filtered to known lift codes (ADR-0006 modification 12 raises on an unknown one)
- Feeds `ValidationContext.pain_lifts` (spec 05) and the router filter (spec 13)

---

### E2. `services.last_done_pct(session, athlete_id) -> dict[str, Fraction]`

Not in spec 12 as written, but ADR-0006's "Open for spec 12" requires it: rule 2 blocks **every** future session of a painful lift that has no baseline, so populating `pain_lifts` without this would make one pain note unplannable rather than capped.

- The **highest** prescribed intensity the athlete has completed, per lift — ADR-0006's definition, not "most recent"
- Read across the athlete's whole logged history, not the current block. A new block has nothing completed in it by definition, so a block-scoped baseline would be empty exactly when a fresh pain note arrived
- "Completed" means the session was logged, its entries record at least one set actually done, **and the logged weight reached the prescribed weight**. Without the last clause a *failed* test single (115 kg prescribed, 90 kg logged) credits 107% and puts the ceiling above the whole block, switching the rule off. It is the same comparison `log_session` already makes to bank a tested 1RM: kilograms against kilograms, no inference

---

### E3. `reindex_notes(session, athlete_id, embedder) -> int`

- Re-embeds every note the athlete has and returns the chunk count
- **Delete-then-insert per log**, so it is idempotent and needs no chunk ordinal. This is also why a failed embed needs no queue table: `session_log.note` is the source of truth, so a log with a note and no chunks *is* the backlog (§13's "queued for reindex", with no schema change — `note_chunk.embedding` is NOT NULL by ADR-0002 and no unembedded row can exist)
- Chunks with `log_id IS NULL` — the seeded demo notes — are re-embedded **in place**, because there is no source row to rebuild them from

---

### F. `scripts/seed_demo_notes.py`

- Inserts ~40 short **synthetic** notes spread over a block (sleep, bar speed, knee tightness, a good PR day), all with `is_synthetic = True`
- README must say these are synthetic

---

## 6. Changes to `domains/strength/services.py`

- `log_session` gains `embedder: Embedder | None = None` and calls `index_log_note` after flushing the log, inside a `begin_nested()` savepoint so a failure cannot poison the transaction the route is about to commit
- Failure to embed must not fail the log. **Every** exception is caught and logged with its traceback, and the note is left for `reindex`: a named tuple of expected errors read as the complete set and was not, and a wrong-width vector from an embedder that does not self-check raised a SQLAlchemy error that escaped and took the log with it
- `validation_context` now fills `pain_lifts` and `last_done_pct`, which ADR-0007 left empty "until spec 12"

---

## 7. Files to Change

- `domains/strength/services.py`, `domains/strength/schemas.py` (`NoteHit`)
- `api/main.py` (include the notes router), `api/deps.py` (`EmbedderDep`), `api/schemas.py` (`NoteHitOut`, `ReindexOut`), `api/routes/sessions.py` (passes the embedder)
- `Makefile` → `demo-notes`, and `demo-data` (which CLAUDE.md's command table already listed but no target existed for)
- `README.md`
- `tests/conftest.py` → `api_client` overrides `get_embedder` with the fake, so no route test reaches Ollama

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
