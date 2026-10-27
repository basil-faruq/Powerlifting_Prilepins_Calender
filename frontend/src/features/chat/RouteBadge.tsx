import { Badge } from "@chakra-ui/react";
import type { Route, RouteKind } from "@/api/types";

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

/**
 * Which tool answered, and -- for `clarify` -- why.
 *
 * `clarify` covers two unlike things (ADR-0015 mod 12): a hard filter stopped the
 * message before any model saw it, or the router could not tell what was being asked.
 * They mean different things to the reader, so the badge says which, and `title`
 * carries the detail rather than inventing a second badge for it.
 */
export function RouteBadge({ route }: { route: Route }) {
  const detail = clarifyDetail(route);
  return (
    <Badge colorPalette={PALETTE[route.kind]} variant="subtle" title={detail ?? undefined}>
      {LABEL[route.kind]}
      {detail !== null && ` · ${detail}`}
    </Badge>
  );
}

function clarifyDetail(route: Route): string | null {
  if (route.kind !== "clarify") return null;
  // A fired filter is deterministic Python, not a model's judgement (CLAUDE.md #3).
  if (route.filter_fired) return `stopped by the ${route.filter_fired} check`;
  if (route.unparseable) return "could not be read";
  if (route.clarify_reason) return route.clarify_reason;
  return null;
}
