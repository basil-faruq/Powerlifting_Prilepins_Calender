# CONTEXT.md

Domain background for the Powerlifting Block Planner. `CLAUDE.md` says how to work in this repo; this file says what the words mean and which facts are already verified.

---

## 1. The problem

The athlete trains an 11-week block built from a spreadsheet template. The template says, for each lift, what to do in week *w* on day-slot *s*: a number of sets, reps, and an intensity expressed as a percentage of the current one-rep max. Intensity starts near 50% and finishes above 100% — the final single **is** the new max test. When the block ends, the tested maxes become the new 1RMs and the same template generates the next block at the new weights.

Three things the spreadsheet cannot do, and the reason this project exists:

1. **It has no calendar.** The template knows "Day 1, Day 2, Day 3", not Monday/Wednesday/Friday, and not dates.
2. **It has no answer for a missed session.** Life interrupts: travel, illness, a tweaked knee. The athlete currently improvises, which quietly breaks the progression.
3. **It cannot tell you whether a change is safe.** Nothing stops a rescheduled week from putting two heavy squat sessions a day apart, or pushing 105% three weeks early.

The planner fills those three gaps: it assigns dates, proposes repairs when a session is missed, and refuses plans that break the rules.

---

## 2. Factory → gym

The project is a production-planning agent whose domain happens to be a gym. The mapping is exact enough that the optional `domains/jobshop` adapter runs on the same engine:

| Production planning | This project |
| --- | --- |
| Job | Planned session |
| Operation | A lift's set block within a session |
| Machine | Weekday slot |
| Machine calendar | The athlete's fixed weekday map |
| Setup / changeover time | Minimum rest gap between sessions sharing a lift |
| Precedence constraint | Week order; a lift's test must follow its ramp |
| Breakdown | Missed session |
| Makespan | Block finish date |
| Capacity | Recoverable weekly volume |

---

## 3. Vocabulary

- **1RM** — one-rep max, the heaviest single for a lift. Stored per athlete per block.
- **Block** — one 11-week cycle. 11 weeks × 3 slots = 33 sessions.
- **Slot** — the template's Day 1 / Day 2 / Day 3. Mapped to real weekdays by the athlete once, then fixed.
- **Session** — one slot on one date. Carries one or more lifts' set blocks.
- **Entry** — one row of the template: lift × week × slot, with sets, reps and intensity.
- **Intensity (pct)** — percentage of 1RM, stored as an exact fraction such as `70/120`.
- **Working weight** — `FLOOR(1RM × pct, increment)`. The spreadsheet uses a 1 kg increment.
- **Rep max percentage (P1RM)** — the share of 1RM a trained lifter can hold for *n* reps. Brzycki-style table.
- **Relative intensity** — `pct ÷ P1RM(reps)`. 1.0 means the set is at the limit for that rep count. Above 1.0 is overreaching, which the template only permits from week 7.
- **Overreach week** — week 7. Before it, relative intensity above 1.0 is a violation; from it, a warning.
- **Test** — a single at over 100%, marking the new max. One per lift, except row.
- **Prilepin zones** — classical sets/reps guidance per intensity band. Used as **warnings** only.
- **Proposal** — a candidate change (new block, reschedule, weekday change) with its validation result, awaiting human approval.
- **Decision** — one chat turn's trace: which filter fired, which route was chosen, which tools ran with what inputs and outputs.

---

## 4. The template, verified

`data/B1_Mine.xlsx`, block B1.

**What the workbook contributes is the block's *pattern* and nothing else:** which lift trains on which day of which week, for how many sets and reps, at what intensity. The maxes on its `1RM` sheet — deadlift 160, squat 140, bench 105, press 70, row 105 kg — are **examples**, and the file records no date for them at all. The importer does not read that sheet. Real 1RMs reach `one_rep_max` only when the athlete enters them together with the date they tested them, and those are the only maxes the planner ever computes from. The numbers are repeated here solely so the sheet's own arithmetic below can be followed.

For the same reason the intensity denominator is **not** a 1RM. The squat rows divide by 120 while that sheet's example max is 140, and bench W11 divides by 125 against 105 — the denominator is the target max the block was programmed against, an authored datum in its own right. A weight is `FLOOR(athlete's 1RM × num/den, increment)`; `pct_den` never stands in for the max.

| Lift | Entries | Slot(s) | Test |
| --- | --- | --- | --- |
| Deadlift | 11 | 2 | W11 S2, 1×1 @ 170/160 |
| Squat | 22 | 1 and 3 | W11 S1, 1×1 @ 130/120 |
| Bench | 22 | 1 close grip, 3 wide grip (W10 both wide) | W10 S3, 1×1 @ 112.5/105 |
| Press | 11 | 2 | W9 S2, 1×1 @ 72.5/70 |
| Row | 11 | 3 | none — row is never tested |

**77 entries total, 4 tests.** All 77 weights were recomputed from the fractions and matched the values Excel had cached — the arithmetic is sound and there is no rounding drift to reproduce. That comparison is the golden test, and it is the one place the example maxes are legitimately used: the fixture asks whether the generator reproduces Excel's own output for Excel's own inputs.

Three oddities to know about:

- **Squat W11 S3** has the literal value `0.5`, not a formula like the others.
- **The Bench sheet's relative-intensity column reads the Sets column instead of Reps.** Wrong in the spreadsheet; recomputed correctly in code.
- **The Prilepin chart sheet was coerced into dates by Excel.** Unrecoverable, so the zone values are hard-coded in the validator.

Also: `Wide Grp` in Bench W11 Day 3 is a typo for `Wide Grip`, normalised on import.

The spreadsheet is treated as read-only source material. Neither error is fixed upstream, because the file is the historical artefact the golden test compares against.

---

## 5. What the validator enforces

Hard rules reject a plan; soft rules attach a warning and let it through. The split is calibrated so the author's own block passes with zero violations.

**Hard:** a session off its home weekday without approval; two sessions on one date; dates not increasing; two sessions sharing a lift closer than the rest gap; above 100% earlier than the week before that lift's test; above 110% anywhere; relative intensity above 1.0 before week 7 or above 1.10 anywhere; a weight that is not a multiple of the plate increment; progression on a lift with a pain note in the last 7 days; a date in the past.

**Soft:** Prilepin reps-per-set and total-reps outside the zone; weekly volume rising while weekly top intensity also rises.

Two calibration points worth remembering, because they look like bugs:

- The 110% cap exists because the athlete's own squat test is 130/120 ≈ **108.3%**. A 108% cap would reject the source template.
- Deadlift week 1 is 6×6 = 36 reps at 56%, outside Prilepin's 18–30. That is deliberate, so Prilepin is a warning.

---

## 6. Missed sessions

Four tiers, by how long the athlete was away. The thresholds are **starting guesses**, not derived values, and are labelled as such in the UI:

| Tier | Gap | Response |
| --- | --- | --- |
| Short | ≤ 2 days | Move the session to the next free day that keeps the rest gap |
| Week | ≤ 10 days | Repeat the week, or step back one week |
| Illness | ≤ 21 days | Step back and re-ramp; reduce the top set |
| Restart | longer | Restart the block, optionally re-testing first |

The solver returns one to three options with their costs; the athlete picks. Nothing is written without approval.

---

## 7. How a request flows

```
message
  → deterministic filters (pain keyword / no 1RMs / no weekday map)
  → LLM selector chooses one of: answer, plan, reschedule, simulate
  → plain-Python dispatch calls exactly one tool
  → solver or generator produces a plan
  → validator accepts or rejects it
  → explainer writes 2–4 sentences using only numbers from the object
  → proposal shown for approval
  → approval signals the Temporal workflow, which writes
```

The LLM appears twice: choosing the route, and phrasing the result. It never loops over the solver and never produces a number.

---

## 8. Data and its limits

- **The template is the athlete's own** and is the only real training data the repository ships. The workbook's 1RMs are **examples** — the maxes the planner computes from are entered by the athlete with the date they tested them, and the importer never reads the sheet's own (§4, ADR-0003).
- **Synthetic 1RMs exist for local testing only**, written by `make seed-dev-1rms` and flagged `is_synthetic` with `source = 'synthetic'`, which a database CHECK keeps consistent. `is_synthetic` is the authoritative "is this real?"; every consumer filters on it and the UI must say so while any row has it set (ADR-0004).
- **Training notes** ("knee tight at depth", "slept 5 h, bar slow") are embedded locally and retrieved by meaning. Safety screening of notes is **keyword-based**, deliberately not embedding-based.
- **Demo notes are synthetic** and flagged as such in the database and the README.
- **OpenPowerlifting** (public domain) calibrates the simulator's progression distributions.
- **Press and row have no meet data.** They borrow a scaled bench distribution. The Banister fitness–fatigue constants are fitted loosely. Simulator output is therefore always percentiles with an assumptions list — it advises, it does not predict a body.

---

## 9. Still open

Recorded here so they are not silently decided in code. Each becomes a `proposed` ADR when addressed.

- Rest rules between *different* lifts — is deadlift allowed the day after squat? Currently a 1-day minimum with no strong justification.
- Rounding: 1 kg floor, matching the spreadsheet, versus 2.5 kg plate reality in a real gym.
- Whether row, as an untested accessory, should be droppable automatically when a week is compressed.
- The missed-session thresholds above.
- After a long break: restart all 11 weeks, or allow a shortened block.
- If a public URL is ever wanted: Cloudflare Tunnel on demand versus an always-on VPS. Either needs basic auth first — there is no login.
- Whether to build the `domains/jobshop` adapter at all.

---

## 10. Known risks

- **Local-model tool-calling reliability is the single biggest risk.** `scripts/eval_router.py` with 30 labelled cases exists to quantify it. If Ollama accuracy is unacceptable, the fallback — a larger model, or Claude as default — needs its own ADR.
- **Single-athlete assumption is baked in** throughout the API and UI. Multi-user is a rewrite, not a feature.
- **No authentication anywhere.** Fine while local; blocking for anything public.
- **Chat history is truncated to the last 10 messages** with no summarisation. A deliberate v1 simplification that will show in long conversations.
