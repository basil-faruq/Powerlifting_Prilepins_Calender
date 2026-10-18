"""Two to four sentences about a result, with every number checked against it.

Spec 13 §5D, and the enforcement half of CLAUDE.md non-negotiable 1. The model
is given the result as JSON and told to use only numbers present in it. Then
the reply is checked, and if it quotes a number the object does not contain it
is **discarded entirely** and a template built from the object's own fields is
used instead.

Discarded, not repaired. A repair prompt is a retry loop, and a retry loop is
where a 14b model starts inventing differently rather than inventing less. One
model call per explanation.

**The hard part is not the regex, it is the normalisation.** The object is JSON
with `Decimal` weights serialised as strings, intensities as numerator and
denominator pairs, ISO dates, and `week`/`slot`/`seq` integers. A reply saying
"125 kg" about a stored `"125.00"`, or "72.5%" about a stored `0.725`, or
"3/5" about `{"num": 3, "den": 5}`, is quoting the object correctly -- and a
naive string match would reject all three, which means every reply falls back
to the template and the model contributes nothing. The table in `_canonical`
is where that work lives.

**Dates are matched structurally, never lexically.** "Monday" and "2 March" are
both legitimate ways to say a date the object contains, and neither appears in
it as those characters. So a date-shaped token is resolved against the *set* of
dates in the object, by value, by (month, day), or by weekday name.

`week`/`slot`/`seq` numbers are matched only against fields of the same name.
That is what stops "W3 S1" from being satisfied by an unrelated 3 somewhere in
a payload -- and it is also ADR-0006's open item: a `squat+bench` session label
is one session's name, not two lifts, and not a source of numbers.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.models import Model

from domains.strength.agent import prompts
from domains.strength.agent.recorder import records

logger = logging.getLogger(__name__)

Subject = Literal["proposal", "simulation", "policy_failure", "plan_invalid"]

# Keys whose string values are names, not data to mine for numbers or lifts.
# ADR-0006's open item: `squat+bench` is one session's label.
LABEL_KEYS = frozenset({"label", "name", "session_label", "template_name", "rule", "kind"})

# Keys whose integer values are positional, and may only satisfy a token that
# said so -- `W3`, `S1`, `seq:12`.
POSITIONAL_KEYS = {"week": "week", "slot": "slot", "seq": "seq"}

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip

# Spelled-out numbers the check cannot read as digits. "one" to "ten" are left
# alone -- "two things worth noting" is a quantity of language, not of training
# -- but anything bigger is almost always a measurement ("one hundred and forty
# kilos"), and a digit-only tokeniser would let it through unchecked. Each is
# emitted as a `word:` token that no object can contain, so a reply using one
# falls back to the template. Over-rejecting here is the safe direction.
SPELLED_BIG_RE = re.compile(
    r"\b(?:eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)\b",
    re.I,
)

# `1,250` is one thousand two hundred and fifty, not 1.25. A comma followed by
# exactly three digits, after one to three, is a thousands separator.
THOUSANDS_RE = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+)(?![\d,])")

# `3x140`: the second number is a weight, and `\w` before it would hide it.
TIMES_RE = re.compile(r"(?<=\d)\s*[x×]\s*(?=\d)", re.I)

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

# Alternation order is load-bearing: `date` before `bare`, or `2026-03-02`
# yields three separate numbers and none of them matches anything.
NUMBER_RE = re.compile(
    r"""
      (?P<date>\d{4}-\d{2}-\d{2})
    | (?P<label>\b[Ww](?P<w>\d{1,2})\s*[Ss](?P<s>\d{1,2})\b)
    | (?P<weekword>\b[Ww]eeks?\s+(?P<wk>\d{1,2})\b)
    | (?P<seq>\bseq[:\s]?(?P<n>\d{1,3})\b)
    | (?P<pct>(?P<pv>\d{1,3}(?:[.,]\d+)?)\s*%)
    | (?P<frac>\b(?P<num>\d{1,3})\s*/\s*(?P<den>\d{1,3})\b)
    | (?P<bare>(?<![\d.])\d{1,9}(?:[.,]\d{1,6})?(?!\d|\.\d))
    """,
    re.VERBOSE,
)
# Two things about `bare` were found by review, both false negatives.
# The lookahead used to be `(?![\d.])`, which refused a number directly before a
# full stop -- so "Your squat is 150." contained no number at all and passed any
# object. And the lookbehind used to be `(?<![\w.])`, which hid the number in
# "RPE8" and "kg100". Now only a digit or a decimal point blocks it, and the
# magnitude is wide enough that 1000000 and 142.2222 are still seen.

# `1st`, `2nd`: ordinals are structure, not measurement.
ORDINAL_RE = re.compile(r"\b\d{1,2}(st|nd|rd|th)\b", re.I)

# "2 March" / "March 2" / "28th of March" / "March 28th": dates written the way
# a person writes them. The ordinal is part of the match, because stripping it
# first (the original order) left "March 28th" with no date at all.
_MONTH_ALT = "|".join(MONTHS)
DAY_MONTH_RE = re.compile(
    rf"\b(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})"
    rf"|({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?)\b",
    re.I,
)


def _decimal(text: str) -> str | None:
    """A number in one canonical form: no trailing zeros, no thousands commas.

    `72.50` and `72.5` are the same number, and so are `100.0` and `100`. The
    object stores weights as `Decimal`-derived strings, and a reply writes them
    however reads best, so both sides go through here.
    """
    try:
        value = Decimal(text.replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None
    # `Decimal("Infinity")` and `Decimal("NaN")` parse, and `int()` of either
    # raises -- so a stray "inf" string in a payload would have been a 500.
    if not value.is_finite():
        return None
    normalised = value.normalize()
    # `normalize()` gives 1E+2 for 100, and 1E+2 matches nothing a person
    # writes, so an integral value goes back through `int`.
    if normalised == normalised.to_integral_value():
        return str(int(normalised))
    return str(normalised)


def numbers_in(text: str) -> tuple[str, ...]:
    """Every number a reply claims, canonicalised. Order preserved, deduplicated.

    **Nothing is exempt, quoted text included.** The first version dropped any
    double-quoted span so a reply could quote a note verbatim -- which let
    `Top set is "142.5 kg"` through unchecked. The payloads the explainer sees
    contain no notes, so there was never anything to quote; the exemption was
    only ever a hole.

    Digits only, plus the spelled-out numbers above "ten". Words like "one
    hundred and forty" are caught by `SPELLED_BIG_RE`; below that they are
    treated as language.
    """
    stripped = UUID_RE.sub(" ", text)
    stripped = THOUSANDS_RE.sub(lambda m: m.group(1).replace(",", ""), stripped)
    stripped = TIMES_RE.sub(" ", stripped)

    found: list[str] = []

    for match in DAY_MONTH_RE.finditer(stripped):
        day = match.group(1) or match.group(4)
        month = (match.group(2) or match.group(3)).lower()
        found.append(f"monthday:{MONTHS.index(month) + 1}-{int(day)}")
    stripped = DAY_MONTH_RE.sub(" ", stripped)
    # After the dates, so "March 28th" is read as a date first.
    stripped = ORDINAL_RE.sub(" ", stripped)

    found.extend(f"word:{word.lower()}" for word in SPELLED_BIG_RE.findall(stripped))

    for name in WEEKDAY_NAMES:
        if re.search(rf"\b{name}\b", stripped, re.I):
            found.append(f"weekday:{name}")

    for match in NUMBER_RE.finditer(stripped):
        if match.group("date"):
            found.append(f"date:{match.group('date')}")
        elif match.group("label"):
            found.append(f"week:{int(match.group('w'))}")
            found.append(f"slot:{int(match.group('s'))}")
        elif match.group("weekword"):
            # "week 3" in prose is the same claim as `W3`: a position in the
            # block, to be satisfied by a `week` field and by nothing else.
            # Without this the commonest sentence about the current week -- "you
            # are in week 1 of 11" -- produced a bare 1 that no keyed field
            # could ever match, so every such answer fell back to the template.
            found.append(f"week:{int(match.group('wk'))}")
        elif match.group("seq"):
            found.append(f"seq:{int(match.group('n'))}")
        elif match.group("pct"):
            value = _decimal(match.group("pv"))
            if value is not None:
                found.append(f"num:{value}")
                # A reply may say 72.5% for a stored fraction of 0.725.
                as_fraction = _decimal(str(Decimal(value) / 100))
                if as_fraction is not None:
                    found.append(f"num:{as_fraction}")
        elif match.group("frac"):
            found.append(f"frac:{int(match.group('num'))}/{int(match.group('den'))}")
        elif match.group("bare"):
            value = _decimal(match.group("bare"))
            if value is not None:
                found.append(f"num:{value}")

    seen: dict[str, None] = {}
    for token in found:
        seen.setdefault(token, None)
    return tuple(seen)


def _fraction_forms(num: int, den: int) -> list[str]:
    """Every way a reply may legitimately write a stored num/den pair."""
    if den == 0:
        return []
    forms = [f"frac:{num}/{den}"]
    exact = Decimal(num) / Decimal(den)
    for candidate in (
        _decimal(str(exact.quantize(Decimal("0.0001")))),
        _decimal(str(exact)),
        _decimal(str((exact * 100).quantize(Decimal("0.1")))),
        _decimal(str((exact * 100).quantize(Decimal("1")))),
    ):
        if candidate is not None:
            forms.append(f"num:{candidate}")
    return forms


def allowed_numbers(obj: Any) -> frozenset[str]:
    """Every number the object contains, in every form a reply may write it.

    Walks the whole structure. Positional integers (`week`, `slot`, `seq`) are
    emitted *only* in their keyed form, so an unrelated 3 in a payload cannot
    satisfy a claim about week 3.
    """
    allowed: set[str] = set()

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, Mapping):
            for name, value in node.items():
                walk(value, str(name))
            # A num/den pair is one number, and only readable as one together.
            for num_key, den_key in (("num", "den"), ("pct_num", "pct_den")):
                if num_key in node and den_key in node:
                    with contextlib.suppress(TypeError, ValueError):
                        allowed.update(_fraction_forms(int(node[num_key]), int(node[den_key])))
            return

        if isinstance(node, list | tuple | set | frozenset):
            for value in node:
                walk(value, key)
            return

        if isinstance(node, bool) or node is None:
            return

        if key in POSITIONAL_KEYS and isinstance(node, int):
            allowed.add(f"{POSITIONAL_KEYS[key]}:{node}")
            return

        if isinstance(node, int | float | Decimal):
            value = _decimal(str(node))
            if value is not None:
                allowed.add(f"num:{value}")
            return

        if isinstance(node, dt.date):
            _add_date(allowed, node)
            return

        if isinstance(node, str):
            if key in LABEL_KEYS:
                # A label is a name. `squat+bench` is one session, not two
                # lifts, and not a source of numbers (ADR-0006).
                return
            parsed = _parse_date(node)
            if parsed is not None:
                _add_date(allowed, parsed)
                return
            value = _decimal(node)
            if value is not None:
                allowed.add(f"num:{value}")
                return
            # Free text from the solver -- `reasons`, `assumptions`, a
            # violation `message`. Those sentences contain numbers, and they
            # *are* the object's numbers: the solver wrote them. Without this
            # the template fallback -- which quotes `reasons` verbatim -- would
            # fail its own post-check, which is how this was found.
            _mine(allowed, node)

    walk(obj, None)
    return frozenset(allowed)


def _mine(allowed: set[str], text: str) -> None:
    """Add every number a sentence states, with dates in all three forms."""
    for token in numbers_in(text):
        allowed.add(token)
        if token.startswith("date:"):
            parsed = _parse_date(token.removeprefix("date:"))
            if parsed is not None:
                _add_date(allowed, parsed)


def _parse_date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text[:10]) if len(text) >= 10 else None
    except ValueError:
        return None


def _add_date(allowed: set[str], value: dt.date) -> None:
    """A date, plus the two other ways a sentence may refer to it."""
    allowed.add(f"date:{value.isoformat()}")
    allowed.add(f"monthday:{value.month}-{value.day}")
    allowed.add(f"weekday:{WEEKDAY_NAMES[value.weekday()]}")


def check(reply: str, obj: Any) -> tuple[bool, tuple[str, ...]]:
    """Whether every number in `reply` is in `obj`, and which are not."""
    allowed = allowed_numbers(obj)
    unsupported = tuple(token for token in numbers_in(reply) if token not in allowed)
    return (not unsupported, unsupported)


# --- The template fallback ----------------------------------------------


def template(subject: Subject, obj: Mapping[str, Any]) -> str:
    """A reply built from the object's own fields. Never involves the model.

    Every number here was copied out of the object, so the post-check would
    pass on it by construction -- which is what makes it a safe fallback
    rather than a second guess.
    """
    if subject == "simulation":
        return _simulation_template(obj)
    if subject == "policy_failure":
        return _failure_template(obj)
    if subject == "plan_invalid":
        return _invalid_template(obj)
    return _proposal_template(obj)


def _synthetic_note(obj: Mapping[str, Any]) -> str:
    if obj.get("has_synthetic_one_rms"):
        return " These numbers come from a development max, not one you tested."
    return ""


def _proposal_template(obj: Mapping[str, Any]) -> str:
    """From `reasons`, which the solver wrote (spec 13 §5D)."""
    reasons = [str(reason) for reason in obj.get("reasons") or ()][:3]
    warnings = obj.get("warnings") or ()

    if obj.get("preview_only"):
        head = "Here's what that block would look like. There's nothing to approve yet."
    elif obj.get("proposal_id"):
        head = "I've put together a change for you to approve."
    else:
        head = "I couldn't make that work."

    body = " ".join(reason.rstrip(".") + "." for reason in reasons)
    note = ""
    if warnings:
        messages = "; ".join(str(w.get("message", w)) for w in list(warnings)[:2])
        note = f" Worth knowing: {messages}."
    return f"{head} {body}{note}{_synthetic_note(obj)}".strip()


def _simulation_template(obj: Mapping[str, Any]) -> str:
    """From percentiles and `assumptions`.

    A `SimulationResult` has no `reasons`, which spec 13 §5D does not account
    for. `assumptions` is the simulator's own prose and is the direct analogue
    -- ADR-0015 records the substitution.
    """
    result = obj.get("result", obj)
    stats = result.get("stats") or {}
    finish = result.get("finish_date")
    missed = result.get("missed_sessions") or {}
    assumptions = [str(line) for line in (result.get("assumptions") or ())][:2]

    if finish:
        head = (
            f"Over {stats.get('runs')} runs the block finishes between {finish.get('p10')} "
            f"and {finish.get('p90')}, most often {finish.get('p50')}."
        )
    else:
        head = (
            f"Over {stats.get('runs')} runs, none reached the end of the block under that scenario."
        )

    middle = ""
    if missed:
        middle = (
            f" You'd expect around {missed.get('p50')} missed sessions, "
            f"up to {missed.get('p90')} in the worse runs."
        )

    restart = ""
    if result.get("p_restart"):
        restart = f" {result['p_restart']} of runs ended in a restart."

    tail = f" Assumptions: {'; '.join(assumptions)}." if assumptions else ""
    return f"{head}{middle}{restart}{tail}{_synthetic_note(obj)}"


def _failure_template(obj: Mapping[str, Any]) -> str:
    failure = obj.get("failure") or obj
    reason = str(failure.get("reason", "no valid option was found"))
    dropped = [str(line) for line in (failure.get("dropped") or ())][:2]
    tail = f" {' '.join(dropped)}" if dropped else ""
    return f"I couldn't find a safe way to do that: {reason}.{tail}"


def _invalid_template(obj: Mapping[str, Any]) -> str:
    violations = obj.get("violations") or ()
    messages = "; ".join(str(v.get("message", v)) for v in list(violations)[:3])
    if not messages:
        return "That plan breaks one of the training rules, so I haven't saved it."
    return f"That plan breaks a rule, so I haven't saved it: {messages}."


# --- The model call -----------------------------------------------------


class Explanation(BaseModel):
    """What came back, and whether the model's own words survived the check.

    A plain `str` return would not carry the fallback flag, and the `@records`
    decorator appends its entry only *after* the function returns -- so there
    is no entry to annotate from inside. Returning it is the honest way to get
    `fallback` into the trace, and it means a caller can tell too.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    used_fallback: bool = False
    unsupported: tuple[str, ...] = ()
    # Set when the model could not be asked at all, or answered unusably. Kept
    # apart from `unsupported` so a trace distinguishes "it invented a number"
    # from "it was down", which are very different things to act on.
    error: str | None = None


def _explain_summary(out: Explanation) -> dict[str, Any]:
    return {
        "chars": len(out.text),
        "fallback": out.used_fallback,
        "unsupported": list(out.unsupported),
        "error": out.error,
    }


@records("answer_check", summarise=_explain_summary)
def verify(reply: str, obj: Any, *, fallback: str) -> Explanation:
    """Check an already-written reply against the object it must come from.

    The `answer` route's reply is written by a model that called tools, not by
    `explain`, and it gets the same post-check: the only guard it had was a
    sentence in the prompt asking for no invented numbers, which is exactly the
    "prompt asking nicely" CLAUDE.md non-negotiable 1 rejects. `obj` is hidden
    from the trace by `recorder.HIDDEN_ARGS`, so what is recorded is the verdict
    and not the payload.
    """
    return verify_reply(reply, obj, fallback=fallback, subject="answer")


@records("explainer", summarise=_explain_summary)
def explain(
    model: Model,
    subject: Subject,
    obj: Mapping[str, Any],
    *,
    fallback: str,
) -> Explanation:
    """Narrate `obj`, or fall back if the model fails or quotes a number it lacks.

    `obj` arrives already JSON-dumped and `fallback` precomputed, so this is
    pure apart from the one model call -- and `template` stays testable without
    a model at all.

    **A model that is down falls back too.** The first version let that
    propagate as a 503, on the argument that a silently degrading explanation
    hides an outage. But by the time this runs the tool has already written its
    proposal rows, and failing the turn then orphaned them: committed, pending,
    absent from the response and from `decision.proposal_id`, and duplicated on
    the athlete's retry (found by review). The deterministic work succeeded and
    the template says so correctly, so the turn is not failed; the outage is
    visible in the trace (`error`) and in the router call that precedes this
    one, which still raises.
    """
    agent: Agent[None, str] = Agent(model, instructions=prompts.explain_instructions(subject))
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)

    try:
        reply = agent.run_sync(prompts.explain_prompt(subject, payload)).output
    except Exception as exc:  # noqa: BLE001 - the work is done; see the docstring
        logger.warning("explainer: model call failed, using the template", exc_info=True)
        return Explanation(
            text=fallback, used_fallback=True, error=f"{type(exc).__name__}: {str(exc)[:160]}"
        )

    return verify_reply(reply, obj, fallback=fallback, subject=subject)


def verify_reply(reply: str, obj: Any, *, fallback: str, subject: str) -> Explanation:
    """`verify` without its own trace entry, for `explain` to call from inside.

    Two entries for one explanation would double the noise in `tool_calls`, and
    `explain`'s own already carries the verdict.
    """
    if not reply.strip():
        return Explanation(text=fallback, used_fallback=True, error="empty reply")

    ok, unsupported = check(reply, obj)
    if not ok:
        logger.warning(
            "explainer: reply quoted %d number(s) the %s does not contain: %s",
            len(unsupported),
            subject,
            ", ".join(unsupported),
        )
        return Explanation(text=fallback, used_fallback=True, unsupported=unsupported)
    return Explanation(text=reply.strip())
