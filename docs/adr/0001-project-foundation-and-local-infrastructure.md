---
title: "Project foundation and local infrastructure"
description: "Repository shape, containerised Postgres and Temporal with host-native Ollama, a single cached settings object, and a health endpoint that degrades instead of failing."
type: adr
category: infrastructure
tags:
  - foundation
  - local-first
  - configuration
  - observability
status: accepted
created: 2026-10-11
updated: 2026-10-11
author: BasilFaruq
project: Powerlifting Block Planner
technologies:
  - python-3.12
  - uv
  - fastapi
  - postgresql
  - pgvector
  - temporal
  - ollama
  - docker-compose
audience:
  - developers
related: []
---

# ADR-0001: Project foundation and local infrastructure

## Status

Accepted — spec 01 (Project Setup) completed **2026-10-11 15:46 +0200** (Europe/Berlin, the project timezone), commit `fd57379`.

## Context

### Background and Problem Statement

The Powerlifting Block Planner plans an 11-week strength block from an athlete's current 1RMs, pins sessions to fixed weekdays, reschedules around missed sessions, and rolls into the next block when a new max is tested. It is a solo portfolio project: one athlete, no authentication, local-first. The stack is deliberately larger than the problem requires, because demonstrating the planner-agent pattern is half the point.

Twenty-three specs are implemented in a fixed build order. Spec 01 is the foundation all of them rest on, and it has to settle four things before any domain code can be written:

1. Where code goes, and which direction dependencies point.
2. What runs in Docker, what runs on the host, and why.
3. How configuration reaches the code.
4. How the application reports whether its dependencies are reachable.

Getting these wrong is expensive later: a dependency pointing the wrong way is a refactor across every spec, and configuration read ad hoc from `os.environ` is impossible to test.

### Current Limitations

The decision is made against an empty repository, so there is no incumbent approach to replace. The constraints that shape it are:

1. **The agent needs a GPU.** Local inference through Ollama is the default provider (`LLM_PROVIDER=ollama`), and a containerised Ollama cannot reach the host GPU without platform-specific plumbing that would differ between the author's machine and any reviewer's.
2. **Two domains are planned, not one.** `domains/strength/` is the real one; `domains/jobshop/` (spec 23) exists to show the engine is domain-agnostic. That claim is only credible if the boundary is enforced from the first commit rather than asserted afterwards.
3. **Local-first is a hard requirement.** No paid service may be needed to build or run the app, which rules out any design where a cloud dependency is load-bearing.
4. **The development machine lacks Docker and GNU Make.** Discovered while implementing; it determines what could actually be verified (see Consequences).

## Decision Drivers

### Primary Decision Drivers

1. **The engine must stay domain-agnostic.** `planner_core/` is the reusable part. If it can reach into `domains/`, the jobshop adapter stops being a demonstration and becomes a fiction.
2. **Local inference must use the GPU.** A 14B model on CPU is too slow to iterate against, which makes this a question of whether the project is usable at all, not a preference.
3. **Configuration must be testable.** Settings have to be constructible with explicit values and overridable per test, which rules out scattered environment reads.
4. **A dependency being down must be diagnosable, not fatal.** During development Postgres and Temporal are frequently not running. The app has to start anyway and say what is missing.

### Secondary Decision Drivers

1. **Reviewer setup cost.** A portfolio project that takes an hour to start does not get looked at, which favours `docker compose up` for the stateful parts.
2. **Reproducibility.** Pinned image tags and a committed lockfile, so the stack resolves identically later.
3. **Secret hygiene.** No key may be committed, and the example configuration has to list every variable without carrying a value.

## Considered Options

### Option 1: Infrastructure in Compose, application on the host, Ollama host-native

**Description**: Postgres and the Temporal dev server run as containers. The FastAPI backend and the Vite frontend run directly on the host with hot reload. Ollama runs natively on the host.

**Technical Characteristics**:
- Two services in `docker-compose.yml`; nothing else containerised during development
- Backend reaches Ollama at `http://localhost:11434` in dev, `http://host.docker.internal:11434` from containers (spec 22)
- Hot reload is the host's problem, not a bind-mount's

**Advantages**:
- Ollama gets the GPU with no passthrough configuration at all
- Fastest possible edit–reload cycle, since no container layer sits between the editor and the process
- The stateful, fiddly-to-install parts (Postgres with the pgvector extension; a Temporal server) are still one command
- Debuggers and profilers attach to an ordinary local process

**Disadvantages**:
- Two execution environments to understand instead of one
- "Works on my machine" risk for the backend, deferred rather than solved
- A fully containerised path still has to be built later (spec 22), so some configuration exists in two forms

**Risk Assessment**:
- **Technical Risk**: Low. The split is conventional and each half is independently replaceable.
- **Schedule Risk**: Low. Nothing downstream depends on where the API process lives.
- **Ecosystem Risk**: Low. Compose, uv and Ollama are all widely used and individually swappable.

### Option 2: Everything in Compose, Ollama included

**Description**: One `docker compose up` brings up Postgres, Temporal, Ollama, the backend and the frontend.

**Technical Characteristics**:
- Uniform environment; a single command for a reviewer
- GPU reaches the container through the NVIDIA container toolkit and a `deploy.resources.reservations.devices` block

**Advantages**:
- One environment, one command, no host prerequisites beyond Docker
- Development matches deployment closely

**Disadvantages**:
- GPU passthrough is platform-specific and fragile, and on Docker Desktop for Windows it depends on the WSL2 backend being configured just so
- Model weights either live in a volume or are re-pulled, both awkward at ~9 GB
- Hot reload through bind mounts on Windows is materially slower

**Disqualifying Factor**: It puts the single highest-risk component of the project — local model inference, already recorded as the biggest risk in `CONTEXT.md` §10 — behind the most fragile piece of infrastructure configuration. Ollama is already installed and working natively on the host; containerising it trades a working setup for a brittle one and buys nothing the project needs.

**Risk Assessment**:
- **Technical Risk**: High. GPU passthrough failures are environment-specific and hard to diagnose.
- **Schedule Risk**: Medium. Time spent on container GPU plumbing is time not spent on the planner.
- **Ecosystem Risk**: Low. The tools themselves are stable.

### Option 3: Everything native, no Docker at all

**Description**: Install Postgres with pgvector and the Temporal CLI directly on the host.

**Advantages**:
- One environment, no container layer anywhere, lowest possible overhead
- No Docker prerequisite — which happens to match the current state of the development machine

**Disadvantages**:
- Building or sourcing the pgvector extension for a native Windows Postgres is the most painful single setup step in the project
- No volume isolation, so resetting the database means dropping and recreating it by hand
- Setup instructions become long and platform-specific, which is exactly what deters a reviewer
- Spec 22 (`make up`, full containerised stack) would start from nothing

**Risk Assessment**:
- **Technical Risk**: Medium. pgvector on native Windows is the weak point.
- **Schedule Risk**: Medium. Setup problems recur on every new machine.
- **Ecosystem Risk**: Low.

## Decision

**Option 1.** Infrastructure is containerised, the application runs on the host during development, and Ollama stays native.

The implementation uses:

- **Python 3.12 with uv** for the backend, as a non-package project (`[tool.uv] package = false`) with `pytest` configured `pythonpath = ["."]`. Nothing imports the project by name, so there is no wheel to build. `uv.lock` is committed.
- **`pgvector/pgvector:pg16`** for Postgres and **`temporalio/temporal:1.9.1`** for the Temporal dev server, both pinned by exact tag. The Temporal image's entrypoint is `temporal`, so the Compose `command` is `server start-dev --ip 0.0.0.0 --ui-port 8233`. Dev-server state is in-memory and resets with the container, which is acceptable until spec 15.
- **Host-native Ollama**, never in Compose, reached at `http://localhost:11434`.
- **Langfuse Cloud** (spec 14), never in Compose. Tracing is disabled while its keys are absent.

Four rules follow from this and bind every later spec:

1. **Dependencies point one way.** `planner_core/` must not import from `domains/`, and the two domains must not import each other. Anything that needs sharing moves into `planner_core` first.
2. **All configuration flows through one object.** A single `lru_cache`d `get_settings()` in `planner_core/config.py`; nothing else in the codebase reads `os.environ`. Three details are deliberate rather than incidental:
   - `LLM_PROVIDER=anthropic` without `ANTHROPIC_API_KEY` **fails at startup** with that exact message, rather than at the first inference call.
   - Blank secrets are normalised to `None`, because `.env.example` ships `ANTHROPIC_API_KEY=` and the Langfuse keys empty and an empty string would otherwise read as "configured".
   - `CORS_ORIGINS` is a list, so it must be written as JSON: `CORS_ORIGINS=["http://localhost:5173"]`. A bare URL fails to parse.
3. **`/health` degrades, it does not fail.** It answers HTTP 200 even when every dependency is down, carrying `{"status", "db", "temporal", "ollama"}` with per-dependency `"ok"`/`"down"`. This makes "the API is up but Postgres is gone" distinguishable from "the API is gone" — a single boolean cannot express that. Every probe is bounded at 2 seconds and the three run concurrently.
4. **The database probe carries an explicit `connect_timeout`.** libpq waits indefinitely by default, and because `ping_db()` is blocking it runs in a worker thread that `asyncio` cannot cancel. Without the timeout, `/health` hangs instead of reporting `"db": "down"` — and the process will not even exit, because the interpreter waits on the thread. It is bounded in two layers: `connect_args={"connect_timeout": 2}` on the engine, and an `asyncio.timeout` around the probe so a slow dependency can never be the reason a response is slow. This was observed, not anticipated: the first implementation hung past 15 seconds.

Two further decisions of record:

- **The implementation specs live in `.claude/specs/`**, not `docs/specs/`. `CLAUDE.md` was corrected to match the filesystem rather than the files being moved.
- **`data/B1_Mine.xlsx` is gitignored.** This overrides spec 00's "(committed)" annotation in favour of the `CLAUDE.md` guardrail, because the file is the athlete's own training data. `CONTEXT.md` §4 records everything about it the golden test needs — 77 entries, 4 tests, the starting 1RMs, and the two spreadsheet errors worked around in code.

## Consequences

### Positive

1. **Local inference works without configuration.** Ollama reaches the GPU because nothing stands between it and the hardware. The project's highest-risk component is also its least fragile to set up.
2. **The domain boundary is enforced from the first commit.** The jobshop adapter's claim to run on the same engine is testable rather than aspirational, because the import direction was never violated.
3. **Configuration is trivially testable.** `Settings(_env_file=None, ...)` constructs an isolated instance, so the credential gate and env parsing are covered by ordinary unit tests with no environment manipulation. Eleven of the 25 tests do exactly this.
4. **Misconfiguration surfaces at startup, not mid-conversation.** Choosing the Anthropic provider without a key fails immediately, where the message is attached to the cause.
5. **A dead dependency is diagnosable in one request.** `/health` distinguishes which of three dependencies is unreachable, and answers within its probe budget whatever their state.

### Negative

1. **Two execution environments during development.** The backend on the host and its data in containers means two things to reason about, and `localhost` means something different inside a container than outside it. Spec 22 resolves this for deployment; in development it is accepted.
2. **Configuration exists in two forms.** Dev points at `localhost`; the containerised stack will point at service names and `host.docker.internal`. Both must be kept correct, and they can drift.
3. **The Temporal probe is shallower than it looks.** It is a raw TCP connect, not a client handshake, because `temporalio` is a spec 15 dependency and spec 01 does not list it. It proves something is listening on the port and nothing more: a Temporal server that is up but unhealthy reports `"ok"`. Noted in the code at the probe itself, to be replaced with a real client connect in spec 15.
4. **Two Definition-of-Done items are unverified.** `make infra` and `/health` showing `db`/`temporal` as `"ok"` could not be exercised, because Docker Desktop and GNU Make are not installed on the development machine. They are stated as unchecked in the plan and the README lists both as prerequisites; neither is assumed to work.
5. **The pinned Temporal command is untested.** `temporalio/temporal:1.9.1` and its `server start-dev` invocation were verified from the registry's tag list and the image's `ENTRYPOINT`, not by running the container. Expect to confirm it on first `make infra`.

### Neutral

1. **A non-package uv project is unusual but inconsequential.** It removes a build step and makes `uv run pytest` work from `backend/` with `pythonpath = ["."]`. If something ever needs to import the project by name, flipping `package = false` is a one-line change.
2. **Spec 01 installs dependencies it does not use.** `sqlmodel`, `alembic` and `pgvector` are present so spec 02 adds none. This is what spec 01 §9 asks for.
3. **Closed ports on the development machine refuse connections in ~2 seconds** rather than instantly, apparently from a security product delaying the RST. It makes `/health` answer in ~2.6 s rather than ~0.1 s when dependencies are down. Harmless, but it is why the measured timings look slow.

## Decision Outcome

The foundation is in place and verified to the extent the machine allows:

| Check | Result |
| --- | --- |
| `ruff check` and `ruff format --check` | clean, 15 files |
| `pytest` | 25 passed; no test touches the network or a database |
| `/health` with Postgres and Temporal absent | `{"status":"ok","db":"down","temporal":"down","ollama":"ok"}`, HTTP 200 in 2.6 s |
| `LLM_PROVIDER=anthropic` with no key | startup fails with the specified message |
| Repository layout | matches `.claude/specs/00-Spec_Index.md` §4 |
| `.env.example` | lists every settings field |

The degradation path that matters most — the API staying up and diagnosable while its dependencies are not — was verified live rather than only in tests, which is how the `connect_timeout` defect was found.

Mitigations:

- For the two unverified DoD items: both are listed as prerequisites in the README with install commands, and they gate spec 02 (which needs Postgres) rather than silently passing.
- For the shallow Temporal probe: the limitation is documented at the call site, so spec 15 inherits a note rather than a surprise.
- For dev/deploy configuration drift: every value is funnelled through one `Settings` class, so the two forms differ in `.env` values and never in code paths.
- For the untested container command: the Compose file records why the `command` is shaped as it is, so a failure on first run is diagnosable from the file itself.

## Related Decisions

None yet — this is the first ADR. Several of the rules above are recorded in `CLAUDE.md` as non-negotiables and will each acquire their own ADR as later specs touch them, in particular the exact-arithmetic rule (spec 04), the validator's hard/soft split (spec 05) and the side-effects-in-activities rule (spec 15).

Open questions deliberately left undecided are listed in `CONTEXT.md` §9; each becomes a `proposed` ADR when addressed.

## Links

- [`.claude/specs/01-Project_Setup.md`](../../.claude/specs/01-Project_Setup.md) — the spec this implements
- [`.claude/specs/00-Spec_Index.md`](../../.claude/specs/00-Spec_Index.md) — build order and repository layout
- [`CONTEXT.md`](../../CONTEXT.md) — domain background, the verified template, known risks
- [`CLAUDE.md`](../../CLAUDE.md) — operating instructions and the non-negotiables derived from this ADR

## More Information

- **Date:** 2026-10-11
- **Completed:** 2026-10-11 15:46 +0200 (Europe/Berlin), commit `fd57379`
- **Source:** spec 01 (Project Setup), implemented in full
- **Related ADRs:** none

## Audit

### 2026-10-11

**Status:** Pending

**Findings:**

| Finding | Files | Lines | Assessment |
| --- | --- | --- | --- |
| Dependency direction holds: no import of `domains` from `planner_core` | `backend/planner_core/` | — | compliant |
| All configuration read through `get_settings()`; no other `os.environ` access | `backend/planner_core/config.py` | L1-L80 | compliant |
| Anthropic credential gate fails at startup with the specified message | `backend/planner_core/config.py` | L71-L75 | compliant |
| Database connect bounded by an explicit libpq timeout | `backend/planner_core/db.py` | L23-L29 | compliant |
| `/health` answers 200 with per-dependency status; probes bounded and concurrent | `backend/api/routes/health.py` | L33-L84 | compliant |
| Temporal probe is a TCP connect, not a client handshake | `backend/api/routes/health.py` | L48-L64 | partial — deliberate, revisit in spec 15 |
| No `create_all()` in application code; migrations only | `backend/` | — | compliant (no schema code yet; spec 02) |
| Image tags pinned exactly | `docker-compose.yml` | L15, L35 | compliant |
| `make infra` and a fully healthy `/health` | `docker-compose.yml`, `Makefile` | — | unverified — Docker and GNU Make absent from the machine |

**Summary:** Every rule this ADR establishes is honoured by the spec 01 implementation. Two items are not fully satisfied and both are deliberate and recorded: the Temporal probe is intentionally shallow until `temporalio` becomes a dependency in spec 15, and the Compose-dependent behaviour is unverified because the toolchain is not installed.

**Action Required:** Install Docker Desktop and GNU Make, then re-run this audit's last row before starting spec 02 — which needs a live Postgres regardless.
