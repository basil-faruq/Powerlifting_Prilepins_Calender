import { Field, Heading, HStack, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { weekdayCheckOptions } from "@/api/hooks/athletes";
import type { LiftCode } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { WEEKDAYS } from "@/lib/lifts";
import { useDebouncedValue } from "@/lib/useDebouncedValue";
import { slotsNeeded } from "./model";
import { ProblemAlert } from "@/components/ProblemAlert";
import { StepNav } from "./StepNav";

/** How long to wait after the last change before asking the server (spec 17 §4). */
export const CHECK_DEBOUNCE_MS = 300;

interface Props {
  athleteId: string;
  templateId: string;
  /** The lifts being trained: only the days they use need a weekday. */
  lifts: LiftCode[];
  byLift: Record<string, number[]>;
  /** slot → weekday (0 = Monday), as currently chosen. */
  weekdays: Record<number, number | undefined>;
  /** True while a block is running: the server refuses a change, so none is offered. */
  locked: boolean;
  saving?: boolean;
  onChange: (slot: number, weekday: number | undefined) => void;
  onBack: () => void;
  onNext: () => void;
}

export function WeekdayStep({
  athleteId,
  templateId,
  lifts,
  byLift,
  weekdays,
  locked,
  saving = false,
  onChange,
  onBack,
  onNext,
}: Props) {
  const needed = slotsNeeded(lifts, byLift);
  const complete = needed.every((slot) => weekdays[slot] !== undefined);

  // Only the days the chosen lifts use are sent. Serialised, because the debounce
  // compares by value and an object rebuilt each render would never settle.
  const chosen = JSON.stringify(
    Object.fromEntries(needed.flatMap((slot) => (weekdays[slot] === undefined ? [] : [[String(slot), weekdays[slot]]]))),
  );
  const settled = useDebouncedValue(chosen, CHECK_DEBOUNCE_MS);
  const slots = JSON.parse(settled) as Record<string, number>;

  const check = useQuery({
    ...weekdayCheckOptions(athleteId, templateId, lifts, slots),
    enabled: complete && !locked && settled === chosen,
  });

  // The verdict on the CURRENT choice only exists once the debounce has settled and the
  // answer is in; until then Next stays off rather than trusting a stale "fine". The
  // previous problems stay on screen meanwhile, so the list does not blink per click.
  const checking = complete && !locked && (settled !== chosen || check.isFetching);
  const problems = complete && !locked ? (check.data?.problems ?? []) : [];
  const failed = check.isError && !checking;
  const canContinue = complete && (locked || (!checking && !failed && problems.length === 0));

  return (
    <Stack gap="5">
      <Heading size="lg">Your training days</Heading>
      <Text color="fg.muted">
        Pick a weekday for each day of the template. Once the block starts they stay put.
      </Text>

      <Stack gap="4">
        {needed.map((slot) => {
          const using = lifts.filter((lift) => (byLift[lift] ?? []).includes(slot));
          return (
            <Field.Root key={slot}>
              <Field.Label>
                <HStack gap="2" wrap="wrap">
                  <span>Day {slot}</span>
                  {using.map((lift) => (
                    <LiftBadge key={lift} lift={lift} />
                  ))}
                </HStack>
              </Field.Label>
              <NativeSelect.Root disabled={locked}>
                <NativeSelect.Field
                  aria-label={`Day ${slot} weekday`}
                  value={weekdays[slot] === undefined ? "" : String(weekdays[slot])}
                  onChange={(e) => onChange(slot, e.target.value === "" ? undefined : Number(e.target.value))}
                >
                  <option value="">Choose a day</option>
                  {WEEKDAYS.map((name, index) => (
                    <option key={name} value={index}>
                      {name}
                    </option>
                  ))}
                </NativeSelect.Field>
                <NativeSelect.Indicator />
              </NativeSelect.Root>
            </Field.Root>
          );
        })}
      </Stack>

      {problems.length > 0 && <ProblemAlert title="These days will not work" lines={problems} />}
      {failed && <ProblemAlert title="Could not check these days" lines={[check.error?.message ?? "Unknown error"]} />}
      {checking && (
        <Text fontSize="sm" color="fg.muted" role="status">
          Checking…
        </Text>
      )}

      <Text fontSize="sm" color="fg.muted">
        These days are locked once the block starts. You can change them later only through a proposal.
        The minimum rest between sessions is a starting value, not a derived one.
      </Text>
      {locked && (
        <Text fontSize="sm" color="fg.muted">
          A block is running, so its days cannot be changed here.
        </Text>
      )}

      <StepNav
        onBack={onBack}
        onNext={onNext}
        nextDisabled={!canContinue}
        busy={saving}
        hint={complete ? undefined : "Choose a weekday for every day to continue."}
      />
    </Stack>
  );
}
