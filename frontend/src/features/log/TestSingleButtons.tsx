import { Button, Field, HStack, Input, Stack, Text } from "@chakra-ui/react";
import { formatKg } from "@/lib/format";
import { madeIt, missedIt, wouldSetNewMax, type LiftEntry } from "./model";

interface Props {
  entry: LiftEntry;
  disabled?: boolean;
  onChange: (next: LiftEntry) => void;
}

/**
 * A test single: made it, or didn't.
 *
 * Two buttons instead of three inputs, because that is the whole decision. The
 * weight stays editable: an athlete who felt good and took more than was
 * prescribed should be able to say so, and that is exactly the case that sets a
 * new max.
 *
 * There is no "missed" field on the wire. A miss is logged as the attempt it
 * was -- zero reps -- and the backend records a new max only at or above the
 * prescribed weight with at least one rep (ADR-0005 mod 5).
 */
export function TestSingleButtons({ entry, disabled = false, onChange }: Props) {
  const made = entry.setsDone === "1" && entry.repsDone[0] === "1";
  const missed = entry.setsDone === "0";
  const newMax = wouldSetNewMax(entry);

  return (
    <Stack gap="3">
      <Text fontSize="sm" fontWeight="medium">
        Test single at {formatKg(entry.prescribed.weightKg)}
      </Text>

      <HStack gap="3">
        <Button
          flex="1"
          h="14"
          colorPalette="green"
          variant={made ? "solid" : "outline"}
          aria-pressed={made}
          disabled={disabled}
          onClick={() => onChange(madeIt(entry))}
        >
          Made it
        </Button>
        <Button
          flex="1"
          h="14"
          colorPalette="red"
          variant={missed ? "solid" : "outline"}
          aria-pressed={missed}
          disabled={disabled}
          onClick={() => onChange(missedIt(entry))}
        >
          Missed it
        </Button>
      </HStack>

      <Field.Root>
        <Field.Label>Weight lifted (kg)</Field.Label>
        <Input
          value={entry.weightKg}
          onChange={(e) => onChange({ ...entry, weightKg: e.target.value })}
          inputMode="decimal"
          aria-label={`Weight for the ${entry.liftCode} test single`}
          h="12"
          disabled={disabled}
        />
        <Field.HelperText>
          {newMax
            ? `Saving this records a new tested ${entry.liftCode} max of ${formatKg(entry.weightKg)}.`
            : `A new max is recorded only at or above the prescribed ${formatKg(entry.prescribed.weightKg)}, with at least one rep.`}
        </Field.HelperText>
      </Field.Root>
    </Stack>
  );
}
