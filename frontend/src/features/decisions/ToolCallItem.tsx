import { Accordion, Badge, HStack, Stack, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { formatDuration } from "@/lib/format";
import { JsonView } from "./JsonView";
import { toolLabel, type ToolCallView } from "./model";

/**
 * One recorded tool call: what it was given, what it reported, how long it took.
 *
 * An `Accordion.Item`, not its own root -- the drawer owns the list so that two calls
 * can be open at once. Every part of the envelope it HAS is rendered, including the
 * `solver_stats` and `validation` the drawer also summarises above: that summary knows
 * the tools this build knows, and this is the raw record that stays readable when a
 * tool it has never heard of turns up carrying stats.
 */
export function ToolCallItem({ call }: { call: ToolCallView }) {
  // Unique even when the same tool ran twice in one turn.
  const value = `${String(call.index)}-${call.name}`;

  return (
    <Accordion.Item value={value}>
      <Accordion.ItemTrigger>
        <HStack gap="2" flex="1" minW="0" textAlign="start" wrap="wrap">
          <Text fontWeight="medium">{toolLabel(call.name)}</Text>
          <Text fontSize="xs" color="fg.muted" fontFamily="mono">
            {call.name}
          </Text>
          {call.error !== null && (
            <Badge colorPalette="orange" variant="subtle">
              raised
            </Badge>
          )}
        </HStack>
        {call.durationMs !== null && (
          <Text fontSize="xs" color="fg.muted" flexShrink="0">
            {formatDuration(call.durationMs)}
          </Text>
        )}
        <Accordion.ItemIndicator />
      </Accordion.ItemTrigger>

      <Accordion.ItemContent>
        <Accordion.ItemBody>
          <Stack gap="3" minW="0">
            {call.error !== null && (
              <Stack gap="1" minW="0">
                <Heading>It raised</Heading>
                {/* The server already truncates this: a database error can carry the
                    SQL parameters, which for a note is the note. */}
                <Text fontSize="xs" fontFamily="mono" whiteSpace="pre-wrap" wordBreak="break-word">
                  {call.error}
                </Text>
                <Text fontSize="xs" color="fg.muted">
                  It raised before producing a summary, so there is no output below.
                </Text>
              </Stack>
            )}

            <Section title="Input">
              <JsonView value={call.input} label={`${call.name} input`} />
            </Section>

            <Section title="Output summary">
              <JsonView value={call.outputSummary} label={`${call.name} output summary`} />
            </Section>

            {call.solverStats !== null && (
              <Section title="Solver stats">
                <JsonView value={call.solverStats} label={`${call.name} solver stats`} />
              </Section>
            )}

            {call.validation !== null && (
              <Section title="Validator">
                <JsonView value={call.validation} label={`${call.name} validation`} />
              </Section>
            )}
          </Stack>
        </Accordion.ItemBody>
      </Accordion.ItemContent>
    </Accordion.Item>
  );
}

function Heading({ children }: { children: ReactNode }) {
  return (
    <Text fontSize="xs" fontWeight="semibold" color="fg.muted" textTransform="uppercase">
      {children}
    </Text>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Stack gap="1" minW="0">
      <Heading>{title}</Heading>
      {children}
    </Stack>
  );
}
