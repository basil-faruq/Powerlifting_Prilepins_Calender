import { Box, Button, Collapsible, Stack, Text } from "@chakra-ui/react";
import { useProposals } from "@/api/hooks/proposals";
import { ErrorState } from "@/components/states/ErrorState";
import { groupOptions } from "./model";
import { ProposalOptions } from "./ProposalOptions";

interface Props {
  athleteId: string | null;
  /** A proposal the calendar's banner pointed at: outlined, named, and scrolled to. */
  highlightId?: string | null;
  /**
   * Proposals already drawn elsewhere on the screen. The chat transcript shows a
   * turn's own proposals as cards, and listing them here too would offer the same
   * decision twice, with two sets of buttons.
   */
  excludeIds?: ReadonlySet<string>;
}

/**
 * Everything waiting on a decision, pinned above the conversation.
 *
 * It polls (spec 19 §10), so a proposal the workflow raised while the athlete was
 * elsewhere turns up here without a reload -- which is the only way one ever appears,
 * since nothing pushes to this frontend.
 */
export function PendingTray({ athleteId, highlightId = null, excludeIds }: Props) {
  const pending = useProposals("pending", athleteId);
  const proposals = (pending.data ?? []).filter(
    (proposal) => excludeIds === undefined || !excludeIds.has(proposal.id),
  );
  const groups = groupOptions(proposals);

  if (pending.isError) {
    return (
      <ErrorState
        title="Could not load pending proposals"
        message={pending.error.message}
        onRetry={() => void pending.refetch()}
      />
    );
  }

  if (proposals.length === 0) return null;

  return (
    <Box borderWidth="1px" rounded="md" p="3" bg="bg.subtle">
      <Collapsible.Root defaultOpen>
        {/* A real button: `asChild` onto a plain Stack gave a div with an onClick,
            which no keyboard could reach and no screen reader announced. */}
        <Collapsible.Trigger asChild>
          <Button variant="ghost" size="sm" width="full" justifyContent="space-between">
            <Text fontWeight="semibold">{proposals.length} waiting for you</Text>
            <Text fontSize="sm" color="fg.muted">
              show / hide
            </Text>
          </Button>
        </Collapsible.Trigger>
        <Collapsible.Content>
          <Stack gap="4" pt="3">
            {groups.map((group) => (
              <ProposalOptions
                key={group.key}
                proposals={group.proposals}
                highlightId={highlightId}
              />
            ))}
          </Stack>
        </Collapsible.Content>
      </Collapsible.Root>
    </Box>
  );
}
