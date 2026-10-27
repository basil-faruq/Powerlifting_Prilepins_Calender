import { queryOptions, useQuery } from "@tanstack/react-query";
import { api } from "../client";
import { keys } from "../keys";
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
