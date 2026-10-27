import { Button, CloseButton, Dialog, Field, HStack, Input, Portal, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useEditProposal } from "@/api/hooks/proposals";
import type { ProposalOut, SessionChange, Violation } from "@/api/types";
import { ApiError, errorMessage } from "@/api/errors";
import { LiftBadge } from "@/components/LiftBadge";
import { formatDate } from "@/lib/format";
import { readChanges, readPlan, readSessionChanges } from "@/lib/proposalPayload";

interface Props {
  proposal: ProposalOut;
  open: boolean;
  onClose: () => void;
  /** The server re-planned and accepted it; the card refreshes from the cache. */
  onEdited: () => void;
  /** 422: the plan was re-checked, it failed, and nothing was stored. */
  onRefused: (violations: Violation[]) => void;
  /**
   * Anything else: a 500, a dropped connection. The proposal is untouched and still
   * approvable, so this must NOT be reported as "that edit does not validate".
   */
  onFailed: (message: string) => void;
}

/**
 * Moving the dates a proposal would move things to.
 *
 * `PATCH /proposals/{id}` rebuilds the plan from the changes it is given and
 * re-validates the whole thing, so the edit is sent as the original change list with
 * only `to_date` altered. The route accepts `move` and nothing else, which is why
 * only moves appear here.
 */
export function EditProposalDialog({
  proposal,
  open,
  onClose,
  onEdited,
  onRefused,
  onFailed,
}: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={(e) => !e.open && onClose()} size="lg">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            {/*
              Mounted only while open, and keyed on the proposal: the form seeds its
              dates once, in `useState`, and a cancelled edit is forgotten because the
              form itself goes away. Deriving that state in an effect instead would
              re-render every open for nothing.
            */}
            {open && (
              <EditForm
                key={proposal.id}
                proposal={proposal}
                onClose={onClose}
                onEdited={onEdited}
                onRefused={onRefused}
                onFailed={onFailed}
              />
            )}
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

function EditForm({ proposal, onClose, onEdited, onRefused, onFailed }: Omit<Props, "open">) {
  const edit = useEditProposal();
  const changes = readSessionChanges(proposal.payload).filter((c) => c.action === "move");
  const liftsBySeq = new Map(readPlan(proposal.payload).map((s) => [s.seq, s.lifts]));
  // seq → the date it is moving FROM, validated. Only dates that are real days are
  // in here, so anything found can be formatted safely.
  const wasOn = new Map(
    readChanges(proposal.payload)
      .filter((change) => change.from !== null)
      .map((change) => [change.seq, change.from as string]),
  );

  /** seq → the date this edit would move it to, seeded from the proposal itself. */
  const [dates, setDates] = useState<Record<number, string>>(() =>
    Object.fromEntries(changes.map((c) => [c.seq, c.to_date ?? ""])),
  );

  const save = () => {
    const edited: SessionChange[] = changes.map((change) => ({
      ...change,
      to_date: dates[change.seq] ?? change.to_date,
    }));
    edit.mutate(
      { proposalId: proposal.id, changes: edited },
      {
        onSuccess: onEdited,
        onError: (error) => {
          // 422 is the expected answer to a date that does not work: it carries the
          // violations and stores nothing. Anything else did not get that far.
          if (error instanceof ApiError && error.status === 422) {
            const { violations, problems, detail } = error.problem;
            onRefused(
              violations.length > 0
                ? violations
                : (problems.length > 0 ? problems : [detail]).map((message) => ({
                    rule: "edit",
                    message,
                    refs: [],
                  })),
            );
          } else {
            onFailed(errorMessage(error));
          }
        },
      },
    );
  };

  return (
    <>
      <Dialog.Header>
        <Dialog.Title>Edit the proposed dates</Dialog.Title>
      </Dialog.Header>
      <Dialog.Body>
        <Stack gap="4">
          <Text fontSize="sm" color="fg.muted">
            The whole block is re-planned and re-checked against these dates. If it does not work
            out, nothing is changed and you keep the proposal as it is.
          </Text>
          {changes.map((change) => (
            <Field.Root key={change.seq}>
              <Field.Label>
                <HStack gap="2" wrap="wrap">
                  {[...new Set((liftsBySeq.get(change.seq) ?? []).map((l) => l.lift_code))].map(
                    (lift) => (
                      <LiftBadge key={lift} lift={lift} short />
                    ),
                  )}
                  <Text fontSize="sm">
                    week {change.week}
                    {/*
                      From the VALIDATED view: `readSessionChanges` passes dates
                      through untouched so the PATCH can echo them, and a date of the
                      right shape that is no real day makes `formatDate` throw.
                    */}
                    {wasOn.get(change.seq) !== undefined && (
                      <> · was {formatDate(wasOn.get(change.seq) ?? "")}</>
                    )}
                  </Text>
                </HStack>
              </Field.Label>
              <Input
                type="date"
                value={dates[change.seq] ?? ""}
                onChange={(e) => setDates((d) => ({ ...d, [change.seq]: e.target.value }))}
                aria-label={`New date for session ${change.seq}`}
              />
            </Field.Root>
          ))}
        </Stack>
      </Dialog.Body>
      <Dialog.Footer>
        <Button variant="outline" onClick={onClose} disabled={edit.isPending}>
          Cancel
        </Button>
        <Button onClick={save} loading={edit.isPending}>
          Save and re-check
        </Button>
      </Dialog.Footer>
      <Dialog.CloseTrigger asChild>
        <CloseButton size="sm" />
      </Dialog.CloseTrigger>
    </>
  );
}
