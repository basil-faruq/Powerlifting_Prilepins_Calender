import { Box, Button, HStack, Stack, Text } from "@chakra-ui/react";
import type { SessionLogOut } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { isLiftCode } from "@/lib/lifts";
import { formatDate, formatKg } from "@/lib/format";

/**
 * A session that has already been logged, read-only.
 *
 * v1 does not edit a log: there is no route for it, and overwriting a training
 * record silently is worse than refusing. The button says so rather than being
 * quietly absent, so it is clear this is a decision and not an oversight.
 */
export function LoggedSessionSummary({ log }: { log: SessionLogOut }) {
  return (
    <Stack gap="3">
      <Text color="fg.muted">Logged on {formatDate(log.performed_on)}.</Text>

      <Stack gap="2">
        {log.entries.map((entry, index) => (
          <Box key={`${entry.lift_code}-${index}`} borderWidth="1px" rounded="md" p="3">
            <HStack gap="2" wrap="wrap">
              {isLiftCode(entry.lift_code) ? (
                <LiftBadge lift={entry.lift_code} />
              ) : (
                <Text fontWeight="medium">{entry.lift_code}</Text>
              )}
              <Text fontSize="sm">
                {entry.sets_done}×
                {Array.isArray(entry.reps_done) ? entry.reps_done.join("/") : entry.reps_done} @{" "}
                {formatKg(String(entry.weight_kg))}
              </Text>
              {entry.rpe !== null && entry.rpe !== undefined && (
                <Text fontSize="sm" color="fg.muted">
                  RPE {String(entry.rpe)}
                </Text>
              )}
            </HStack>
          </Box>
        ))}
      </Stack>

      {log.note !== null && log.note !== "" && (
        <Box borderWidth="1px" rounded="md" p="3" bg="bg.subtle">
          <Text whiteSpace="pre-wrap">{log.note}</Text>
        </Box>
      )}

      <Button variant="outline" disabled alignSelf="flex-start">
        Edit log (not in this version)
      </Button>
    </Stack>
  );
}
