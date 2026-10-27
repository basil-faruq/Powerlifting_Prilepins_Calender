import { Box, Button, Collapsible, Field, Heading, HStack, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { blockPreviewOptions, useCreateBlock } from "@/api/hooks/blocks";
import { fetchProposal, useApproveProposal } from "@/api/hooks/proposals";
import type { BlockCreateBody, LiftCode } from "@/api/types";
import { BlockPreviewTable } from "@/components/BlockPreviewTable";
import { LiftBadge } from "@/components/LiftBadge";
import { ErrorState } from "@/components/states/ErrorState";
import { LoadingState } from "@/components/states/LoadingState";
import { formatDate } from "@/lib/format";
import { classifyApproveError, waitForDecision, type ApproveOutcome } from "./approve";
import { ProblemAlert } from "./ProblemAlert";
import { summarize } from "./summary";

export interface CreatedBlock {
  blockId: string;
  proposalId: string;
}

interface Props {
  athleteId: string;
  templateId: string;
  lifts: LiftCode[];
  startDate: string;
  /** The Mondays on offer: a block must start on one. */
  mondays: string[];
  onStartDate: (iso: string) => void;
  /** The draft block already made, if any: a retried approve must not make another. */
  created: CreatedBlock | null;
  onCreated: (created: CreatedBlock | null) => void;
  onBack: () => void;
}

export function PreviewStep({
  athleteId,
  templateId,
  lifts,
  startDate,
  mondays,
  onStartDate,
  created,
  onCreated,
  onBack,
}: Props) {
  const navigate = useNavigate();
  const body: BlockCreateBody = { athlete_id: athleteId, template_id: templateId, lifts, start_date: startDate };
  const preview = useQuery(blockPreviewOptions(body));
  const createBlock = useCreateBlock();
  // Approve failures are explained here, not toasted: a 409 "already approved" is a success.
  const approve = useApproveProposal({ silent: true });
  const [outcome, setOutcome] = useState<ApproveOutcome | null>(null);

  const [waiting, setWaiting] = useState(false);

  const start = async () => {
    if (created === null) return;
    setOutcome(null);
    try {
      const decided = await approve.mutateAsync(created.proposalId);
      let decision: "approved" | "pending" | "other" = decided.status === "approved" ? "approved" : "pending";
      if (decision === "pending") {
        // A first approval only starts the workflow; the 200 can precede the commit.
        setWaiting(true);
        decision = await waitForDecision(async () => (await fetchProposal(created.proposalId)).status);
      }
      if (decision === "approved") void navigate("/calendar");
      else if (decision === "pending") setOutcome({ kind: "pending" });
      else setOutcome({ kind: "other", message: "This proposal was decided another way. Start from a fresh preview." });
    } catch (error) {
      const result = classifyApproveError(error);
      if (result.kind === "started") void navigate("/calendar");
      else setOutcome(result);
    } finally {
      setWaiting(false);
    }
  };

  const create = () =>
    createBlock.mutate(body, {
      onSuccess: (made) => onCreated({ blockId: made.block_id, proposalId: made.proposal.id }),
    });

  const data = preview.data;
  const summary = data ? summarize(data.draft.sessions) : null;
  const violations = data?.validation.violations ?? [];
  const warnings = data?.validation.warnings ?? [];
  const blocked = violations.length > 0;

  return (
    <Stack gap="5">
      <Heading size="lg">Start date</Heading>

      <Field.Root>
        <Field.Label>First day of week 1</Field.Label>
        <NativeSelect.Root disabled={approve.isPending || waiting}>
          <NativeSelect.Field
            aria-label="Start date"
            value={startDate}
            onChange={(e) => onStartDate(e.target.value)}
          >
            {mondays.map((iso) => (
              <option key={iso} value={iso}>
                {formatDate(iso)}
              </option>
            ))}
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
        <Field.HelperText>A block starts on a Monday.</Field.HelperText>
      </Field.Root>

      {preview.isPending && <LoadingState label="Planning your block…" />}
      {preview.isError && (
        <ErrorState title="Could not preview this block" message={preview.error.message} onRetry={() => void preview.refetch()} />
      )}

      {data && summary && (
        <Stack gap="4">
          {data.has_synthetic_one_rms && (
            <ProblemAlert status="warning" title="Development data">
              These weights are computed from synthetic 1RMs seeded for local testing.
            </ProblemAlert>
          )}

          <Box borderWidth="1px" rounded="md" p="4">
            <Stack gap="2">
              <Text>
                <strong>{summary.sessionCount}</strong> sessions
                {summary.endDate && <> · ends {formatDate(summary.endDate)}</>}
              </Text>
              {summary.tests.map((test) => (
                <HStack key={`${test.lift}-${test.date}`} gap="2">
                  <LiftBadge lift={test.lift} />
                  <Text fontSize="sm">tested {formatDate(test.date)}</Text>
                </HStack>
              ))}
            </Stack>
          </Box>

          {blocked && (
            <ProblemAlert
              title="This plan breaks rules, so it cannot be created"
              lines={violations.map((v) => v.message)}
            />
          )}

          {warnings.length > 0 && (
            <Collapsible.Root>
              <Collapsible.Trigger asChild>
                <Button variant="ghost" size="sm">
                  {warnings.length} warning{warnings.length === 1 ? "" : "s"} (do not block)
                </Button>
              </Collapsible.Trigger>
              <Collapsible.Content>
                <ProblemAlert status="warning" title="Worth a look" lines={warnings.map((w) => w.message)} />
              </Collapsible.Content>
            </Collapsible.Root>
          )}

          <BlockPreviewTable sessions={data.draft.sessions} />
        </Stack>
      )}

      {outcome?.kind === "unavailable" && (
        <ProblemAlert title="The workflow service is not reachable">
          {outcome.message} Start it with <code>make infra</code> and <code>make worker</code>, then press
          “Start this block” again. Your entries are kept.
        </ProblemAlert>
      )}
      {outcome?.kind === "pending" && (
        <ProblemAlert title="Waiting for the workflow worker" status="warning">
          The block was handed to the workflow service, but nothing has applied it yet. Is the
          worker running? Start it with <code>make worker</code>: the block begins by itself as soon
          as it does. Press “Start this block” again to check. Your entries are kept.
        </ProblemAlert>
      )}
      {outcome?.kind === "invalid" && <ProblemAlert title="This plan no longer validates" lines={outcome.lines} />}
      {outcome?.kind === "stale" && (
        <ProblemAlert title="This proposal has changed" status="warning">
          {outcome.message}{" "}
          <Button
            size="xs"
            variant="outline"
            onClick={() => {
              onCreated(null);
              setOutcome(null);
              void preview.refetch();
            }}
          >
            Start from a fresh preview
          </Button>
        </ProblemAlert>
      )}
      {outcome?.kind === "other" && <ProblemAlert title="Could not start the block" lines={[outcome.message]} />}

      <HStack justify="space-between">
        <Button variant="outline" onClick={onBack} disabled={createBlock.isPending || approve.isPending || waiting}>
          Back
        </Button>
        {created === null ? (
          <Button onClick={create} disabled={!data || blocked} loading={createBlock.isPending}>
            Create block
          </Button>
        ) : (
          <Button onClick={() => void start()} loading={approve.isPending || waiting}>
            Start this block
          </Button>
        )}
      </HStack>
    </Stack>
  );
}
