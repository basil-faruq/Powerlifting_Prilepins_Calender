import type { ProposalOut } from "@/api/types";
import { readChanges } from "@/lib/proposalPayload";

/**
 * Turning a pending proposal's moves into something the grid can draw.
 *
 * The narrowing itself lives in `lib/proposalPayload` -- spec 19 gave the proposal
 * cards the same problem, and one hand-written reader is the rule (ADR-0020). What is
 * left here is the calendar's own question: which moves land in which cell. Anything
 * unreadable yields no moves, and the banner still appears.
 */

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

/**
 * The `action: "move"` entries of a payload's diff, as cells to draw.
 *
 * `calendar_solver.to_changes` emits exactly one `move` per session whose date
 * actually changed. The other actions are deliberately ignored here: `skip` and
 * `fold` do not put a session anywhere new, and `repeat_week` carries no dates of its
 * own. A move that ends where it started draws nothing either.
 */
export function readMoves(payload: unknown): GhostMove[] {
  const moves: GhostMove[] = [];
  for (const change of readChanges(payload)) {
    if (change.action !== "move") continue;
    const { seq, from, to } = change;
    if (from === null || to === null || from === to) continue;
    moves.push({ seq, from, to });
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
