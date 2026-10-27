import { Badge, Box, Button, Collapsible, HStack, Separator, Stack, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { fetchProposal, useApproveProposal, useRejectProposal } from "@/api/hooks/proposals";
import type { ProposalOut, Violation } from "@/api/types";
import { BlockPreviewTable } from "@/components/BlockPreviewTable";
import { LiftBadge } from "@/components/LiftBadge";
import { ProblemAlert } from "@/components/ProblemAlert";
import { toaster } from "@/components/ui/toaster";
import { classifyApproveError, waitForDecision, type ApproveOutcome } from "@/lib/approval";
import { formatDate } from "@/lib/format";
import { WEEKDAYS } from "@/lib/lifts";
import { readCost, readPlan, readSlots } from "@/lib/proposalPayload";
import { EditProposalDialog } from "./EditProposalDialog";
import { RejectDialog } from "./RejectDialog";
import { changeLines, decidedLabel, isDecided, sourceLabel, titleFor } from "./model";

interface Props {
  proposal: ProposalOut;
  /** "Option 2 of 3" when the solver offered several answers to one event. */
  optionLabel?: string;
  /** Outlined when the calendar's banner sent the athlete to this one. */
  highlighted?: boolean;
}

/**
 * One proposal, and the decision it is waiting for.
 *
 * Approving is the only write path in the app (ADR-0007), and this is one of its two
 * doors -- the setup wizard is the other. Nothing here edits a session directly.
 */
export function ProposalCard({ proposal, optionLabel, highlighted = false }: Props) {
  // Both explain their own failures in place, so neither raises the generic toast.
  const approve = useApproveProposal({ silent: true });
  const reject = useRejectProposal({ silent: true });

  const [outcome, setOutcome] = useState<ApproveOutcome | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [editing, setEditing] = useState(false);
  /**
   * Violations from an edit the server refused. Nothing was stored (ADR-0007 mod 7),
   * so the proposal below is still the original and still approvable -- but approving
   * while these are on screen would approve something other than what was just asked
   * for, so the button waits until they are dismissed (spec 19 §12).
   */
  const [editViolations, setEditViolations] = useState<Violation[] | null>(null);

  const cardRef = useRef<HTMLDivElement>(null);
  // Bring the card the calendar's banner pointed at into view -- the card itself,
  // not the tray around it, which says nothing about which of three options it is.
  useEffect(() => {
    if (highlighted) cardRef.current?.scrollIntoView({ block: "nearest" });
  }, [highlighted]);

  const decided = isDecided(proposal);
  const busy = approve.isPending || reject.isPending || waiting;
  const lines = changeLines(proposal);
  const cost = readCost(proposal.cost);
  const plan = proposal.kind === "new_block" ? readPlan(proposal.payload) : [];

  const onApprove = async () => {
    setOutcome(null);
    try {
      const result = await approve.mutateAsync(proposal.id);
      // A reschedule is an update against a running workflow and comes back decided.
      // A first block only STARTS one, so its 200 can still say pending (ADR-0019).
      let decision = result.status === "approved" ? "approved" : "pending";
      if (decision === "pending") {
        setWaiting(true);
        decision = await waitForDecision(async () => (await fetchProposal(proposal.id)).status);
      }
      if (decision === "approved") toaster.create({ type: "success", title: "Approved" });
      else if (decision === "pending") setOutcome({ kind: "pending" });
      else setOutcome({ kind: "stale", message: "This proposal was decided another way." });
    } catch (error) {
      const result = classifyApproveError(error);
      // "Already approved" means the first attempt landed and only its answer was lost.
      if (result.kind === "started") toaster.create({ type: "success", title: "Approved" });
      else if (result.kind === "stale") {
        toaster.create({ type: "error", title: "This proposal is out of date", description: result.message });
      } else setOutcome(result);
    } finally {
      setWaiting(false);
    }
  };

  const onReject = (reason: string) => {
    reject.mutate(
      { proposalId: proposal.id, reason },
      {
        onSuccess: () => {
          setRejecting(false);
          toaster.create({ type: "success", title: "Rejected" });
        },
        onError: (error) => {
          setRejecting(false);
          const result = classifyApproveError(error);
          // "Already approved" is a success for APPROVE and a genuine conflict here:
          // there is nothing left to reject. Either way the card is out of date.
          if (result.kind === "stale" || result.kind === "started") {
            toaster.create({
              type: "error",
              title: "This proposal is out of date",
              description:
                result.kind === "started"
                  ? "It has already been approved."
                  : result.message,
            });
          } else setOutcome(result);
        },
      },
    );
  };

  return (
    <Box
      ref={cardRef}
      borderWidth={highlighted ? "2px" : "1px"}
      borderColor={highlighted ? "blue.solid" : undefined}
      rounded="md"
      p="4"
      opacity={decided ? 0.6 : 1}
      data-testid="proposal-card"
      // The outline is not the only signal: colour alone would say nothing to a
      // screen reader, or to anyone who cannot separate it from its neighbours.
      aria-current={highlighted ? "true" : undefined}
    >
      <Stack gap="3">
        <Stack direction="row" justify="space-between" align="flex-start" gap="2" wrap="wrap">
          <Stack gap="1" minW="0">
            <Text fontWeight="semibold">{titleFor(proposal)}</Text>
            <HStack gap="2" wrap="wrap">
              <Badge variant="subtle">{sourceLabel(proposal)}</Badge>
              {optionLabel && <Badge variant="outline">{optionLabel}</Badge>}
              {highlighted && <Badge colorPalette="blue">the one you came from</Badge>}
              {decided && <Badge colorPalette="gray">{decidedLabel(proposal)}</Badge>}
            </HStack>
          </Stack>
        </Stack>

        <WeekdaySlots proposal={proposal} />

        {proposal.reasons.length > 0 && (
          <Stack gap="1">
            {proposal.reasons.map((reason, i) => (
              <Text key={`${i}-${reason}`} fontSize="sm" color="fg.muted">
                {reason}
              </Text>
            ))}
          </Stack>
        )}

        {lines.length > 0 && (
          <Stack gap="1" as="ul" listStyleType="none" aria-label="Proposed changes">
            {lines.map((line) => (
              <HStack as="li" key={`${line.seq}-${line.action}`} gap="2" wrap="wrap" fontSize="sm">
                {line.lifts.map((lift) => (
                  <LiftBadge key={lift} lift={lift} short />
                ))}
                {line.note === null && line.from !== null && line.to !== null ? (
                  <Text>
                    {formatDate(line.from)} → {formatDate(line.to)}
                  </Text>
                ) : (
                  <Text>
                    {line.note}
                    {line.from !== null && <> · {formatDate(line.from)}</>}
                  </Text>
                )}
              </HStack>
            ))}
          </Stack>
        )}

        <CostLine cost={cost} />

        {plan.length > 0 && (
          <Collapsible.Root>
            <Collapsible.Trigger asChild>
              <Button variant="ghost" size="sm" alignSelf="flex-start">
                Show the whole plan
              </Button>
            </Collapsible.Trigger>
            <Collapsible.Content>
              <Box pt="2">
                <BlockPreviewTable sessions={plan} />
              </Box>
            </Collapsible.Content>
          </Collapsible.Root>
        )}

        {proposal.warnings.length > 0 && (
          <Collapsible.Root>
            <Collapsible.Trigger asChild>
              <Button variant="ghost" size="sm" alignSelf="flex-start">
                {proposal.warnings.length} warning{proposal.warnings.length === 1 ? "" : "s"} (do not
                block)
              </Button>
            </Collapsible.Trigger>
            <Collapsible.Content>
              <ProblemAlert
                status="warning"
                title="Worth a look"
                lines={proposal.warnings.map((w) => w.message)}
              />
            </Collapsible.Content>
          </Collapsible.Root>
        )}

        {editViolations !== null && (
          <ProblemAlert title="That edit does not validate, so it was not applied" lines={editViolations.map((v) => v.message)}>
            The proposal below is unchanged and can still be approved as it stands.{" "}
            <Button size="xs" variant="outline" onClick={() => setEditViolations(null)}>
              Discard edit
            </Button>
          </ProblemAlert>
        )}

        <ApproveOutcomeAlert outcome={outcome} />

        {!decided && (
          <>
            <Separator />
            <HStack gap="2" wrap="wrap">
              <Button
                size="sm"
                onClick={() => void onApprove()}
                loading={approve.isPending || waiting}
                disabled={busy || editViolations !== null}
              >
                Approve
              </Button>
              <Button size="sm" variant="outline" onClick={() => setRejecting(true)} disabled={busy}>
                Reject
              </Button>
              {lines.some((line) => line.action === "move") && (
                <Button size="sm" variant="ghost" onClick={() => setEditing(true)} disabled={busy}>
                  Edit
                </Button>
              )}
            </HStack>
          </>
        )}
      </Stack>

      <RejectDialog
        open={rejecting}
        pending={reject.isPending}
        onCancel={() => setRejecting(false)}
        onReject={onReject}
      />
      <EditProposalDialog
        proposal={proposal}
        open={editing}
        onClose={() => setEditing(false)}
        onRefused={(violations) => {
          setEditing(false);
          setEditViolations(violations);
        }}
        onFailed={(message) => {
          // The edit never reached a verdict, so the proposal is untouched and stays
          // approvable: this is an outcome, not a validation failure.
          setEditing(false);
          setOutcome({ kind: "other", message });
        }}
        onEdited={() => {
          setEditing(false);
          setEditViolations(null);
          toaster.create({ type: "success", title: "Edit applied", description: "Re-validated and still awaiting approval." });
        }}
      />
    </Box>
  );
}

/**
 * The training days a `weekday_change` proposes.
 *
 * Without this the card offered a change of training days while naming none of them,
 * which is not something anyone can approve meaningfully. `slots` is the whole
 * content of that payload -- it carries no `changes` for the generic list to draw.
 */
function WeekdaySlots({ proposal }: { proposal: ProposalOut }) {
  if (proposal.kind !== "weekday_change") return null;
  const slots = readSlots(proposal.payload);
  if (slots === null) return null;

  return (
    <Stack gap="1" as="ul" listStyleType="none" aria-label="Proposed training days">
      {[...slots].map(([slot, weekday]) => (
        <Text as="li" key={slot} fontSize="sm">
          Day {slot} → {WEEKDAYS[weekday] ?? `weekday ${weekday}`}
        </Text>
      ))}
    </Stack>
  );
}

/**
 * What the option costs, as the solver scored it. These are floats for ranking and
 * display (ADR-0006); no weight is computed from them.
 */
function CostLine({ cost }: { cost: ReturnType<typeof readCost> }) {
  const parts: string[] = [];
  if (cost.delayDays !== null) parts.push(`${cost.delayDays} day${cost.delayDays === 1 ? "" : "s"} later`);
  if (cost.offWeekdayCount !== null) {
    parts.push(`${cost.offWeekdayCount} session${cost.offWeekdayCount === 1 ? "" : "s"} off their weekday`);
  }
  if (cost.endSlipDays !== null) {
    parts.push(`block ends ${cost.endSlipDays} day${cost.endSlipDays === 1 ? "" : "s"} later`);
  }
  if (parts.length === 0) return null;
  return (
    <Text fontSize="sm" color="fg.muted">
      {parts.join(" · ")}
    </Text>
  );
}

function ApproveOutcomeAlert({ outcome }: { outcome: ApproveOutcome | null }) {
  if (outcome === null) return null;
  switch (outcome.kind) {
    case "unavailable":
      return (
        // The server's own sentence is shown only when it says something the heading
        // does not: a 503 here usually reads "the workflow service is not reachable",
        // which would otherwise appear twice in the same alert.
        <ProblemAlert title="Not approved: the workflow did not answer">
          {!/not reachable/i.test(outcome.message) && <>{outcome.message} </>}
          Approving goes through the block's workflow, so it needs a worker: start one with{" "}
          <code>make worker</code> and press Approve again. Nothing was lost.
        </ProblemAlert>
      );
    case "pending":
      return (
        <ProblemAlert title="Waiting for the workflow worker" status="warning">
          This was handed to the workflow service, but nothing has applied it yet. Start a worker
          with <code>make worker</code>; it is applied as soon as one runs.
        </ProblemAlert>
      );
    case "invalid":
      return <ProblemAlert title="This plan no longer validates" lines={outcome.lines} />;
    case "stale":
      return <ProblemAlert title="This proposal has changed" status="warning" lines={[outcome.message]} />;
    case "other":
      return <ProblemAlert title="Could not decide this proposal" lines={[outcome.message]} />;
    case "started":
      return null;
  }
}
