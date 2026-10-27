import { Button, CloseButton, Dialog, Field, Portal, Textarea } from "@chakra-ui/react";
import { useState } from "react";

interface Props {
  open: boolean;
  pending: boolean;
  onCancel: () => void;
  onReject: (reason: string) => void;
}

/**
 * Rejecting, with an optional reason.
 *
 * The reason is kept on the proposal and is the only record of why an option was
 * turned down, so it is offered -- but never required: a reschedule the athlete
 * simply does not want needs no justification.
 */
export function RejectDialog({ open, pending, onCancel, onReject }: Props) {
  const [reason, setReason] = useState("");

  return (
    <Dialog.Root open={open} onOpenChange={(e) => !e.open && onCancel()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>Reject this proposal</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Field.Root>
                <Field.Label>Why? (optional)</Field.Label>
                <Textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Kept for the record, so a later you knows what this was about."
                  rows={3}
                />
              </Field.Root>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onCancel} disabled={pending}>
                Cancel
              </Button>
              <Button colorPalette="red" onClick={() => onReject(reason)} loading={pending}>
                Reject
              </Button>
            </Dialog.Footer>
            <Dialog.CloseTrigger asChild>
              <CloseButton size="sm" />
            </Dialog.CloseTrigger>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
