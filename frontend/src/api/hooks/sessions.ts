import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../client";
import { keys } from "../keys";
import type { SessionLogIn } from "../types";
import { unwrap } from "../unwrap";

/**
 * One session with its log. `SessionDetail` is `SessionOut` plus `log`, so this is
 * only worth fetching for the session the drawer is actually showing -- the grid
 * already has everything else from `useBlockSessions`.
 *
 * The note the athlete wrote lives on the log, not on the session, which is why the
 * drawer's "notes" come from here rather than from a call of their own.
 */
export const sessionOptions = (sessionId: string) =>
  queryOptions({
    queryKey: keys.sessions.detail(sessionId),
    queryFn: () =>
      unwrap(api.GET("/sessions/{session_id}", { params: { path: { session_id: sessionId } } })),
  });

export const useSession = (sessionId: string | null) =>
  useQuery({ ...sessionOptions(sessionId ?? ""), enabled: sessionId !== null });

/**
 * Log what was actually done.
 *
 * This is the input everything else is built on: the calendar's cell turns
 * `done`, a tested single becomes a new 1RM, the note is embedded for retrieval,
 * and the workflow is told the session happened. So it invalidates widely --
 * the grid's list and the drawer's copy are different keys, and a tested single
 * changes the athlete's maxes and the chart that draws them.
 *
 * The route commits before it signals, and answers 201 even if the workflow is
 * unreachable (ADR-0017 rule 11): the log is the athlete's record, and nothing
 * about the planner being down should lose it.
 */
export function useLogSession() {
  const queryClient = useQueryClient();
  return useMutation({
    // The form keeps the entries and offers a retry, rather than a toast that
    // disappears while the athlete is still holding the barbell.
    meta: { silent: true },
    mutationFn: ({ sessionId, body }: { sessionId: string; body: SessionLogIn }) =>
      unwrap(
        api.POST("/sessions/{session_id}/log", {
          params: { path: { session_id: sessionId } },
          body,
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.sessions.all }),
        queryClient.invalidateQueries({ queryKey: keys.blocks.all }),
        queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
      ]);
    },
  });
}
