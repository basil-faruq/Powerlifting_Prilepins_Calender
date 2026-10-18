> **Revised 2026-10-17 (ADR-0015).** Implemented and accepted with twenty
> modifications. The ones that change what this document says:
>
> - §5A's `reschedule` needed `services.save_proposals`, which did not exist —
>   spec 08's policy is pure, so its proposals were value objects nothing
>   persisted. Its option counts are also wrong; see the note there.
> - §5A's `plan_block` returns a *preview* for `weeks`/`test_week` (ADR-0011).
> - §5B's pain filter is `notes.is_pain_text(message)`, not `pain_flags`.
> - §5B's `Route` is split into `RouterChoice` (what the model may return) and
>   `Route` (what is stored), and its `scenario` is four flat scalar fields.
> - §5D's template fallback uses `assumptions` for a simulation, which has no
>   `reasons`.
> - `log_session` is built and tested but no route kind dispatches to it.
> - No migration: `run_id = f"chat:{conversation_id}"`, the router is
>   `tool_calls[0]`, the athlete comes from a `chat_message` join, and
>   `outcome IS NULL` means pending.
> - `GET /conversations/{id}/messages` requires `athlete_id`.
> - The router asks for structured output natively on Ollama (`llm.structured`); the default tool-call mode scored 7/30 on `qwen2.5:14b`, native scored **29/30 = 97%**.
> - A dead model becomes a 503 only via `llm.unavailable_from`; the `answer` route's reply is number-checked like every other.
>
> Read `docs/adr/0015-agent-router-and-tools.md` before changing anything here.

## 1. Overview

The **Pydantic AI agent** behind the chat panel. It has six tools and a two-layer **router**: deterministic filters first, then an LLM selector — the same pattern as your DFKI planner (hard filters, then a selector).

The LLM **routes and explains**; it never invents numbers. Every number comes from the solver, simulator, generator or database, and every change it proposes goes through the validator and your approval.

---

## 2. Depends on

- 06-Core_API
- 07-Calendar_Solver, 08-Missed_Session_Policy, 09-Block_Shaping_Solver, 10-Simulator
- 11-LLM_Provider_And_Embeddings
- 12-Notes_RAG

---

## 3. Routes

- `POST /chat` → body `{athlete_id, conversation_id?, message}` → `ChatResponse`
    - `reply: str`
    - `route: Route`
    - `proposals: list[Proposal]` (pending, for approval cards)
    - `simulation: SimulationResult | None`
    - `decision_id: UUID`
    - `conversation_id: UUID`
    - `has_synthetic_one_rms: bool` — **added** (ADR-0007 rule 5 / ADR-0004 rule 4)
    - `trace_url: str | None` — **added** (spec 14)
- `GET /conversations/{id}/messages?athlete_id=` → history. **`athlete_id` is required**: `conversation_id` is not a foreign key, so without it a guessed UUID reads another conversation (ADR-0015).

---

## 4. Database Schema

- No changes. Writes `chat_message`, `decision`, `proposal`.

---

## 5. Functions to Implement (`domains/strength/agent/`)

---

### A. Tools (`tools.py`) — plain typed functions, also registered with Pydantic AI

| Tool | Input | Returns | Backed by |
| --- | --- | --- | --- |
| `get_block` | athlete_id | current block, next 3 sessions, 1RMs, misses so far | DB |
| `search_notes` | query, lift?, since? | top-5 note hits with dates | spec 12 |
| `plan_block` | lifts, start_date, weeks?, test_week? | pending `new_block` proposal — **or a preview, with no proposal, when `weeks` or `test_week` is given** (ADR-0011) | spec 04 or 09 + validator |
| `reschedule` | missed_dates?, unavailable_from, unavailable_until?, injury? | **up to 3 for `in_week_move`, one for `week_shift`/`fold`, and one `new_block` for `restart`** (ADR-0010) | spec 08 + `services.save_proposals` |
| `simulate_block` | scenario | `SimulationResult` | spec 10 |
| `log_session` | session date, entries, note? | saved log. **Implemented and tested, but no route kind dispatches to it** — a `log` kind would need a migration (ADR-0015) | spec 06 |

- Each tool records into the current `Decision.tool_calls`: `{name, input, output_summary, duration_ms, solver_stats?, validation?}` (the decision trace view in spec 21 reads these)

---

### B. Router (`router.py`)

**Layer 1 — deterministic filters (no LLM), in order:**

| Filter | Trigger | Route |
| --- | --- | --- |
| `pain_keyword` | **`notes.is_pain_text(message)`** — `pain_flags` takes a `Session` and screens stored notes, so it cannot screen a sentence (ADR-0014) | `safety`: fixed reply — no progression for that lift, suggest a physio, offer to log the note |
| `no_one_rms` | athlete has no 1RMs | `clarify`: ask for 1RMs |
| `no_weekday_map` | no weekday map and message is about planning | `clarify`: ask which weekdays |

**Layer 2 — LLM selector** (`get_model("router")`, `output_type=Route`):

- **Split in two.** `RouterChoice` is what the model may return — the four kinds above, and `scenario` flattened to `scenario_kind`, `scenario_start_week`, `scenario_start_date`, `scenario_days` (one event per turn). `Route` is what is persisted and carries all six of `enums.DecisionRoute`, so the model cannot name `safety` or `clarify`. See ADR-0015.
- System prompt gives today's date, the athlete's weekdays and current block week so relative dates ("tomorrow", "next Tuesday") resolve correctly
- Examples:
    - "what's on Thursday?" → `answer`
    - "new block with squat, bench and deadlift" → `plan`
    - "I can't train tomorrow" / "I was sick for 9 days" → `reschedule`
    - "what if I travel in week 7?" / "will I hit 150 on squat?" → `simulate`

---

### C. Dispatch (`agent.py` → `handle_message`)

- Plain Python `match route.kind` calls the tool; **the LLM never loops over the solver**
- `answer` → an answer agent (`get_model("agent")`) that may call only the read-only tools `get_block` and `search_notes`
- `plan` / `reschedule` → tool → proposals → explainer
- `simulate` → tool → explainer
- Persists user + assistant `chat_message`s and one `decision` row

---

### D. Explainer (`explainer.py`)

- Turns a `Proposal` or `SimulationResult` into 2–4 plain sentences
- Receives the object as JSON and is told to use **only numbers present in it**
- Post-check: every number in the reply must appear in the object; if not, fall back to a template built from `reasons` — **or, for a `SimulationResult`, from its percentiles and `assumptions`, since it has no `reasons`**. Matching is structural, not lexical: see ADR-0015 for the normalisation.

---

### E. Router eval (`scripts/eval_router.py`)

- 30 labelled messages in `tests/fixtures/router_cases.yaml` (≥ 6 per route, including German-English mixes and relative dates)
- Prints accuracy and a confusion table for the configured model
- Run it on Ollama and on Claude to compare — a good README table and interview talking point

---

## 6. Changes to `api/main.py`

- Include `api/routes/chat.py`

---

## 7. Files to Change

- `api/main.py`
- `api/deps.py` — `ChatModelsDep`, so `TestModel` is injectable
- `api/schemas.py` — `ChatCreate`, `ChatResponse`, `ChatMessageOut`
- `domains/strength/services.py` — **`save_proposals`** (new; see §5A)
- `domains/strength/missed_policy.py` — `PolicyResult.stats`, for spec 14

---

## 8. Files to Create

- `backend/domains/strength/agent/__init__.py`, `tools.py`, `router.py`, `agent.py`, `explainer.py`, `prompts.py`, **`schemas.py`** and **`recorder.py`** (both forced by import cycles — ADR-0015)
- `backend/api/routes/chat.py`
- `backend/scripts/eval_router.py`
- `backend/tests/fixtures/router_cases.yaml`
- `backend/tests/test_explainer.py`, `test_recorder.py`, `test_agent_schemas.py`, `test_eval_router.py`; `backend/tests/api/test_router.py`, `test_agent_dispatch.py`, `test_agent_tools.py`, `test_chat.py` (the DB-backed ones live under `tests/api/` for its fixtures)

---

## 9. Dependencies

- `pydantic-ai-slim` (already added), `pyyaml`

---

## 10. Conversation Context

- Last 10 messages of the conversation are sent to the router and answer agent
- Older messages are dropped (no summarisation in v1)

---

## 11. Rules for Implementation

- All prompts live in `prompts.py`
- Notes and logs are inserted into prompts as quoted data, never as instructions
- Unit tests use `TestModel` / `FunctionModel`; no real LLM in CI
- If the LLM returns invalid structured output twice → reply "I couldn't understand that; try rephrasing" and record `outcome = failed`
- The agent cannot approve proposals; only the user can (spec 15 / UI)

---

## 12. Expected Behavior

- "My knee hurts on squats" → `safety` route without any LLM call; `decision.filter_fired = pain_keyword`
- "I can't make it tomorrow" (tomorrow is a Day 1) → `reschedule`; 1–3 proposals returned with explanations
- "Start a new block with squat and deadlift on Monday" → `plan`; one pending proposal with 33 sessions
- "What if I'm sick for 10 days in week 6?" → `simulate`; reply quotes p50 finish date from the result
- Router eval ≥ 90% on Claude; record the Ollama result honestly. **Run on Ollama only** — `make eval-router` requires `--yes` on `anthropic` so no API spend happens unchosen (ADR-0015).

---

## 13. Error Handling Expectations

- LLM provider unreachable → 503 with a readable message; decision `outcome = failed`
- Tool raises (e.g. `PlanInvalid`) → reply explains the violated rule; no proposal saved
- Unknown athlete → 404

---

## 14. Definition of Done

- [x] Six tools implemented and unit-tested (`log_session` unrouted — ADR-0015)
- [x] Router filters run before any LLM call (asserted with a model that raises on use)
- [x] Dispatch is plain Python
- [x] Explainer number check in place
- [x] Router eval script with 30 cases, results recorded for Ollama (**29/30 = 97%**; Claude not run)
- [x] Every chat turn writes a `decision` row, including the 503 path
