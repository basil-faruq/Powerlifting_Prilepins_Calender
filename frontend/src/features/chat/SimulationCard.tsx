import { Box, Collapsible, Button, HStack, Stack, Text } from "@chakra-ui/react";
import type { LiftOutlook, SimulationOut } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { isLiftCode } from "@/lib/lifts";
import { formatDate, formatKg, formatPct } from "@/lib/format";

/**
 * What the simulator thinks, as a range and never as a number.
 *
 * Every figure here is a percentile band or a probability, with the assumptions that
 * produced it one click away (CLAUDE.md non-negotiable 8). Nothing is computed in
 * this component: the bands arrive as decimal strings and are printed as they came.
 */
export function SimulationCard({ simulation }: { simulation: SimulationOut }) {
  const { finish_date: finish, missed_sessions: missed } = simulation;

  return (
    <Box borderWidth="1px" rounded="md" p="3" bg="bg.subtle">
      <Stack gap="3">
        <Text fontWeight="semibold" fontSize="sm">
          If the block runs from here
        </Text>

        {/* The shell banners synthetic maxes for the whole app, but this is the card
            that projects FROM them: a projection of a development max is a projection
            of nothing real, and has to say so where it is read (ADR-0004). */}
        {simulation.has_synthetic_one_rms && (
          <Text fontSize="xs" color="fg.muted">
            Projected from synthetic 1RMs seeded for local testing, so these bands
            describe no real athlete.
          </Text>
        )}

        <Stack gap="1" fontSize="sm">
          <Text>
            Planned to end {formatDate(simulation.planned_end_date)}.
          </Text>
          {finish === null ? (
            <Text color="fg.muted">
              No simulated run finished the block, so there is no finish date to give.
            </Text>
          ) : (
            <Text>
              Finishing between {formatDate(finish.p10)} and {formatDate(finish.p90)}, most often{" "}
              {formatDate(finish.p50)}.
            </Text>
          )}
          {/* Not "missed": `missed` means a session a human wrote off (ADR-0017), and
              a simulated absence is nobody's decision. The calendar is strict about
              this word and so is this card. */}
          <Text>
            Sessions not done: {missed.p10}–{missed.p90}, typically {missed.p50}.
          </Text>
          <Text color="fg.muted">
            Restart {formatPct(String(simulation.p_restart))} of the time
            {simulation.p_unplannable > 0 && (
              <> · no workable plan {formatPct(String(simulation.p_unplannable))} of the time</>
            )}
            .
          </Text>
        </Stack>

        <Stack gap="2">
          {Object.entries(simulation.per_lift).map(([lift, outlook]) => (
            <LiftLine key={lift} lift={lift} outlook={outlook} />
          ))}
        </Stack>

        {/*
          Always rendered, even when the server sends no assumptions: the caveat that
          the constants are guesses is the one line that must never be conditional
          (CLAUDE.md, "label a threshold that is a guess as a starting value"), and
          bands shown with nothing qualifying them are exactly what that forbids.
        */}
        <Collapsible.Root>
          <Collapsible.Trigger asChild>
            <Button variant="ghost" size="xs" alignSelf="flex-start">
              What this assumes
              {simulation.assumptions.length > 0 && ` (${simulation.assumptions.length})`}
            </Button>
          </Collapsible.Trigger>
          <Collapsible.Content>
            <Box as="ul" ps="5" fontSize="xs" color="fg.muted" pt="1">
              {simulation.assumptions.map((assumption, i) => (
                <li key={`${i}-${assumption}`}>{assumption}</li>
              ))}
              <li>
                The constants behind these numbers are starting values chosen to be reasonable,
                not figures derived from your own training.
              </li>
            </Box>
          </Collapsible.Content>
        </Collapsible.Root>

        <Text fontSize="xs" color="fg.muted">
          {simulation.stats.runs} simulated runs, seed {simulation.stats.seed}.
        </Text>
      </Stack>
    </Box>
  );
}

function LiftLine({ lift, outlook }: { lift: string; outlook: LiftOutlook }) {
  return (
    <HStack gap="2" wrap="wrap" fontSize="sm">
      {isLiftCode(lift) ? <LiftBadge lift={lift} short /> : <Text fontWeight="medium">{lift}</Text>}
      <Text>
        {formatKg(outlook.new_1rm.p10)}–{formatKg(outlook.new_1rm.p90)}
        <Text as="span" color="fg.muted">
          {" "}
          (now {formatKg(outlook.current_1rm_kg)})
        </Text>
      </Text>
      <Text color="fg.muted">{testOutlook(outlook)}</Text>
    </HStack>
  );
}

/**
 * `p_test_made` is null when there is no test left to make -- either it has already
 * happened or the block has no test for this lift. Saying "0%" there would be a lie.
 */
function testOutlook(outlook: LiftOutlook): string {
  if (outlook.p_test_made === null) {
    return outlook.test === "none" ? "no test this block" : "test already behind you";
  }
  return `${formatPct(String(outlook.p_test_made))} chance of making the test`;
}
