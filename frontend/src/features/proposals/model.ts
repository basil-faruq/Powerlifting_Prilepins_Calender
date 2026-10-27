import type { LiftCode, ProposalOut } from "@/api/types";
import { isRestart, readChanges, readPlan, type PayloadChange } from "@/lib/proposalPayload";

/**
 * What a proposal is called, who raised it, and what it would do -- derived from the
 * proposal alone, so a card can be drawn without the block it belongs to.
 */

/** A restart is a `new_block` too, but it replaces a block rather than following one. */
export function titleFor(proposal: ProposalOut): string {
  switch (proposal.kind) {
    case "new_block":
      return isRestart(proposal) ? "Restart this block" : "New block";
    case "reschedule":
      return "Reschedule";
    case "weekday_change":
      return "Change training days";
  }
}

/**
 * Where a proposal came from.
 *
 * `workflow` says only that the block's own workflow raised it, and it has more than
 * one cause: a session whose date passed with nothing logged against it, or an
 * unavailability the athlete declared. A live run produced a restart from the second
 * of those, so naming either cause here would have been wrong half the time.
 *
 * It is never "missed" either: `missed` is never written and means written off by a
 * human (ADR-0017 rule 2). The card's reasons carry the actual cause, in the
 * server's own words.
 */
export function sourceLabel(proposal: ProposalOut): string {
  return proposal.source === "chat" ? "from chat" : "raised automatically";
}

/** One line of "what would change", ready to render. */
export interface ChangeLine {
  seq: number;
  action: PayloadChange["action"];
  lifts: LiftCode[];
  from: string | null;
  to: string | null;
  /** The sentence for actions that are not a plain move, e.g. "folded into week 4". */
  note: string | null;
}

/**
 * Every change a proposal would make, in the solver's own order.
 *
 * The lifts come from the payload's own plan, joined on `seq`: a reschedule carries
 * the whole resulting schedule, so "Squat + Bench" can be named without reading the
 * live block. A session the plan does not describe still gets a line, without lifts.
 */
export function changeLines(proposal: ProposalOut): ChangeLine[] {
  const liftsBySeq = new Map(readPlan(proposal.payload).map((s) => [s.seq, s.lifts]));

  return readChanges(proposal.payload).map((change) => {
    const lifts = (liftsBySeq.get(change.seq) ?? []).map((lift) => lift.lift_code);
    return {
      seq: change.seq,
      action: change.action,
      lifts: [...new Set(lifts)],
      from: change.from,
      to: change.to,
      note: noteFor(change),
    };
  });
}

function noteFor(change: PayloadChange): string | null {
  switch (change.action) {
    case "move":
      return null;
    case "skip":
      return "dropped from the block";
    case "fold":
      return change.intoSeq === null
        ? "folded into another session"
        : `folded into session ${change.intoSeq}`;
    case "repeat_week":
      return "this week runs again";
    case "create":
      return "added to the block";
    default:
      // An action this frontend predates. Say so rather than draw nothing.
      return change.action;
  }
}

/**
 * The options for one event, side by side.
 *
 * There is no group id on the wire. The solver answers one event with up to three
 * proposals that share a block and a kind, and approving any of them supersedes its
 * siblings (`_supersede_siblings`), so sharing both is what "the same event" means
 * here. Order is the API's, which is the solver's ranking by cost.
 */
export interface ProposalGroup {
  key: string;
  proposals: ProposalOut[];
}

export function groupOptions(proposals: readonly ProposalOut[]): ProposalGroup[] {
  const groups = new Map<string, ProposalOut[]>();
  for (const proposal of proposals) {
    const key = `${proposal.block_id}:${proposal.kind}`;
    const existing = groups.get(key);
    if (existing === undefined) groups.set(key, [proposal]);
    else existing.push(proposal);
  }
  return [...groups].map(([key, grouped]) => ({ key, proposals: grouped }));
}

/** A decided proposal is history: the card greys out and its buttons go. */
export function isDecided(proposal: ProposalOut): boolean {
  return proposal.status !== "pending";
}

/** What a decided card says instead of its buttons. */
export function decidedLabel(proposal: ProposalOut): string {
  switch (proposal.status) {
    case "approved":
      return "Approved";
    case "rejected":
      return "Rejected";
    case "superseded":
      return "Superseded by another option";
    default:
      return proposal.status;
  }
}
