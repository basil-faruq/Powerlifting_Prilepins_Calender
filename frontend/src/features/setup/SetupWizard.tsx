import { Box, Button, HStack, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { useAthlete, useSaveWeekdayMap } from "@/api/hooks/athletes";
import { useTemplate, useTemplates } from "@/api/hooks/templates";
import { useAthleteContext } from "@/components/AthleteProvider";
import { ErrorState } from "@/components/states/ErrorState";
import { LoadingState } from "@/components/states/LoadingState";
import { CreateAthleteStep } from "./CreateAthleteStep";
import { LiftsStep } from "./LiftsStep";
import {
  EMPTY_DRAFT,
  LAST_STEP,
  STEP_TITLES,
  effectiveWeekday,
  programmedLifts,
  slotsByLift,
  slotsNeeded,
  todayIso,
  upcomingMondays,
  type SetupDraft,
} from "./model";
import { OneRmStep } from "./OneRmStep";
import { PreviewStep, type CreatedBlock } from "./PreviewStep";
import { TemplateStep } from "./TemplateStep";
import { WeekdayStep } from "./WeekdayStep";

const MONDAYS_OFFERED = 26;

/**
 * Five steps from an empty database to a pending proposal. The step number lives in
 * the URL (`?step=3`), so back and refresh behave; what has been typed lives here, so
 * a step can be revisited without losing the later ones.
 *
 * The wizard never writes a session. It saves the athlete's own inputs, then creates
 * a proposal and asks for it to be approved -- the one write path (ADR-0007).
 */
export function SetupWizard() {
  const { athleteId, athlete, currentBlockId } = useAthleteContext();
  const [params, setParams] = useSearchParams();
  const [draft, setDraft] = useState<SetupDraft>(EMPTY_DRAFT);
  const [created, setCreated] = useState<CreatedBlock | null>(null);

  const detail = useAthlete(athleteId);
  const templates = useTemplates();
  const template = templates.data?.[0];
  const templateDetail = useTemplate(template?.id ?? null);
  const saveWeekdays = useSaveWeekdayMap();

  if (athleteId === null) return <CreateAthleteStep />;
  if (templates.isPending) return <LoadingState label="Loading your setup…" />;
  if (templates.isError) {
    return (
      <ErrorState message={templates.error.message} onRetry={() => void templates.refetch()} />
    );
  }

  const requested = Number(params.get("step"));
  const wanted = Number.isInteger(requested) && requested >= 1 && requested <= LAST_STEP ? requested : 1;
  // Nothing past step 1 means anything without a template.
  const step = template === undefined ? 1 : wanted;
  const goTo = (n: number) => setParams({ step: String(n) });

  // Anything the athlete changes upstream makes a block already created stale.
  const update = (patch: Partial<SetupDraft>) => {
    setDraft((previous) => ({ ...previous, ...patch }));
    setCreated(null);
  };

  const needsEntries = step >= 3;
  if (needsEntries && templateDetail.isPending) return <LoadingState label="Reading the template…" />;
  if (needsEntries && templateDetail.isError) {
    return (
      <ErrorState message={templateDetail.error.message} onRetry={() => void templateDetail.refetch()} />
    );
  }

  const entries = templateDetail.data?.entries ?? [];
  const byLift = slotsByLift(entries);
  const available = programmedLifts(entries);
  const chosen = draft.lifts?.filter((lift) => available.includes(lift)) ?? available;
  const needed = slotsNeeded(chosen, byLift);
  const weekdays = Object.fromEntries(needed.map((slot) => [slot, effectiveWeekday(draft, detail.data, slot)]));
  const locked = currentBlockId !== null && (detail.data?.weekday_map.some((row) => row.locked_from !== null) ?? false);

  const today = todayIso(athlete?.timezone ?? "Europe/Berlin");
  const mondays = upcomingMondays(today, MONDAYS_OFFERED);
  const startDate = draft.startDate !== undefined && mondays.includes(draft.startDate) ? draft.startDate : (mondays[0] ?? today);

  const saveDaysThenPreview = async () => {
    if (template === undefined) return;
    const slots = Object.fromEntries(
      needed.flatMap((slot) => {
        const weekday = weekdays[slot];
        return weekday === undefined ? [] : [[String(slot), weekday]];
      }),
    );
    try {
      // The preview is built from the SAVED weekday map, so it must be saved first.
      if (!locked) await saveWeekdays.mutateAsync({ athleteId, templateId: template.id, lifts: chosen, slots });
      goTo(5);
    } catch {
      // A 409 (a block is running) or 422 has already been toasted with the server's words.
    }
  };

  return (
    <Stack gap="6" maxW="2xl">
      <Box>
        <HStack gap="2" wrap="wrap">
          {STEP_TITLES.map((title, index) => (
            <Button
              key={title}
              size="xs"
              rounded="full"
              variant={step === index + 1 ? "solid" : "outline"}
              disabled={index + 1 > step}
              aria-label={`Step ${index + 1}: ${title}`}
              aria-current={step === index + 1 ? "step" : undefined}
              onClick={() => goTo(index + 1)}
            >
              {index + 1}
            </Button>
          ))}
        </HStack>
        <Text mt="2" fontSize="sm" color="fg.muted">
          Step {step} of {LAST_STEP}
        </Text>
      </Box>

      {step === 1 && <TemplateStep template={template} onNext={() => goTo(2)} />}

      {step === 2 && (
        <OneRmStep
          athleteId={athleteId}
          detail={detail.data}
          draft={draft}
          today={today}
          onChange={update}
          onBack={() => goTo(1)}
          onNext={() => goTo(3)}
        />
      )}

      {step === 3 && (
        <LiftsStep
          available={available}
          byLift={byLift}
          chosen={chosen}
          detail={detail.data}
          onChange={(lifts) => update({ lifts })}
          onBack={() => goTo(2)}
          onNext={() => goTo(4)}
        />
      )}

      {step === 4 && template && (
        <WeekdayStep
          athleteId={athleteId}
          templateId={template.id}
          lifts={chosen}
          byLift={byLift}
          weekdays={weekdays}
          locked={locked}
          saving={saveWeekdays.isPending}
          onChange={(slot, weekday) => update({ weekdays: { ...draft.weekdays, [slot]: weekday } })}
          onBack={() => goTo(3)}
          onNext={() => void saveDaysThenPreview()}
        />
      )}

      {step === 5 && template && (
        <PreviewStep
          athleteId={athleteId}
          templateId={template.id}
          lifts={chosen}
          startDate={startDate}
          mondays={mondays}
          onStartDate={(iso) => update({ startDate: iso })}
          created={created}
          onCreated={setCreated}
          onBack={() => goTo(4)}
        />
      )}
    </Stack>
  );
}
