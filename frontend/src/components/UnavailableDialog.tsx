import {
  Button,
  Checkbox,
  CloseButton,
  Dialog,
  Field,
  Input,
  Portal,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useState } from "react";
import { useMarkUnavailable } from "@/api/hooks/blocks";
import { ApiError, errorMessage } from "@/api/errors";
import { useAthleteContext } from "@/components/AthleteProvider";
import { ProblemAlert } from "@/components/ProblemAlert";
import { toaster } from "@/components/ui/toaster";
import { todayIso } from "@/lib/date";

interface Props {
  open: boolean;
  blockId: string | null;
  /** Defaults the "from" date, so the calendar can open it on a given session. */
  defaultFrom?: string;
  onClose: () => void;
}

/**
 * "I can't make it."
 *
 * In `components/` rather than `features/log/` because the calendar's session
 * drawer opens it too, and a feature may not import another feature.
 *
 * It sends a signal, not a change: the route answers 202 and the repair arrives
 * later as proposals to approve in Chat. The dialog says what to expect before
 * sending, because the two outcomes are very different -- a short gap moves
 * sessions, while an injury restarts the block at reduced planning maxes.
 */
export function UnavailableDialog({ open, blockId, defaultFrom, onClose }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            {/* Mounted only while open and keyed on the date it starts from, so the
                form seeds itself once and a cancelled dialog is forgotten. */}
            {open && blockId !== null && (
              <UnavailableForm
                key={defaultFrom ?? "today"}
                blockId={blockId}
                defaultFrom={defaultFrom}
                onClose={onClose}
              />
            )}
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

function UnavailableForm({
  blockId,
  defaultFrom,
  onClose,
}: {
  blockId: string;
  defaultFrom?: string;
  onClose: () => void;
}) {
  const mark = useMarkUnavailable();
  const { athlete } = useAthleteContext();
  const [from, setFrom] = useState(
    defaultFrom ?? todayIso(athlete?.timezone ?? "Europe/Berlin"),
  );
  const [until, setUntil] = useState("");
  const [injury, setInjury] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const backwards = until !== "" && until < from;

  const send = () => {
    setFailure(null);
    mark.mutate(
      {
        blockId,
        body: { from, injury, ...(until === "" ? {} : { until }) },
      },
      {
        onSuccess: (state) => {
          onClose();
          // 202 with `state: "unknown"` means the signal is durable but no worker
          // could be asked what came of it (ADR-0017 rule 10). Not a failure.
          toaster.create(
            state?.state === "unknown"
              ? {
                  type: "info",
                  title: "Saved",
                  description: "Your options will appear in Chat once the planner is running.",
                }
              : {
                  type: "success",
                  title: "Noted",
                  description: "Look in Chat for what to do about it.",
                },
          );
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 409) {
            setFailure("This block is not running, so there is nothing to reschedule yet.");
          } else setFailure(errorMessage(error));
        },
      },
    );
  };

  return (
    <>
      <Dialog.Header>
        <Dialog.Title>I can&rsquo;t make it</Dialog.Title>
      </Dialog.Header>
      <Dialog.Body>
        <Stack gap="4">
          <Field.Root>
            <Field.Label>From</Field.Label>
            <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} h="12" />
          </Field.Root>

          <Field.Root invalid={backwards}>
            <Field.Label>Until (optional)</Field.Label>
            <Input type="date" value={until} onChange={(e) => setUntil(e.target.value)} h="12" />
            <Field.HelperText>
              Leave it empty if you do not know yet &mdash; say so later when you do.
            </Field.HelperText>
            {backwards && <Field.ErrorText>That is before the first day.</Field.ErrorText>}
          </Field.Root>

          <Checkbox.Root
            checked={injury}
            onCheckedChange={(e) => setInjury(e.checked === true)}
          >
            <Checkbox.HiddenInput />
            <Checkbox.Control />
            <Checkbox.Label>This is an injury</Checkbox.Label>
          </Checkbox.Root>

          <Text fontSize="sm" color="fg.muted">
            {injury
              ? "An injury proposes a restart: a fresh block at reduced planning maxes. Your recorded 1RMs do not change — only a tested single changes those."
              : "A short gap moves sessions; a long one can restart the block. Either way it arrives in Chat as something to approve, and nothing changes until you do."}
          </Text>

          {failure !== null && <ProblemAlert title="Could not send that" lines={[failure]} />}
        </Stack>
      </Dialog.Body>
      <Dialog.Footer>
        <Button variant="outline" onClick={onClose} disabled={mark.isPending}>
          Cancel
        </Button>
        <Button onClick={send} loading={mark.isPending} disabled={backwards}>
          Tell the planner
        </Button>
      </Dialog.Footer>
      <Dialog.CloseTrigger asChild>
        <CloseButton size="sm" />
      </Dialog.CloseTrigger>
    </>
  );
}
