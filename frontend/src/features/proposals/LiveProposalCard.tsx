import { useProposal } from "@/api/hooks/proposals";
import type { ProposalOut } from "@/api/types";
import { ProposalCard } from "./ProposalCard";

/**
 * A card backed by the proposal's own query rather than by a copy.
 *
 * A proposal arriving in a chat reply is a snapshot of the moment it was raised. Left
 * as that snapshot it would still read `pending` after being approved -- deciding it
 * invalidates the proposal queries, which a plain object does not listen to. Seeding
 * the query with the snapshot keeps the card instant AND current.
 *
 * "Current" means two things, and only one of them is the tray: deciding anything
 * invalidates `proposals.all`, which covers this key, and the query polls on its own
 * while the proposal is pending so that a sibling approved in another tab, or a
 * workflow superseding it, greys this card out too. The tray's own list poll does
 * NOT refresh it -- that is a different key.
 */
export function LiveProposalCard({ seed }: { seed: ProposalOut }) {
  const live = useProposal(seed.id, seed);
  return <ProposalCard proposal={live.data ?? seed} />;
}
