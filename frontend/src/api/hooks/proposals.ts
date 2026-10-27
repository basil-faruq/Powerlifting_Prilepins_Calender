import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../client";
import { keys, type ProposalStatus } from "../keys";
import type { ProposalOut, SessionChange } from "../types";
import { unwrap } from "../unwrap";

/**
 * How often a pending list re-reads itself.
 *
 * Nothing pushes to this frontend. A proposal the WORKFLOW raised -- a session that
 * went unlogged while the athlete was away from the screen -- would otherwise appear
 * only on a reload, so spec 19 asks for half a minute.
 */
const PENDING_POLL_MS = 30_000;

/**
 * `status` is required on purpose. The route defaults an omitted status to
 * "pending", so leaving it out does NOT mean "all" -- and a query string has no way
 * to send "all". Every caller states which status it wants.
 *
 * Only the pending list polls. A decided proposal cannot change back, so re-reading
 * approved or rejected ones would be traffic that never finds anything.
 */
export const proposalsOptions = (athleteId: string, status: ProposalStatus) =>
  queryOptions({
    queryKey: keys.proposals.list(athleteId, status),
    queryFn: () =>
      unwrap(api.GET("/proposals", { params: { query: { athlete_id: athleteId, status } } })),
    ...(status === "pending"
      ? { refetchInterval: PENDING_POLL_MS, refetchOnWindowFocus: true }
      : {}),
  });

/**
 * One proposal as a query, for a card that must stay live while it waits.
 *
 * It polls on its own account. A card drawn in the chat transcript is not in the
 * pending list the tray refreshes, so without this it would sit there reading
 * `pending` after a sibling option was approved in another tab, or after the
 * workflow superseded it.
 */
export const proposalOptions = (proposalId: string) =>
  queryOptions({
    queryKey: keys.proposals.detail(proposalId),
    queryFn: () => fetchProposal(proposalId),
    refetchInterval: (query) =>
      query.state.data?.status === "pending" ? PENDING_POLL_MS : false,
  });

/** One proposal, read fresh. Used to wait for a decision a workflow is about to make. */
export const fetchProposal = (proposalId: string) =>
  unwrap(api.GET("/proposals/{proposal_id}", { params: { path: { proposal_id: proposalId } } }));

export const useProposals = (status: ProposalStatus, athleteId: string | null) =>
  useQuery({ ...proposalsOptions(athleteId ?? "", status), enabled: athleteId !== null });

/**
 * One proposal, seeded with the copy the caller already has.
 *
 * A card drawn from a chat reply starts with that reply's own `ProposalOut`, so it
 * renders immediately; the query then keeps it current as the tray polls around it.
 */
export const useProposal = (proposalId: string, initialData?: ProposalOut) =>
  useQuery({ ...proposalOptions(proposalId), initialData });

/**
 * Approval is the one write path (ADR-0007). It changes the block, its sessions,
 * the proposal list and -- for a first approval -- the athlete's weekday lock, so it
 * invalidates all of them rather than patching any.
 */
export function useApproveProposal({ silent = false }: { silent?: boolean } = {}) {
  const queryClient = useQueryClient();
  return useMutation({
    // A caller that explains the failure itself (spec 17: 503 vs "already approved")
    // opts out of the generic toast.
    meta: { silent },
    mutationFn: (proposalId: string) =>
      unwrap(
        api.POST("/proposals/{proposal_id}/approve", {
          params: { path: { proposal_id: proposalId } },
        }),
      ),
    onSettled: () => invalidateAfterDecision(queryClient),
  });
}

/**
 * Everything an approval can have changed.
 *
 * Deciding one proposal supersedes its siblings, so the whole proposal list goes --
 * not just the one decided. `sessions.all` is listed separately from `blocks.all`
 * because the drawer holds a session on its own key, and a reschedule moves it.
 */
function invalidateAfterDecision(queryClient: ReturnType<typeof useQueryClient>) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: keys.blocks.all }),
    queryClient.invalidateQueries({ queryKey: keys.proposals.all }),
    queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
    queryClient.invalidateQueries({ queryKey: keys.sessions.all }),
  ]);
}

/**
 * Rejecting is a decision too: it supersedes nothing, but it takes this proposal out
 * of the pending list the tray and the badge both read.
 */
export function useRejectProposal({ silent = false }: { silent?: boolean } = {}) {
  const queryClient = useQueryClient();
  return useMutation({
    meta: { silent },
    mutationFn: ({ proposalId, reason }: { proposalId: string; reason?: string }) =>
      unwrap(
        api.POST("/proposals/{proposal_id}/reject", {
          params: { path: { proposal_id: proposalId } },
          body: { reason: reason?.trim() ? reason.trim() : null },
        }),
      ),
    onSettled: () => invalidateAfterDecision(queryClient),
  });
}

/**
 * Editing re-plans and re-validates, and decides nothing.
 *
 * The proposal comes back still pending, with fresh warnings and a reason recording
 * the edit. A plan that no longer validates is a 422 and NOTHING is stored (ADR-0007
 * mod 7), so the card the athlete is looking at is still approvable as it was -- which
 * is why this neither invalidates on error nor toasts: the dialog shows the
 * violations itself.
 */
export function useEditProposal() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: { silent: true },
    mutationFn: ({ proposalId, changes }: { proposalId: string; changes: SessionChange[] }) =>
      unwrap(
        api.PATCH("/proposals/{proposal_id}", {
          params: { path: { proposal_id: proposalId } },
          body: { changes },
        }),
      ),
    onSuccess: async (proposal) => {
      if (proposal !== undefined) {
        queryClient.setQueryData(keys.proposals.detail(proposal.id), proposal);
      }
      await queryClient.invalidateQueries({ queryKey: keys.proposals.all });
    },
  });
}
