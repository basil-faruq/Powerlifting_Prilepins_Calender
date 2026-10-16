## 1. Overview

A **SimPy discrete-event simulation** that runs the rest of your block about 1,000 times with random disruptions (illness, travel) and returns **ranges**: finish date, chance of making each test single, and expected new 1RM.

It answers "what if" questions ("sick 10 days in week 6", "will I hit 150 on squat?") without touching your real plan. Disruptions are the gym version of machine breakdowns in the factory idea.

It is a **decision aid, not a prediction of your body**: every result lists its assumptions.

---

## 2. Depends on

- 08-Missed_Session_Policy

---

## 3. Routes

- `POST /blocks/{id}/simulate` → `{scenario?: ForcedEvent[], runs?: int, seed?: int}` → `SimulationResult`

> **Amended 2026-10-14.** The response also carries `block_id` and
> `has_synthetic_one_rms` (ADR-0007 rule 5, ADR-0004 rule 4): a projection built
> on a development max must say so. `seed` defaults to 0, so the same request is
> the same answer. See ADR-0012.

---

## 4. Database Schema

- No changes. Calibration data lives in a committed JSON file.

---

## 5. Functions to Implement (`domains/strength/simulator.py`)

---

### A. `SimConfig` (defaults are placeholders to tune)

| Field | Default | Meaning |
| --- | --- | --- |
| `runs` | 1000 | Monte Carlo repetitions |
| `illness_mean_interval_days` | 120 | exponential gap between illnesses |
| `illness_days_range` | (3, 10) | uniform duration |
| `travel_prob_per_week` | 0.03 | chance a week has a 3–5 day trip |
| `tau_fitness` / `tau_fatigue` | 42 / 7 days | Banister time constants |
| `k_fitness` / `k_fatigue` | 1.0 / 2.0 | Banister gains |
| `fail_softness` | 0.03 | width of the failure sigmoid |

> **Amended 2026-10-14.** Four fields the spec needed and did not list, each a
> labelled starting value: `readiness_scale` (0.25), `test_demand` (0.95),
> `travel_days_range` ((3, 5) — the spec said "3–5 day trip" in prose only) and
> `fallback_gain_range` ((0.02, 0.05) — §13's "flat 2–5%"). `runs` is capped at
> 5000 in the model itself. `illness_mean_interval_days = inf` switches illness
> off. `MissedPolicyConfig` is a separate argument to `simulate`, not a field.

---

### B. Athlete model (Banister fitness–fatigue)

- Daily load from a completed session = `Σ sets × reps × pct` over its lifts
- `fitness(t) = Σ load × e^(−(t − t_i)/tau_fitness)`, `fatigue(t)` likewise with `tau_fatigue`
- `performance(t) = k_fitness × fitness − k_fatigue × fatigue`, normalised so a clean run of the template ends at readiness 1.0 in the test week
- `readiness(t) = 1 + scale × (performance(t) − performance_ref)`
- A set fails with probability `sigmoid((rel_intensity − readiness) / fail_softness)`

> **Amended 2026-10-14.** The two formulas above name `scale`, `performance_ref`
> and `rel_intensity` without defining them, and read literally they cannot give
> the §12 behaviour (a squat test at 1.083 against a readiness of 1.0 fails
> ~94% of the time). As built:
>
> - `performance_ref` is **a curve, not a scalar**: a clean run of the remaining
>   plan is simulated once, and `performance` just before each session is kept
>   per `seq`. A scalar reference would drive readiness to ~0 in week 1, when
>   fitness has not built up.
> - `readiness = 1 + readiness_scale × (performance − performance_clean[seq]) /
>   perf_norm`, where `perf_norm` is the largest `|performance|` in the clean
>   run. A clean run is therefore at exactly 1.0 throughout.
> - A test single fails with probability `sigmoid((test_demand − readiness) /
>   fail_softness)`. `rel_intensity` is not used: it is a constant per test, so
>   it is absorbed into `test_demand`.
> - **Only test singles are sampled.** The spec gives a missed ordinary set no
>   consequence, so sampling one would cost time and change nothing.
> - Fitness and fatigue are carried by recursive decay between session dates,
>   exactly equal to the sum above and O(n) rather than O(n²).
> - Sessions already `done` count at their planned load; sessions dated in the
>   past and not done are treated as missed on day 1 and re-planned.

---

### C. Processes (SimPy, 1 time unit = 1 day)

- **athlete**: walks the planned sessions in date order; on its day, if not disrupted → performs it (load added, test outcome sampled); if disrupted → session missed
- **disruptions**: fires illness events (exponential gaps) and travel events (weekly Bernoulli); each sets an unavailable window
- After a disruption ends, the **same missed-session policy** (spec 08) re-plans, using `fast=True` (greedy dates, no CP-SAT) so 1,000 runs finish quickly
- **Forced events** from the scenario (`{start: date or week, days}`) are injected in every run

> **Amended 2026-10-14.**
>
> - **What the policy's answer becomes.** `propose` returns a `PolicyResult`
>   (ADR-0010 mod. 7). The simulator takes `proposals[0]` (lowest cost) and
>   applies it the way `services._apply_plan` does: a re-dated session becomes
>   `moved`, a folded session becomes `skipped` and stays in the plan, and later
>   re-plans waive the weekday rule for `moved` sessions. A `PolicyFailed` ends
>   the run as **unplannable**.
> - **`restart` is terminal.** A restart needs a replacement block built from a
>   database row, which a pure simulation does not have. A run that reaches it
>   ends there, and is reported as `p_restart`.
> - **Spec 15.** Running `simulate` as a Temporal activity is not done here; the
>   route runs on FastAPI's threadpool, as `shape-preview` does.
> - **Process ordering.** Each window is drawn before the clock reaches its first
>   day, so the athlete never races the process that creates windows.
> - `ForcedEvent` is defined: `kind` (`illness` | `travel`, a label), exactly one
>   of `start_week` or `start_date`, and `days` in 1–90. A week means that
>   week's Monday. A window already over, or a week past the block's end, is a
>   422.

---

### D. Test outcome and new 1RM

- Test made → `new_1rm = old × (1 + gain)`, `gain` sampled from the calibrated distribution for that lift, scaled by readiness
- Test failed or missed → `new_1rm = old`

> **Amended 2026-10-14.**
>
> - `gain_eff = max(0, gain) × clip(readiness, 0, 1)`. A made test is a lift
>   above the old max, so a negative draw is clamped: a max never falls on a
>   successful test (ADR-0005 rule 6).
> - Sampling: inverse-CDF linear interpolation over `p10…p90`; tails are clamped
>   to `p10` and `p90`. Without `gains.json`, uniform over `fallback_gain_range`.
> - `row` is never tested (CONTEXT.md §4): `p_test_made = 0.0`, max unchanged.
> - A lift whose test is already done or written off reports `p_test_made =
>   null` and an unchanged max; it is not re-sampled.
> - A restarted or unplannable run leaves every max unchanged and counts as a
>   test not made, so the 1RM ranges and `p_test_made` cover **all** runs; only
>   `finish_date` covers the runs that finished.

---

### E. `simulate(block_state, scenario, cfg, seed) -> SimulationResult`

- `finish_date: {p10, p50, p90}`
- `per_lift: {lift: {p_test_made, new_1rm: {p10, p50, p90}}}`
- `missed_sessions_mean: float`
- `assumptions: list[str]` (config values + calibration source in plain words)
- `stats: {runs, wall_time_s, seed}`

> **Amended 2026-10-14.** Added: `planned_end_date`, `missed_sessions:
> {p10,p50,p90}` beside `missed_sessions_mean` (a lone mean breaks §11's own
> "never a single number"), `p_restart`, `p_unplannable`, and per lift `test`
> (`pending` | `behind` | `none`) and `current_1rm_kg`. `finish_date` is null
> when no run finished. `stats` also reports `runs_finished`, `runs_restart` and
> `runs_unplannable`. `wall_time_s` is the one field two identical runs may
> disagree on.

---

### F. `scripts/calibrate_simulator.py`

- Input: OpenPowerlifting bulk CSV (public domain data)
- Keep: `Event = SBD`, `Equipment = Raw`, lifters with two meets 60–150 days apart, best lifts within ±20% of your current 1RMs
- Compute relative change per lift between consecutive meets
- Write `domains/strength/calibration/gains.json`: per lift `{p10, p25, p50, p75, p90, n}`
- Press and row have no meet data → use bench's distribution scaled by 0.8 (flagged in `assumptions`)
- Stream the CSV in chunks; do **not** commit the CSV, only the JSON

> **Amended 2026-10-14.**
>
> - The ±20% band takes **required** `--squat/--bench/--deadlift` arguments with
>   no defaults, and applies to the *earlier* meet. They are never read from the
>   database: a database seeded with `make seed-dev-1rms` holds invented maxes,
>   and calibrating against them would produce a file that looks measured and is
>   not (CLAUDE.md non-negotiable #9, ADR-0004).
> - Press and row are written with `derived: true` and `n = 0`; the simulator
>   repeats that in its assumptions.
> - **`gains.json` is not committed with this spec.** The OpenPowerlifting export
>   is ~1 GB and was not downloaded. The script is proven against a hand-written
>   fixture (`tests/fixtures/meets_sample.csv`), and the §13 flat-fallback path is
>   what runs until the athlete generates the file (`make calibrate`).

---

## 6. Changes to `api/routes/blocks.py`

- Add the simulate route

---

## 7. Files to Change

- `api/routes/blocks.py`

> **Amended 2026-10-14.** Routes carry no business logic (ADR-0007 rule 2), so
> the list is longer: `domains/strength/services.py` (`simulate_block`),
> `api/schemas.py` (`SimulateCreate`, `SimulationOut`), `planner_core/errors.py`
> (`BlockNotRunning`), `api/main.py` (its 409), `pyproject.toml`, `Makefile`
> (`calibrate`) and `.gitignore` (the CSV).

---

## 8. Files to Create

- `backend/domains/strength/simulator.py`
- `backend/domains/strength/calibration/gains.json`
- `backend/scripts/calibrate_simulator.py`
- `backend/tests/test_simulator.py`
- `backend/tests/api/test_simulate.py`, `backend/tests/fixtures/meets_sample.csv`

---

## 9. Dependencies

- `simpy`, `numpy`
- `polars` (calibration script only; dev dependency)

---

## 10. Scenario Format

```json
{
  "scenario": [
    {"kind": "illness", "start_week": 6, "days": 10},
    {"kind": "travel", "start_date": "2026-12-20", "days": 5}
  ],
  "runs": 1000,
  "seed": 42
}
```

---

## 11. Rules for Implementation

- Same `seed` → identical result (use `numpy.random.Generator` passed through; no global random)
- No DB access inside the simulation loop; load block state once
- 1,000 runs of an 11-week block in under 10 s on a laptop
- Never present a single number as the answer; always percentiles + assumptions

---

## 12. Expected Behavior

- No disruptions (`illness_mean_interval_days = ∞`, travel 0) → finish date p10 = p50 = p90 = planned end date; `p_test_made` high for every lift
- Forced 10-day illness in week 6 → p50 finish date later than the plan; `p_test_made` lower than baseline
- Two runs with the same seed → equal results

---

## 13. Error Handling Expectations

- Block not `active` → 409

> **Amended 2026-10-14.** `active` **and** `paused` are accepted (ADR-0010: a
> paused block still counts as running). Anything else — `draft`, `done`,
> `abandoned` — is a 409 from `BlockNotRunning`, checked *before* the block is
> loaded so a draft is not reported as a 422 about missing sessions. A forced
> event the block cannot contain is a 422.
- `runs` > 5000 → 422
- Missing calibration file → simulator still runs with a flat 2–5% gain range and says so in `assumptions`

---

## 14. Definition of Done

- [x] Simulation runs and returns percentiles + assumptions
- [x] Deterministic with a seed
- [x] Scenario injection works
- [x] Calibration script produces `gains.json` — _the script is built and tested against a fixture; no real `gains.json` is committed (see §5F note)_
- [x] Performance target met
