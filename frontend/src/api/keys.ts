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
  },
  templates: {
    all: ["templates"] as const,
    list: () => ["templates", "list"] as const,
    detail: (templateId: string) => ["templates", "detail", templateId] as const,
  },
  blocks: {
    all: ["blocks"] as const,
    current: (athleteId: string) => ["blocks", "current", athleteId] as const,
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
};

export interface SessionRange {
  from?: string;
  to?: string;
}

export type ProposalStatus = "pending" | "approved" | "rejected" | "superseded";
