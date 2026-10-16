"""Calibrate the simulator's 1RM gains from the OpenPowerlifting bulk export.

Spec 10 §5F. Reads the public-domain CSV from https://openpowerlifting.org/data,
keeps lifters who competed twice 60-150 days apart in Raw SBD meets, and measures
how much each lift changed between consecutive meets. The distribution of those
changes is what the simulator samples when a test single is made.

    uv run python -m scripts.calibrate_simulator \\
        --csv openpowerlifting-latest.csv --squat 145 --bench 100 --deadlift 180

The three maxes are **required and have no defaults**. They narrow the data to
lifters of your size (an earlier meet within +/-20% of your max), and they are
arguments rather than a database read on purpose: a database seeded with
`make seed-dev-1rms` holds invented maxes, and calibrating against those would
write a file that looks measured and is not (CLAUDE.md non-negotiable #9,
ADR-0004). Nothing here writes a 1RM anywhere.

Press and row have no meet data. They borrow bench's distribution scaled by 0.8
and are marked `derived` in the file, which the simulator repeats in its
assumptions.

The CSV is large (~1 GB) and is never committed -- only the JSON it produces is.
It is streamed through polars rather than loaded whole.

Lifters are matched on name and sex, which the export does not guarantee to be
unique; two people sharing both can contribute a pair that is not one lifter.
That is noise in a distribution this coarse, and is noted in the file's source
line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from domains.strength.simulator import GAINS_PATH, GainQuantiles, Gains

# Spec 10 §5F, each a starting value rather than a derived one.
MIN_GAP_DAYS = 60
MAX_GAP_DAYS = 150
BAND = 0.20
PRESS_ROW_SCALE = 0.8

_MEASURED = ("squat", "bench", "deadlift")
_COLUMN = {"squat": "Best3SquatKg", "bench": "Best3BenchKg", "deadlift": "Best3DeadliftKg"}
_QUANTILES = {"p10": 0.10, "p25": 0.25, "p50": 0.50, "p75": 0.75, "p90": 0.90}


def gains_between_meets(csv: Path, maxes: dict[str, float]) -> dict[str, pl.DataFrame]:
    """Relative change per lift between each lifter's consecutive qualifying meets.

    Lazy until the final collect, so only the eight columns used are read and the
    Event/Equipment filter runs before anything is held in memory. A failed lift
    is recorded as a negative number in the export, so `> 0` drops it.
    """
    lifter = ["Name", "Sex"]
    meets = (
        pl.scan_csv(
            csv,
            infer_schema=False,
            # Everything is read as text, then cast: the export mixes blanks and
            # numbers in the lift columns and a guessed schema fails on row 800k.
        )
        .select([*lifter, "Event", "Equipment", "Date", *_COLUMN.values()])
        .filter((pl.col("Event") == "SBD") & (pl.col("Equipment") == "Raw"))
        .with_columns(
            pl.col("Date").str.to_date("%Y-%m-%d", strict=False),
            *(pl.col(column).cast(pl.Float64, strict=False) for column in _COLUMN.values()),
        )
        .drop_nulls(["Date", *_COLUMN.values()])
        .filter(pl.all_horizontal(pl.col(column) > 0 for column in _COLUMN.values()))
        .sort([*lifter, "Date"])
    )

    paired = meets.with_columns(
        (pl.col("Date") - pl.col("Date").shift(1).over(lifter)).dt.total_days().alias("gap"),
        *(
            pl.col(column).shift(1).over(lifter).alias(f"{lift}_before")
            for lift, column in _COLUMN.items()
        ),
    ).filter(pl.col("gap").is_between(MIN_GAP_DAYS, MAX_GAP_DAYS))

    result: dict[str, pl.DataFrame] = {}
    for lift, column in _COLUMN.items():
        before = pl.col(f"{lift}_before")
        result[lift] = (
            paired.filter(before.is_between(maxes[lift] * (1 - BAND), maxes[lift] * (1 + BAND)))
            .select(((pl.col(column) - before) / before).alias("gain"))
            .collect(engine="streaming")
        )
    return result


def calibrate(csv: Path, maxes: dict[str, float], *, source: str) -> Gains:
    """The gains file for these maxes, or `ValueError` if a lift has no data."""
    lifts: dict[str, GainQuantiles] = {}
    for lift, frame in gains_between_meets(csv, maxes).items():
        n = frame.height
        if n == 0:
            raise ValueError(
                f"no {lift} pairs fall within {BAND:.0%} of {maxes[lift]:g} kg; "
                "the file would calibrate nothing for this lift"
            )
        lifts[lift] = GainQuantiles(
            n=n,
            **{
                name: float(frame["gain"].quantile(q, interpolation="linear"))
                for name, q in _QUANTILES.items()
            },
        )
    bench = lifts["bench"]
    for lift in ("press", "row"):
        lifts[lift] = GainQuantiles(
            n=0,
            derived=True,
            **{name: getattr(bench, name) * PRESS_ROW_SCALE for name in _QUANTILES},
        )
    return Gains(source=source, generated_on=dt.date.today(), lifts=lifts)


def _kilograms(text: str) -> Decimal:
    """A positive, finite weight. argparse reports a `ValueError` as a usage error;
    a bare `Decimal("abc")` raises `InvalidOperation`, which it does not catch."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive weight in kg")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--csv", type=Path, required=True, help="OpenPowerlifting bulk CSV")
    parser.add_argument("--out", type=Path, default=GAINS_PATH, help="where to write gains.json")
    for lift in _MEASURED:
        parser.add_argument(
            f"--{lift}",
            type=_kilograms,
            required=True,
            help=f"your current {lift} 1RM in kg; narrows the data, is never stored",
        )
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"error: {args.csv} does not exist", file=sys.stderr)
        return 1
    maxes = {lift: float(getattr(args, lift)) for lift in _MEASURED}
    source = (
        f"OpenPowerlifting bulk export ({args.csv.name}); Raw SBD, consecutive meets "
        f"{MIN_GAP_DAYS}-{MAX_GAP_DAYS} days apart, earlier lift within {BAND:.0%} of your max; "
        "lifters matched on name and sex"
    )
    try:
        gains = calibrate(args.csv, maxes, source=source)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(gains.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for lift, entry in gains.lifts.items():
        tag = " (derived from bench)" if entry.derived else f" n={entry.n}"
        print(f"{lift:9s} p10={entry.p10:+.3f} p50={entry.p50:+.3f} p90={entry.p90:+.3f}{tag}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
