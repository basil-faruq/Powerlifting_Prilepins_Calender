import { describe, expect, it } from "vitest";
import type { PlannedLiftOut, SessionOut } from "@/api/types";
import {
  type LiftEntry,
  madeIt,
  missedIt,
  pickSession,
  prefill,
  toLogIn,
  validateAll,
  validateEntry,
  wouldSetNewMax,
} from "../model";

/**
 * The logger's whole job is to carry a prescription to the API without touching
 * it. Nothing here computes a weight, so most of what is asserted below is that
 * a weight came out as the exact STRING it went in as -- and that the one piece
 * of arithmetic there is, the plate check, is a warning rather than a refusal.
 */

const planned = (over: Partial<PlannedLiftOut> = {}): PlannedLiftOut => ({
  lift_code: "squat",
  variation: null,
  sets: 3,
  reps: 5,
  pct_num: "75",
  pct_den: "100",
  weight_kg: "87.50",
  rel_intensity: "0.7500",
  is_test: false,
  ...over,
});

const session = (over: Partial<SessionOut> & { date: string }): SessionOut => ({
  id: `s-${over.date}`,
  block_id: "b1",
  week: 1,
  slot: 1,
  seq: 1,
  original_date: over.date,
  status: "planned",
  lifts: [planned()],
  ...over,
});

const entry = (over: Partial<LiftEntry> = {}): LiftEntry => ({
  liftCode: "squat",
  setsDone: "3",
  repsDone: ["5", "5", "5"],
  weightKg: "87.50",
  rpe: "",
  isTest: false,
  prescribed: { sets: 3, reps: 5, weightKg: "87.50" },
  ...over,
});

const TODAY = "2026-03-03";

describe("pickSession", () => {
  it("opens on today's session when there is one", () => {
    const sessions = [
      session({ date: "2026-03-01" }),
      session({ date: TODAY }),
      session({ date: "2026-03-05" }),
    ];
    const picked = pickSession(sessions, TODAY);
    expect(picked?.session.date).toBe(TODAY);
    expect(picked?.isToday).toBe(true);
  });

  it("offers the next session still to come when today is a rest day", () => {
    const sessions = [
      session({ date: "2026-03-06" }),
      session({ date: "2026-03-04" }),
      session({ date: "2026-03-05" }),
    ];
    const picked = pickSession(sessions, TODAY);
    expect(picked?.session.date).toBe("2026-03-04");
    expect(picked?.isToday).toBe(false);
  });

  it("skips a future session that is already logged", () => {
    const sessions = [
      session({ date: "2026-03-04", status: "done" }),
      session({ date: "2026-03-06" }),
    ];
    expect(pickSession(sessions, TODAY)?.session.date).toBe("2026-03-06");
  });

  it("falls back to the most recent session nobody ever logged", () => {
    // Nothing ahead: a session trained yesterday can still be recorded.
    const sessions = [
      session({ date: "2026-02-26" }),
      session({ date: "2026-03-02" }),
      session({ date: "2026-03-01", status: "done" }),
    ];
    const picked = pickSession(sessions, TODAY);
    expect(picked?.session.date).toBe("2026-03-02");
    expect(picked?.isToday).toBe(false);
  });

  it("leaves a past session alone once it is logged or written off", () => {
    const sessions = [
      session({ date: "2026-03-02", status: "skipped" }),
      session({ date: "2026-03-01", status: "done" }),
    ];
    expect(pickSession(sessions, TODAY)).toBeNull();
  });

  it("has nothing to open when the block is empty", () => {
    expect(pickSession([], TODAY)).toBeNull();
  });
});

describe("prefill", () => {
  it("starts the form at exactly what was prescribed, so done-as-written is one tap", () => {
    const entries = prefill([planned({ sets: 4, reps: 6 })]);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.liftCode).toBe("squat");
    expect(entries[0]?.setsDone).toBe("4");
    expect(entries[0]?.repsDone).toEqual(["6"]);
  });

  it("carries the weight as the string the backend sent, never as a number", () => {
    const entries = prefill([planned({ weight_kg: "87.50" })]);
    expect(entries[0]?.weightKg).toBe("87.50");
    expect(entries[0]?.prescribed.weightKg).toBe("87.50");
    expect(typeof entries[0]?.weightKg).toBe("string");
  });

  it("marks a test single as one, since that is what decides a new max", () => {
    expect(prefill([planned({ is_test: true })])[0]?.isTest).toBe(true);
    expect(prefill([planned()])[0]?.isTest).toBe(false);
  });

  it("drops a lift this build has no code for rather than sending one the API refuses", () => {
    const entries = prefill([planned(), planned({ lift_code: "curl" }), planned({ lift_code: "row" })]);
    expect(entries.map((e) => e.liftCode)).toEqual(["squat", "row"]);
  });
});

describe("madeIt / missedIt", () => {
  it("records a made single as one rep at the weight on the bar", () => {
    const made = madeIt(entry({ weightKg: "142.50", isTest: true }));
    expect(made.setsDone).toBe("1");
    expect(made.repsDone).toEqual(["1"]);
  });

  it("records a missed single as an attempt with nothing completed", () => {
    // There is no "missed" field on the wire: zero reps is how a failed attempt
    // is stated, and it is precisely what stops the backend writing a max.
    const missed = missedIt(entry({ weightKg: "142.50", isTest: true }));
    expect(missed.setsDone).toBe("0");
    expect(missed.repsDone).toEqual(["0"]);
  });

  it("changes neither the weight the athlete attempted nor the prescription", () => {
    const attempt = entry({ weightKg: "142.50", isTest: true });
    for (const after of [madeIt(attempt), missedIt(attempt)]) {
      expect(after.weightKg).toBe("142.50");
      expect(after.prescribed).toEqual(attempt.prescribed);
    }
  });
});

describe("wouldSetNewMax", () => {
  const single = (over: Partial<LiftEntry> = {}): LiftEntry =>
    entry({
      isTest: true,
      setsDone: "1",
      repsDone: ["1"],
      weightKg: "117.5",
      prescribed: { sets: 1, reps: 1, weightKg: "117.5" },
      ...over,
    });

  it("is true at exactly the prescribed weight", () => {
    // `117.5 >= 117.5` is the comparison a float could get wrong; this one is exact.
    expect(wouldSetNewMax(single())).toBe(true);
    expect(wouldSetNewMax(single({ weightKg: "117.50" }))).toBe(true);
  });

  it("is true above it", () => {
    expect(wouldSetNewMax(single({ weightKg: "120" }))).toBe(true);
  });

  it("is false below it", () => {
    expect(wouldSetNewMax(single({ weightKg: "115" }))).toBe(false);
  });

  it("is false for an attempt where nothing was completed", () => {
    expect(wouldSetNewMax(single({ setsDone: "0" }))).toBe(false);
    expect(wouldSetNewMax(single({ repsDone: ["0"] }))).toBe(false);
  });

  it("is false for a working set, however heavy", () => {
    expect(wouldSetNewMax(single({ isTest: false, weightKg: "200" }))).toBe(false);
  });
});

describe("validateEntry", () => {
  const STEP = "2.5";

  it("refuses a set count outside what the API takes", () => {
    expect(validateEntry(entry({ setsDone: "11" }), STEP).errors).not.toHaveLength(0);
    expect(validateEntry(entry({ setsDone: "-1" }), STEP).errors).not.toHaveLength(0);
  });

  it("refuses a rep count outside what the API takes", () => {
    expect(validateEntry(entry({ repsDone: ["21"] }), STEP).errors).not.toHaveLength(0);
  });

  it("refuses a weight of nothing and a weight past the ceiling", () => {
    expect(validateEntry(entry({ weightKg: "0" }), STEP).errors).not.toHaveLength(0);
    expect(validateEntry(entry({ weightKg: "500.1" }), STEP).errors).not.toHaveLength(0);
  });

  it("refuses an RPE off the scale at either end", () => {
    expect(validateEntry(entry({ rpe: "5" }), STEP).errors).not.toHaveLength(0);
    expect(validateEntry(entry({ rpe: "11" }), STEP).errors).not.toHaveLength(0);
  });

  it("is happy with a prescription logged as written", () => {
    expect(validateEntry(entry(), STEP)).toEqual({ errors: [], warnings: [] });
  });

  it("warns about a weight off the plate step and never refuses it", () => {
    // `plate_increment_kg` is a setting, and a gym with a pair of 0.5 kg plates
    // in a drawer is a real thing: refusing it would make the RECORD wrong.
    const problems = validateEntry(entry({ weightKg: "117.5" }), "5");
    expect(problems.errors).toEqual([]);
    expect(problems.warnings).not.toHaveLength(0);
  });

  it("says nothing about a weight that sits on the step", () => {
    expect(validateEntry(entry({ weightKg: "120" }), "5").warnings).toEqual([]);
  });
});

describe("validateAll", () => {
  it("is ok when every entry is ok and the note is short enough", () => {
    const result = validateAll([entry(), entry({ liftCode: "bench" })], "2.5", "felt strong");
    expect(result.ok).toBe(true);
    expect(result.noteError).toBeNull();
    expect(result.byEntry).toHaveLength(2);
  });

  it("is not ok when any one entry has an error", () => {
    expect(validateAll([entry(), entry({ setsDone: "11" })], "2.5", "").ok).toBe(false);
  });

  it("is not ok when the note runs past the limit", () => {
    const result = validateAll([entry()], "2.5", "x".repeat(2001));
    expect(result.ok).toBe(false);
    expect(result.noteError).not.toBeNull();
  });

  it("stays ok with a warning, because a warning is not a refusal", () => {
    const result = validateAll([entry({ weightKg: "117.5" })], "5", "");
    expect(result.ok).toBe(true);
    expect(result.byEntry[0]?.warnings).not.toHaveLength(0);
  });
});

describe("toLogIn", () => {
  it("collapses sets that all matched to the single number the athlete meant", () => {
    const body = toLogIn([entry({ repsDone: ["5", "5", "5"] })], TODAY, "");
    expect(body.entries[0]?.reps_done).toBe(5);
  });

  it("keeps the per-set list when the sets differed", () => {
    const body = toLogIn([entry({ repsDone: ["5", "4", "3"] })], TODAY, "");
    expect(body.entries[0]?.reps_done).toEqual([5, 4, 3]);
  });

  it("sends the weight as the string it has been all along", () => {
    const body = toLogIn([entry({ weightKg: "87.50" })], TODAY, "");
    expect(body.entries[0]?.weight_kg).toBe("87.50");
  });

  it("leaves rpe and note out entirely rather than sending an empty one", () => {
    expect(toLogIn([entry({ rpe: "  " })], TODAY, "   ")).toStrictEqual({
      performed_on: TODAY,
      entries: [{ lift_code: "squat", sets_done: 3, reps_done: 5, weight_kg: "87.50" }],
    });
  });

  it("sends an rpe and a note that were filled in, trimmed", () => {
    const body = toLogIn([entry({ rpe: " 8.5 " })], TODAY, "  back felt tight  ");
    expect(body.entries[0]?.rpe).toBe("8.5");
    expect(body.note).toBe("back felt tight");
  });
});
