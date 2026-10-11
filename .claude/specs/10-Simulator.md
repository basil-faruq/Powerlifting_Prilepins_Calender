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

---

### B. Athlete model (Banister fitness–fatigue)

- Daily load from a completed session = `Σ sets × reps × pct` over its lifts
- `fitness(t) = Σ load × e^(−(t − t_i)/tau_fitness)`, `fatigue(t)` likewise with `tau_fatigue`
- `performance(t) = k_fitness × fitness − k_fatigue × fatigue`, normalised so a clean run of the template ends at readiness 1.0 in the test week
- `readiness(t) = 1 + scale × (performance(t) − performance_ref)`
- A set fails with probability `sigmoid((rel_intensity − readiness) / fail_softness)`

---

### C. Processes (SimPy, 1 time unit = 1 day)

- **athlete**: walks the planned sessions in date order; on its day, if not disrupted → performs it (load added, test outcome sampled); if disrupted → session missed
- **disruptions**: fires illness events (exponential gaps) and travel events (weekly Bernoulli); each sets an unavailable window
- After a disruption ends, the **same missed-session policy** (spec 08) re-plans, using `fast=True` (greedy dates, no CP-SAT) so 1,000 runs finish quickly
- **Forced events** from the scenario (`{start: date or week, days}`) are injected in every run

---

### D. Test outcome and new 1RM

- Test made → `new_1rm = old × (1 + gain)`, `gain` sampled from the calibrated distribution for that lift, scaled by readiness
- Test failed or missed → `new_1rm = old`

---

### E. `simulate(block_state, scenario, cfg, seed) -> SimulationResult`

- `finish_date: {p10, p50, p90}`
- `per_lift: {lift: {p_test_made, new_1rm: {p10, p50, p90}}}`
- `missed_sessions_mean: float`
- `assumptions: list[str]` (config values + calibration source in plain words)
- `stats: {runs, wall_time_s, seed}`

---

### F. `scripts/calibrate_simulator.py`

- Input: OpenPowerlifting bulk CSV (public domain data)
- Keep: `Event = SBD`, `Equipment = Raw`, lifters with two meets 60–150 days apart, best lifts within ±20% of your current 1RMs
- Compute relative change per lift between consecutive meets
- Write `domains/strength/calibration/gains.json`: per lift `{p10, p25, p50, p75, p90, n}`
- Press and row have no meet data → use bench's distribution scaled by 0.8 (flagged in `assumptions`)
- Stream the CSV in chunks; do **not** commit the CSV, only the JSON

---

## 6. Changes to `api/routes/blocks.py`

- Add the simulate route

---

## 7. Files to Change

- `api/routes/blocks.py`

---

## 8. Files to Create

- `backend/domains/strength/simulator.py`
- `backend/domains/strength/calibration/gains.json`
- `backend/scripts/calibrate_simulator.py`
- `backend/tests/test_simulator.py`

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
- `runs` > 5000 → 422
- Missing calibration file → simulator still runs with a flat 2–5% gain range and says so in `assumptions`

---

## 14. Definition of Done

- [ ] Simulation runs and returns percentiles + assumptions
- [ ] Deterministic with a seed
- [ ] Scenario injection works
- [ ] Calibration script produces `gains.json`
- [ ] Performance target met
