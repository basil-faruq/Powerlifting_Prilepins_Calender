import { Badge } from "@chakra-ui/react";
import type { RouteKind } from "@/api/types";

const LABEL: Record<RouteKind, string> = {
  answer: "answer",
  plan: "plan",
  reschedule: "reschedule",
  simulate: "simulate",
  safety: "safety",
  clarify: "clarify",
};

const PALETTE: Record<RouteKind, string> = {
  answer: "gray",
  plan: "green",
  reschedule: "orange",
  simulate: "purple",
  safety: "red",
  clarify: "yellow",
};

interface Props {
  /** The six-value route vocabulary. `ChatResponse.route.kind` and `DecisionOut.route`
   *  are the same union from two schema sources. */
  kind: RouteKind;
  /** The deterministic check that decided, when one did. Non-null means no model was
   *  asked -- which is the fact this badge exists to make visible. */
  filterFired?: string | null;
  /** The router's structured output could not be read. */
  unparseable?: boolean;
  /** The router's own words for what it needed. */
  clarifyReason?: string | null;
}

/**
 * Which tool answered, and -- for `clarify` -- why.
 *
 * Shared rather than owned by chat, because the decision trail (spec 21) shows the
 * same badge. It takes the four values loose rather than a `Route` object on purpose:
 * on a decision they come from three different places -- `kind` and `filterFired` from
 * their own columns, the other two only from the recorded router tool call -- so there
 * is no `Route` to pass, and fabricating one would be inventing a wire shape to
 * satisfy a prop (spec 16 §11).
 */
export function RouteBadge({
  kind,
  filterFired = null,
  unparseable = false,
  clarifyReason = null,
}: Props) {
  const detail = clarifyDetail(kind, filterFired, unparseable, clarifyReason);
  return (
    <Badge colorPalette={PALETTE[kind]} variant="subtle" title={detail ?? undefined}>
      {LABEL[kind]}
      {detail !== null && ` · ${detail}`}
    </Badge>
  );
}

/**
 * `clarify` covers two unlike things (ADR-0015 mod 12): a hard filter stopped the
 * message before any model saw it, or the router could not tell what was being asked.
 * They mean different things to the reader, so the badge says which, and `title`
 * carries the detail rather than inventing a second badge for it.
 */
function clarifyDetail(
  kind: RouteKind,
  filterFired: string | null,
  unparseable: boolean,
  clarifyReason: string | null,
): string | null {
  if (kind !== "clarify") return null;
  // A fired filter is deterministic Python, not a model's judgement (CLAUDE.md #3).
  if (filterFired) return `stopped by the ${filterFired} check`;
  if (unparseable) return "could not be read";
  if (clarifyReason) return clarifyReason;
  return null;
}
