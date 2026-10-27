import { Box, Stack, Text } from "@chakra-ui/react";
import type { SessionOut } from "@/api/types";
import { addDays } from "@/lib/date";
import { formatDayMonth } from "@/lib/format";
import { WEEKDAYS } from "@/lib/lifts";
import type { GhostPlan } from "./ghost";
import { WEEKDAY_COLUMNS, type WeekRow } from "./model";
import { SessionCell } from "./SessionCell";

/**
 * Plain CSS grid, no Gantt library (spec 18 §9): a label column and seven day
 * columns. Every row uses the same template, so the columns line up down the page.
 */
export const GRID_COLUMNS = "4.5rem repeat(7, minmax(0, 1fr))";

interface Props {
  row: WeekRow;
  /** Today in the athlete's timezone. */
  today: string;
  isCurrent: boolean;
  /** The weekdays the athlete trains on, for the subtle column highlight. */
  trainingDays: ReadonlySet<number>;
  ghosts: GhostPlan;
  showGhosts: boolean;
  /** Every session of the block by `seq`, so a ghost arriving can be drawn in full. */
  sessionsBySeq: ReadonlyMap<number, SessionOut>;
  onOpen: (session: SessionOut) => void;
}

/** One calendar week: the week label, then Monday to Sunday. */
export function CalendarWeekRow({
  row,
  today,
  isCurrent,
  trainingDays,
  ghosts,
  showGhosts,
  sessionsBySeq,
  onOpen,
}: Props) {
  return (
    <Box
      display="grid"
      gridTemplateColumns={GRID_COLUMNS}
      gap="1"
      px="1"
      py="1"
      rounded="md"
      bg={isCurrent ? "bg.emphasized" : undefined}
    >
      <Stack gap="0" minW="0" justify="center">
        <Text fontSize="sm" fontWeight="bold">
          W{row.week}
        </Text>
        <Text fontSize="2xs" color="fg.muted">
          {formatDayMonth(row.monday)}
        </Text>
      </Stack>

      {WEEKDAY_COLUMNS.map((weekday) => {
        const date = addDays(row.monday, weekday);
        const sessions = row.byWeekday.get(weekday) ?? [];
        const arriving = showGhosts ? (ghosts.arriving.get(date) ?? []) : [];

        return (
          <Stack
            key={weekday}
            gap="1"
            minW="0"
            minH="12"
            p="0.5"
            rounded="sm"
            // A training day is tinted so the week's shape is readable even where no
            // session sits; the weekday name in the header is what names it.
            bg={trainingDays.has(weekday) ? "bg.subtle" : undefined}
            aria-label={`${WEEKDAYS[weekday]} ${formatDayMonth(date)}`}
          >
            {sessions.map((session) => (
              <SessionCell
                key={session.id}
                session={session}
                today={today}
                isToday={session.date === today}
                ghost={showGhosts && ghosts.leaving.has(session.seq) ? "leaving" : null}
                onOpen={onOpen}
              />
            ))}

            {arriving.flatMap((move) => {
              const session = sessionsBySeq.get(move.seq);
              return session === undefined
                ? []
                : [
                    <SessionCell
                      key={`ghost-${move.seq}`}
                      session={session}
                      today={today}
                      ghost="arriving"
                      onOpen={onOpen}
                    />,
                  ];
            })}
          </Stack>
        );
      })}
    </Box>
  );
}
