import type { DecisionOutcome, DecisionRoute, DecisionSource } from "./types";

// One place for every query key, so a mutation can invalidate by prefix:
// `keys.blocks.all` covers a block, its state, its sessions and the current block.
export const keys = {
  athletes: {
    all: ["athletes"] as const,
    list: () => ["athletes", "list"] as const,
    detail: (athleteId: string) => ["athletes", "detail", athleteId] as const,
    weekdayCheck: (
      athleteId: string,
      templateId: string,
      lifts: readonly string[],
      slots: Readonly<Record<string, number>>,
    ) => ["athletes", "weekday-check", athleteId, templateId, [...lifts], slots] as const,
    // Every max ever recorded, which is what the progress chart draws. Under
    // `athletes` so that logging a tested single invalidates it with everything
    // else the athlete owns.
    oneRmHistory: (athleteId: string) => ["athletes", "one-rm-history", athleteId] as const,
  },
  templates: {
    all: ["templates"] as const,
    list: () => ["templates", "list"] as const,
    detail: (templateId: string) => ["templates", "detail", templateId] as const,
  },
  blocks: {
    all: ["blocks"] as const,
    current: (athleteId: string) => ["blocks", "current", athleteId] as const,
    // Every block, drafts included: the progress table's "what have I planned",
    // as against `current`'s "what am I training now".
    list: (athleteId: string) => ["blocks", "list", athleteId] as const,
    detail: (blockId: string) => ["blocks", "detail", blockId] as const,
    state: (blockId: string) => ["blocks", "state", blockId] as const,
    preview: (body: {
      athlete_id: string;
      template_id: string;
      lifts: readonly string[];
      start_date: string;
    }) => ["blocks", "preview", body.athlete_id, body.template_id, [...body.lifts], body.start_date] as const,
    sessions: (blockId: string, range: SessionRange) =>
      ["blocks", "sessions", blockId, range.from ?? null, range.to ?? null] as const,
  },
  // A session on its own, for the calendar's drawer. Separate from `blocks.sessions`
  // because spec 20's log mutation has to invalidate both: the drawer's copy, and the
  // grid's list that shows the cell as done.
  sessions: {
    all: ["sessions"] as const,
    detail: (sessionId: string) => ["sessions", "detail", sessionId] as const,
  },
  proposals: {
    all: ["proposals"] as const,
    list: (athleteId: string, status: ProposalStatus) =>
      ["proposals", "list", athleteId, status] as const,
    // One proposal, for a card that has to stay live while the tray around it polls.
    detail: (proposalId: string) => ["proposals", "detail", proposalId] as const,
  },
  conversations: {
    all: ["conversations"] as const,
    messages: (conversationId: string) => ["conversations", "messages", conversationId] as const,
  },
  // The decision trail, and one decision for a `?id=` deep link. `list` normalises
  // every absent filter to null so `{}` and `{ route: undefined }` are one entry
  // rather than two copies of the same page.
  decisions: {
    all: ["decisions"] as const,
    list: (filters: DecisionFilters) =>
      ["decisions", "list", filters.route ?? null, filters.outcome ?? null, filters.source ?? null] as const,
    detail: (decisionId: string) => ["decisions", "detail", decisionId] as const,
  },
};

export interface SessionRange {
  from?: string;
  to?: string;
}

export type ProposalStatus = "pending" | "approved" | "rejected" | "superseded";

/**
 * The three filters spec 21 §4A offers, as the route can take them. Null means
 * "every value", which the query string says by omitting the parameter.
 *
 * `athlete_id` is deliberately absent. The route joins it through `chat_message`
 * (`role = 'user'`), so any value at all hides every workflow decision -- which §12
 * requires to appear -- and one install has one athlete, so it buys nothing.
 *
 * Typed off the generated schema rather than hand-copied the way `ProposalStatus`
 * copies its four: six route literals is where a second copy starts to drift.
 */
export interface DecisionFilters {
  route?: DecisionRoute | null;
  outcome?: DecisionOutcome | null;
  source?: DecisionSource | null;
}
