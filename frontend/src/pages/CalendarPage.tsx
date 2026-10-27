import { CloseButton, Drawer, Portal } from "@chakra-ui/react";
import { useState } from "react";
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
  const [proposalId, setProposalId] = useState<string | null>(null);

  return (
    <>
      <BlockCalendar onOpenProposal={setProposalId} />

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
    </>
  );
}
