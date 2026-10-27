import { Field, Heading, Input, NativeSelect, SimpleGrid, Stack, Text } from "@chakra-ui/react";
import { useSaveOneRepMaxes, usePatchAthlete } from "@/api/hooks/athletes";
import type { AthleteDetail, LiftCode } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { LIFTS } from "@/lib/lifts";
import {
  PLATE_INCREMENTS,
  effectiveIncrement,
  effectiveMax,
  effectiveRounding,
  maxChanged,
  maxProblem,
  trimDecimal,
  type MaxEntry,
  type Rounding,
  type SetupDraft,
} from "./model";
import { StepNav } from "./StepNav";

interface Props {
  athleteId: string;
  detail: AthleteDetail | undefined;
  draft: SetupDraft;
  /** Today in the athlete's timezone: the latest date a max can have been tested. */
  today: string;
  onChange: (patch: Partial<SetupDraft>) => void;
  onBack: () => void;
  onNext: () => void;
}

export function OneRmStep({ athleteId, detail, draft, today, onChange, onBack, onNext }: Props) {
  const saveMaxes = useSaveOneRepMaxes();
  const patchAthlete = usePatchAthlete();

  const entries = LIFTS.map((lift) => ({ lift, entry: effectiveMax(draft, detail, lift) }));
  const problems = new Map(entries.map(({ lift, entry }) => [lift, maxProblem(entry, today)] as const));
  const hasProblem = [...problems.values()].some((p) => p !== null);

  const increment = effectiveIncrement(draft, detail);
  const rounding = effectiveRounding(draft, detail);
  // An increment the athlete already has that is not one of ours stays selectable.
  const increments = PLATE_INCREMENTS.includes(increment as (typeof PLATE_INCREMENTS)[number])
    ? [...PLATE_INCREMENTS]
    : [increment, ...PLATE_INCREMENTS];

  const edit = (lift: LiftCode, patch: Partial<MaxEntry>) =>
    onChange({ maxes: { ...draft.maxes, [lift]: { ...effectiveMax(draft, detail, lift), ...patch } } });

  const save = async () => {
    // Only maxes that differ from what the server holds are sent: the endpoint
    // appends a row per lift, and re-sending an unchanged one would pile up history.
    const rows = entries.flatMap(({ lift, entry }) =>
      (entry.kg !== "" || entry.setOn !== "") && maxChanged(detail, lift, entry)
        ? [{ lift_code: lift, kg: entry.kg, set_on: entry.setOn, source: "tested" as const }]
        : [],
    );
    try {
      if (rows.length > 0) await saveMaxes.mutateAsync({ athleteId, rows });
      const plateChanged = detail !== undefined && increment !== trimDecimal(detail.plate_increment_kg);
      const roundingChanged = detail !== undefined && rounding !== detail.rounding;
      if (plateChanged || roundingChanged) {
        await patchAthlete.mutateAsync({ athleteId, plateIncrementKg: increment, rounding });
      }
      onNext();
    } catch {
      // The server's own message has already been toasted by the mutation cache.
    }
  };

  return (
    <Stack gap="5">
      <Heading size="lg">Your maxes</Heading>
      <Text color="fg.muted">
        The heaviest single you have tested in each lift, and the day you tested it. Leave a lift
        blank if you do not train it. Nothing here is pre-filled: a max is yours to enter.
      </Text>

      <Stack gap="4">
        {entries.map(({ lift, entry }) => {
          const problem = problems.get(lift) ?? null;
          return (
            <Stack key={lift} gap="2">
              <LiftBadge lift={lift} showAccessory />
              <SimpleGrid columns={2} gap="3">
                <Field.Root invalid={problem?.kg !== undefined}>
                  <Field.Label>Max (kg)</Field.Label>
                  <Input
                    type="number"
                    inputMode="decimal"
                    step="0.5"
                    min="0"
                    value={entry.kg}
                    aria-label={`${lift} max in kilograms`}
                    onChange={(e) => edit(lift, { kg: e.target.value })}
                  />
                  <Field.ErrorText>{problem?.kg}</Field.ErrorText>
                </Field.Root>
                <Field.Root invalid={problem?.setOn !== undefined}>
                  <Field.Label>Tested on</Field.Label>
                  <Input
                    type="date"
                    max={today}
                    value={entry.setOn}
                    aria-label={`${lift} test date`}
                    onChange={(e) => edit(lift, { setOn: e.target.value })}
                  />
                  <Field.ErrorText>{problem?.setOn}</Field.ErrorText>
                </Field.Root>
              </SimpleGrid>
            </Stack>
          );
        })}
      </Stack>

      <Stack gap="3">
        <Heading size="md">Plates</Heading>
        <SimpleGrid columns={2} gap="3">
          <Field.Root>
            <Field.Label>Smallest jump (kg)</Field.Label>
            <NativeSelect.Root>
              <NativeSelect.Field
                value={increment}
                aria-label="Plate increment"
                onChange={(e) => onChange({ increment: e.target.value })}
              >
                {increments.map((value) => (
                  <option key={value} value={value}>
                    {value} kg
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
          <Field.Root>
            <Field.Label>Rounding</Field.Label>
            <NativeSelect.Root>
              <NativeSelect.Field
                value={rounding}
                aria-label="Rounding"
                onChange={(e) => onChange({ rounding: e.target.value as Rounding })}
              >
                <option value="nearest">Nearest</option>
                <option value="floor">Down</option>
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
        </SimpleGrid>
        <Text fontSize="sm" color="fg.muted">
          A bar loads in pairs, so the smallest real step is usually 5 kg. Your spreadsheet used 1 kg,
          rounding down; choose that to reproduce its weights.
        </Text>
      </Stack>

      <StepNav
        onBack={onBack}
        onNext={() => void save()}
        nextDisabled={hasProblem}
        hint="Fix the highlighted maxes to continue."
        busy={saveMaxes.isPending || patchAthlete.isPending}
      />
    </Stack>
  );
}
