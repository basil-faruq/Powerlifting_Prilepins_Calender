import { Badge, Box, HStack, Stack, Table, Text } from "@chakra-ui/react";
import type { LiftCode } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { formatDate, formatKg } from "@/lib/format";
import type { BlockRow } from "./model";

/**
 * What each block was planned against.
 *
 * The heading says "planned against" and not "1RM" on purpose: a block's
 * snapshot is what its weights were COMPUTED from, and a restart reduces it
 * deliberately (ADR-0010 mod 6). Read as a max it would look like the athlete
 * got weaker, which is exactly the misreading the footnote exists to prevent.
 */
export function BlockHistoryTable({
  rows,
  lifts,
}: {
  rows: readonly BlockRow[];
  lifts: readonly LiftCode[];
}) {
  if (rows.length === 0) {
    return (
      <Text color="fg.muted" fontSize="sm">
        No blocks yet.
      </Text>
    );
  }

  return (
    <Stack gap="2">
      {/* Wide table on a desktop; stacked cards on a phone, so nothing scrolls
          sideways. Both live in the DOM and are switched with `display`, the
          idiom the calendar and the shell already use. */}
      <Box display={{ base: "none", md: "block" }} overflowX="auto">
        <Table.Root size="sm" aria-label="Block history">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Block</Table.ColumnHeader>
              <Table.ColumnHeader>Started</Table.ColumnHeader>
              {lifts.map((lift) => (
                <Table.ColumnHeader key={lift}>
                  <LiftBadge lift={lift} short />
                </Table.ColumnHeader>
              ))}
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows.map((row) => (
              <Table.Row key={row.id}>
                <Table.Cell>
                  <HStack gap="2">
                    <Text>{row.number}</Text>
                    {row.status === "draft" && <Badge variant="subtle">draft</Badge>}
                  </HStack>
                </Table.Cell>
                <Table.Cell>{formatDate(row.startDate)}</Table.Cell>
                {lifts.map((lift) => {
                  const planned = row.planned.get(lift);
                  return (
                    <Table.Cell key={lift}>
                      {planned === undefined ? (
                        <Text color="fg.muted">—</Text>
                      ) : (
                        <HStack gap="2">
                          <Text>{formatKg(planned.kg)}</Text>
                          <Delta delta={planned.delta} />
                        </HStack>
                      )}
                    </Table.Cell>
                  );
                })}
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Box>

      <Stack display={{ base: "flex", md: "none" }} gap="3">
        {rows.map((row) => (
          <Box key={row.id} borderWidth="1px" rounded="md" p="3">
            <Stack gap="2">
              <HStack gap="2" wrap="wrap">
                <Text fontWeight="semibold">Block {row.number}</Text>
                {row.status === "draft" && <Badge variant="subtle">draft</Badge>}
                <Text fontSize="sm" color="fg.muted">
                  {formatDate(row.startDate)}
                </Text>
              </HStack>
              {lifts.map((lift) => {
                const planned = row.planned.get(lift);
                if (planned === undefined) return null;
                return (
                  <HStack key={lift} gap="2">
                    <LiftBadge lift={lift} short />
                    <Text fontSize="sm">{formatKg(planned.kg)}</Text>
                    <Delta delta={planned.delta} />
                  </HStack>
                );
              })}
            </Stack>
          </Box>
        ))}
      </Stack>

      <Text fontSize="xs" color="fg.muted">
        These are the maxes each block was <strong>planned against</strong>, not maxes you
        tested. A block restarted after time off is planned at reduced numbers on purpose,
        so a drop here is the plan being careful rather than you getting weaker.
      </Text>
    </Stack>
  );
}

/** The change from the previous block, as an exact decimal and a word. */
function Delta({ delta }: { delta: string | null }) {
  if (delta === null) return null;
  if (delta === "0") {
    return (
      <Text fontSize="xs" color="fg.muted">
        same
      </Text>
    );
  }
  const up = !delta.startsWith("-");
  return (
    <Text fontSize="xs" color={up ? "green.fg" : "red.fg"}>
      {up ? "+" : ""}
      {delta} kg
    </Text>
  );
}
