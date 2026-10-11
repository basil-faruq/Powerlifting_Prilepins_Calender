## 1. Overview

Define every table as a **SQLModel** class and create them through **Alembic** migrations in Postgres, with the **pgvector** extension enabled.

This is the data foundation: templates, blocks, planned sessions, logs, notes (with embeddings), proposals and the agent's decision audit trail.

---

## 2. Depends on

- 01-Project_Setup

---

## 3. Routes

- No new routes

---

## 4. Database Schema

All primary keys are `UUID` (generated in Python, `uuid4`) unless stated. All tables get `created_at TIMESTAMPTZ DEFAULT now()`.

---

### A. athlete

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| name | TEXT | Not null |
| plate_increment_kg | NUMERIC(4,2) | Not null, default 1.00 (matches your sheet's 1 kg floor) |
| rounding | TEXT | Not null, `floor` or `nearest`, default `floor` |
| timezone | TEXT | Not null, default `Europe/Berlin` |

---

### B. lift

| Column | Type | Constraints |
| --- | --- | --- |
| code | TEXT | Primary key: `squat`, `bench`, `deadlift`, `press`, `row` |
| name | TEXT | Not null |
| is_accessory | BOOLEAN | Not null, default false (`row` = true) |
| variations | JSONB | Not null, default `[]` (bench: `["close_grip", "wide_grip"]`) |

Seeded by migration.

---

### C. one_rep_max

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| athlete_id | UUID | FK → athlete.id, not null |
| lift_code | TEXT | FK → lift.code, not null |
| kg | NUMERIC(6,2) | Not null, > 0 |
| set_on | DATE | Not null |
| source | TEXT | `tested`, `estimated` or `imported` |
| block_id | UUID | FK → block.id, nullable |

Index: `(athlete_id, lift_code, set_on DESC)`. The current 1RM = newest row per lift.

---

### D. block_template

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| name | TEXT | Not null, unique |
| weeks | INTEGER | Not null (11) |
| slots | INTEGER | Not null (3 = Day 1–3) |
| overreach_from_week | INTEGER | Not null, default 7 |
| source_file | TEXT | Nullable |

---

### E. template_entry

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| template_id | UUID | FK → block_template.id, not null, ON DELETE CASCADE |
| lift_code | TEXT | FK → lift.code, not null |
| variation | TEXT | Nullable |
| week | INTEGER | Not null, 1..weeks |
| slot | INTEGER | Not null, 1..slots |
| sets | INTEGER | Not null, > 0 |
| reps | INTEGER | Not null, > 0 |
| pct_num | NUMERIC(10,4) | Not null (e.g. 70) |
| pct_den | NUMERIC(10,4) | Not null, > 0 (e.g. 120) |
| is_test | BOOLEAN | Not null, default false |

Unique: `(template_id, lift_code, week, slot)`.

---

### F. weekday_map

| Column | Type | Constraints |
| --- | --- | --- |
| athlete_id | UUID | FK → athlete.id, part of PK |
| slot | INTEGER | Part of PK, 1..3 |
| weekday | INTEGER | Not null, 0..6 (0 = Monday) |
| locked_from | DATE | Nullable; set when a block starts |

Unique: `(athlete_id, weekday)` — two slots cannot share a day.

---

### G. block

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| athlete_id | UUID | FK → athlete.id, not null |
| template_id | UUID | FK → block_template.id, not null |
| number | INTEGER | Not null (1, 2, 3… per athlete) |
| start_date | DATE | Not null, must be a Monday |
| status | TEXT | `draft`, `approved`, `active`, `paused`, `done`, `abandoned` |
| lifts | JSONB | Not null, chosen lift codes |
| one_rm_snapshot | JSONB | Not null, `{lift_code: kg}` at block start |
| workflow_id | TEXT | Nullable (spec 15) |

---

### H. planned_session

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| block_id | UUID | FK → block.id, not null, ON DELETE CASCADE |
| week | INTEGER | Not null |
| slot | INTEGER | Not null |
| seq | INTEGER | Not null, order inside the block (1..n) |
| date | DATE | Not null |
| original_date | DATE | Not null (date first planned) |
| status | TEXT | `planned`, `done`, `missed`, `moved`, `skipped` |

Unique: `(block_id, seq)`.

---

### I. planned_lift

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| session_id | UUID | FK → planned_session.id, ON DELETE CASCADE |
| lift_code | TEXT | FK → lift.code |
| variation | TEXT | Nullable |
| sets | INTEGER | Not null |
| reps | INTEGER | Not null |
| pct_num | NUMERIC(10,4) | Not null |
| pct_den | NUMERIC(10,4) | Not null |
| weight_kg | NUMERIC(6,2) | Not null |
| rel_intensity | NUMERIC(6,4) | Not null |
| is_test | BOOLEAN | Not null |

---

### J. session_log

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| planned_session_id | UUID | FK → planned_session.id, unique |
| performed_on | DATE | Not null |
| entries | JSONB | Not null: `[{lift_code, sets_done, reps_done, weight_kg, rpe?}]` |
| note | TEXT | Nullable |

---

### K. note_chunk

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| athlete_id | UUID | FK → athlete.id, not null |
| log_id | UUID | FK → session_log.id, nullable, ON DELETE CASCADE |
| lift_codes | JSONB | Not null, default `[]` |
| text | TEXT | Not null |
| noted_on | DATE | Not null |
| embedding | VECTOR(768) | Not null (dimension = `settings.embed_dim`) |
| is_synthetic | BOOLEAN | Not null, default false (demo seed notes) |

Index: HNSW on `embedding vector_cosine_ops`.

---

### L. proposal

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| block_id | UUID | FK → block.id, not null |
| kind | TEXT | `new_block`, `reschedule`, `weekday_change` |
| status | TEXT | `pending`, `approved`, `rejected`, `superseded` |
| payload | JSONB | Not null: the change set (see spec 05) |
| cost | JSONB | Not null, default `{}` |
| reasons | JSONB | Not null, default `[]` |
| warnings | JSONB | Not null, default `[]` |
| source | TEXT | `chat` or `workflow` |
| decided_at | TIMESTAMPTZ | Nullable |

---

### M. decision

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| run_id | TEXT | Not null |
| source | TEXT | `chat` or `workflow` |
| route | TEXT | `answer`, `plan`, `reschedule`, `simulate`, `safety`, `clarify` |
| filter_fired | TEXT | Nullable (e.g. `pain_keyword`) |
| tool_calls | JSONB | Not null, default `[]` |
| proposal_id | UUID | FK → proposal.id, nullable |
| outcome | TEXT | Nullable: `approved`, `rejected`, `answered`, `failed` |
| langfuse_trace_id | TEXT | Nullable |

---

### N. chat_message

| Column | Type | Constraints |
| --- | --- | --- |
| id | UUID | Primary key |
| conversation_id | UUID | Not null, indexed |
| athlete_id | UUID | FK → athlete.id |
| role | TEXT | `user` or `assistant` |
| content | TEXT | Not null |
| decision_id | UUID | FK → decision.id, nullable |

---

## 5. Functions to Implement

---

### A. Models (`domains/strength/models.py`)

- One SQLModel class per table above (`table=True`)
- `embedding` uses `pgvector.sqlalchemy.Vector(settings.embed_dim)` via `sa_column`
- JSONB columns via `sa_column=Column(JSONB)`
- Enum-like text columns validated with `Literal[...]` types from `domains/strength/enums.py`

---

### B. Migrations

- `0001_init`: `CREATE EXTENSION IF NOT EXISTS vector;` + all tables + indexes
- `0002_seed_lifts`: inserts the 5 lifts (idempotent `ON CONFLICT DO NOTHING`)

---

### C. `scripts/seed_athlete.py`

- Creates one athlete "Basil" (if none exists) with 1RMs from the sheet as `source = imported`: deadlift 160, squat 140, bench 105, press 70, row 105

---

## 6. Changes to `api/main.py`

- None (migrations run through `make migrate`)

---

## 7. Files to Change

- `Makefile` → add `migrate` (`uv run alembic upgrade head`) and `seed` targets

---

## 8. Files to Create

- `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001_init.py`, `0002_seed_lifts.py`
- `backend/domains/strength/enums.py`, `models.py`
- `backend/scripts/seed_athlete.py`
- `backend/tests/test_models.py`

---

## 9. Dependencies

- Already installed in spec 01: `sqlmodel`, `alembic`, `psycopg`, `pgvector`

---

## 10. Fixed Values

- Lift codes: `squat`, `bench`, `deadlift`, `press`, `row`
- Variations: `close_grip`, `wide_grip` (bench only)
- Embedding dimension: 768 (`nomic-embed-text`)

---

## 11. Rules for Implementation

- Migrations are the only way the schema changes; never `create_all()` in app code
- Percentages are stored as `pct_num` / `pct_den`, never as one rounded float
- Money-like precision for kg: `NUMERIC`, converted to `Decimal` in Python
- Every FK has an index
- Tests run against a separate database `planner_test` (created by `conftest.py`)

---

## 12. Expected Behavior

- `make migrate` on an empty database creates all 14 tables and the `vector` extension
- Running it twice is a no-op
- `make seed` creates the athlete and five 1RM rows once

---

## 13. Error Handling Expectations

- Duplicate `(athlete_id, weekday)` in `weekday_map` → `IntegrityError`
- `template_entry` with unknown `lift_code` → FK violation
- `note_chunk` with wrong embedding length → error from pgvector

---

## 14. Definition of Done

- [ ] All 14 tables exist with the listed constraints
- [ ] `vector` extension enabled; HNSW index on `note_chunk.embedding`
- [ ] Lifts seeded; seed athlete script idempotent
- [ ] Tests cover one FK violation and one unique violation
