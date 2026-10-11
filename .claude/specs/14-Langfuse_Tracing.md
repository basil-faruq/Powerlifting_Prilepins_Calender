## 1. Overview

Trace **every agent decision** in **Langfuse**: the router's route and which filter fired, every tool call, solver stats, validator verdicts and the approval outcome.

Pydantic AI emits OpenTelemetry spans and Langfuse ingests them; custom spans cover the non-LLM steps. Each trace id is stored on the `decision` row, so the UI can open the exact trace behind any change to your plan.

Langfuse runs on its **Cloud free tier** (Hobby: 50k units/month, 30-day retention, 2 users). The app must work exactly the same with tracing switched off.

---

## 2. Depends on

- 13-Agent_Router_And_Tools

---

## 3. Routes

- `GET /decisions?athlete_id=&route=&outcome=&limit=50` → list of decisions with `trace_url`
- `GET /decisions/{id}` → decision with tool calls and `trace_url`

---

## 4. Database Schema

- No changes (`decision.langfuse_trace_id` exists)

---

## 5. Functions to Implement (`planner_core/tracing.py`)

---

### A. `init_tracing(settings)`

- If both Langfuse keys are set: initialise the Langfuse client and enable Pydantic AI instrumentation (`Agent.instrument_all()` or the equivalent in the installed versions — follow Langfuse's current Pydantic AI integration guide)
- Otherwise: no-op; log `Tracing disabled (no Langfuse keys)`

---

### B. `traced(name: str, **attrs)` context manager

- Opens a Langfuse span (no-op when disabled) and yields an object with `.set(key, value)` and `.trace_id`
- Used around non-LLM steps

---

### C. Instrumentation points

| Where | Span name | Attributes |
| --- | --- | --- |
| `handle_message` (spec 13) | `chat_turn` (root) | athlete_id, conversation_id, message length |
| Router layer 1 | `router.filters` | filter_fired |
| Router layer 2 | (auto from Pydantic AI) | model, tokens |
| Each tool | `tool.<name>` | input summary, output summary, duration |
| `calendar_solver.solve` | `solver.calendar` | status, objective, wall_time, num_options |
| `shaping_solver.solve` | `solver.shaping` | status, objective, wall_time |
| `simulate` | `simulator.run` | runs, seed, wall_time |
| `validator.validate` | `validator` | ok, violation rules, warning count |
| Approval (spec 15) | `approval` | proposal_id, outcome |

- Workflow-originated runs (spec 15) use root span `workflow.missed_session`

---

### D. `trace_url(trace_id) -> str | None`

- `{langfuse_host}/project/{langfuse_project_id}/traces/{trace_id}`; `None` when disabled or ids missing

---

### E. Decision linking

- `handle_message` stores the root span's trace id in `decision.langfuse_trace_id`
- Approval outcome added to the same trace when the user approves later (by trace id), and `decision.outcome` updated

---

## 6. Changes to `api/main.py`

- Call `init_tracing(settings)` on startup
- Include `api/routes/decisions.py`

---

## 7. Files to Change

- `api/main.py`
- `domains/strength/agent/agent.py`, `router.py`, `tools.py`
- `domains/strength/calendar_solver.py`, `shaping_solver.py`, `simulator.py`, `validator.py` (wrap in `traced`, no logic change)

---

## 8. Files to Create

- `backend/planner_core/tracing.py`
- `backend/api/routes/decisions.py`
- `backend/tests/test_tracing.py`

---

## 9. Dependencies

- `langfuse` (v3+, OpenTelemetry-based)

---

## 10. Privacy

- Training notes may appear in traces; that is acceptable for a personal project on your own Langfuse account
- Never put API keys or full DB rows into span attributes; summaries only

---

## 11. Rules for Implementation

- Domain code imports only `planner_core.tracing.traced`, never the Langfuse SDK
- Tracing failures must never break a request (catch, log, continue)
- Tests run with tracing disabled and assert `traced` is a no-op

---

## 12. Expected Behavior

- With keys set, one chat turn → one Langfuse trace with nested router, tool, solver and validator spans
- `GET /decisions` returns rows with working `trace_url`s
- Without keys, everything works; `trace_url` is `null`

---

## 13. Error Handling Expectations

- Langfuse unreachable → warning in logs, request succeeds
- Invalid keys → warning at startup, tracing disabled

---

## 14. Definition of Done

- [ ] Root span per chat turn and per workflow decision
- [ ] Solver, simulator and validator spans with stats
- [ ] Trace id saved on every `decision`
- [ ] Decisions API returns trace links
- [ ] App fully functional with tracing off
