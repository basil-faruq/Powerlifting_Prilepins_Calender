import { Accordion, Badge, Box, Stack, Text } from "@chakra-ui/react";
import type { SessionOut } from "@/api/types";
import { addDays } from "@/lib/date";
import { formatDayMonth } from "@/lib/format";
import { WEEKDAYS } from "@/lib/lifts";
import type { GhostPlan } from "./ghost";
import { WEEKDAY_COLUMNS, type WeekRow } from "./model";
import { SessionCell } from "./SessionCell";

interface Props {
  rows: readonly WeekRow[];
  today: string;
  currentMonday: string | null;
  ghosts: GhostPlan;
  showGhosts: boolean;
  sessionsBySeq: ReadonlyMap<number, SessionOut>;
  onOpen: (session: SessionOut) => void;
}

/**
 * The phone layout (spec 18 §10): one card per week with its sessions stacked, the
 * current week open and the rest collapsed. Seven columns cannot be squeezed into
 * 375 px without either a sideways scroll or cells too small to read, so below the
 * `md` breakpoint the grid is abandoned rather than shrunk.
 */
export function WeekList({
  rows,
  today,
  currentMonday,
  ghosts,
  showGhosts,
  sessionsBySeq,
  onOpen,
}: Props) {
  return (
    <Accordion.Root collapsible defaultValue={currentMonday === null ? [] : [currentMonday]}>
      {rows.map((row) => {
        const count = [...row.byWeekday.values()].reduce((n, day) => n + day.length, 0);
        return (
          <Accordion.Item key={row.monday} value={row.monday}>
            <Accordion.ItemTrigger>
              <Stack gap="0" flex="1" minW="0" textAlign="start">
                <Text fontWeight="bold">Week {row.week}</Text>
                <Text fontSize="xs" color="fg.muted">
                  from {formatDayMonth(row.monday)} · {count} {count === 1 ? "session" : "sessions"}
                </Text>
              </Stack>
              {row.monday === currentMonday && (
                <Badge size="sm" variant="subtle">
                  this week
                </Badge>
              )}
              <Accordion.ItemIndicator />
            </Accordion.ItemTrigger>

            <Accordion.ItemContent>
              <Accordion.ItemBody>
                <Stack gap="2" minW="0">
                  {WEEKDAY_COLUMNS.flatMap((weekday) => {
                    const date = addDays(row.monday, weekday);
                    const sessions = row.byWeekday.get(weekday) ?? [];
                    const arriving = showGhosts ? (ghosts.arriving.get(date) ?? []) : [];
                    if (sessions.length === 0 && arriving.length === 0) return [];

                    return [
                      <Box key={weekday} minW="0">
                        <Text fontSize="xs" color="fg.muted" mb="1">
                          {WEEKDAYS[weekday]} {formatDayMonth(date)}
                        </Text>
                        <Stack gap="1">
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
                      </Box>,
                    ];
                  })}
                </Stack>
              </Accordion.ItemBody>
            </Accordion.ItemContent>
          </Accordion.Item>
        );
      })}
    </Accordion.Root>
  );
}
