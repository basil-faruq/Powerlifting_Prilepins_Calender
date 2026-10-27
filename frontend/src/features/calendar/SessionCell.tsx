import { Badge, chakra, HStack, Stack, Text } from "@chakra-ui/react";
import type { SessionOut } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { formatDate, formatDayMonth, formatKg } from "@/lib/format";
import { LIFT_LABEL } from "@/lib/lifts";
import { shadeBg, shadeFg, STEP_LABEL, topStep } from "./intensity";
import { cellState, knownLifts } from "./model";

/**
 * A real `<button>`, so the cell is reachable by keyboard and announced as actionable.
 * `Box as="button"` would not accept `type` or `disabled` under Chakra v3's
 * polymorphic typing.
 */
const CellButton = chakra("button");

/** How the session is drawn relative to a pending proposal (spec 18 §4C). */
export type GhostRole = "leaving" | "arriving";

interface Props {
  session: SessionOut;
  /** Today in the athlete's timezone, so "overdue" means what it means to them. */
  today: string;
  /** Outlined when the session sits on today. */
  isToday?: boolean;
  ghost?: GhostRole | null;
  onOpen?: (session: SessionOut) => void;
}

/**
 * One session in the grid: its lifts, the weight for each, and what became of it.
 *
 * Colour is never the only signal (spec 18 §11). The shade carries the intensity and
 * is backed by the printed kg; every status carries a glyph *and* a word. There is no
 * icon library in this project, so the glyphs are characters, each with a text label
 * beside it rather than an `aria-label` doing the work alone.
 */
export function SessionCell({ session, today, isToday = false, ghost = null, onOpen }: Props) {
  const state = cellState(session, today);
  const step = topStep(session.lifts);
  const lifts = knownLifts(session.lifts);

  const marks = statusMarks(state);
  const movedTitle = state.movedFrom === null ? undefined : `Moved from ${formatDate(state.movedFrom)}`;

  return (
    <CellButton
      type="button"
      onClick={onOpen === undefined ? undefined : () => onOpen(session)}
      disabled={onOpen === undefined}
      textAlign="start"
      w="full"
      minW="0"
      p="1.5"
      rounded="md"
      bg={shadeBg(step)}
      color={shadeFg(step)}
      // A ghost arriving is dashed at its proposed date; the real cell it would leave
      // is faded where it still sits. The outline for today wins either way.
      borderWidth={isToday ? "2px" : "1px"}
      borderStyle={ghost === "arriving" || state.overdue ? "dashed" : "solid"}
      borderColor={isToday ? "fg" : marks.border}
      opacity={ghost === "leaving" ? 0.4 : ghost === "arriving" ? 0.75 : 1}
      textDecoration={session.status === "skipped" ? "line-through" : undefined}
      filter={session.status === "skipped" ? "grayscale(1)" : undefined}
      title={movedTitle}
      aria-label={label(session, state, step === null ? null : STEP_LABEL[step], ghost)}
    >
      <Stack gap="1">
        <HStack gap="1" justify="space-between">
          <Text fontSize="xs" fontWeight="medium">
            {formatDayMonth(session.date)}
          </Text>
          {marks.glyph !== null && (
            <Text fontSize="xs" aria-hidden="true">
              {marks.glyph}
            </Text>
          )}
        </HStack>

        {lifts.map((lift, i) => (
          <HStack key={`${lift.lift_code}-${i}`} gap="1" wrap="wrap">
            <LiftBadge lift={lift.lift_code} short />
            <Text fontSize="xs">
              {lift.sets}×{lift.reps} @ {formatKg(lift.weight_kg)}
            </Text>
          </HStack>
        ))}

        <HStack gap="1" wrap="wrap">
          {state.isTest && (
            <Badge size="xs" colorPalette="red" variant="outline">
              TEST
            </Badge>
          )}
          {marks.word !== null && (
            <Text fontSize="2xs" fontWeight="medium">
              {marks.word}
            </Text>
          )}
          {ghost === "arriving" && (
            <Text fontSize="2xs" fontWeight="medium">
              proposed
            </Text>
          )}
        </HStack>
      </Stack>
    </CellButton>
  );
}

interface Marks {
  glyph: string | null;
  word: string | null;
  border: string;
}

/**
 * The glyph, the word and the border for a session's state.
 *
 * `overdue` is checked before `moved`, because what matters most about a session
 * whose date has gone by unlogged is that nobody logged it. It is deliberately not
 * "missed": ADR-0017 says `missed` is never written and means *written off*, which
 * only a human does.
 */
function statusMarks(state: ReturnType<typeof cellState>): Marks {
  if (state.status === "done") return { glyph: "✓", word: "done", border: "border" };
  if (state.status === "missed") return { glyph: "✕", word: "missed", border: "red.solid" };
  if (state.status === "skipped") return { glyph: "—", word: "skipped", border: "border" };
  if (state.overdue) return { glyph: "!", word: "not logged", border: "yellow.solid" };
  if (state.status === "moved") return { glyph: "→", word: "moved", border: "border" };
  return { glyph: null, word: null, border: "border" };
}

/** One sentence naming the whole cell, for anyone not reading the colours. */
function label(
  session: SessionOut,
  state: ReturnType<typeof cellState>,
  stepLabel: string | null,
  ghost: GhostRole | null,
): string {
  const parts = [`Week ${session.week}, ${formatDate(session.date)}`];
  for (const lift of knownLifts(session.lifts)) {
    parts.push(`${LIFT_LABEL[lift.lift_code]} ${lift.sets}×${lift.reps} at ${formatKg(lift.weight_kg)}`);
  }
  if (stepLabel !== null) parts.push(`top intensity ${stepLabel}`);
  if (state.isTest) parts.push("test single");
  const marks = statusMarks(state);
  if (marks.word !== null) parts.push(marks.word);
  if (state.movedFrom !== null) parts.push(`moved from ${formatDate(state.movedFrom)}`);
  if (ghost === "arriving") parts.push("proposed new date");
  if (ghost === "leaving") parts.push("would move away");
  return parts.join(". ");
}
