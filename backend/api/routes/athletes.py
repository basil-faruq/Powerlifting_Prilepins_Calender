"""The athlete, their maxes and their training days.

Everything the planner needs before it can plan anything: a 1RM per lift the
athlete intends to train, and a weekday for each slot.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Query, status

from api.deps import SessionDep
from api.schemas import (
    AthleteCreate,
    AthleteDetail,
    AthleteOut,
    AthletePatch,
    OneRepMaxIn,
    OneRepMaxOut,
    WeekdayMapIn,
    WeekdayMapOut,
    WeekdaySlotOut,
)
from domains.strength import models, services

router = APIRouter(prefix="/athletes", tags=["athletes"])


@router.post("", response_model=AthleteOut, status_code=status.HTTP_201_CREATED)
def create_athlete(body: AthleteCreate, session: SessionDep) -> AthleteOut:
    """Create the athlete, or hand back the one that exists.

    Get-or-create rather than a plain insert: single-athlete is a recorded
    decision (CLAUDE.md), so a second POST is far more likely to be spec 17's
    setup wizard being re-run than a request for a second person. Idempotent is
    what makes that wizard safe to re-enter.
    """
    existing = session.scalar(sa.select(models.Athlete).order_by(models.Athlete.created_at))
    if existing is not None:
        return AthleteOut.of(existing)

    fields: dict[str, object] = {"name": body.name}
    if body.plate_increment_kg is not None:
        fields["plate_increment_kg"] = body.plate_increment_kg
    if body.rounding is not None:
        fields["rounding"] = body.rounding
    athlete = models.Athlete(**fields)
    session.add(athlete)
    session.commit()
    session.refresh(athlete)
    return AthleteOut.of(athlete)


@router.get("/{athlete_id}", response_model=AthleteDetail)
def read_athlete(athlete_id: uuid.UUID, session: SessionDep) -> AthleteDetail:
    athlete = services.get_athlete(session, athlete_id)
    maxes = services.current_one_rms(session, athlete_id)
    rows = sorted(
        (OneRepMaxOut.of(row) for row in maxes.values()),
        key=lambda row: row.lift_code,
    )
    return AthleteDetail(
        **AthleteOut.of(athlete).model_dump(),
        one_rep_maxes=rows,
        weekday_map=[WeekdaySlotOut.of(row) for row in services.weekday_rows(session, athlete_id)],
        has_synthetic_one_rms=any(row.is_synthetic for row in rows),
    )


@router.patch("/{athlete_id}", response_model=AthleteOut)
def patch_athlete(athlete_id: uuid.UUID, body: AthletePatch, session: SessionDep) -> AthleteOut:
    """Change the athlete's loading preferences.

    This does **not** re-round the weights of a block already planned: those
    are stored rows, and spec 06 has no path that recomputes them. Changing the
    increment mid-block therefore leaves a plan the validator will flag as not
    loadable, which is the honest outcome -- the alternative is silently
    rewriting a schedule the athlete already approved.
    """
    athlete = services.get_athlete(session, athlete_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(athlete, field, value)
    session.add(athlete)
    session.commit()
    session.refresh(athlete)
    return AthleteOut.of(athlete)


@router.put("/{athlete_id}/one-rep-maxes", response_model=list[OneRepMaxOut])
def put_one_rep_maxes(
    athlete_id: uuid.UUID,
    body: list[OneRepMaxIn],
    session: SessionDep,
) -> list[OneRepMaxOut]:
    """Record measured maxes, and return what is current afterwards.

    `is_synthetic` is not in the request model at all, so no request can flag a
    row as development data -- that stays `scripts/seed_dev_one_rms.py`'s alone
    (ADR-0004 rule 3). Nor can `source` be `synthetic`: the literal does not
    include it, so it is unrepresentable rather than merely rejected.
    """
    maxes = services.record_one_rep_maxes(session, athlete_id, body)
    session.commit()
    return sorted(
        (OneRepMaxOut.of(row) for row in maxes.values()),
        key=lambda row: row.lift_code,
    )


@router.get("/{athlete_id}/one-rep-maxes/history", response_model=list[OneRepMaxOut])
def one_rep_max_history(
    athlete_id: uuid.UUID,
    session: SessionDep,
    lift: str | None = Query(default=None, description="Restrict to one lift code"),
) -> list[OneRepMaxOut]:
    """Every recorded max, newest first."""
    services.get_athlete(session, athlete_id)
    query = sa.select(models.OneRepMax).where(models.OneRepMax.athlete_id == athlete_id)
    if lift is not None:
        query = query.where(models.OneRepMax.lift_code == lift)
    rows = session.scalars(
        query.order_by(models.OneRepMax.set_on.desc(), models.OneRepMax.created_at.desc())
    )
    return [OneRepMaxOut.of(row) for row in rows]


@router.put("/{athlete_id}/weekday-map", response_model=WeekdayMapOut)
def put_weekday_map(
    athlete_id: uuid.UUID,
    body: WeekdayMapIn,
    session: SessionDep,
    dry_run: bool = Query(default=False, description="Check the map without saving it"),
) -> WeekdayMapOut:
    """Pin each slot to a weekday, 0 = Monday.

    A dry run answers with the problems and writes nothing, so spec 17's wizard
    can show them as the athlete picks days. A real PUT with problems is a 422
    instead, and one while a block is running is a 409.
    """
    if dry_run:
        services.get_athlete(session, athlete_id)
        problems = services.check_weekday_slots(
            session, body.slots, template_id=body.template_id, lifts=body.lifts
        )
        return WeekdayMapOut(
            slots=[
                WeekdaySlotOut(slot=slot, weekday=weekday, locked_from=None)
                for slot, weekday in sorted(body.slots.items())
            ],
            problems=problems,
        )

    rows = services.set_weekday_map(
        session,
        athlete_id,
        body.slots,
        template_id=body.template_id,
        lifts=body.lifts,
    )
    session.commit()
    return WeekdayMapOut(slots=[WeekdaySlotOut.of(row) for row in rows], problems=[])
