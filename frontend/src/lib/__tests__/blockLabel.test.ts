import { describe, expect, it } from "vitest";
import type { BlockOut, BlockStateOut } from "@/api/types";
import { blockLabel } from "../blockLabel";

const block = { id: "b", number: 1 } as BlockOut;

const state = (overrides: Partial<BlockStateOut>): BlockStateOut =>
  ({ state: "ACTIVE", block_id: "b", pending_proposal_ids: [], ...overrides });

describe("blockLabel", () => {
  it("says so when there is no block", () => {
    expect(blockLabel(null, undefined, false)).toBe("No active block");
  });

  it("reads 'Block 1 · Week 3' from the next session", () => {
    const next = { week: 3 } as NonNullable<BlockStateOut["next_session"]>;
    expect(blockLabel(block, state({ next_session: next }), false)).toBe("Block 1 · Week 3");
  });

  it("treats a workflow that never started as a state, not a failure", () => {
    expect(blockLabel(block, state({ state: "none" }), false)).toBe("Block 1 · not started");
  });

  it("flags a block awaiting a decision", () => {
    const next = { week: 4 } as NonNullable<BlockStateOut["next_session"]>;
    expect(
      blockLabel(block, state({ state: "AWAITING_RESCHEDULE_APPROVAL", next_session: next }), false),
    ).toBe("Block 1 · Week 4 · reschedule awaiting approval");
  });

  it("degrades when the state could not be read", () => {
    expect(blockLabel(block, undefined, true)).toBe("Block 1 · status unavailable");
  });

  it("shows just the block while the state is still loading", () => {
    expect(blockLabel(block, undefined, false)).toBe("Block 1");
  });
});
