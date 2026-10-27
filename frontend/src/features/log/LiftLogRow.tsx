import { Box, Button, Field, HStack, Input, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { LiftBadge } from "@/components/LiftBadge";
import { formatKg } from "@/lib/format";
import { LIMITS, type EntryProblems, type LiftEntry } from "./model";
import { TestSingleButtons } from "./TestSingleButtons";

interface Props {
  entry: LiftEntry;
  problems: EntryProblems;
  disabled?: boolean;
  onChange: (next: LiftEntry) => void;
}

/** Big enough to hit with a thumb, on a phone, between sets. */
const TAP = "12";

/**
 * One lift of the session: what was prescribed, and what was actually done.
 *
 * Prefilled from the prescription, so a session done as written needs no input
 * at all. The weight is text, not a number input: it is a decimal string end to
 * end and `valueAsNumber` would be the one place a float crept in.
 */
export function LiftLogRow({ entry, problems, disabled = false, onChange }: Props) {
  const perSet = entry.repsDone.length > 1;

  const setReps = (index: number, value: string) => {
    const repsDone = [...entry.repsDone];
    repsDone[index] = value;
    onChange({ ...entry, repsDone });
  };

  const splitPerSet = () => {
    const sets = Number.parseInt(entry.setsDone, 10);
    const count = Number.isFinite(sets) && sets > 0 ? sets : 1;
    const first = entry.repsDone[0] ?? "0";
    onChange({ ...entry, repsDone: Array.from({ length: count }, () => first) });
  };

  return (
    <Box borderWidth="1px" rounded="md" p="3">
      <Stack gap="3">
        <HStack gap="2" wrap="wrap">
          <LiftBadge lift={entry.liftCode} />
          <Text fontSize="sm" color="fg.muted">
            {entry.prescribed.sets}×{entry.prescribed.reps} @{" "}
            {formatKg(entry.prescribed.weightKg)}
          </Text>
        </HStack>

        {entry.isTest ? (
          <TestSingleButtons entry={entry} disabled={disabled} onChange={onChange} />
        ) : (
          <Stack gap="3">
            <HStack gap="3" align="flex-start">
              <Field.Root>
                <Field.Label>Sets</Field.Label>
                <Input
                  value={entry.setsDone}
                  onChange={(e) => onChange({ ...entry, setsDone: e.target.value })}
                  // `inputMode` brings up the number pad without making the value
                  // a number: a text input keeps the string exactly as typed.
                  inputMode="numeric"
                  aria-label={`Sets done for ${entry.liftCode}`}
                  h={TAP}
                  disabled={disabled}
                />
              </Field.Root>

              {!perSet && (
                <Field.Root>
                  <Field.Label>Reps</Field.Label>
                  <Input
                    value={entry.repsDone[0] ?? ""}
                    onChange={(e) => setReps(0, e.target.value)}
                    inputMode="numeric"
                    aria-label={`Reps done for ${entry.liftCode}`}
                    h={TAP}
                    disabled={disabled}
                  />
                </Field.Root>
              )}

              <Field.Root>
                <Field.Label>Weight (kg)</Field.Label>
                <Input
                  value={entry.weightKg}
                  onChange={(e) => onChange({ ...entry, weightKg: e.target.value })}
                  inputMode="decimal"
                  aria-label={`Weight for ${entry.liftCode}`}
                  h={TAP}
                  disabled={disabled}
                />
              </Field.Root>
            </HStack>

            {perSet && (
              <Stack gap="2">
                <Text fontSize="sm">Reps per set</Text>
                <HStack gap="2" wrap="wrap">
                  {entry.repsDone.map((reps, index) => (
                    <Input
                      key={index}
                      value={reps}
                      onChange={(e) => setReps(index, e.target.value)}
                      inputMode="numeric"
                      aria-label={`Reps in set ${index + 1} for ${entry.liftCode}`}
                      h={TAP}
                      w="20"
                      disabled={disabled}
                    />
                  ))}
                </HStack>
              </Stack>
            )}

            <HStack gap="3" align="flex-end" wrap="wrap">
              <Field.Root w="32">
                <Field.Label>RPE</Field.Label>
                <NativeSelect.Root disabled={disabled}>
                  <NativeSelect.Field
                    value={entry.rpe}
                    onChange={(e) => onChange({ ...entry, rpe: e.target.value })}
                    aria-label={`RPE for ${entry.liftCode}`}
                  >
                    <option value="">—</option>
                    {RPE_CHOICES.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </NativeSelect.Field>
                  <NativeSelect.Indicator />
                </NativeSelect.Root>
              </Field.Root>

              <Button
                size="sm"
                variant="ghost"
                disabled={disabled}
                onClick={() =>
                  perSet ? onChange({ ...entry, repsDone: [entry.repsDone[0] ?? "0"] }) : splitPerSet()
                }
              >
                {perSet ? "Same reps every set" : "Reps differed per set"}
              </Button>
            </HStack>
          </Stack>
        )}

        {problems.errors.map((message) => (
          <Text key={message} fontSize="sm" color="fg.error">
            {message}
          </Text>
        ))}
        {problems.warnings.map((message) => (
          <Text key={message} fontSize="sm" color="fg.muted">
            {message}
          </Text>
        ))}
      </Stack>
    </Box>
  );
}

/** Halves are real: lifters do say 8.5. */
const RPE_CHOICES = (() => {
  const values: string[] = [];
  for (let rpe = LIMITS.rpe.min * 2; rpe <= LIMITS.rpe.max * 2; rpe += 1) {
    values.push(String(rpe / 2));
  }
  return values;
})();
