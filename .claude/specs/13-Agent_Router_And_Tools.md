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
- `GET /conversations/{id}/messages` → history

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
| `plan_block` | lifts, start_date, weeks?, test_week? | pending `new_block` proposal | spec 04 or 09 + validator |
| `reschedule` | missed_dates?, unavailable_from, unavailable_until?, injury? | 1–3 pending `reschedule` proposals | spec 08 |
| `simulate_block` | scenario | `SimulationResult` | spec 10 |
| `log_session` | session date, entries, note? | saved log | spec 06 |

- Each tool records into the current `Decision.tool_calls`: `{name, input, output_summary, duration_ms, solver_stats?, validation?}` (the decision trace view in spec 21 reads these)

---

### B. Router (`router.py`)

**Layer 1 — deterministic filters (no LLM), in order:**

| Filter | Trigger | Route |
| --- | --- | --- |
| `pain_keyword` | message matches the `pain_flags` keyword list (spec 12) | `safety`: fixed reply — no progression for that lift, suggest a physio, offer to log the note |
| `no_one_rms` | athlete has no 1RMs | `clarify`: ask for 1RMs |
| `no_weekday_map` | no weekday map and message is about planning | `clarify`: ask which weekdays |

**Layer 2 — LLM selector** (`get_model("router")`, `output_type=Route`):

- `Route(kind: Literal["answer", "plan", "reschedule", "simulate"], lifts: list[str] = [], start_date: date | None, weeks: int | None, missed_dates: list[date] = [], unavailable_from: date | None, unavailable_until: date | None, scenario: list[ForcedEvent] = [], question: str | None)`
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
- Post-check: every number in the reply must appear in the object (regex); if not, fall back to a template sentence built from `reasons`

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

---

## 8. Files to Create

- `backend/domains/strength/agent/__init__.py`, `tools.py`, `router.py`, `agent.py`, `explainer.py`, `prompts.py`
- `backend/api/routes/chat.py`
- `backend/scripts/eval_router.py`
- `backend/tests/fixtures/router_cases.yaml`
- `backend/tests/test_router.py`, `test_agent_dispatch.py`, `test_explainer.py`

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
- Router eval ≥ 90% on Claude; record the Ollama result honestly

---

## 13. Error Handling Expectations

- LLM provider unreachable → 503 with a readable message; decision `outcome = failed`
- Tool raises (e.g. `PlanInvalid`) → reply explains the violated rule; no proposal saved
- Unknown athlete → 404

---

## 14. Definition of Done

- [ ] Six tools implemented and unit-tested
- [ ] Router filters run before any LLM call
- [ ] Dispatch is plain Python
- [ ] Explainer number check in place
- [ ] Router eval script with 30 cases, results recorded for Ollama
- [ ] Every chat turn writes a `decision` row
