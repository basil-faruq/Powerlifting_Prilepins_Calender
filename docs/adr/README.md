# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for the **Powerlifting Block Planner**.

## What is an ADR?

An Architectural Decision Record (ADR) captures an important architectural decision made along with its context and consequences. ADRs help teams understand why decisions were made and provide a historical record of architectural evolution.

In this repository they carry particular weight: `CLAUDE.md` derives its non-negotiables from these records, and where a spec and an accepted ADR disagree, **the ADR wins** — the spec is updated to match. Breaking one requires a superseding ADR, not a convenient exception.

## ADR Index

| ID | Title | Status | Date |
| --- | --- | --- | --- |
| [0001](0001-project-foundation-and-local-infrastructure.md) | Project foundation and local infrastructure | accepted | 2026-10-11 |

## Status Legend

| Status | Description |
| --- | --- |
| proposed | Decision is under consideration |
| accepted | Decision has been approved and is active |
| deprecated | Decision is no longer recommended |
| superseded | Decision has been replaced by another ADR |
| rejected | Decision was considered but not adopted |

## Creating New ADRs

Use the `/adr-new` command to create a new ADR:

```
/adr-new "Title of the decision"
```

Format is `structured-madr` and numbering is four-digit, both set in `.claude/adr.local.md`.

## ADR Summary

### [ADR-0001: Project foundation and local infrastructure](0001-project-foundation-and-local-infrastructure.md)

**accepted** — 2026-10-11

Settles the four things every later spec rests on: where code goes and which way dependencies point, what runs in Docker versus on the host, how configuration reaches the code, and how the application reports dependency health.

Infrastructure (Postgres with pgvector, the Temporal dev server) is containerised with pinned tags; the application runs on the host during development; Ollama stays host-native so it can use the GPU. Configuration flows through a single cached `get_settings()` and nothing else reads `os.environ`. `/health` answers 200 with per-dependency status even when everything behind it is down, and every probe is bounded — including an explicit libpq `connect_timeout`, without which the endpoint hangs rather than reporting a dead database.

Records spec 01 as complete at 2026-10-11 15:46 +0200 (commit `fd57379`), with two Definition-of-Done items left explicitly unverified because Docker and GNU Make are not installed on the development machine.

---

## Custom Content

Open questions that are deliberately undecided — rest rules between different lifts, 1 kg versus 2.5 kg rounding, the missed-session thresholds, and others — are listed in [`CONTEXT.md`](../../CONTEXT.md) §9. Each becomes a `proposed` ADR when it is addressed, so that none of them is silently decided in code.
