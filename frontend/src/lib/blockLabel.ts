import type { BlockOut, BlockStateOut } from "@/api/types";

// `BlockStateOut.state` is a plain string in the schema. These are the values the
// backend actually sends (workflows/contracts.py), plus the two the API adds.
const STATE_NOTE: Record<string, string> = {
  none: "not started",
  unknown: "status unavailable",
  ACTIVE: "",
  AWAITING_RESCHEDULE_APPROVAL: "reschedule awaiting approval",
  AWAITING_ROLLOVER_APPROVAL: "next block awaiting approval",
  PAUSED: "paused",
  DONE: "done",
  ERROR: "needs attention",
};

/** The header's "Block 1 · Week 3". */
export function blockLabel(
  block: BlockOut | null,
  state: BlockStateOut | undefined,
  stateFailed: boolean,
): string {
  if (block === null) return "No active block";
  const base = `Block ${block.number}`;
  if (stateFailed) return `${base} · ${STATE_NOTE.unknown}`;
  if (state === undefined) return base;

  const week = state.next_session?.week;
  const note = STATE_NOTE[state.state] ?? state.state.toLowerCase();
  return [base, week !== undefined ? `Week ${week}` : null, note || null]
    .filter((part): part is string => part !== null)
    .join(" · ");
}
