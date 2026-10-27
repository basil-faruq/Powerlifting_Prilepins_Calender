import { CloseButton, Drawer, Portal } from "@chakra-ui/react";
import { useState } from "react";
import { useSession } from "@/api/hooks/sessions";
import { useAthleteContext } from "@/components/AthleteProvider";
import { UnavailableDialog } from "@/components/UnavailableDialog";
import { ChatPanel } from "@/features/chat/ChatPanel";
import { BlockCalendar } from "@/features/calendar/BlockCalendar";

/**
 * The calendar, and the conversation that decides what it shows.
 *
 * The page composes the two: the grid raises a proposal id, and the chat panel opens
 * over it already pointing at that proposal. Keeping the wiring here is what lets
 * `features/calendar` and `features/chat` stay unaware of each other.
 */
export function CalendarPage() {
  const { currentBlockId } = useAthleteContext();
  const [proposalId, setProposalId] = useState<string | null>(null);
  const [awaySessionId, setAwaySessionId] = useState<string | null>(null);

  // The dialog defaults its "from" to the day the athlete pressed the button on,
  // which is the session's date rather than today.
  const away = useSession(awaySessionId);

  return (
    <>
      <BlockCalendar onOpenProposal={setProposalId} onCantMakeIt={setAwaySessionId} />

      <Drawer.Root
        open={proposalId !== null}
        onOpenChange={(e) => !e.open && setProposalId(null)}
        size="lg"
      >
        <Portal>
          <Drawer.Backdrop />
          <Drawer.Positioner>
            <Drawer.Content>
              <Drawer.Header>
                <Drawer.Title>Waiting for a decision</Drawer.Title>
              </Drawer.Header>
              <Drawer.Body>
                {proposalId !== null && <ChatPanel highlightId={proposalId} />}
              </Drawer.Body>
              <Drawer.CloseTrigger asChild>
                <CloseButton size="sm" />
              </Drawer.CloseTrigger>
            </Drawer.Content>
          </Drawer.Positioner>
        </Portal>
      </Drawer.Root>

      <UnavailableDialog
        open={awaySessionId !== null}
        blockId={currentBlockId}
        defaultFrom={away.data?.date}
        onClose={() => setAwaySessionId(null)}
      />
    </>
  );
}
