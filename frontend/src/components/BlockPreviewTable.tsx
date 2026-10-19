import { Badge, Box, HStack, Stack, Table, Text } from "@chakra-ui/react";
import type { DraftSession } from "@/api/types";
import { formatDate, formatKg } from "@/lib/format";
import { LiftBadge } from "./LiftBadge";

/**
 * A block as weeks down and training days across, one session per cell. Reused by
 * the setup wizard's preview and, in spec 19, by proposals awaiting approval.
 *
 * The weights are read as the server computed them; nothing here derives one.
 */
export function BlockPreviewTable({ sessions }: { sessions: readonly DraftSession[] }) {
  const weeks = [...new Set(sessions.map((s) => s.week))].sort((a, b) => a - b);
  const slots = [...new Set(sessions.map((s) => s.slot))].sort((a, b) => a - b);
  const bySlot = new Map(sessions.map((s) => [`${s.week}:${s.slot}`, s] as const));

  return (
    // The table scrolls inside its own box, so a wide plan never widens the page.
    <Box overflowX="auto" borderWidth="1px" rounded="md">
      <Table.Root size="sm" aria-label="Block preview">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Week</Table.ColumnHeader>
            {slots.map((slot) => (
              <Table.ColumnHeader key={slot}>Day {slot}</Table.ColumnHeader>
            ))}
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {weeks.map((week) => (
            <Table.Row key={week}>
              <Table.Cell fontWeight="semibold">{week}</Table.Cell>
              {slots.map((slot) => {
                const session = bySlot.get(`${week}:${slot}`);
                return (
                  <Table.Cell key={slot} verticalAlign="top" minW="40">
                    {session && (
                      <Stack gap="1">
                        <Text fontSize="xs" color="fg.muted">
                          {formatDate(session.date)}
                        </Text>
                        {session.lifts.map((lift, i) => (
                          <HStack key={`${lift.lift_code}-${i}`} gap="2" wrap="wrap">
                            <LiftBadge lift={lift.lift_code} />
                            <Text fontSize="sm">
                              {lift.sets}×{lift.reps} @ {formatKg(lift.weight_kg)}
                            </Text>
                            {lift.is_test && (
                              <Badge colorPalette="red" variant="outline">
                                test
                              </Badge>
                            )}
                          </HStack>
                        ))}
                      </Stack>
                    )}
                  </Table.Cell>
                );
              })}
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
    </Box>
  );
}
