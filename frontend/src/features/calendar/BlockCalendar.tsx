import { Box, Button, HStack, Stack, Text } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { useAthlete } from "@/api/hooks/athletes";
import { useBlockSessions, useBlockState } from "@/api/hooks/blocks";
import { useProposals } from "@/api/hooks/proposals";
import type { SessionOut } from "@/api/types";
import { useAthleteContext } from "@/components/AthleteProvider";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorState } from "@/components/states/ErrorState";
import { LoadingState } from "@/components/states/LoadingState";
import { todayIso } from "@/lib/date";
import { CalendarHeader } from "./CalendarHeader";
import { CalendarWeekRow } from "./CalendarWeekRow";
import { NO_GHOSTS, overlayFor, pendingForBlock } from "./ghost";
import { IntensityLegend } from "./IntensityLegend";
import { buildWeekRows, currentMonday, trainingWeekdays } from "./model";
import { ProposalOverlayToggle } from "./ProposalOverlayToggle";
import { SessionDrawer } from "./SessionDrawer";
import { WeekList } from "./WeekList";

/**
 * The block calendar: the main screen (spec 18).
 *
 * Read-only. The only thing it can do is open a drawer, because `approve_proposal`
 * is the single write path (ADR-0007) and the UI never edits sessions directly.
 *
 * The grid is built from `GET /blocks/{id}/sessions` alone. `/blocks/{id}/state` is
 * an enrichment and nothing more: it 503s whenever no Temporal worker is running, and
 * the calendar has to render in full without one (ADR-0018 mod 2).
 */
interface Props {
  /**
   * Open a pending proposal for a decision. The page supplies this, because the
   * conversation that decides a proposal is another feature and features do not
   * import each other; without one, the banner falls back to the chat route.
   */
  onOpenProposal?: (proposalId: string) => void;
  /**
   * Declare a day unavailable, from the drawer's "I can't make it". The dialog
   * is shared with the logger and lives in `components/`, so the page owns it
   * and the calendar only raises which session it was opened on.
   */
  onCantMakeIt?: (sessionId: string) => void;
}

export function BlockCalendar({ onOpenProposal, onCantMakeIt }: Props = {}) {
  const { athlete, athleteId, currentBlockId } = useAthleteContext();
  const navigate = useNavigate();

  const detail = useAthlete(athleteId);
  const sessions = useBlockSessions(currentBlockId);
  const state = useBlockState(currentBlockId);
  const proposals = useProposals("pending", athleteId);

  const [showGhosts, setShowGhosts] = useState(false);
  const [open, setOpen] = useState<SessionOut | null>(null);

  const today = todayIso(athlete?.timezone ?? "Europe/Berlin");

  const rows = useMemo(() => buildWeekRows(sessions.data ?? []), [sessions.data]);
  const sessionsBySeq = useMemo(
    () => new Map((sessions.data ?? []).map((session) => [session.seq, session])),
    [sessions.data],
  );

  // A draft block is never "current", so reaching here with no block means there is
  // genuinely none to show. A PAUSED block is current and does render (ADR-0010 mod 9).
  if (currentBlockId === null) {
    return (
      <EmptyState
        title="No block yet"
        description="Import your template, enter your maxes and pick your training days."
        action={
          <Button asChild variant="solid">
            <Link to="/setup">Set up a block</Link>
          </Button>
        }
      />
    );
  }

  if (sessions.isPending) return <LoadingState label="Loading your block…" />;

  if (sessions.isError) {
    return (
      <ErrorState
        message={sessions.error.message}
        onRetry={() => void sessions.refetch()}
      />
    );
  }

  if (rows.length === 0) {
    return (
      <EmptyState
        title="This block has no sessions yet"
        description="Sessions are written when the block is approved. If you have just approved it, give the workflow worker a moment and reload."
      />
    );
  }

  const trainingDays = trainingWeekdays(detail.data?.weekday_map);
  const current = currentMonday(rows, today, state.data?.next_session?.date ?? null);
  const pending = pendingForBlock(proposals.data, currentBlockId);
  const overlay = overlayFor(proposals.data, currentBlockId);
  const ghosts = overlay?.plan ?? NO_GHOSTS;

  return (
    <Stack gap="4" minW="0">
      {/* The block and its workflow state are already named in the shell's header
          (spec 16 §5A), so this says only what the shell does not. */}
      <HStack gap="2" wrap="wrap" minW="0">
        <Text fontSize="sm" color="fg.muted">
          {sessions.data.length} sessions · {rows.length} weeks
        </Text>
        {state.isError && (
          <Text fontSize="sm" color="fg.muted">
            · workflow status unavailable, the plan below is unaffected
          </Text>
        )}
      </HStack>

      <ProposalOverlayToggle
        pending={pending}
        drawable={overlay === null ? null : { proposal: overlay.proposal, moves: overlay.plan.moves.length }}
        show={showGhosts}
        onToggle={setShowGhosts}
        onOpenProposal={(proposalId) =>
          onOpenProposal === undefined
            ? void navigate(`/chat?proposal=${proposalId}`)
            : onOpenProposal(proposalId)
        }
      />

      {/* Two layouts, both in the DOM, switched by CSS -- the idiom AppShell uses.
          Seven columns do not fit 375 px, so below `md` the grid gives way to cards. */}
      <Box display={{ base: "none", md: "block" }} minW="0">
        <CalendarHeader trainingDays={trainingDays} />
        <Stack gap="1" minW="0">
          {rows.map((row) => (
            <CalendarWeekRow
              key={row.monday}
              row={row}
              today={today}
              isCurrent={row.monday === current}
              trainingDays={trainingDays}
              ghosts={ghosts}
              showGhosts={showGhosts}
              sessionsBySeq={sessionsBySeq}
              onOpen={setOpen}
            />
          ))}
        </Stack>
      </Box>

      <Box display={{ base: "block", md: "none" }} minW="0">
        <WeekList
          rows={rows}
          today={today}
          currentMonday={current}
          ghosts={ghosts}
          showGhosts={showGhosts}
          sessionsBySeq={sessionsBySeq}
          onOpen={setOpen}
        />
      </Box>

      <IntensityLegend />

      <SessionDrawer
        session={open}
        today={today}
        onClose={() => setOpen(null)}
        onCantMakeIt={
          onCantMakeIt === undefined
            ? undefined
            : (sessionId) => {
                setOpen(null);
                onCantMakeIt(sessionId);
              }
        }
      />
    </Stack>
  );
}
