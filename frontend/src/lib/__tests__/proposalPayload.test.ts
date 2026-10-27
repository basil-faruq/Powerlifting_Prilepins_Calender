import { describe, expect, it } from "vitest";
import type { ProposalOut } from "@/api/types";
import {
  isRestart,
  readChanges,
  readCost,
  readPlan,
  readSessionChanges,
  readSlots,
  supersededBlockId,
} from "../proposalPayload";

/**
 * These readers sit between a payload the schema types as `{ [key: string]: unknown }`
 * and everything that draws it. The contract is narrow and absolute: never throw, and
 * never invent a value. So most of what is below is garbage in, empty out.
 */

const proposal = (over: Partial<ProposalOut> = {}): ProposalOut => ({
  id: "p1",
  block_id: "b1",
  kind: "new_block",
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

/** Everything a reader may be handed that is not a payload at all. */
const NOT_A_PAYLOAD = [null, undefined, "payload", 7, [], {}, { changes: "no" }];

describe("readChanges", () => {
  it("reads every change the solver emitted, in its order", () => {
    const payload = {
      changes: [
        { seq: 4, action: "move", from_date: "2026-10-21", to_date: "2026-10-22" },
        { seq: 5, action: "fold", from_date: "2026-10-23", to_date: null, into_seq: 4 },
      ],
    };
    expect(readChanges(payload)).toEqual([
      { seq: 4, action: "move", from: "2026-10-21", to: "2026-10-22", intoSeq: null },
      { seq: 5, action: "fold", from: "2026-10-23", to: null, intoSeq: 4 },
    ]);
  });

  it("reads anything that is not a payload with changes as no changes at all", () => {
    for (const bad of NOT_A_PAYLOAD) {
      expect(readChanges(bad)).toEqual([]);
    }
  });

  it("drops an entry it cannot identify rather than guessing at a seq or an action", () => {
    const payload = {
      changes: [
        { action: "move", to_date: "2026-10-22" },
        { seq: 5 },
        { seq: "6", action: "move" },
        null,
        "move seq 7",
        [{ seq: 8, action: "move" }],
        { seq: 9, action: "move" },
      ],
    };
    expect(readChanges(payload).map((c) => c.seq)).toEqual([9]);
  });

  it("keeps an action this frontend has never heard of", () => {
    // A server that grows a sixth action must not make the whole diff vanish.
    expect(readChanges({ changes: [{ seq: 1, action: "teleport" }] })).toEqual([
      { seq: 1, action: "teleport", from: null, to: null, intoSeq: null },
    ]);
  });

  it("reads a date that is absent or not a date as no date", () => {
    const payload = {
      changes: [
        { seq: 1, action: "move" },
        { seq: 2, action: "move", from_date: "21/10/2026", to_date: 12345 },
        { seq: 3, action: "move", from_date: null, to_date: "" },
      ],
    };
    for (const change of readChanges(payload)) {
      expect(change.from).toBeNull();
      expect(change.to).toBeNull();
    }
  });

  it("rejects a date of the right shape that is not a real day", () => {
    // "2026-13-99" satisfies /^\d{4}-\d{2}-\d{2}$/ but is no date. Left as a string it
    // reaches `Intl.DateTimeFormat#format` as an Invalid Date, which THROWS -- the one
    // way a payload could take down the card this module exists to keep standing.
    expect(readChanges({ changes: [{ seq: 1, action: "move", from_date: "2026-13-99" }] })).toEqual(
      [{ seq: 1, action: "move", from: null, to: null, intoSeq: null }],
    );
  });

  it("keeps one entry per session when an edit has been merged in", () => {
    // `services.edit_proposal` persists the solver's changes AND the hand edits
    // appended after them, so one session moved twice is stored twice. The server
    // applies them last-wins into a dict keyed on seq; this reads them back the same.
    const payload = {
      changes: [
        { seq: 4, week: 2, slot: 1, action: "move", from_date: "2026-10-19", to_date: "2026-10-20" },
        { seq: 9, week: 3, slot: 1, action: "move", from_date: "2026-10-26", to_date: "2026-10-27" },
        { seq: 4, week: 2, slot: 1, action: "move", from_date: "2026-10-19", to_date: "2026-10-22" },
      ],
    };
    const read = readChanges(payload);
    expect(read).toHaveLength(2);
    expect(read[0]).toMatchObject({ seq: 4, to: "2026-10-22" });
    // The solver's ordering survives: the edited session does not jump to the end.
    expect(read[1]).toMatchObject({ seq: 9, to: "2026-10-27" });
  });

  it("rejects a day that would roll over into the next month", () => {
    const payload = { changes: [{ seq: 1, action: "move", from_date: "2026-02-31" }] };
    expect(readChanges(payload)[0]?.from).toBeNull();
  });

  it("keeps a real leap day", () => {
    const payload = { changes: [{ seq: 1, action: "move", from_date: "2028-02-29" }] };
    expect(readChanges(payload)[0]?.from).toBe("2028-02-29");
  });

  it("reads a non-finite into_seq as no target rather than as session zero", () => {
    const payload = { changes: [{ seq: 1, action: "fold", into_seq: Number.NaN }] };
    expect(readChanges(payload)[0]?.intoSeq).toBeNull();
  });
});

describe("readSessionChanges", () => {
  it("hands a valid change back exactly as it arrived, for a PATCH to send on", () => {
    const change = {
      seq: 4,
      week: 2,
      slot: 1,
      action: "move",
      from_date: "2026-10-21",
      to_date: "2026-10-22",
      into_seq: null,
    };
    expect(readSessionChanges({ changes: [change] })).toEqual([change]);
  });

  it("drops a change that does not carry everything the route requires", () => {
    const payload = {
      changes: [
        { seq: 1, week: 1, slot: 1 },
        { seq: 2, week: 1, action: "move" },
        { seq: 3, slot: 1, action: "move" },
        { week: 1, slot: 1, action: "move" },
        { seq: "4", week: 1, slot: 1, action: "move" },
        { seq: 5, week: "1", slot: 1, action: "move" },
        { seq: 6, week: 1, slot: "1", action: "move" },
        { seq: 7, week: 1, slot: 1, action: 9 },
        { seq: 8, week: 1, slot: 1, action: "move" },
      ],
    };
    expect(readSessionChanges(payload).map((c) => c.seq)).toEqual([8]);
  });

  it("reads anything that is not a payload with changes as nothing to send", () => {
    for (const bad of NOT_A_PAYLOAD) {
      expect(readSessionChanges(bad)).toEqual([]);
    }
  });
});

describe("readPlan", () => {
  it("gives every kept session a lifts array, whatever the server sent", () => {
    // `BlockPreviewTable` maps over `lifts` unguarded, so a session without one was
    // a blank screen rather than a degraded card.
    const plan = [
      { week: 1, slot: 1, seq: 1, date: "2026-10-19" },
      { week: 1, slot: 2, seq: 2, date: "2026-10-21", lifts: "not a list" },
    ];
    const read = readPlan({ plan });
    expect(read).toHaveLength(2);
    expect(read.every((session) => Array.isArray(session.lifts))).toBe(true);
  });

  it("drops a session whose week, slot or seq is not a number", () => {
    const plan = [
      { week: "one", slot: 1, seq: 1, date: "2026-10-19", lifts: [] },
      { week: 1, slot: 1, seq: null, date: "2026-10-19", lifts: [] },
    ];
    expect(readPlan({ plan })).toEqual([]);
  });

  it("keeps only lifts that carry the code and the weight the UI prints", () => {
    const plan = [
      {
        week: 1,
        slot: 1,
        seq: 1,
        date: "2026-10-19",
        lifts: [{ lift_code: "squat", weight_kg: "87.50" }, { lift_code: "bench" }, "nonsense"],
      },
    ];
    expect(readPlan({ plan })[0]?.lifts).toHaveLength(1);
  });

  it("reads the schedule a new_block carries", () => {
    const sessions = [
      { week: 1, slot: 1, seq: 1, date: "2026-10-19", lifts: [] },
      { week: 1, slot: 2, seq: 2, date: "2026-10-21", lifts: [] },
    ];
    expect(readPlan({ plan: sessions })).toEqual(sessions);
  });

  it("keeps only the entries that are records with a date", () => {
    const payload = {
      plan: [
        { week: 1, slot: 1, seq: 1, date: "2026-10-19", lifts: [] },
        { week: 1, slot: 2, seq: 2, date: "tomorrow", lifts: [] },
        { week: 1, slot: 3, seq: 3, lifts: [] },
        "2026-10-21",
        null,
        [],
      ],
    };
    expect(readPlan(payload).map((s) => s.seq)).toEqual([1]);
  });

  it("reads a payload with no plan as no sessions -- a reschedule carries only its diff", () => {
    for (const bad of [...NOT_A_PAYLOAD, { plan: "the whole block" }]) {
      expect(readPlan(bad)).toEqual([]);
    }
  });
});

describe("readCost", () => {
  it("reads the three parts the solver ranks by", () => {
    expect(readCost({ delay_days: 2, off_weekday_count: 1, end_slip_days: 3.5 })).toEqual({
      delayDays: 2,
      offWeekdayCount: 1,
      endSlipDays: 3.5,
    });
  });

  it("reads a missing part as 'not stated', which is not the same as costing nothing", () => {
    const cost = readCost({ delay_days: 2 });
    expect(cost.delayDays).toBe(2);
    expect(cost.offWeekdayCount).toBeNull();
    expect(cost.endSlipDays).toBeNull();
  });

  it("reads a part that is not a finite number as not stated", () => {
    const cost = readCost({
      delay_days: Number.NaN,
      off_weekday_count: Number.POSITIVE_INFINITY,
      end_slip_days: "3",
    });
    expect(cost.delayDays).toBeNull();
    expect(cost.offWeekdayCount).toBeNull();
    expect(cost.endSlipDays).toBeNull();
  });

  it("keeps a real zero as zero, because costing nothing is a fact", () => {
    const cost = readCost({ delay_days: 0, off_weekday_count: 0, end_slip_days: 0 });
    expect(cost).toEqual({ delayDays: 0, offWeekdayCount: 0, endSlipDays: 0 });
  });

  it("reads anything that is not a cost object as nothing stated", () => {
    for (const bad of [null, undefined, "free", 0, []]) {
      expect(readCost(bad)).toEqual({
        delayDays: null,
        offWeekdayCount: null,
        endSlipDays: null,
      });
    }
  });
});

describe("isRestart", () => {
  it("is true only when the payload names the block it would supersede", () => {
    expect(isRestart(proposal({ payload: { supersedes_block_id: "b0" } }))).toBe(true);
  });

  it("is false for a first block, a rollover, and a superseded id that is not a string", () => {
    expect(isRestart(proposal())).toBe(false);
    expect(isRestart(proposal({ payload: { supersedes_block_id: null } }))).toBe(false);
    expect(isRestart(proposal({ payload: { supersedes_block_id: 7 } }))).toBe(false);
  });
});

describe("supersededBlockId", () => {
  it("names the block a restart would replace", () => {
    expect(supersededBlockId({ supersedes_block_id: "b0" })).toBe("b0");
  });

  it("names nothing when there is nothing to name", () => {
    for (const bad of [...NOT_A_PAYLOAD, { supersedes_block_id: 7 }]) {
      expect(supersededBlockId(bad)).toBeNull();
    }
  });
});

describe("readSlots", () => {
  it("reads the proposed slot → weekday map", () => {
    const slots = readSlots({ slots: { 1: 0, 2: 2, 3: 4 } });
    expect(slots).toEqual(
      new Map([
        [1, 0],
        [2, 2],
        [3, 4],
      ]),
    );
  });

  it("tells 'no change proposed' from a change, by returning null for an absent map", () => {
    for (const bad of [...NOT_A_PAYLOAD, { slots: [0, 2, 4] }, { slots: null }]) {
      expect(readSlots(bad)).toBeNull();
    }
  });

  it("drops a weekday outside Monday–Sunday and a slot that is not a whole number", () => {
    const slots = readSlots({
      slots: { 1: 0, 2: 7, 3: -1, 4: 2.5, 5: "3", "6.5": 1, seven: 1 },
    });
    expect(slots).toEqual(new Map([[1, 0]]));
  });

  it("returns null rather than an empty map when nothing in the map could be read", () => {
    expect(readSlots({ slots: { 1: 9, 2: "Monday" } })).toBeNull();
  });
});
