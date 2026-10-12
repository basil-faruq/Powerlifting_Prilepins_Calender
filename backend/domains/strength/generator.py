"""Turning a template plus the athlete's maxes into a dated, loaded block.

Four functions, one of which touches the database:

* `check_weekday_map` -- is this choice of training days coherent at all?
* `generate_block` -- the template, the maxes and a start Monday become 33
  dated sessions carrying 77 weights. A pure function.
* `persist_block` -- the only writer. Turns a draft into rows.
* `next_block_one_rms` -- what the athlete's maxes become after the block.
  Also pure: it reads the plan from the draft and the performance from logs.

No solver here. Dates come straight from the weekday map; spec 07 is what moves
a session once something changes.

Three rules this module exists to honour:

1. **The weight comes from the athlete's 1RM, never from `pct_den`.** The
   denominator is the target max the block was programmed against, and it
   differs from the sheet's example max in 23 of the 77 rows (ADR-0003 rule 3).
   An implementation that confuses the two passes most of the golden test.
2. **Exact arithmetic throughout.** `Fraction` from the template to the
   rounding step, `Decimal` after it, `float` nowhere.
3. **No 1RM is ever invented.** `next_block_one_rms` returns a number the
   athlete lifted on a day they logged, or it returns the old one unchanged
   (CLAUDE.md non-negotiable #9).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Protocol

import sqlalchemy as sa
from sqlmodel import Session

from domains.strength import enums, models, rep_max
from domains.strength.rounding import round_weight
from domains.strength.schemas import (
    BlockDraft,
    DraftLift,
    DraftSession,
    LoggedSession,
    NewOneRm,
)

# 0 = Monday, matching CLAUDE.md, `weekday_map.weekday` and Python's
# `date.weekday()`. Note PostgreSQL's DOW is 0 = Sunday and its ISODOW is
# 1 = Monday -- three numberings coexist and must never be compared directly
# (ADR-0002 rule 4). These names are for error messages only.
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

DAYS_PER_WEEK = 7

# Two sessions sharing a lift must be at least this far apart, including across
# the week boundary. A starting value from the author's own layout
# (Mon/Wed/Fri), not a derived constant: squat on Day 1 and Day 3 sits 2 days
# apart there, and Sunday-then-Monday is the case this rejects.
MIN_REST_DAYS = 2

# How far back `next_block_one_rms` looks for an accessory's best set, counted
# in template weeks rather than days: it needs no clock, and a block that was
# rescheduled around a missed week still means the same thing by it.
ESTIMATE_WINDOW_WEEKS = 4


class TemplateRow(Protocol):
    """What the generator needs of a template entry.

    Structural, so both `models.TemplateEntry` (rows from the database) and
    `schemas.ParsedEntry` (a fresh parse, or a hand-built test fixture) satisfy
    it without either importing the other.
    """

    lift_code: str
    variation: str | None
    week: int
    slot: int
    sets: int
    reps: int
    pct_num: Decimal
    pct_den: Decimal
    is_test: bool


def entry_pct(entry: TemplateRow) -> Fraction:
    """A template row's intensity as one exact rational.

    Two single-argument `Fraction`s divided, never `Fraction(num, den)`, which
    raises `TypeError` on Decimals (ADR-0002 rule 2). Works on a database row
    handing back `Decimal('70.0000')` as readily as on a parsed `70`.

    Always compare intensities through this, never as a `(num, den)` tuple:
    equal intensities may hold unequal pairs (ADR-0003 rule 2).
    """
    return Fraction(entry.pct_num) / Fraction(entry.pct_den)


# --- Weekday maps ---------------------------------------------------------


def check_weekday_map(
    weekday_map: Mapping[int, int],
    template_entries: Iterable[TemplateRow],
    lifts: Sequence[str],
) -> list[str]:
    """Problems with pinning these slots to these weekdays; empty means fine.

    Every problem is reported, not just the first, so one call tells the
    athlete everything they need to change. Messages name days rather than
    numbers, because the reader is choosing days of the week.

    Only the slots the chosen lifts actually need are examined. A deadlift-only
    block needs Day 2 alone, so Days 1 and 3 may be absent or absurd.
    """
    chosen = set(lifts)
    needed: dict[int, set[str]] = {}
    for entry in template_entries:
        if entry.lift_code in chosen:
            needed.setdefault(entry.slot, set()).add(entry.lift_code)

    problems: list[str] = []

    # 1. Every needed slot is mapped, and to a real weekday.
    for slot in sorted(needed):
        if slot not in weekday_map:
            which = ", ".join(sorted(needed[slot]))
            problems.append(f"Day {slot} ({which}) has no weekday")
        elif not 0 <= weekday_map[slot] <= 6:
            problems.append(f"Day {slot}: weekday {weekday_map[slot]} is not in 0-6 (0 = Monday)")

    mapped = {
        slot: weekday_map[slot]
        for slot in sorted(needed)
        if slot in weekday_map and 0 <= weekday_map[slot] <= 6
    }
    if not mapped:
        return problems

    # 2. No two slots on the same weekday.
    seen: dict[int, int] = {}
    for slot, weekday in mapped.items():
        if weekday in seen:
            problems.append(
                f"Day {slot} and Day {seen[weekday]} are both on {WEEKDAY_NAMES[weekday]}"
            )
        else:
            seen[weekday] = slot

    # 3. Slots run in order through the week: Day 1 before Day 2 before Day 3.
    ordered = sorted(mapped.items())
    # strict=False: pairing a list with its own tail is deliberately ragged.
    for (earlier_slot, earlier), (later_slot, later) in zip(ordered, ordered[1:], strict=False):
        if later <= earlier:
            problems.append(
                f"Day {later_slot} ({WEEKDAY_NAMES[later]}) must come after "
                f"Day {earlier_slot} ({WEEKDAY_NAMES[earlier]})"
            )

    # 4. A lift trained twice needs rest between, including across the wrap to
    #    next week -- the case that is easy to miss, and the reason Sunday as
    #    Day 3 fails against Monday as Day 1.
    for lift in sorted(chosen):
        slots = [slot for slot, lift_codes in needed.items() if lift in lift_codes]
        # Sorted by *weekday*, not by slot: when the slots are themselves out
        # of order (rule 3 reports that separately) walking them in slot order
        # would measure the gaps backwards and miss a short one.
        days = sorted(
            ((slot, mapped[slot]) for slot in slots if slot in mapped),
            key=lambda item: item[1],
        )
        if len(days) < 2:
            continue
        for index, (slot, weekday) in enumerate(days):
            next_slot, next_weekday = days[(index + 1) % len(days)]
            if next_slot == slot:
                continue
            gap = next_weekday - weekday
            wrapped = gap <= 0
            if wrapped:
                gap += DAYS_PER_WEEK
            if gap < MIN_REST_DAYS:
                across = " across the week boundary" if wrapped else ""
                problems.append(
                    f"{lift}: Day {slot} ({WEEKDAY_NAMES[weekday]}) to "
                    f"Day {next_slot} ({WEEKDAY_NAMES[next_weekday]}) is only "
                    f"{gap} day{'s' if gap != 1 else ''} apart{across}, "
                    f"and {MIN_REST_DAYS} are needed"
                )

    return problems


# --- Generation -----------------------------------------------------------


def generate_block(
    athlete: models.Athlete,
    template: models.BlockTemplate,
    entries: Iterable[TemplateRow],
    one_rms: Mapping[str, Decimal],
    lifts: Sequence[str],
    start_date: dt.date,
    weekday_map: Mapping[int, int],
) -> BlockDraft:
    """The template, applied to these maxes, pinned to these weekdays.

    Pure: no database, no clock, no randomness. The same inputs always give the
    same block, which is what lets the golden test compare 77 weights against
    numbers Excel computed years ago.

    Choosing a subset of lifts cannot change the remaining lifts' numbers --
    each weight depends only on its own template row and its own 1RM, so there
    is nothing for a dropped lift to influence.
    """
    if start_date.weekday() != 0:
        raise ValueError("start_date must be a Monday")

    chosen = list(dict.fromkeys(lifts))
    if not chosen:
        raise ValueError("at least one lift must be chosen")

    unknown = [lift for lift in chosen if lift not in enums.LIFT_CODES]
    if unknown:
        raise ValueError(f"unknown lift {unknown[0]!r}")

    rows = [entry for entry in entries if entry.lift_code in set(chosen)]

    trained = {entry.lift_code for entry in rows}
    missing = [lift for lift in chosen if lift not in trained]
    if missing:
        raise ValueError(f"the template has no entries for {missing[0]}")

    # Before the weekday map, because "no 1RM for press" is the more useful
    # message when both are wrong.
    for lift in chosen:
        if lift not in one_rms:
            raise ValueError(f"no 1RM for {lift}")
        if one_rms[lift] <= 0:
            raise ValueError(f"1RM for {lift} must be positive, got {one_rms[lift]}")

    problems = check_weekday_map(weekday_map, rows, chosen)
    if problems:
        # Includes spec §13's "weekday map missing a slot that the chosen lifts
        # need". Raising on *every* problem keeps a draft from existing in a
        # shape the validator would only reject later.
        raise ValueError("; ".join(problems))

    for entry in rows:
        if entry.week > template.weeks:
            raise ValueError(f"template entry in week {entry.week} exceeds {template.weeks} weeks")
        if entry.slot > template.slots:
            raise ValueError(f"template entry in slot {entry.slot} exceeds {template.slots} slots")

    grouped: dict[tuple[int, int], list[TemplateRow]] = {}
    for entry in rows:
        grouped.setdefault((entry.week, entry.slot), []).append(entry)

    lift_order = {lift: index for index, lift in enumerate(enums.LIFT_CODES)}
    dated: list[tuple[dt.date, int, int, list[TemplateRow]]] = []
    for (week, slot), group in grouped.items():
        offset = DAYS_PER_WEEK * (week - 1) + weekday_map[slot]
        dated.append((start_date + dt.timedelta(days=offset), week, slot, group))

    draft_sessions: list[DraftSession] = []
    # Sorted by date, so `seq` is the order the athlete will actually train in.
    # Slot breaks a same-date tie, which the weekday map's no-duplicates rule
    # already makes unreachable -- it is here so the sort is total regardless.
    for seq, (date, week, slot, group) in enumerate(sorted(dated, key=lambda item: item[:3]), 1):
        group.sort(key=lambda entry: (lift_order[entry.lift_code], entry.variation or ""))
        draft_sessions.append(
            DraftSession(
                week=week,
                slot=slot,
                seq=seq,
                date=date,
                lifts=tuple(
                    _draft_lift(entry, one_rms[entry.lift_code], athlete) for entry in group
                ),
            )
        )

    return BlockDraft(
        athlete_id=athlete.id,
        template_id=template.id,
        template_weeks=template.weeks,
        start_date=start_date,
        lifts=tuple(chosen),
        # Only the chosen lifts: a max that did not shape this block has no
        # business being recorded as the baseline it will be measured against.
        one_rm_snapshot={lift: one_rms[lift] for lift in chosen},
        sessions=tuple(draft_sessions),
    )


def _draft_lift(entry: TemplateRow, one_rm: Decimal, athlete: models.Athlete) -> DraftLift:
    """One template row, loaded against one max."""
    pct = entry_pct(entry)
    return DraftLift(
        lift_code=entry.lift_code,
        variation=entry.variation,
        sets=entry.sets,
        reps=entry.reps,
        # The authored pair travels through unreduced, so the athlete can still
        # be shown the `70/120` they wrote.
        pct_num=entry.pct_num,
        pct_den=entry.pct_den,
        weight_kg=round_weight(
            Fraction(one_rm) * pct, athlete.plate_increment_kg, athlete.rounding
        ),
        rel_intensity=rep_max.rel_intensity(pct, entry.reps),
        is_test=entry.is_test,
    )


# --- Persistence ----------------------------------------------------------


def persist_block(
    session: Session,
    draft: BlockDraft,
    status: enums.BlockStatus = "draft",
) -> models.Block:
    """Write a draft as `block`, `planned_session` and `planned_lift` rows.

    Flushes but never commits, matching `importer.persist_template`: the caller
    owns the transaction, so the whole block lands or none of it does, and a
    test can roll it back.

    Note spec 06 persists a block at proposal time but its sessions only at
    approval (`approve_proposal` is the single write path, CLAUDE.md
    non-negotiable #2). Splitting this to match is spec 06's call to make, not
    work to pull forward here.
    """
    # `.scalar` rather than `.exec`: the latter hands back a Row for a plain
    # SQLAlchemy select, and a Row does not add to an int.
    previous = session.scalar(
        sa.select(sa.func.max(models.Block.number)).where(
            models.Block.athlete_id == draft.athlete_id
        )
    )

    block = models.Block(
        athlete_id=draft.athlete_id,
        template_id=draft.template_id,
        number=(previous or 0) + 1,
        start_date=draft.start_date,
        status=status,
        lifts=list(draft.lifts),
        # JSONB cannot hold a Decimal, and a float would defeat the whole
        # point of the exact arithmetic above. Strings round-trip exactly.
        one_rm_snapshot={lift: str(kg) for lift, kg in draft.one_rm_snapshot.items()},
    )
    session.add(block)
    session.flush()

    for draft_session in draft.sessions:
        planned = models.PlannedSession(
            block_id=block.id,
            week=draft_session.week,
            slot=draft_session.slot,
            seq=draft_session.seq,
            date=draft_session.date,
            # Equal on creation; a reschedule moves `date` and leaves this, so
            # the original plan stays visible.
            original_date=draft_session.date,
        )
        session.add(planned)
        session.flush()

        for lift in draft_session.lifts:
            session.add(
                models.PlannedLift(
                    session_id=planned.id,
                    lift_code=lift.lift_code,
                    variation=lift.variation,
                    sets=lift.sets,
                    reps=lift.reps,
                    pct_num=lift.pct_num,
                    pct_den=lift.pct_den,
                    weight_kg=lift.weight_kg,
                    rel_intensity=lift.rel_intensity,
                    is_test=lift.is_test,
                )
            )
    session.flush()
    return block


# --- Rolling into the next block -----------------------------------------


def next_block_one_rms(
    block: BlockDraft,
    logs: Sequence[LoggedSession],
) -> dict[str, NewOneRm]:
    """Each lift's 1RM for the next block, and where that number came from.

    * **A lift with a test**, made: the single was logged at or above the
      prescribed weight, so the new max is **what they actually lifted** --
      going heavier than planned counts in full. `source = "tested"`.
    * **A lift with a test**, missed or failed: the old max carries over
      unchanged, `source = None`. The documented rule, and the safe direction:
      a test that did not happen is not evidence of anything.
    * **A lift with no test** (`row`): the best `estimate_1rm` from its logged
      sets in the last `ESTIMATE_WINDOW_WEEKS` template weeks, but only if that
      beats the old max. `source = "estimated"`.

    Pure, and no number here was invented: every raised max is a weight the
    athlete logged, dated by the day they logged it.
    """
    by_seq = {log.seq: log for log in logs}
    cutoff = block.template_weeks - ESTIMATE_WINDOW_WEEKS

    results: dict[str, NewOneRm] = {}
    for lift in block.lifts:
        old = block.one_rm_snapshot[lift]
        tested = _tested_max(block, by_seq, lift)
        if tested is not None:
            results[lift] = tested
            continue
        if _has_test(block, lift):
            # Prescribed but not made. Keep the old max rather than falling
            # back on an estimate: spec 08 says a missed test keeps it, and a
            # failed attempt is evidence against a rise, not for one.
            results[lift] = NewOneRm(lift_code=lift, kg=old)
            continue
        results[lift] = _estimated_max(block, by_seq, lift, old, cutoff)
    return results


def _has_test(block: BlockDraft, lift: str) -> bool:
    return any(
        planned.lift_code == lift and planned.is_test
        for session in block.sessions
        for planned in session.lifts
    )


def _tested_max(
    block: BlockDraft,
    by_seq: Mapping[int, LoggedSession],
    lift: str,
) -> NewOneRm | None:
    """The new max from a completed test single, or None if there was not one."""
    for session in block.sessions:
        for planned in session.lifts:
            if planned.lift_code != lift or not planned.is_test:
                continue
            log = by_seq.get(session.seq)
            if log is None:
                continue
            for done in log.entries:
                if done.lift_code != lift:
                    continue
                if done.sets_done < 1 or _max_reps(done.reps_done) < 1:
                    continue
                if done.weight_kg < planned.weight_kg:
                    # Attempted and missed the prescribed weight.
                    continue
                return NewOneRm(
                    lift_code=lift,
                    kg=done.weight_kg,
                    source="tested",
                    set_on=log.performed_on,
                )
    return None


def _estimated_max(
    block: BlockDraft,
    by_seq: Mapping[int, LoggedSession],
    lift: str,
    old: Decimal,
    cutoff: int,
) -> NewOneRm:
    """The best estimate from recent logged sets, if it beats the old max."""
    best: Decimal | None = None
    best_date: dt.date | None = None
    for session in block.sessions:
        if session.week <= cutoff:
            continue
        log = by_seq.get(session.seq)
        if log is None:
            continue
        for done in log.entries:
            if done.lift_code != lift or done.sets_done < 1:
                continue
            for reps in _rep_values(done.reps_done):
                if not 1 <= reps <= rep_max.MAX_ESTIMATE_REPS:
                    # A 20-rep back-off set says nothing useful about a max.
                    continue
                estimate = rep_max.estimate_1rm(done.weight_kg, reps)
                if best is None or estimate > best:
                    best, best_date = estimate, log.performed_on

    if best is None or best <= old:
        return NewOneRm(lift_code=lift, kg=old)
    return NewOneRm(lift_code=lift, kg=best, source="estimated", set_on=best_date)


def _rep_values(reps_done: int | tuple[int, ...]) -> tuple[int, ...]:
    """Per-set reps as a tuple, whether logged as one number or several."""
    return (reps_done,) if isinstance(reps_done, int) else tuple(reps_done)


def _max_reps(reps_done: int | tuple[int, ...]) -> int:
    values = _rep_values(reps_done)
    return max(values) if values else 0
