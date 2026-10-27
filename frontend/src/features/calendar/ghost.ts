import type { ProposalOut } from "@/api/types";

/**
 * Reading a pending proposal's moves off its payload.
 *
 * `ProposalOut.payload` is `{ [key: string]: unknown }` in the generated schema, on
 * purpose: its shape depends on `kind`, and a union there would generate a type that
 * has to be narrowed before it can be read at all. So this is the one place in the
 * frontend that narrows an API shape by hand, and it is written to survive a payload
 * it does not recognise -- the calendar is a read-only screen and must never break on
 * one. Anything unreadable yields no moves, and the banner still appears.
 */

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * The kinds that move dates within the block on screen. A `restart` arrives as
 * `new_block` carrying a DIFFERENT `block_id`, so it must not draw a ghost over this
 * block's grid.
 */
const MOVING_KINDS: ReadonlySet<ProposalOut["kind"]> = new Set<ProposalOut["kind"]>([
  "reschedule",
  "weekday_change",
]);

/** One session's proposed move. `seq` joins it to the live session. */
export interface GhostMove {
  seq: number;
  from: string;
  to: string;
}

export interface GhostPlan {
  moves: readonly GhostMove[];
  /** `seq` → the move taking it away. Its real cell draws faded at the old date. */
  leaving: ReadonlyMap<number, GhostMove>;
  /** ISO date → the moves landing there. Each draws a dashed cell at the new date. */
  arriving: ReadonlyMap<string, readonly GhostMove[]>;
}

export const NO_GHOSTS: GhostPlan = { moves: [], leaving: new Map(), arriving: new Map() };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isoDate(value: unknown): string | null {
  return typeof value === "string" && ISO_DATE.test(value) ? value : null;
}

/**
 * The `action: "move"` entries of a payload's diff.
 *
 * `calendar_solver.to_changes` emits exactly one `move` per session whose date
 * actually changed, so this is the diff rather than a comparison of two schedules.
 * The other actions are deliberately ignored here: `skip` and `fold` do not put a
 * session anywhere new, and `repeat_week` carries no dates of its own.
 */
export function readMoves(payload: unknown): GhostMove[] {
  if (!isRecord(payload) || !Array.isArray(payload.changes)) return [];

  const moves: GhostMove[] = [];
  for (const change of payload.changes) {
    if (!isRecord(change) || change.action !== "move") continue;
    const from = isoDate(change.from_date);
    const to = isoDate(change.to_date);
    if (typeof change.seq !== "number" || from === null || to === null || from === to) continue;
    moves.push({ seq: change.seq, from, to });
  }
  return moves;
}

/** The moves indexed both ways round, so a cell can ask about itself in one lookup. */
export function ghostPlan(payload: unknown): GhostPlan {
  const moves = readMoves(payload);
  if (moves.length === 0) return NO_GHOSTS;

  const leaving = new Map<number, GhostMove>();
  const arriving = new Map<string, GhostMove[]>();
  for (const move of moves) {
    leaving.set(move.seq, move);
    const onDay = arriving.get(move.to) ?? [];
    onDay.push(move);
    arriving.set(move.to, onDay);
  }
  return { moves, leaving, arriving };
}

/**
 * The pending proposals that concern this block, newest first as the API returns
 * them. These drive the banner, whether or not any of them turns out to be drawable.
 */
export function pendingForBlock(
  proposals: readonly ProposalOut[] | undefined,
  blockId: string | null,
): ProposalOut[] {
  if (proposals === undefined || blockId === null) return [];
  return proposals.filter((proposal) => proposal.block_id === blockId);
}

/**
 * The one proposal the overlay draws: the newest pending one for this block that
 * both could move dates and actually parses into moves.
 *
 * ADR-0010's `in_week_move` can offer up to three options and spec 19 is where they
 * are chosen between; until then the overlay shows the newest, and the banner is what
 * sends the athlete to the proposal itself.
 */
export function overlayFor(
  proposals: readonly ProposalOut[] | undefined,
  blockId: string | null,
): { proposal: ProposalOut; plan: GhostPlan } | null {
  for (const proposal of pendingForBlock(proposals, blockId)) {
    if (!MOVING_KINDS.has(proposal.kind)) continue;
    const plan = ghostPlan(proposal.payload);
    if (plan.moves.length > 0) return { proposal, plan };
  }
  return null;
}
