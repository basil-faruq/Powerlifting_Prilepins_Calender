import { SimpleGrid, Stack, Text } from "@chakra-ui/react";
import type { ProposalOut } from "@/api/types";
import { ProposalCard } from "./ProposalCard";

interface Props {
  proposals: readonly ProposalOut[];
  highlightId?: string | null;
}

/**
 * The answers to one event, side by side.
 *
 * The solver can offer up to three ways out of the same problem, ranked by cost.
 * Approving any one of them supersedes the rest, so they are shown together and
 * labelled -- choosing between them is the whole point of the screen.
 *
 * They stack on a phone, where side by side would make three unreadable columns.
 */
export function ProposalOptions({ proposals, highlightId = null }: Props) {
  const many = proposals.length > 1;

  return (
    <Stack gap="2">
      {many && (
        <Text fontSize="sm" color="fg.muted">
          {proposals.length} ways to fix this. Approving one drops the others.
        </Text>
      )}
      <SimpleGrid columns={{ base: 1, md: many ? 2 : 1 }} gap="3" alignItems="start">
        {proposals.map((proposal, i) => (
          <ProposalCard
            key={proposal.id}
            proposal={proposal}
            optionLabel={many ? `Option ${i + 1} of ${proposals.length}` : undefined}
            highlighted={proposal.id === highlightId}
          />
        ))}
      </SimpleGrid>
    </Stack>
  );
}
