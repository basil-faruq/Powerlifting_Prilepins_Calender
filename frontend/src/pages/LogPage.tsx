import { Badge, Button, Field, HStack, Heading, Stack, Text, Textarea } from "@chakra-ui/react";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { useAthlete } from "@/api/hooks/athletes";
import { useBlockSessions } from "@/api/hooks/blocks";
import { useLogSession, useSession } from "@/api/hooks/sessions";
import { errorMessage } from "@/api/errors";
import type { SessionOut } from "@/api/types";
import { useAthleteContext } from "@/components/AthleteProvider";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorState } from "@/components/states/ErrorState";
import { LoadingState } from "@/components/states/LoadingState";
import { toaster } from "@/components/ui/toaster";
import { UnavailableDialog } from "@/components/UnavailableDialog";
import { LiftLogRow } from "@/features/log/LiftLogRow";
import { LoggedSessionSummary } from "@/features/log/LoggedSessionSummary";
import { LIMITS, pickSession, prefill, toLogIn, validateAll, type LiftEntry } from "@/features/log/model";
import { todayIso } from "@/lib/date";
import { formatDate } from "@/lib/format";

/**
 * Logging what was actually done (spec 20).
 *
 * Phone-first and prefilled: a session performed as written is one tap on Save.
 */
export function LogPage() {
  const [params] = useSearchParams();
  const askedFor = params.get("session");
  const { athlete, currentBlockId } = useAthleteContext();
  const today = todayIso(athlete?.timezone ?? "Europe/Berlin");

  const sessions = useBlockSessions(askedFor === null ? currentBlockId : null, {});
  const asked = useSession(askedFor);

  if (currentBlockId === null) {
    return (
      <EmptyState
        title="No block to log against"
        description="Set a block up first; then today's session appears here."
      />
    );
  }

  if (askedFor !== null) {
    if (asked.isPending) return <LoadingState label="Loading the session…" />;
    if (asked.isError) {
      return (
        <ErrorState
          title="Could not load that session"
          message={asked.error.message}
          onRetry={() => void asked.refetch()}
        />
      );
    }
    return <Logger session={asked.data} isToday={asked.data.date === today} today={today} />;
  }

  if (sessions.isPending) return <LoadingState label="Finding today's session…" />;
  if (sessions.isError) {
    return (
      <ErrorState
        title="Could not load your sessions"
        message={sessions.error.message}
        onRetry={() => void sessions.refetch()}
      />
    );
  }

  const picked = pickSession(sessions.data, today);
  if (picked === null) {
    return (
      <EmptyState
        title="Nothing left to log"
        description="Every session in this block has been logged or skipped."
      />
    );
  }

  return <Logger session={picked.session} isToday={picked.isToday} today={today} />;
}

function Logger({
  session,
  isToday,
  today,
}: {
  session: SessionOut;
  isToday: boolean;
  today: string;
}) {
  const navigate = useNavigate();
  const { athleteId } = useAthleteContext();
  const athlete = useAthlete(athleteId);
  const detail = useSession(session.id);
  const log = useLogSession();

  const [entries, setEntries] = useState<LiftEntry[]>(() => prefill(session.lifts));
  const [note, setNote] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  // A session already logged is read-only in v1. The grid's copy can be stale,
  // so the detail's own `log` is what decides.
  const existing = detail.data?.log ?? null;
  const plateIncrement = athlete.data?.plate_increment_kg ?? "2.5";
  const { byEntry, noteError, ok } = validateAll(entries, plateIncrement, note);

  const save = () => {
    setFailure(null);
    log.mutate(
      { sessionId: session.id, body: toLogIn(entries, isToday ? today : session.date, note) },
      {
        onSuccess: () => {
          toaster.create({ type: "success", title: "Logged" });
          void navigate("/calendar");
        },
        onError: (error) => setFailure(errorMessage(error)),
      },
    );
  };

  return (
    <Stack gap="5" maxW="2xl">
      <Stack gap="1">
        <Heading size="lg">Week {session.week}</Heading>
        <HStack gap="2" wrap="wrap">
          <Text color="fg.muted">{formatDate(session.date)}</Text>
          {!isToday && <Badge colorPalette="yellow">not today</Badge>}
          {existing !== null && <Badge colorPalette="green">logged</Badge>}
        </HStack>
        {!isToday && existing === null && (
          <Text fontSize="sm" color="fg.muted">
            Nothing is scheduled for today. This is the session either side of it — log it
            anyway if that is the one you did.
          </Text>
        )}
      </Stack>

      {existing !== null ? (
        <LoggedSessionSummary log={existing} />
      ) : (
        <>
          <Stack gap="3">
            {entries.map((entry, index) => (
              <LiftLogRow
                key={`${entry.liftCode}-${index}`}
                entry={entry}
                problems={byEntry[index] ?? { errors: [], warnings: [] }}
                disabled={log.isPending}
                onChange={(next) =>
                  setEntries((current) => current.map((e, i) => (i === index ? next : e)))
                }
              />
            ))}
          </Stack>

          <Field.Root invalid={noteError !== null}>
            <Field.Label>How did it feel?</Field.Label>
            <Textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="How did it feel? Sleep, bar speed, anything sore?"
              rows={4}
              maxLength={LIMITS.noteMax}
              disabled={log.isPending}
            />
            <Field.HelperText>
              {note.length}/{LIMITS.noteMax}
            </Field.HelperText>
            {noteError !== null && <Field.ErrorText>{noteError}</Field.ErrorText>}
          </Field.Root>

          {failure !== null && (
            <ProblemAlert title="That did not save" lines={[failure]}>
              Nothing you typed has been lost. Press Save again when you are ready.
            </ProblemAlert>
          )}

          <Stack direction={{ base: "column", sm: "row" }} gap="3">
            <Button flex="1" h="14" onClick={save} loading={log.isPending} disabled={!ok}>
              Save
            </Button>
            <Button
              flex="1"
              h="14"
              variant="outline"
              disabled={log.isPending}
              onClick={() => setUnavailable(true)}
            >
              I can&rsquo;t make it
            </Button>
          </Stack>
        </>
      )}

      <UnavailableDialog
        open={unavailable}
        blockId={session.block_id}
        defaultFrom={session.date}
        onClose={() => setUnavailable(false)}
      />
    </Stack>
  );
}
