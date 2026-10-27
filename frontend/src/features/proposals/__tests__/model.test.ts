import { describe, expect, it } from "vitest";
import type { ProposalOut } from "@/api/types";
import { changeLines, decidedLabel, groupOptions, isDecided, sourceLabel, titleFor } from "../model";

const proposal = (over: Partial<ProposalOut> = {}): ProposalOut => ({
  id: "p1",
  block_id: "b1",
  kind: "reschedule",
  status: "pending",
  payload: {},
  cost: {},
  reasons: [],
  warnings: [],
  source: "chat",
  decided_at: null,
  decision_reason: null,
  ...over,
});

/** One session of a payload's own plan, enough for `readPlan` to keep it. */
const planned = (seq: number, lifts: string[]) => ({
  week: 1,
  slot: 1,
  seq,
  date: "2026-10-19",
  lifts: lifts.map((lift_code) => ({
    lift_code,
    variation: null,
    sets: 3,
    reps: 10,
    pct_num: "70",
    pct_den: "120",
    weight_kg: "75.00",
    rel_intensity: "0.7778",
    is_test: false,
  })),
});

describe("titleFor", () => {
  it("names a first block or a rollover a new block", () => {
    expect(titleFor(proposal({ kind: "new_block" }))).toBe("New block");
  });

  it("says a new_block that supersedes one is a restart, not ordinary new work", () => {
    const restart = proposal({ kind: "new_block", payload: { supersedes_block_id: "b0" } });
    expect(titleFor(restart)).toBe("Restart this block");
  });

  it("names the other two kinds", () => {
    expect(titleFor(proposal({ kind: "reschedule" }))).toBe("Reschedule");
    expect(titleFor(proposal({ kind: "weekday_change" }))).toBe("Change training days");
  });
});

describe("sourceLabel", () => {
  it("says a chat proposal came from chat", () => {
    expect(sourceLabel(proposal({ source: "chat" }))).toBe("from chat");
  });

  it("never calls a workflow proposal a missed session", () => {
    // ADR-0017 rule 2: `missed` is never written and means written off by a human.
    const label = sourceLabel(proposal({ source: "workflow" }));
    expect(label).not.toMatch(/missed/i);
    expect(label).toBe("raised automatically");
  });

  it("claims no particular cause for a workflow proposal", () => {
    // `workflow` has more than one cause: a session nobody logged, or an
    // unavailability the athlete declared. A live run produced a RESTART from the
    // second, so naming either here would be wrong half the time. The reasons on
    // the card carry the real cause, in the server's words.
    const label = sourceLabel(proposal({ source: "workflow" }));
    expect(label).not.toMatch(/unlogged|log|travel|injur/i);
  });
});

describe("changeLines", () => {
  it("names the lifts of each changed session, from the payload's own plan", () => {
    const payload = {
      plan: [planned(4, ["squat", "bench"])],
      changes: [{ seq: 4, action: "move", from_date: "2026-10-21", to_date: "2026-10-22" }],
    };
    expect(changeLines(proposal({ payload }))).toEqual([
      {
        seq: 4,
        action: "move",
        lifts: ["squat", "bench"],
        from: "2026-10-21",
        to: "2026-10-22",
        note: null,
      },
    ]);
  });

  it("names a lift once however many times the session trains it", () => {
    const payload = {
      plan: [planned(4, ["squat", "squat", "bench", "squat"])],
      changes: [{ seq: 4, action: "move" }],
    };
    expect(changeLines(proposal({ payload }))[0]?.lifts).toEqual(["squat", "bench"]);
  });

  it("still draws a line for a session the plan does not describe", () => {
    const payload = { plan: [planned(4, ["squat"])], changes: [{ seq: 9, action: "move" }] };
    const lines = changeLines(proposal({ payload }));
    expect(lines).toHaveLength(1);
    expect(lines[0]?.lifts).toEqual([]);
  });

  it("gives a plain move no note -- the dates already say everything", () => {
    const payload = { changes: [{ seq: 1, action: "move", to_date: "2026-10-22" }] };
    expect(changeLines(proposal({ payload }))[0]?.note).toBeNull();
  });

  it("explains in a sentence every action that is not a move", () => {
    const payload = {
      changes: [
        { seq: 1, action: "skip" },
        { seq: 2, action: "fold" },
        { seq: 3, action: "fold", into_seq: 7 },
        { seq: 4, action: "repeat_week" },
        { seq: 5, action: "create" },
      ],
    };
    expect(changeLines(proposal({ payload })).map((line) => line.note)).toEqual([
      "dropped from the block",
      "folded into another session",
      "folded into session 7",
      "this week runs again",
      "added to the block",
    ]);
  });

  it("says the action's own name for one this frontend predates", () => {
    const payload = { changes: [{ seq: 1, action: "teleport" }] };
    expect(changeLines(proposal({ payload }))[0]?.note).toBe("teleport");
  });

  it("draws no lines at all from a payload it cannot read", () => {
    expect(changeLines(proposal({ payload: {} }))).toEqual([]);
  });
});

describe("groupOptions", () => {
  it("puts the options for one event side by side, in the solver's ranking", () => {
    const options = [
      proposal({ id: "a", block_id: "b1", kind: "reschedule" }),
      proposal({ id: "b", block_id: "b1", kind: "reschedule" }),
      proposal({ id: "c", block_id: "b1", kind: "reschedule" }),
    ];
    const groups = groupOptions(options);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.proposals.map((p) => p.id)).toEqual(["a", "b", "c"]);
  });

  it("separates two kinds raised against the same block", () => {
    const groups = groupOptions([
      proposal({ id: "a", block_id: "b1", kind: "reschedule" }),
      proposal({ id: "b", block_id: "b1", kind: "weekday_change" }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(["b1:reschedule", "b1:weekday_change"]);
  });

  it("separates the same kind raised against two blocks", () => {
    const groups = groupOptions([
      proposal({ id: "a", block_id: "b1", kind: "reschedule" }),
      proposal({ id: "b", block_id: "b2", kind: "reschedule" }),
    ]);
    expect(groups.map((g) => g.proposals.map((p) => p.id))).toEqual([["a"], ["b"]]);
  });

  it("orders the groups by where each one first appears, and keeps interleaving intact", () => {
    const groups = groupOptions([
      proposal({ id: "a", block_id: "b1", kind: "reschedule" }),
      proposal({ id: "b", block_id: "b2", kind: "reschedule" }),
      proposal({ id: "c", block_id: "b1", kind: "reschedule" }),
    ]);
    expect(groups.map((g) => [g.key, g.proposals.map((p) => p.id)])).toEqual([
      ["b1:reschedule", ["a", "c"]],
      ["b2:reschedule", ["b"]],
    ]);
  });

  it("groups nothing out of nothing", () => {
    expect(groupOptions([])).toEqual([]);
  });
});

describe("isDecided", () => {
  it("is false only while a proposal is still waiting on the athlete", () => {
    expect(isDecided(proposal({ status: "pending" }))).toBe(false);
    expect(isDecided(proposal({ status: "approved" }))).toBe(true);
    expect(isDecided(proposal({ status: "rejected" }))).toBe(true);
    expect(isDecided(proposal({ status: "superseded" }))).toBe(true);
  });
});

describe("decidedLabel", () => {
  it("says what happened to a decided proposal", () => {
    expect(decidedLabel(proposal({ status: "approved" }))).toBe("Approved");
    expect(decidedLabel(proposal({ status: "rejected" }))).toBe("Rejected");
    expect(decidedLabel(proposal({ status: "superseded" }))).toBe("Superseded by another option");
  });

  it("falls back to the status itself, so a pending card never reads as blank", () => {
    expect(decidedLabel(proposal({ status: "pending" }))).toBe("pending");
  });
});
