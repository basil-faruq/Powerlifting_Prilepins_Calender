"""`today_at` is how the athlete's day boundary reaches a Temporal activity.

An activity may not read the clock (spec 15 §10). It can be retried, and a
second attempt that lands after midnight would compute a different `today` --
and so a different verdict, because ADR-0006 says the validator's answer
depends on it. The workflow reads `workflow.now()` once, which replays the
same forever, and hands that reading to every activity.

What must not get lost in the move is the part `today_for` already got right:
the day boundary belongs to the athlete, not to the server. These tests pin
both halves.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from domains.strength import models, services


def _athlete(timezone: str) -> models.Athlete:
    return models.Athlete(name="probe", timezone=timezone)


def test_the_date_is_the_athletes_own_not_the_servers() -> None:
    """22:30 UTC is already tomorrow in Berlin, and still today in New York.

    This is the whole reason neither function calls `date.today()`.
    """
    instant = dt.datetime(2026, 10, 18, 22, 30, tzinfo=dt.UTC)

    assert services.today_at(_athlete("Europe/Berlin"), instant) == dt.date(2026, 10, 19)
    assert services.today_at(_athlete("America/New_York"), instant) == dt.date(2026, 10, 18)


def test_a_reading_from_any_zone_lands_on_the_same_date() -> None:
    """`workflow.now()` is UTC, but the function must not depend on that."""
    berlin = ZoneInfo("Europe/Berlin")
    same_instant_elsewhere = dt.datetime(2026, 10, 18, 22, 30, tzinfo=dt.UTC).astimezone(
        ZoneInfo("Asia/Tokyo")
    )

    assert (
        services.today_at(_athlete("Europe/Berlin"), same_instant_elsewhere)
        == dt.datetime(2026, 10, 18, 22, 30, tzinfo=dt.UTC).astimezone(berlin).date()
    )


def test_the_same_reading_always_gives_the_same_date() -> None:
    """The retry property. Two attempts, one `as_of`, one answer.

    `today_for` cannot promise this -- it reads the clock, so a retry near
    midnight gets a different day -- which is why the activities use this
    instead.
    """
    athlete = _athlete("Europe/Berlin")
    as_of = dt.datetime(2026, 10, 18, 23, 59, 59, tzinfo=dt.UTC)

    assert services.today_at(athlete, as_of) == services.today_at(athlete, as_of)


def test_it_agrees_with_today_for_when_the_reading_is_now() -> None:
    """The two must not disagree: chat uses one and the workflow the other,
    and a reschedule proposed through either has to judge the same day."""
    athlete = _athlete("Europe/Berlin")

    assert services.today_at(athlete, dt.datetime.now(dt.UTC)) == services.today_for(athlete)
