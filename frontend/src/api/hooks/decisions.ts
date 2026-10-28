import { keepPreviousData, queryOptions, useQuery } from "@tanstack/react-query";
import { api } from "../client";
import { keys, type DecisionFilters } from "../keys";
import type { DecisionOut } from "../types";
import { unwrap } from "../unwrap";

/**
 * The decision trail (spec 21), read-only in both directions: there is no mutation
 * on `/decisions` at all, which `test_decisions_are_read_only` asserts server-side.
 *
 * `athlete_id` is never sent. The route joins it through `chat_message`, so passing
 * it would hide every workflow decision -- see `DecisionFilters`.
 */

/** The route's own default. It caps at 200 and offers no cursor, so this is the page. */
export const PAGE_LIMIT = 50;

export const decisionsOptions = (filters: DecisionFilters) =>
  queryOptions({
    queryKey: keys.decisions.list(filters),
    queryFn: () =>
      unwrap(
        api.GET("/decisions", {
          // Built by spreading rather than by passing nulls: an omitted parameter
          // means "every value" to the route, where a literal null in the query
          // string is a 422.
          params: {
            query: {
              ...(filters.route ? { route: filters.route } : {}),
              ...(filters.outcome ? { outcome: filters.outcome } : {}),
              ...(filters.source ? { source: filters.source } : {}),
              limit: PAGE_LIMIT,
            },
          },
        }),
      ),
    // Keep the page on screen while a changed filter loads, so the list does not
    // blink through its empty state every time a select moves.
    placeholderData: keepPreviousData,
  });

export const decisionOptions = (decisionId: string) =>
  queryOptions({
    queryKey: keys.decisions.detail(decisionId),
    queryFn: () =>
      unwrap(api.GET("/decisions/{decision_id}", { params: { path: { decision_id: decisionId } } })),
  });

export const useDecisions = (filters: DecisionFilters) => useQuery(decisionsOptions(filters));

/**
 * One decision, for a `?id=` that arrived without its list row -- a link from chat, a
 * bookmark, or a decision older than the newest page.
 *
 * Seeded with the list's own copy when there is one. The list returns the FULL shape,
 * `tool_calls` included, so a row already on screen opens with no request at all.
 */
export const useDecision = (decisionId: string | null, initialData?: DecisionOut) =>
  useQuery({ ...decisionOptions(decisionId ?? ""), enabled: decisionId !== null, initialData });
