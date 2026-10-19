import { Button, Checkbox, Heading, HStack, Stack, Text } from "@chakra-ui/react";
import type { AthleteDetail, LiftCode } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { LIFT_LABEL } from "@/lib/lifts";
import { savedMax } from "./model";
import { StepNav } from "./StepNav";

interface Props {
  /** The lifts this template programmes. */
  available: LiftCode[];
  byLift: Record<string, number[]>;
  chosen: LiftCode[];
  detail: AthleteDetail | undefined;
  onChange: (lifts: LiftCode[]) => void;
  onBack: () => void;
  onNext: () => void;
}

const days = (slots: number[]) => slots.map((slot) => `Day ${slot}`).join(" · ");

export function LiftsStep({ available, byLift, chosen, detail, onChange, onBack, onNext }: Props) {
  const missing = chosen.filter((lift) => savedMax(detail, lift) === undefined);

  const toggle = (lift: LiftCode, on: boolean) =>
    onChange(available.filter((candidate) => (candidate === lift ? on : chosen.includes(candidate))));

  return (
    <Stack gap="5">
      <Heading size="lg">Your lifts</Heading>
      <Text color="fg.muted">Which of the template&apos;s lifts are you training this block?</Text>

      <Stack gap="3">
        {available.map((lift) => (
          <Checkbox.Root
            key={lift}
            checked={chosen.includes(lift)}
            onCheckedChange={(e) => toggle(lift, e.checked === true)}
          >
            <Checkbox.HiddenInput aria-label={LIFT_LABEL[lift]} />
            <Checkbox.Control />
            <Checkbox.Label>
              <HStack gap="3" wrap="wrap">
                <LiftBadge lift={lift} showAccessory />
                <Text fontSize="sm" color="fg.muted">
                  {days(byLift[lift] ?? [])}
                </Text>
              </HStack>
            </Checkbox.Label>
          </Checkbox.Root>
        ))}
      </Stack>

      {chosen.length === 0 && <Text color="fg.error">Choose at least one lift.</Text>}
      {missing.length > 0 && (
        <Stack gap="2" alignItems="flex-start">
          <Text color="fg.error">
            No max entered for {missing.map((lift) => LIFT_LABEL[lift]).join(", ")}.
          </Text>
          <Button size="sm" variant="outline" onClick={onBack}>
            Back to your maxes
          </Button>
        </Stack>
      )}

      <StepNav
        onBack={onBack}
        onNext={onNext}
        nextDisabled={chosen.length === 0 || missing.length > 0}
      />
    </Stack>
  );
}
