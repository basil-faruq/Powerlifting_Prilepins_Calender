import { Alert, Button, Stack, Switch, Text } from "@chakra-ui/react";
import type { ProposalOut } from "@/api/types";

interface Props {
  /** Every pending proposal for the block on screen. The banner counts them. */
  pending: readonly ProposalOut[];
  /** The one the overlay can actually draw, with how many sessions it would move. */
  drawable: { proposal: ProposalOut; moves: number } | null;
  show: boolean;
  onToggle: (show: boolean) => void;
  /** Opens the proposal itself. Spec 19 turns this into the chat drawer. */
  onOpenProposal: (proposalId: string) => void;
}

/**
 * The pending-proposal banner, and the switch that draws its changes over the grid
 * (spec 18 §4C).
 *
 * The banner and the switch are separate on purpose. A pending proposal always
 * deserves saying -- it is waiting on the athlete -- but not every one can be drawn:
 * a `repeat_week` carries no dates of its own, and a payload this frontend cannot
 * read yields no moves. In those cases the banner stands alone and the switch is
 * absent, rather than offering a toggle that would change nothing.
 */
export function ProposalOverlayToggle({ pending, drawable, show, onToggle, onOpenProposal }: Props) {
  if (pending.length === 0) return null;

  const first = pending[0];
  if (first === undefined) return null;

  const count = pending.length;

  return (
    <Alert.Root status="info" alignItems="start">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>
          {count === 1 ? "A change is waiting for you" : `${count} changes are waiting for you`}
        </Alert.Title>
        <Alert.Description>
          <Stack gap="2" minW="0" align="start">
            {first.reasons.length > 0 && (
              // The backend worded these, and the explainer is forbidden from adding a
              // number to them (planner_core/proposal.py). Shown verbatim.
              <Stack gap="0.5" minW="0">
                {first.reasons.map((reason, i) => (
                  <Text key={i} fontSize="sm">
                    {reason}
                  </Text>
                ))}
              </Stack>
            )}

            {drawable !== null && (
              <Switch.Root checked={show} onCheckedChange={(e) => onToggle(e.checked)} size="sm">
                <Switch.HiddenInput />
                <Switch.Control />
                <Switch.Label>
                  Show proposed changes ({drawable.moves}{" "}
                  {drawable.moves === 1 ? "session moves" : "sessions move"})
                </Switch.Label>
              </Switch.Root>
            )}

            <Button size="xs" variant="outline" onClick={() => onOpenProposal(first.id)}>
              {count === 1 ? "Review it" : "Review them"}
            </Button>
          </Stack>
        </Alert.Description>
      </Alert.Content>
    </Alert.Root>
  );
}
