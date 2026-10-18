"""The eval fixture and the scoring, without calling a model.

The eval itself needs a real model and so cannot run in CI. What can be checked
here is everything around it: that the fixture parses, that it meets spec 13
§5E's shape, that every label is a route the model may actually choose, and
that the confusion table counts correctly -- because a scoring bug would make
every future measurement wrong in a way nobody would notice.
"""

from __future__ import annotations

import yaml

from scripts import eval_router

MIN_CASES = 30
MIN_PER_ROUTE = 6


def test_the_fixture_parses_with_safe_load():
    """`safe_load`, never `load`: the fixture is data."""
    raw = yaml.safe_load(eval_router.CASES.read_text(encoding="utf-8"))

    assert isinstance(raw, dict)
    assert isinstance(raw["cases"], list)


def test_there_are_at_least_thirty_cases():
    """Spec 13 §5E."""
    cases = eval_router.load_cases(eval_router.CASES)

    assert len(cases) >= MIN_CASES


def test_there_are_at_least_six_cases_per_route():
    """Spec 13 §5E. Thinner than six makes a route's own accuracy a coin flip."""
    cases = eval_router.load_cases(eval_router.CASES)

    for route in eval_router.ROUTES:
        matching = [case for case in cases if case.expected == route]
        assert len(matching) >= MIN_PER_ROUTE, f"{route} has only {len(matching)}"


def test_every_label_is_a_route_the_model_may_choose():
    """`safety` and `clarify` are layer 1's, and `RouterChoice` cannot express
    them -- so a case labelled either would be unscoreable."""
    from domains.strength.agent.schemas import RouterChoice

    cases = eval_router.load_cases(eval_router.CASES)
    allowed = set(RouterChoice.model_fields["kind"].annotation.__args__)

    assert {case.expected for case in cases} <= allowed


def test_a_bad_label_is_refused(tmp_path):
    """Guards the loader: a typo'd label must fail loudly, not score as a miss."""
    import pytest

    path = tmp_path / "bad.yaml"
    path.write_text("cases:\n  - message: hi\n    expected: safety\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot choose"):
        eval_router.load_cases(path)


def test_the_fixture_includes_german_cases():
    """Spec 13 §5E: the athlete writes German at an English agent."""
    cases = eval_router.load_cases(eval_router.CASES)
    german_words = ("ich", "mach", "was passiert", "woche", "montag", "freitag")

    german = [case for case in cases if any(word in case.message.lower() for word in german_words)]

    assert len(german) >= 4


def test_the_fixture_includes_relative_dates():
    """The thing the router is forbidden to compute, so the thing most worth
    measuring."""
    cases = eval_router.load_cases(eval_router.CASES)
    relative = ("tomorrow", "next ", "last week", "today", "letzte woche")

    matching = [case for case in cases if any(word in case.message.lower() for word in relative)]

    assert len(matching) >= 4


def test_no_case_would_fire_a_layer_one_filter():
    """A pain message never reaches the model, so scoring one measures nothing.

    This is the check that keeps the fixture honest as it grows: adding "my
    knee hurts on squats" would silently become a permanent miss, because the
    router would return `safety` and the fixture cannot label that.
    """
    from domains.strength import notes

    cases = eval_router.load_cases(eval_router.CASES)
    would_filter = [case.message for case in cases if notes.is_pain_text(case.message)]

    assert would_filter == []


# --- The scoring --------------------------------------------------------


def test_the_confusion_table_counts_correctly():
    """A scoring bug would make every future measurement wrong invisibly."""
    cases = [
        eval_router.Case(message="a", expected="plan"),
        eval_router.Case(message="b", expected="plan"),
        eval_router.Case(message="c", expected="simulate"),
        eval_router.Case(message="d", expected="answer"),
    ]
    results = [
        (cases[0], "plan"),
        (cases[1], "reschedule"),
        (cases[2], "simulate"),
        (cases[3], None),
    ]

    table = eval_router.confusion(results)
    rows = {line.split()[0]: line.split()[1:] for line in table.splitlines()[2:]}

    # Columns are answer, plan, reschedule, simulate, "-".
    assert rows["plan"] == ["0", "1", "1", "0", "0"]
    assert rows["simulate"] == ["0", "0", "0", "1", "0"]
    assert rows["answer"] == ["0", "0", "0", "0", "1"]
    assert rows["reschedule"] == ["0", "0", "0", "0", "0"]


def test_the_table_has_a_row_for_every_route():
    results = [(eval_router.Case(message="a", expected="plan"), "plan")]

    table = eval_router.confusion(results)

    for route in eval_router.ROUTES:
        assert route in table


def test_an_unusable_output_is_its_own_column():
    """Spec 13 §11's two-strikes failure is not a wrong route; it is no route,
    and lumping the two together would hide a schema problem as a routing one."""
    results = [(eval_router.Case(message="a", expected="plan"), None)]

    table = eval_router.confusion(results)

    assert table.splitlines()[0].rstrip().endswith("-")


def test_the_fixed_context_is_a_monday():
    """The template's week grid starts on a Monday, and the prompt's date table
    is built from this -- so a mid-week value would make "next Monday" mean
    something different in the eval than in the app."""
    assert eval_router.TODAY.weekday() == 0
    assert eval_router.CONTEXT.today == eval_router.TODAY
    assert eval_router.CONTEXT.has_block is True


def test_the_eval_runs_no_model_at_import():
    """Importing the script must not construct a model or touch a network.

    `tests/conftest.py` pins `ALLOW_MODEL_REQUESTS = False`, so this passing at
    all is part of the guarantee.
    """
    import importlib

    importlib.reload(eval_router)

    assert eval_router.ROUTES == ("answer", "plan", "reschedule", "simulate")
