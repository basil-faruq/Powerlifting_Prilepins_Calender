import { Badge, Button, HStack, Stack, Text } from "@chakra-ui/react";
import { useMemo } from "react";
import type { DecisionOut } from "@/api/types";
import { RouteBadge } from "@/components/RouteBadge";
import { formatDateTime } from "@/lib/format";
import { badgeProps, decisionTitle, outcomeView, readRouter, readToolCalls, rowLabel } from "./model";

interface Props {
  decision: DecisionOut;
  /** Drawn as selected while its drawer is open. */
  selected: boolean;
  onOpen: (decisionId: string) => void;
}

/**
 * One decision in the timeline: when, which route, from where, about what, and how it
 * ended.
 *
 * ONE DOM tree, with the layout difference carried by a responsive flex direction --
 * not the two-layouts-in-the-DOM idiom `BlockHistoryTable` and `BlockCalendar` use.
 * That idiom earns its cost where the two presentations are genuinely different
 * components, a table against a stack of cards. A row here is a timestamp, two badges
 * and a sentence; duplicating it would put a hundred rows in the DOM for fifty
 * decisions and make every figure in the list ambiguous to a test query.
 */
export function DecisionRow({ decision, selected, onOpen }: Props) {
  // Only for the badge's clarify detail, which lives in the recorded router call
  // rather than in a column. Memoised because the list re-renders on every filter.
  const router = useMemo(() => readRouter(readToolCalls(decision)), [decision]);
  const outcome = outcomeView(decision);

  return (
    <Button
      variant={selected ? "subtle" : "ghost"}
      onClick={() => onOpen(decision.id)}
      aria-label={rowLabel(decision, outcome)}
      h="auto"
      py="3"
      px="3"
      w="full"
      justifyContent="flex-start"
      textAlign="start"
      whiteSpace="normal"
    >
      <Stack
        direction={{ base: "column", md: "row" }}
        align={{ base: "start", md: "center" }}
        gap={{ base: "1", md: "3" }}
        w="full"
        minW="0"
      >
        <Text fontSize="xs" color="fg.muted" flexShrink="0" minW={{ md: "32" }}>
          {formatDateTime(decision.created_at)}
        </Text>
        <HStack gap="2" flexShrink="0" wrap="wrap">
          <RouteBadge {...badgeProps(decision, router)} />
          <Badge variant="outline">{decision.source}</Badge>
        </HStack>
        <Text flex="1" minW="0" truncate>
          {decisionTitle(decision)}
        </Text>
        <Badge colorPalette={outcome.palette} variant="subtle" flexShrink="0">
          {outcome.label}
        </Badge>
      </Stack>
    </Button>
  );
}
