import { describe, expect, it } from "vitest";
import type { ProposalOut } from "@/api/types";
import { ghostPlan, overlayFor, pendingForBlock, readMoves } from "../ghost";

/** A reschedule payload shaped as `StrengthPayload` serialises it. */
const payload = (changes: unknown[]) => ({ changes, plan: [] });

const proposal = (over: Partial<ProposalOut>): ProposalOut =>
  ({
    id: "p1",
    block_id: "block-1",
    kind: "reschedule",
    status: "pending",
    payload: payload([]),
    cost: {},
    reasons: [],
    warnings: [],
    source: "workflow",
    decided_at: null,
    decision_reason: null,
    ...over,
  }) as unknown as ProposalOut;

describe("readMoves", () => {
  it("reads the move entries of a reschedule diff", () => {
    const moves = readMoves(
      payload([
        { seq: 4, week: 2, slot: 1, action: "move", from_date: "2026-10-26", to_date: "2026-10-27" },
        { seq: 5, week: 2, slot: 2, action: "move", from_date: "2026-10-28", to_date: "2026-10-29" },
      ]),
    );
    expect(moves).toEqual([
      { seq: 4, from: "2026-10-26", to: "2026-10-27" },
      { seq: 5, from: "2026-10-28", to: "2026-10-29" },
    ]);
  });

  it("ignores the actions that put a session nowhere new", () => {
    // `skip` and `fold` write a session off; `repeat_week` carries no dates at all.
    const moves = readMoves(
      payload([
        { seq: 1, action: "skip", from_date: "2026-10-26", to_date: null },
        { seq: 2, action: "fold", from_date: "2026-10-28", to_date: null, into_seq: 3 },
        { seq: 4, action: "repeat_week", from_date: null, to_date: null },
        { seq: 5, action: "create", from_date: null, to_date: "2026-11-02" },
      ]),
    );
    expect(moves).toEqual([]);
  });

  it("drops a move that does not actually change the date", () => {
    const moves = readMoves(
      payload([{ seq: 1, action: "move", from_date: "2026-10-26", to_date: "2026-10-26" }]),
    );
    expect(moves).toEqual([]);
  });

  it("survives a payload it does not recognise rather than breaking the calendar", () => {
    for (const bad of [
      undefined,
      null,
      "nonsense",
      42,
      [],
      {},
      { changes: null },
      { changes: "move" },
      payload([null, 7, "move"]),
      payload([{ action: "move" }]),
      payload([{ seq: "4", action: "move", from_date: "2026-10-26", to_date: "2026-10-27" }]),
      payload([{ seq: 4, action: "move", from_date: "26/10/2026", to_date: "2026-10-27" }]),
      payload([{ seq: 4, action: "move", from_date: "2026-10-26" }]),
    ]) {
      expect(readMoves(bad)).toEqual([]);
    }
  });
});

describe("ghostPlan", () => {
  it("indexes a move by the session leaving and by the day it arrives on", () => {
    const plan = ghostPlan(
      payload([{ seq: 4, action: "move", from_date: "2026-10-26", to_date: "2026-10-27" }]),
    );
    expect(plan.leaving.get(4)).toEqual({ seq: 4, from: "2026-10-26", to: "2026-10-27" });
    expect(plan.arriving.get("2026-10-27")).toHaveLength(1);
  });

  it("keeps two sessions landing on the same day together", () => {
    const plan = ghostPlan(
      payload([
        { seq: 4, action: "move", from_date: "2026-10-26", to_date: "2026-10-27" },
        { seq: 5, action: "move", from_date: "2026-10-28", to_date: "2026-10-27" },
      ]),
    );
    expect(plan.arriving.get("2026-10-27")?.map((m) => m.seq)).toEqual([4, 5]);
  });

  it("has nothing to draw for an empty diff", () => {
    expect(ghostPlan(payload([])).moves).toEqual([]);
  });
});

describe("pendingForBlock", () => {
  it("keeps only the proposals about the block on screen", () => {
    const list = [proposal({ id: "a" }), proposal({ id: "b", block_id: "block-2" })];
    expect(pendingForBlock(list, "block-1").map((p) => p.id)).toEqual(["a"]);
  });

  it("has nothing to show before the proposals have loaded, or with no block", () => {
    expect(pendingForBlock(undefined, "block-1")).toEqual([]);
    expect(pendingForBlock([proposal({})], null)).toEqual([]);
  });
});

describe("overlayFor", () => {
  const moving = payload([
    { seq: 4, action: "move", from_date: "2026-10-26", to_date: "2026-10-27" },
  ]);

  it("draws the newest pending proposal that would move something", () => {
    const found = overlayFor([proposal({ id: "a", payload: moving })], "block-1");
    expect(found?.proposal.id).toBe("a");
    expect(found?.plan.moves).toHaveLength(1);
  });

  it("draws a weekday change too, since it moves dates as well", () => {
    const found = overlayFor(
      [proposal({ id: "w", kind: "weekday_change", payload: moving })],
      "block-1",
    );
    expect(found?.proposal.id).toBe("w");
  });

  it("never draws a restart, which proposes a different block entirely", () => {
    // `_propose_restart` sends `kind: "new_block"` with `supersedes_block_id` set.
    const restart = proposal({ id: "r", kind: "new_block", payload: moving });
    expect(overlayFor([restart], "block-1")).toBeNull();
  });

  it("draws nothing when the one pending proposal has no readable moves", () => {
    expect(overlayFor([proposal({ payload: { changes: "?" } })], "block-1")).toBeNull();
  });

  it("skips past a proposal it cannot draw to one it can", () => {
    const found = overlayFor(
      [proposal({ id: "unreadable", payload: {} }), proposal({ id: "ok", payload: moving })],
      "block-1",
    );
    expect(found?.proposal.id).toBe("ok");
  });
});
