"""Spec 10, as amended: the Monte-Carlo simulator and its calibration script.

Pure and offline. The block is built in memory from the committed golden
fixture, exactly as `test_missed_policy` does, and the policy it re-plans
through is the real one.

The maxes (122.5/87.5/162.5/52.5/72.5) belong to this file alone -- not the
spreadsheet's example maxes, not the policy suite's, not the API suite's -- so a
weight in a failure message can be traced here (CLAUDE.md guardrail).

Statistical assertions use wide margins and fixed seeds. They check *direction*
(a long illness lowers the chance of a test; a calibrated gain moves the max)
rather than a particular probability, because the constants behind the numbers
are starting values and a test that pinned them would fail whenever one was
tuned.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from domains.strength import generator, simulator
from domains.strength.schemas import BlockState
from domains.strength.simulator import (
    ForcedEvent,
    GainQuantiles,
    Gains,
    ScenarioError,
    SimConfig,
    simulate,
)
from domains.strength.validator import ValidationContext, read_test_weeks
from scripts import calibrate_simulator
from tests import golden

SIM_MAXES = {
    "squat": Decimal("122.5"),
    "bench": Decimal("87.5"),
    "deadlift": Decimal("162.5"),
    "press": Decimal("52.5"),
    "row": Decimal("72.5"),
}
TESTED = ("squat", "bench", "deadlift", "press")

# Nothing random: every run is the same future, so what differs between two
# results is down to the scenario and the test draws alone.
CALM = SimConfig(runs=200, illness_mean_interval_days=float("inf"), travel_prob_per_week=0.0)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def draft(golden_entries):
    return generator.generate_block(
        golden.athlete("5.00", "nearest"),
        golden.template(),
        golden_entries,
        SIM_MAXES,
        sorted(SIM_MAXES),
        golden.START,
        golden.MON_WED_FRI,
    )


@pytest.fixture(scope="module")
def entries(golden_entries):
    return golden_entries


def state_of(draft, *, done=(), status="active") -> BlockState:
    status_of = {s.seq: "planned" for s in draft.sessions}
    status_of.update({seq: "done" for seq in done})
    return BlockState(
        block_id=uuid.uuid4(),
        athlete_id=draft.athlete_id,
        template_id=draft.template_id,
        status=status,
        start_date=draft.start_date,
        template_weeks=draft.template_weeks,
        lifts=draft.lifts,
        one_rm_snapshot=draft.one_rm_snapshot,
        weekday_map=golden.MON_WED_FRI,
        plan=draft.sessions,
        status_of=status_of,
    )


def ctx_of(entries, today=golden.START) -> ValidationContext:
    return ValidationContext(
        weekday_map=golden.MON_WED_FRI,
        overreach_from_week=7,
        test_week=read_test_weeks(entries),
        plate_increment_kg=Decimal("5.00"),
        today=today,
    )


def comparable(result):
    """Everything but the one field two identical runs may disagree on."""
    dump = result.model_dump()
    dump["stats"].pop("wall_time_s")
    return dump


def week6_illness(days=10):
    return (ForcedEvent(kind="illness", start_week=6, days=days),)


# --- 1. The behaviours spec 10 §12 names ------------------------------------


def test_with_no_disruptions_every_run_finishes_on_the_planned_date(draft, entries):
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, seed=1)

    assert result.finish_date is not None
    assert result.finish_date.p10 == result.finish_date.p50 == result.finish_date.p90
    assert result.finish_date.p50 == result.planned_end_date
    assert result.p_restart == 0.0
    assert result.p_unplannable == 0.0
    assert result.missed_sessions.p90 == 0


def test_with_no_disruptions_a_test_single_is_usually_made(draft, entries):
    """Spec 10 §12: `p_test_made` high for every lift."""
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, seed=1)

    for lift in TESTED:
        assert result.per_lift[lift].test == "pending"
        assert result.per_lift[lift].p_test_made >= 0.7, lift


def test_row_is_never_tested_so_its_max_cannot_move(draft, entries):
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, seed=1)

    row = result.per_lift["row"]
    assert row.test == "none"
    assert row.p_test_made == 0.0
    assert row.new_1rm.p10 == row.new_1rm.p50 == row.new_1rm.p90 == SIM_MAXES["row"]
    assert any("row is never tested" in line for line in result.assumptions)


def test_a_ten_day_illness_in_week_six_delays_the_finish(draft, entries):
    calm = simulate(state_of(draft), ctx_of(entries), cfg=CALM, seed=1)
    ill = simulate(state_of(draft), ctx_of(entries), cfg=CALM, scenario=week6_illness(), seed=1)

    assert ill.finish_date.p50 > calm.finish_date.p50
    assert ill.missed_sessions.p50 > 0


def test_a_ten_day_illness_in_week_six_lowers_the_chance_of_a_test(draft, entries):
    calm = simulate(state_of(draft), ctx_of(entries), cfg=CALM, seed=1)
    ill = simulate(state_of(draft), ctx_of(entries), cfg=CALM, scenario=week6_illness(), seed=1)

    for lift in TESTED:
        assert ill.per_lift[lift].p_test_made <= calm.per_lift[lift].p_test_made, lift
    assert ill.per_lift["squat"].p_test_made < calm.per_lift["squat"].p_test_made - 0.1


def test_the_same_seed_gives_the_same_result(draft, entries):
    cfg = SimConfig(runs=60)
    one = simulate(state_of(draft), ctx_of(entries), cfg=cfg, seed=7)
    two = simulate(state_of(draft), ctx_of(entries), cfg=cfg, seed=7)

    assert comparable(one) == comparable(two)


def test_a_different_seed_gives_a_different_result(draft, entries):
    """Otherwise the seed is decoration and the previous test proves nothing."""
    cfg = SimConfig(runs=60)
    one = simulate(state_of(draft), ctx_of(entries), cfg=cfg, seed=7)
    two = simulate(state_of(draft), ctx_of(entries), cfg=cfg, seed=8)

    assert comparable(one) != comparable(two)


# --- 2. Scenario injection --------------------------------------------------


def test_a_forced_event_may_name_a_week_or_a_date_and_means_the_same_thing(draft, entries):
    sixth_monday = golden.START + dt.timedelta(days=35)
    by_week = simulate(state_of(draft), ctx_of(entries), cfg=CALM, scenario=week6_illness(), seed=3)
    by_date = simulate(
        state_of(draft),
        ctx_of(entries),
        cfg=CALM,
        scenario=(ForcedEvent(kind="illness", start_date=sixth_monday, days=10),),
        seed=3,
    )

    assert by_week.finish_date == by_date.finish_date
    assert by_week.per_lift == by_date.per_lift


def test_a_forced_event_needs_exactly_one_start():
    with pytest.raises(ValueError, match="exactly one"):
        ForcedEvent(kind="illness", days=3)
    with pytest.raises(ValueError, match="exactly one"):
        ForcedEvent(kind="illness", start_week=2, start_date=dt.date(2026, 1, 1), days=3)


def test_a_forced_event_past_the_end_of_the_block_is_refused(draft, entries):
    with pytest.raises(ScenarioError, match="past the block's 11 weeks"):
        simulate(
            state_of(draft),
            ctx_of(entries),
            cfg=CALM,
            scenario=(ForcedEvent(kind="illness", start_week=12, days=3),),
        )


def test_a_forced_event_that_is_already_over_is_refused(draft, entries):
    """A window that closed before the simulation starts changes nothing, so
    accepting it silently would let a typo read as a reassuring answer."""
    with pytest.raises(ScenarioError, match="already over"):
        simulate(
            state_of(draft),
            ctx_of(entries, today=dt.date(2026, 3, 1)),
            cfg=CALM,
            scenario=(ForcedEvent(kind="travel", start_date=dt.date(2026, 1, 12), days=3),),
        )


def test_every_forced_event_is_listed_in_the_assumptions(draft, entries):
    result = simulate(
        state_of(draft),
        ctx_of(entries),
        cfg=SimConfig(runs=5, illness_mean_interval_days=float("inf"), travel_prob_per_week=0.0),
        scenario=week6_illness(),
    )
    assert any("Forced in every run: illness, 10 days from week 6" in a for a in result.assumptions)


# --- 3. The restart tier ends a run, and says so ---------------------------


def test_three_empty_weeks_end_every_run_in_a_restart(draft, entries):
    """21 days from week 4's Monday leaves weeks 4-6 empty: a restart, which a
    pure simulation cannot build, so it is where the run stops."""
    result = simulate(
        state_of(draft),
        ctx_of(entries),
        cfg=SimConfig(runs=20, illness_mean_interval_days=float("inf"), travel_prob_per_week=0.0),
        scenario=(ForcedEvent(kind="illness", start_week=4, days=21),),
        seed=1,
    )

    assert result.p_restart == 1.0
    assert result.finish_date is None
    for lift in TESTED:
        assert result.per_lift[lift].p_test_made == 0.0
        assert result.per_lift[lift].new_1rm.p50 == SIM_MAXES[lift]
    assert any("ended in a restart" in line for line in result.assumptions)


# --- 4. Where the block already is -----------------------------------------


def test_a_block_part_way_through_simulates_from_today(draft, entries):
    done = range(1, 13)  # weeks 1-4
    today = next(s.date for s in draft.sessions if s.seq == 13)
    result = simulate(state_of(draft, done=done), ctx_of(entries, today), cfg=CALM, seed=1)

    assert result.finish_date.p50 == result.planned_end_date
    assert result.per_lift["squat"].test == "pending"


def test_a_lift_whose_test_is_behind_you_is_not_resampled(draft, entries):
    """Press tests in week 9. With everything but the last week done, that test
    is history: there is nothing left to sample, and saying 0% would be a lie."""
    last_week = {s.seq for s in draft.sessions if s.week == 11}
    done = [s.seq for s in draft.sessions if s.seq not in last_week]
    today = min(s.date for s in draft.sessions if s.seq in last_week)
    result = simulate(state_of(draft, done=done), ctx_of(entries, today), cfg=CALM, seed=1)

    assert result.per_lift["press"].test == "behind"
    assert result.per_lift["press"].p_test_made is None
    assert result.per_lift["press"].new_1rm.p50 == SIM_MAXES["press"]
    assert result.per_lift["squat"].test == "pending"


def test_sessions_dated_in_the_past_and_not_done_are_made_up_through_the_policy(draft, entries):
    """Planned, past-dated sessions are the ones the athlete means to make up.
    They are missed on day one and re-planned, not silently performed."""
    done = range(1, 7)
    today = next(s.date for s in draft.sessions if s.seq == 9)
    result = simulate(state_of(draft, done=done), ctx_of(entries, today), cfg=CALM, seed=1)

    assert result.missed_sessions.p50 >= 2
    # Made up through a week shift, so the block ends later -- not silently
    # performed on the day they were found overdue.
    assert result.finish_date is not None
    assert result.finish_date.p50 > result.planned_end_date


def test_a_block_that_is_not_running_is_refused(draft, entries):
    with pytest.raises(ValueError, match="not running"):
        simulate(state_of(draft, status="done"), ctx_of(entries), cfg=CALM)


def test_a_paused_block_is_simulated(draft, entries):
    """ADR-0010: paused counts as running, and 'what if I resume' is the point."""
    result = simulate(state_of(draft, status="paused"), ctx_of(entries), cfg=CALM, seed=1)
    assert result.finish_date is not None


def test_a_stale_session_left_behind_by_a_fold_does_not_crash_the_next_window(draft, entries):
    """Out-of-order logging leaves 9 undone with 10-12 done and 13-15 past-dated.
    The fold handles only the earliest, so the rest used to stay pending with a
    past date, fall outside the next window, and build an event naming no
    session -- a `ValidationError`, which the route turned into a 500."""
    done = [*range(1, 9), 10, 11, 12]
    today = dt.date(2026, 2, 7)
    result = simulate(
        state_of(draft, done=done),
        ctx_of(entries, today),
        cfg=CALM.model_copy(update={"runs": 5}),
        scenario=(ForcedEvent(kind="travel", start_date=today, days=1),),
        seed=1,
    )

    assert result.stats.runs_unplannable == 0
    assert result.finish_date is not None


def test_stale_sessions_are_never_all_performed_on_one_day(draft, entries):
    """The silent variant of the same defect: with no scenario at all, three
    past-dated sessions were 'performed' together on day zero."""
    done = [*range(1, 9), 10, 11, 12]
    result = simulate(
        state_of(draft, done=done),
        ctx_of(entries, dt.date(2026, 2, 7)),
        cfg=CALM.model_copy(update={"runs": 5}),
        seed=1,
    )

    assert result.missed_sessions.p50 >= 4
    assert result.finish_date.p50 > result.planned_end_date


def test_a_negative_seed_is_refused(draft, entries):
    with pytest.raises(ValueError, match="seed"):
        simulate(state_of(draft), ctx_of(entries), cfg=CALM, seed=-1)


def test_an_absence_that_keeps_being_extended_ends_the_run_instead_of_hanging(draft, entries):
    """A fortnight-long trip every single week never lets the athlete back.
    The recheck is bounded, and the run is reported as unplannable."""
    cfg = SimConfig(
        runs=3,
        illness_mean_interval_days=float("inf"),
        travel_prob_per_week=1.0,
        travel_days_range=(14, 14),
    )
    result = simulate(state_of(draft), ctx_of(entries), cfg=cfg, seed=1)

    assert result.stats.runs_unplannable == 3
    assert result.p_unplannable == 1.0
    assert any("could not be re-planned" in line for line in result.assumptions)


# --- 5. Gains and their provenance -----------------------------------------


def flat_gains(value: float) -> Gains:
    entry = GainQuantiles(p10=value, p25=value, p50=value, p75=value, p90=value, n=100)
    return Gains(
        source="a synthetic test distribution",
        generated_on=dt.date(2026, 1, 1),
        lifts={lift: entry for lift in SIM_MAXES},
    )


def test_without_a_calibration_file_the_flat_fallback_is_used_and_said(draft, entries):
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, gains=None, seed=1)

    assert any("No calibration file" in line and "2%" in line for line in result.assumptions)
    low, high = SIM_MAXES["squat"] * Decimal("1.02"), SIM_MAXES["squat"] * Decimal("1.05")
    assert low - Decimal("0.01") <= result.per_lift["squat"].new_1rm.p50 <= high + Decimal("0.01")


def test_a_calibrated_gain_is_what_moves_the_max(draft, entries):
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, gains=flat_gains(0.20), seed=1)

    assert any("a synthetic test distribution" in line for line in result.assumptions)
    assert result.per_lift["squat"].new_1rm.p90 > SIM_MAXES["squat"] * Decimal("1.15")


def test_a_made_test_never_lowers_a_max_even_if_the_sampled_gain_is_negative(draft, entries):
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, gains=flat_gains(-0.30), seed=1)
    for lift in TESTED:
        assert result.per_lift[lift].new_1rm.p10 >= SIM_MAXES[lift], lift


def test_a_derived_gain_is_flagged_in_the_assumptions(draft, entries):
    gains = flat_gains(0.03)
    derived = gains.lifts["press"].model_copy(update={"derived": True})
    gains = gains.model_copy(update={"lifts": {**gains.lifts, "press": derived}})
    result = simulate(state_of(draft), ctx_of(entries), cfg=CALM, gains=gains, seed=1)
    assert any("press have no meet data" in line for line in result.assumptions)


def test_load_gains_is_none_when_the_file_is_absent(tmp_path):
    assert simulator.load_gains(tmp_path / "nope.json") is None


def test_load_gains_refuses_a_malformed_file_rather_than_falling_back(tmp_path):
    """A broken file silently replaced by the flat range would present a
    placeholder as if it were the athlete's calibration."""
    bad = tmp_path / "gains.json"
    bad.write_text(json.dumps({"source": "x"}), encoding="utf-8")
    with pytest.raises(ValueError):
        simulator.load_gains(bad)


def test_load_gains_round_trips(tmp_path):
    path = tmp_path / "gains.json"
    gains = flat_gains(0.04)
    path.write_text(gains.model_dump_json(), encoding="utf-8")
    assert simulator.load_gains(path) == gains


# --- 6. What every result must carry ---------------------------------------


def test_every_reported_range_is_ordered(draft, entries):
    result = simulate(
        state_of(draft), ctx_of(entries), cfg=SimConfig(runs=80), scenario=week6_illness(), seed=5
    )
    ranges = [result.missed_sessions]
    if result.finish_date is not None:
        ranges.append(result.finish_date)
    ranges += [outlook.new_1rm for outlook in result.per_lift.values()]
    for triple in ranges:
        assert triple.p10 <= triple.p50 <= triple.p90


def test_a_result_is_ranges_and_assumptions_never_a_single_number(draft, entries):
    result = simulate(state_of(draft), ctx_of(entries), cfg=SimConfig(runs=40), seed=5)

    assert result.assumptions
    assert any("not a prediction" in line for line in result.assumptions)
    assert any("starting value" in line for line in result.assumptions)
    assert result.stats.runs == 40
    assert result.stats.seed == 5
    assert (
        result.stats.runs_finished + result.stats.runs_restart + result.stats.runs_unplannable == 40
    )


def test_every_tunable_is_labelled_a_starting_value():
    """CLAUDE.md guardrail: a guess must say it is one, in code."""
    missing = [
        name
        for name, field in SimConfig.model_fields.items()
        if name != "runs" and "Starting value" not in (field.description or "")
    ]
    assert missing == []


def test_the_run_count_is_capped_at_five_thousand():
    SimConfig(runs=5000)
    with pytest.raises(ValueError):
        SimConfig(runs=5001)


def test_nonsense_ranges_are_refused():
    with pytest.raises(ValueError):
        SimConfig(illness_days_range=(10, 3))
    with pytest.raises(ValueError):
        SimConfig(travel_days_range=(0, 4))


# --- 7. Budget and purity ---------------------------------------------------


def test_two_hundred_runs_with_re_planning_fit_comfortably_in_the_budget(draft, entries):
    """Spec 10 §11: 1,000 runs in under 10 s. The full measurement is a manual
    benchmark; this guards the same cost at a fifth of the size, with the
    re-planning path switched on, so a regression shows up in CI."""
    result = simulate(
        state_of(draft),
        ctx_of(entries),
        cfg=SimConfig(runs=200),
        scenario=week6_illness(),
        seed=1,
    )
    assert result.stats.wall_time_s < 10


def test_the_simulator_has_no_database_clock_or_global_randomness():
    """Randomness arrives as a `Generator` spawned from the seed, and nowhere
    else: no `random`, no `numpy.random.<global>`, no clock, no database."""
    tree = ast.parse(Path(simulator.__file__).read_text(encoding="utf-8"))

    banned = {"sqlalchemy", "sqlmodel", "random", "planner_core.db"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {alias.name for alias in node.names} & banned
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in banned
            assert not (node.module == "planner_core" and any(a.name == "db" for a in node.names))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "today", "utcnow"}

    allowed_random = {"SeedSequence", "default_rng", "Generator"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "random"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in {"np", "numpy"}
        ):
            assert node.attr in allowed_random, f"global numpy.random.{node.attr}"


# --- 8. Calibration ---------------------------------------------------------

CALIBRATION_MAXES = {"squat": 145.0, "bench": 100.0, "deadlift": 180.0}


def test_calibration_keeps_only_qualifying_consecutive_meets():
    """The fixture holds one lifter for each way to be excluded: too close
    together, too far apart, wrapped, bench-only, a lift outside the band, a
    failed lift -- and one lifter with three meets, who yields two pairs."""
    frames = calibrate_simulator.gains_between_meets(
        FIXTURES / "meets_sample.csv", CALIBRATION_MAXES
    )

    # Squat loses Fir (300 kg is nowhere near 145); bench and deadlift keep him.
    assert frames["squat"].height == 3
    assert frames["bench"].height == 4
    assert frames["deadlift"].height == 4


def test_calibration_measures_the_relative_change():
    gains = calibrate_simulator.calibrate(
        FIXTURES / "meets_sample.csv", CALIBRATION_MAXES, source="a test fixture"
    )

    assert gains.lifts["squat"].n == 3
    assert gains.lifts["squat"].p50 == pytest.approx(10 / 140)
    assert gains.lifts["bench"].p50 == pytest.approx((0.05 + 5 / 95) / 2)
    assert gains.lifts["deadlift"].p50 == pytest.approx(10 / 180)
    for entry in gains.lifts.values():
        assert entry.p10 <= entry.p25 <= entry.p50 <= entry.p75 <= entry.p90


def test_press_and_row_borrow_a_scaled_bench_and_say_so():
    gains = calibrate_simulator.calibrate(
        FIXTURES / "meets_sample.csv", CALIBRATION_MAXES, source="a test fixture"
    )

    for lift in ("press", "row"):
        entry = gains.lifts[lift]
        assert entry.derived is True
        assert entry.n == 0
        assert entry.p50 == pytest.approx(gains.lifts["bench"].p50 * 0.8)
    assert not any(gains.lifts[lift].derived for lift in CALIBRATION_MAXES)


def test_calibration_with_no_data_for_a_lift_refuses_to_write_a_file():
    with pytest.raises(ValueError, match="no squat pairs"):
        calibrate_simulator.calibrate(
            FIXTURES / "meets_sample.csv", {**CALIBRATION_MAXES, "squat": 500.0}, source="x"
        )


def test_the_script_writes_a_file_the_simulator_can_load(tmp_path):
    out = tmp_path / "gains.json"
    code = calibrate_simulator.main(
        [
            "--csv", str(FIXTURES / "meets_sample.csv"),
            "--out", str(out),
            "--squat", "145", "--bench", "100", "--deadlift", "180",
        ]
    )  # fmt: skip

    assert code == 0
    loaded = simulator.load_gains(out)
    assert loaded is not None
    assert set(loaded.lifts) == {"squat", "bench", "deadlift", "press", "row"}


def test_the_script_refuses_a_missing_csv(tmp_path, capsys):
    code = calibrate_simulator.main(
        [
            "--csv", str(tmp_path / "absent.csv"),
            "--out", str(tmp_path / "gains.json"),
            "--squat", "145", "--bench", "100", "--deadlift", "180",
        ]
    )  # fmt: skip

    assert code == 1
    assert "does not exist" in capsys.readouterr().err
    assert not (tmp_path / "gains.json").exists()


def test_the_script_has_no_default_for_any_max():
    """CLAUDE.md non-negotiable #9: a 1RM comes from the athlete."""
    with pytest.raises(SystemExit):
        calibrate_simulator.main(["--csv", str(FIXTURES / "meets_sample.csv")])
