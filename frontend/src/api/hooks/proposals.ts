import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../client";
import { keys, type ProposalStatus } from "../keys";
import { unwrap } from "../unwrap";

/**
 * `status` is required on purpose. The route defaults an omitted status to
 * "pending", so leaving it out does NOT mean "all" -- and a query string has no way
 * to send "all". Every caller states which status it wants.
 */
export const proposalsOptions = (athleteId: string, status: ProposalStatus) =>
  queryOptions({
    queryKey: keys.proposals.list(athleteId, status),
    queryFn: () =>
      unwrap(api.GET("/proposals", { params: { query: { athlete_id: athleteId, status } } })),
  });

/** One proposal, read fresh. Used to wait for a decision a workflow is about to make. */
export const fetchProposal = (proposalId: string) =>
  unwrap(api.GET("/proposals/{proposal_id}", { params: { path: { proposal_id: proposalId } } }));

export const useProposals = (status: ProposalStatus, athleteId: string | null) =>
  useQuery({ ...proposalsOptions(athleteId ?? "", status), enabled: athleteId !== null });

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
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.blocks.all }),
        queryClient.invalidateQueries({ queryKey: keys.proposals.all }),
        queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
      ]);
    },
  });
}
