---
# ADR Plugin Configuration
# Copy this file to: .claude/adr.local.md

# ADR directories (supports multiple for module-level ADRs)
adr_paths:
  - docs/adr/                # Primary ADR directory

# Default template format
# Options: madr, structured-madr, nygard, y-statement, alexandrian, business-case, tyree-akerman
default_format: structured-madr

# MADR variant (when using madr format)
# Options: full, minimal, bare, bare-minimal
# Note: structured-madr has no variants (always full with audit section)
madr_variant: full

# Numbering configuration
numbering:
  pattern: "4digit"          # Options: 4digit, 3digit, date, custom
  # custom_pattern: "{YYYY}{MM}{DD}-{slug}"  # When pattern is "custom"
  start_from: 1              # Starting number for new projects

# Status workflow configuration
statuses:
  workflow:                  # Ordered list of valid statuses
    - proposed
    - accepted
    - deprecated
    - superseded
  allow_rejected: true       # Include 'rejected' as valid status
  custom_statuses: []        # Additional project-specific statuses

# ADR linking
linking:
  auto_update_superseded: true    # Auto-add superseded-by when superseding
  require_supersedes_reason: true # Require reason when superseding

# Index (README.md) configuration
index:
  auto_generate: true        # Auto-regenerate index on changes
  include_summary: true      # Include decision summary in index
  include_status_badge: true # Show status badges
  group_by_status: false     # Group ADRs by status in index
  custom_header: null        # Custom header content for README.md

# Git integration
git:
  enabled: false
  auto_commit: false         # Auto-commit ADR changes
  commit_template: "docs(adr): {action} ADR-{id} {title}"

# Compliance checking
compliance:
  enabled: true
  check_all_accepted: true   # Check against all accepted ADRs
  # enforceable_tags: []     # Only check ADRs with these tags (if check_all_accepted: false)
  file_patterns:             # Patterns to check for compliance
    - "**/*.ts"
    - "**/*.js"
    - "**/*.py"
    - "**/*.go"
    - "**/*.java"
  ignore_patterns:
    - "**/node_modules/**"
    - "**/vendor/**"
    - "**/.git/**"
    - "**/dist/**"
    - "**/build/**"

# Export settings
export:
  default_format: html       # Options: html, json, pdf
  html_template: default     # Template for HTML export
  include_toc: true          # Include table of contents
  include_status_badges: true
  output_dir: null           # Default: same directory as ADRs

# Custom templates (supplement bundled templates)
custom_templates:
  # template_name: path/to/template.md

# Research settings
research:
  include_codebase: true     # Analyze codebase for context
  include_web: true          # Search web for best practices
  max_web_results: 5         # Limit web search results
---

# Project ADR Context

**Powerlifting Block Planner** — an agent that plans an 11-week strength training block from the athlete's current 1RMs, pins each session to fixed weekdays, reschedules around missed sessions, and rolls into the next block when a new max is tested.

The project is a production-planning agent whose domain happens to be a gym rather than a factory: training sessions are jobs, weekdays are machines, missed sessions are breakdowns. It deliberately keeps the full technical stack of that pattern — FastAPI, Pydantic v2, Postgres + pgvector, Pydantic AI, OR-Tools CP-SAT, SimPy, Temporal, Langfuse, React — because the point is to demonstrate the pattern, not just to produce training plans.

**Primary reference documents:**

- `Powerlifting Block Planner — Project Spec` (shared doc) — design rationale and trade-offs
- `.claude/specs/00-Spec_Index.md` through `23-JobShop_Adapter.md` — buildable implementation specs, numbered in build order
- `data/B1_Mine.xlsx` — the authored training template the whole system derives from

Specs describe **what to build**. ADRs record **why a choice was made over the alternatives**, especially where a later reader (or an interviewer) would reasonably ask "why this and not the obvious thing?". When a spec and an accepted ADR disagree, the ADR wins and the spec should be updated to match.

**Context for ADR agents:** this is a solo portfolio project built over roughly four weeks, intended for a public GitHub repository and discussion in job interviews. Prefer ADRs that are short, honest about trade-offs, and specific about what was given up. Do not write ADRs that read as marketing. Where a decision was made for CV or learning reasons rather than engineering ones, say so plainly in the ADR — that is a legitimate driver on this project and pretending otherwise makes the document less useful.

## Architecture Principles

These are the non-negotiables. An ADR that proposes breaking one must explicitly supersede the ADR that established it.

**1. The LLM routes and explains; it never computes.**
Every number reaching the user comes from the solver, the simulator, the generator or the database. The agent selects tools and phrases results. Any proposal that the LLM could have invented a number into is a bug. The explainer post-checks that every number in its output appears in the object it was given.

**2. Solver proposes, validator rejects, human approves.**
Nothing is written to the athlete's calendar without passing the validator and then an explicit human approval. There is exactly one write path (`approve_proposal`, reached via a Temporal signal once spec 15 lands). Convenience must never create a second one.

**3. Hard filters first, then a selector.**
Safety-critical routing (pain and injury keywords, missing prerequisites) is deterministic Python that runs *before* any LLM call. The LLM selector only chooses among already-safe routes. This mirrors the planner pattern used in the author's DFKI work and is the project's central design claim.

**4. The core is domain-agnostic; powerlifting is a plug-in.**
`planner_core/` must not import from `domains/`. Shared abstractions (`Proposal`, `ValidationResult`, `Validator`, LLM provider, tracing) live in the core; everything sport-specific lives in `domains/strength/`. The optional `domains/jobshop/` adapter exists to prove this boundary is real, and the two domains must never import each other.

**5. Local-first, with a swappable provider.**
The entire system must build and run with no paid service: Ollama for both the agent LLM and embeddings, Postgres and Temporal in Docker, Langfuse optional. `LLM_PROVIDER` switches to Claude for demos without touching any other code. Embeddings stay local in every mode. No module outside `planner_core/llm.py` may import a provider SDK.

**6. Weights are computed with exact arithmetic.**
Working weights use `Fraction` and `Decimal` end to end; intensities are stored as numerator/denominator pairs, never as rounded floats. The authored spreadsheet is the golden test: the generator must reproduce all 77 weights exactly, and that test is not allowed to be weakened to accommodate a refactor.

**7. The validator must accept the author's own training plan.**
Rules are calibrated so the real template passes with zero violations. Prilepin's rep ranges and the volume trend are *warnings*, because the authored block breaks them deliberately (deadlift week 1 is 36 reps at 56%, outside Prilepin's 18–30). A rule that would reject the source template is miscalibrated, not a finding.

**8. Side effects live in activities, never in workflow code.**
Temporal workflow functions contain no database access, no HTTP, no `datetime.now()`, no randomness and no solver calls. Determinism is a correctness requirement, not a style preference.

**9. Migrations are the only path to schema change.**
No `create_all()` in application code. Every column, index and constraint arrives through Alembic.

**10. Uncertainty is surfaced, not hidden.**
Simulator output is always percentiles plus an explicit assumptions list, never a single predicted number. Tuned-by-guess thresholds (missed-session tiers, Banister constants) are labelled as starting values in both code and UI. The system advises; it does not claim to predict a body.

## Decision Making Process

**Decision maker:** Mohammad Basil Faruqui, sole developer and sole user. There is no review board, and ADRs should not pretend there is one.

**Why ADRs exist on a solo project:**

1. To stop re-litigating settled choices mid-build, when a four-week budget leaves no room for it.
2. To make the reasoning legible to interviewers — a repo whose non-obvious choices are documented reads very differently from one where they are not.
3. To force the alternatives to be written down at the moment of choosing, while the trade-off is still clear.

**When to write an ADR:**

- A choice between named technologies or libraries that a reviewer could reasonably question (Pydantic AI over LangChain, CP-SAT over a heuristic, SQLModel over plain SQLAlchemy)
- Anything that touches one of the Architecture Principles above
- A default that is a judgement call rather than a derivation (rounding mode, rest-gap rules, missed-session thresholds)
- A deliberate omission (no authentication, no multi-user support, no mobile app)
- A reversal of an earlier decision — as a superseding ADR, never an edit to the old one

**When *not* to write one:** routine implementation detail already covered by a spec, library version bumps, formatting and lint configuration, anything reversible in under an hour.

**Process:**

1. Draft as `proposed` at the moment the choice is being made, not retroactively at the end of the build.
2. State at least two genuine alternatives with their real downsides. An ADR with one straw-man alternative is worse than no ADR.
3. Move to `accepted` once the corresponding spec is implemented and its Definition of Done passes.
4. Commit alongside the code that implements it, using the configured commit template.
5. Record consequences honestly, including the ones that turned out badly. An ADR whose Consequences section admits a cost is more credible than one that does not.

**Communication:** `docs/adr/README.md` is linked from the main README under "Design decisions". The index is the artefact an interviewer is most likely to open first, so keep the titles self-explanatory.

## Areas of Focus

**Decisions already made and worth recording (write these first):**

- **Pydantic AI over LangChain / LangGraph** — typed tool I/O suits a solver-backed agent; the two overlap, so only one belongs in the repo
- **Ollama (`qwen2.5:14b`) as the default LLM, Claude as a swap-in** — cost, offline operation, and the explicit tool-calling reliability trade-off that comes with it
- **Local embeddings (`nomic-embed-text`) in every mode, including demos** — no API key needed for retrieval
- **Percentages stored as exact fractions rather than computed kilograms** — makes the template reusable across 1RM changes and keeps the golden test exact
- **Prilepin and volume-trend rules as warnings, not violations** — the authored template breaks them intentionally
- **Temporal for an 11-week durable workflow** — against the simpler alternative of a cron job plus a status column
- **Local-first hosting with no authentication** — and the consequence that any public link needs basic auth first
- **CP-SAT for block shaping even though a template lookup suffices for the default case** — this is partly a demonstration choice and the ADR should say so

**Pending decisions (unresolved; capture as `proposed` when addressed):**

- Rest rules between *different* lifts — is deadlift permitted the day after squat? Currently defaulted to a 1-day minimum with no strong justification
- Rounding default: 1 kg floor (matching the spreadsheet) versus 2.5 kg plate reality in an actual gym
- Whether `row`, as an untested accessory, should be automatically droppable when a week is compressed
- Missed-session tier thresholds (2 / 10 / 21 days) — currently placeholders
- After a long break: restart the full 11 weeks, or permit a shortened block
- If and when a public URL is wanted: Cloudflare Tunnel on demand versus an always-on VPS
- Whether to build the `domains/jobshop` adapter at all — it costs about a day and buys back the direct link to production-planning roles

**Known technical debt and risk areas:**

- **Tool-calling reliability of local models is the single biggest project risk.** The router eval (`scripts/eval_router.py`, 30 labelled cases) exists to quantify it. If Ollama accuracy proves unacceptable, the fallback decision — larger model, or Claude as the default — needs its own ADR.
- **Simulator calibration is weak by construction.** Press and row have no meet data and borrow a scaled bench distribution; Banister constants are fitted loosely. Any ADR touching the simulator must preserve principle 10.
- **The source spreadsheet contains two known errors** (the Bench sheet's relative-intensity formula reads the Sets column; Excel coerced the Prilepin ranges into dates). Both are worked around rather than fixed upstream — documented in spec 03.
- **Single-athlete assumption is baked in** throughout the API and UI. Multi-user would be a substantial change, not a feature addition.
- **No authentication anywhere.** Acceptable while local-only; blocking for any public deployment.
- **Chat history is truncated to the last 10 messages** with no summarisation — a deliberate v1 simplification that will show up as a limitation in long conversations.
