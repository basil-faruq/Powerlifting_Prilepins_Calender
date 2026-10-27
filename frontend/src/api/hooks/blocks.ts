import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../client";
import { ApiError } from "../errors";
import { keys, type SessionRange } from "../keys";
import type { BlockCreateBody, UnavailableIn } from "../types";
import { unwrap } from "../unwrap";

/**
 * The athlete's active or paused block, or `null`.
 *
 * The API answers 404 when there is none. That is an expected state -- a fresh
 * install, or a wizard whose block is still a draft -- so it is data here, not a
 * failure: the home route reads `null` as "go to setup".
 */
export const currentBlockOptions = (athleteId: string) =>
  queryOptions({
    queryKey: keys.blocks.current(athleteId),
    queryFn: async () => {
      try {
        return await unwrap(
          api.GET("/athletes/{athlete_id}/blocks/current", {
            params: { path: { athlete_id: athleteId } },
          }),
        );
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
  });

export const blockStateOptions = (blockId: string) =>
  queryOptions({
    queryKey: keys.blocks.state(blockId),
    queryFn: () =>
      unwrap(api.GET("/blocks/{block_id}/state", { params: { path: { block_id: blockId } } })),
  });

export const blockSessionsOptions = (blockId: string, range: SessionRange) =>
  queryOptions({
    queryKey: keys.blocks.sessions(blockId, range),
    queryFn: () =>
      unwrap(
        api.GET("/blocks/{block_id}/sessions", {
          params: { path: { block_id: blockId }, query: { from: range.from, to: range.to } },
        }),
      ),
  });

export const useCurrentBlock = (athleteId: string | null) =>
  useQuery({ ...currentBlockOptions(athleteId ?? ""), enabled: athleteId !== null });

/**
 * Where the block's workflow has got to. `state` is `"none"` for a block whose
 * workflow never started -- a 200, not an error (ADR-0017).
 */
export const useBlockState = (blockId: string | null) =>
  useQuery({ ...blockStateOptions(blockId ?? ""), enabled: blockId !== null });

export const useBlockSessions = (blockId: string | null, range: SessionRange = {}) =>
  useQuery({ ...blockSessionsOptions(blockId ?? "", range), enabled: blockId !== null });

/**
 * The plan and the verdict, written nowhere. A POST, but a read: it is a query so a
 * preview already shown is not recomputed on every render. `staleTime: 0` and
 * `gcTime: 0` because the verdict is only true for the day and the maxes and weekday
 * map it was computed from -- coming back to this step must ask again.
 */
export const blockPreviewOptions = (body: BlockCreateBody) =>
  queryOptions({
    queryKey: keys.blocks.preview(body),
    queryFn: () => unwrap(api.POST("/blocks/preview", { body })),
    staleTime: 0,
    gcTime: 0,
  });

/** A `draft` block and its pending `new_block` proposal. Writes no sessions. */
export function useCreateBlock() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: BlockCreateBody) => unwrap(api.POST("/blocks", { body })),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.blocks.all }),
        queryClient.invalidateQueries({ queryKey: keys.proposals.all }),
      ]),
  });
}

/**
 * Every block the athlete has planned, oldest first, each with the maxes it was
 * computed from. The progress table's content (spec 20 §4B).
 */
export const athleteBlocksOptions = (athleteId: string) =>
  queryOptions({
    queryKey: keys.blocks.list(athleteId),
    queryFn: () =>
      unwrap(api.GET("/athletes/{athlete_id}/blocks", { params: { path: { athlete_id: athleteId } } })),
  });

export const useAthleteBlocks = (athleteId: string | null) =>
  useQuery({ ...athleteBlocksOptions(athleteId ?? ""), enabled: athleteId !== null });

/**
 * Tell the block the athlete cannot train for a while.
 *
 * It is a signal, not a write: the route answers 202 and the repair arrives
 * later as pending proposals in Chat. How long the gap is decides what is
 * proposed -- a day moves a session, nine days can restart the block entirely
 * at reduced planning maxes (ADR-0010), which is why the dialog says so before
 * sending.
 *
 * `state: "unknown"` in the reply means the signal landed but no worker could
 * be asked what came of it (ADR-0017 rule 10). That is not a failure.
 */
export function useMarkUnavailable() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: { silent: true },
    mutationFn: ({ blockId, body }: { blockId: string; body: UnavailableIn }) =>
      unwrap(
        api.POST("/blocks/{block_id}/unavailable", {
          params: { path: { block_id: blockId } },
          body,
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.blocks.all }),
        queryClient.invalidateQueries({ queryKey: keys.proposals.all }),
      ]);
    },
  });
}
