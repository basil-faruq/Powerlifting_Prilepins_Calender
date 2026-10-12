## 1. Overview

Index of all implementation specs for the **Powerlifting Block Planner**: an agent that plans an 11-week strength block from current 1RMs, pins sessions to fixed weekdays, reschedules around missed days, and rolls into the next block after a test.

Source of truth for design decisions: **Powerlifting Block Planner — Project Spec** (the shared doc). These files turn it into buildable steps. Implement them **in order**; each spec lists what it depends on.

---

## 2. Depends on

Nothing — read this first.

---

## 3. Spec List

| No | Spec | Depends on | Build week |
| --- | --- | --- | --- |
| 01 | Project_Setup | — | 1 |
| 02 | Database_Schema | 01 | 1 |
| 03 | XLSX_Template_Import | 02 | 1 |
| 04 | Block_Generator | 03 | 1 |
| 05 | Validator | 04 | 1 |
| 06 | Core_API | 02–05 | 1 |
| 07 | Calendar_Solver | 04, 05 | 2 |
| 08 | Missed_Session_Policy | 07 | 2 |
| 09 | Block_Shaping_Solver | 04, 05 | 2 |
| 10 | Simulator | 08 | 2 |
| 11 | LLM_Provider_And_Embeddings | 01 | 3 |
| 12 | Notes_RAG | 02, 11 | 3 |
| 13 | Agent_Router_And_Tools | 06–12 | 3 |
| 14 | Langfuse_Tracing | 13 | 3 |
| 15 | Temporal_Workflow | 06, 08, 13 | 3 |
| 16 | Frontend_Setup | 06 | 4 |
| 17 | Setup_Wizard | 16 | 4 |
| 18 | Block_Calendar | 16 | 4 |
| 19 | Chat_Panel_And_Approvals | 13, 15, 16 | 4 |
| 20 | Session_Logger_And_Progress | 15, 16 | 4 |
| 21 | Decision_Trace_View | 14, 16 | 4 |
| 22 | Local_Deployment | all above | 4 |
| 23 | JobShop_Adapter (optional) | 05, 15 | after 4 |

---

## 4. Repository Layout (all specs refer to these paths)

```
powerlifting-planner/
├── docker-compose.yml
├── .env.example
├── Makefile
├── README.md
├── data/
│   └── B1_Mine.xlsx                  # your template
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/
│   ├── planner_core/                 # domain-agnostic engine
│   │   ├── config.py                 # Settings (pydantic-settings)
│   │   ├── db.py                     # engine + session
│   │   ├── proposal.py               # Proposal, Violation, ValidationResult
│   │   ├── llm.py                    # get_model(), get_embedder()
│   │   └── tracing.py                # Langfuse / OpenTelemetry
│   ├── domains/
│   │   ├── strength/                 # powerlifting domain plug-in
│   │   │   ├── enums.py
│   │   │   ├── models.py             # SQLModel tables
│   │   │   ├── schemas.py            # non-table Pydantic models
│   │   │   ├── importer.py
│   │   │   ├── brzycki.py
│   │   │   ├── rounding.py
│   │   │   ├── generator.py
│   │   │   ├── validator.py
│   │   │   ├── calendar_solver.py
│   │   │   ├── missed_policy.py
│   │   │   ├── shaping_solver.py
│   │   │   ├── simulator.py
│   │   │   ├── notes.py
│   │   │   ├── agent/                # router.py, tools.py, agent.py, explainer.py
│   │   │   └── workflows/            # block_workflow.py, activities.py, worker.py
│   │   └── jobshop/                  # optional, spec 23
│   ├── api/
│   │   ├── main.py
│   │   ├── deps.py
│   │   └── routes/
│   ├── scripts/
│   └── tests/
│       └── fixtures/
└── frontend/
    ├── package.json
    ├── vite.config.ts
    └── src/
```

---

## 5. Global Conventions

- **Python 3.12**, dependencies managed with **uv** (`backend/pyproject.toml`)
- **Pydantic v2** for every data shape; **SQLModel** for tables
- **Type hints everywhere**; `ruff` for lint/format; `pytest` for tests
- **Units:** kilograms; percentages stored as **fractions** (numerator / denominator), never as rounded floats
- **Dates:** ISO `YYYY-MM-DD`; weekdays as integers `0 = Monday … 6 = Sunday`
- **Timezone:** `Europe/Berlin` (configurable) for anything day-boundary related
- **SQL:** ORM or parameterized queries only; never string-format SQL
- **LLM:** never computes numbers; every number comes from the solver, simulator, generator or database
- **Local-first:** everything runs with `docker compose` + Ollama on the host; no paid service is required to build or use the app

---

## 6. Definition of Done (whole project)

- [ ] All specs 01–22 implemented and their own Definition of Done met
- [ ] `make up` starts the full stack locally
- [ ] Golden test: generator reproduces all 77 weights from `B1_Mine.xlsx`
- [ ] A full block can be simulated end-to-end in Temporal's time-skipping test environment
- [ ] README contains architecture diagram, factory ↔ gym mapping, setup steps and demo GIF
