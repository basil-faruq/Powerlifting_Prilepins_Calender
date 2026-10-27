import { describe, expect, it } from "vitest";
import type { SessionOut } from "@/api/types";
import {
  buildWeekRows,
  cellState,
  currentMonday,
  knownLifts,
  mondayOf,
  trainingWeekdays,
} from "../model";

/** Only the fields the model reads; the rest of `SessionOut` is irrelevant here. */
const session = (over: Partial<SessionOut> & { date: string }): SessionOut =>
  ({
    id: `s-${over.date}-${over.slot ?? 1}`,
    block_id: "b",
    week: 1,
    slot: 1,
    seq: 1,
    original_date: over.date,
    status: "planned",
    lifts: [],
    ...over,
  }) as unknown as SessionOut;

describe("mondayOf", () => {
  it("takes a Monday to itself", () => {
    expect(mondayOf("2026-10-19")).toBe("2026-10-19");
  });

  it("takes every other day back to its own Monday, Sunday included", () => {
    expect(mondayOf("2026-10-21")).toBe("2026-10-19"); // Wednesday
    expect(mondayOf("2026-10-25")).toBe("2026-10-19"); // Sunday, not the next Monday
  });

  it("crosses a month boundary", () => {
    expect(mondayOf("2026-11-01")).toBe("2026-10-26"); // a Sunday
  });
});

describe("buildWeekRows", () => {
  it("files each session under the weekday it falls on", () => {
    const rows = buildWeekRows([
      session({ date: "2026-10-19", slot: 1, seq: 1 }),
      session({ date: "2026-10-21", slot: 2, seq: 2 }),
      session({ date: "2026-10-23", slot: 3, seq: 3 }),
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0]?.monday).toBe("2026-10-19");
    expect([...(rows[0]?.byWeekday.keys() ?? [])].sort()).toEqual([0, 2, 4]);
  });

  it("orders rows oldest first, whatever order the sessions arrive in", () => {
    const rows = buildWeekRows([
      session({ date: "2026-11-02", week: 3, seq: 7 }),
      session({ date: "2026-10-19", week: 1, seq: 1 }),
      session({ date: "2026-10-26", week: 2, seq: 4 }),
    ]);
    expect(rows.map((row) => row.monday)).toEqual(["2026-10-19", "2026-10-26", "2026-11-02"]);
  });

  it("gives a repeated week a row of its own, because it reuses its week number", () => {
    // ADR-0010's `repeat_week` writes fresh seqs carrying the SAME week number at
    // later dates. Keyed on `week`, these two training weeks would become one row.
    const rows = buildWeekRows([
      session({ date: "2026-10-19", week: 4, seq: 10 }),
      session({ date: "2026-10-26", week: 4, seq: 11 }),
    ]);

    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.week)).toEqual([4, 4]);
    expect(rows.map((row) => row.monday)).toEqual(["2026-10-19", "2026-10-26"]);
  });

  it("keeps two sessions on one day together, in seq order", () => {
    const rows = buildWeekRows([
      session({ date: "2026-10-21", seq: 5 }),
      session({ date: "2026-10-21", seq: 2 }),
    ]);
    expect(rows[0]?.byWeekday.get(2)?.map((s) => s.seq)).toEqual([2, 5]);
  });

  it("has no rows for no sessions", () => {
    expect(buildWeekRows([])).toEqual([]);
  });
});

describe("currentMonday", () => {
  const rows = buildWeekRows([
    session({ date: "2026-10-19", week: 1, seq: 1 }),
    session({ date: "2026-10-26", week: 2, seq: 2 }),
  ]);

  it("picks the week containing today, so it needs nothing from the workflow", () => {
    expect(currentMonday(rows, "2026-10-28")).toBe("2026-10-26");
  });

  it("falls back to the next session's week when today is outside the block", () => {
    expect(currentMonday(rows, "2026-09-01", "2026-10-26")).toBe("2026-10-26");
  });

  it("falls back to the first week when there is no next session either", () => {
    expect(currentMonday(rows, "2026-12-25")).toBe("2026-10-19");
  });

  it("has nothing to highlight in an empty block", () => {
    expect(currentMonday([], "2026-10-19")).toBeNull();
  });
});

describe("trainingWeekdays", () => {
  it("reads the athlete's pinned days off the weekday map", () => {
    const map = [
      { slot: 1, weekday: 0, locked_from: null },
      { slot: 2, weekday: 2, locked_from: null },
      { slot: 3, weekday: 4, locked_from: null },
    ];
    expect([...trainingWeekdays(map)].sort()).toEqual([0, 2, 4]);
  });

  it("highlights nothing when the map has not been saved yet", () => {
    expect(trainingWeekdays(undefined).size).toBe(0);
  });
});

describe("cellState", () => {
  it("leaves a session still to come alone", () => {
    const state = cellState(session({ date: "2026-10-30" }), "2026-10-21");
    expect(state).toMatchObject({ status: "planned", overdue: false, movedFrom: null });
  });

  it("does not call today's session overdue", () => {
    expect(cellState(session({ date: "2026-10-21" }), "2026-10-21").overdue).toBe(false);
  });

  it("calls a past session that was never logged overdue, NOT missed", () => {
    // ADR-0017: `missed` is never written. Logging is what sets `done`, so a past
    // session still reading `planned` is one nobody logged.
    const state = cellState(session({ date: "2026-10-14" }), "2026-10-21");
    expect(state.overdue).toBe(true);
    expect(state.status).toBe("planned");
    expect(state.status).not.toBe("missed");
  });

  it("does not call a logged session overdue, however old", () => {
    expect(cellState(session({ date: "2026-01-05", status: "done" }), "2026-10-21").overdue).toBe(false);
  });

  it("does not call a written-off session overdue", () => {
    for (const status of ["skipped", "missed"] as const) {
      expect(cellState(session({ date: "2026-01-05", status }), "2026-10-21").overdue).toBe(false);
    }
  });

  it("still calls a moved session overdue if its new date has passed unlogged", () => {
    const moved = session({ date: "2026-10-15", original_date: "2026-10-14", status: "moved" });
    expect(cellState(moved, "2026-10-21")).toMatchObject({
      status: "moved",
      overdue: true,
      movedFrom: "2026-10-14",
    });
  });

  it("reports where a session came from only when it actually moved", () => {
    expect(cellState(session({ date: "2026-10-21" }), "2026-10-21").movedFrom).toBeNull();
  });

  it("flags a session holding a test single", () => {
    const lifts = [{ is_test: false }, { is_test: true }] as SessionOut["lifts"];
    expect(cellState(session({ date: "2026-10-21", lifts }), "2026-10-21").isTest).toBe(true);
  });
});

describe("knownLifts", () => {
  it("narrows the schema's plain string so a lift can be given its colour", () => {
    const lifts = [{ lift_code: "squat" }, { lift_code: "deadlift" }] as SessionOut["lifts"];
    expect(knownLifts(lifts).map((lift) => lift.lift_code)).toEqual(["squat", "deadlift"]);
  });

  it("drops a lift code this frontend does not know rather than rendering it colourless", () => {
    const lifts = [{ lift_code: "squat" }, { lift_code: "zercher" }] as SessionOut["lifts"];
    expect(knownLifts(lifts)).toHaveLength(1);
  });
});
