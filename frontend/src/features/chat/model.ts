import type { ChatResponse } from "@/api/types";

/**
 * A question whose answer has not arrived, or never will.
 *
 * The transcript itself is the conversation's cached message list: the send mutation
 * writes both messages into it, so a turn is never held in two places. What a turn
 * leaves behind HERE is only what the history route does not carry -- the route
 * badge, the proposals and the simulation -- plus the two states no message exists
 * for yet, in flight and failed.
 */
export interface OpenTurn {
  id: string;
  question: string;
  error: { message: string; retryable: boolean } | null;
}

/**
 * The extra a turn produced, keyed on the decision that produced it.
 *
 * `ChatMessageOut.decision_id` is on both the question and the reply, which is what
 * lets a message read back from the cache be matched to the turn it came from. A
 * turn from an earlier session has no entry and renders as plain text -- deliberate:
 * the alternative was widening the history route, which spec 19 rules out.
 */
export type TurnDetails = ReadonlyMap<string, ChatResponse>;

export const SUGGESTED_PROMPTS = [
  "What's on this week?",
  "I can't train tomorrow",
  "What if I'm sick for a week in week 6?",
] as const;
