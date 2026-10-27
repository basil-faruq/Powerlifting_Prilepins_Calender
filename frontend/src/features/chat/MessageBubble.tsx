import { Box, Button, HStack, Stack, Text } from "@chakra-ui/react";
import { Link } from "react-router";
import type { ChatResponse } from "@/api/types";
import { LiveProposalCard } from "@/features/proposals/LiveProposalCard";
import { RouteBadge } from "./RouteBadge";
import { SimulationCard } from "./SimulationCard";

/** A plain bubble: either side, text only. This is all history can give. */
export function MessageBubble({
  role,
  children,
}: {
  role: "user" | "assistant";
  children: React.ReactNode;
}) {
  const mine = role === "user";
  return (
    <Box
      alignSelf={mine ? "flex-end" : "flex-start"}
      maxW={{ base: "100%", md: "85%" }}
      bg={mine ? "colorPalette.subtle" : "bg.subtle"}
      colorPalette={mine ? "blue" : "gray"}
      borderWidth="1px"
      rounded="md"
      px="3"
      py="2"
    >
      {children}
    </Box>
  );
}

/** Whatever text a bubble holds, with blank lines kept as the athlete typed them. */
export function BubbleText({ children }: { children: string }) {
  return (
    <Text whiteSpace="pre-wrap" wordBreak="break-word">
      {children}
    </Text>
  );
}

/**
 * An answer given in this session, with everything the turn produced attached: which
 * tool answered, any proposals it raised, and any simulation it ran.
 *
 * The proposals are drawn as full cards rather than links. A reschedule offered in
 * conversation is decided in that conversation -- that is the approval gate (ADR-0007).
 */
export function AssistantTurn({ response }: { response: ChatResponse }) {
  return (
    <Stack gap="2" alignSelf="flex-start" maxW={{ base: "100%", md: "85%" }} w="full">
      <MessageBubble role="assistant">
        <Stack gap="2">
          <HStack gap="2" wrap="wrap">
            <RouteBadge route={response.route} />
            <Button asChild variant="plain" size="xs" h="auto" p="0">
              <Link to={`/decisions?id=${response.decision_id}`}>View trace</Link>
            </Button>
          </HStack>
          <BubbleText>{response.reply}</BubbleText>
        </Stack>
      </MessageBubble>

      {response.simulation && <SimulationCard simulation={response.simulation} />}

      {response.proposals.map((proposal) => (
        <LiveProposalCard key={proposal.id} seed={proposal} />
      ))}
    </Stack>
  );
}

/**
 * A turn that never got an answer.
 *
 * A 503 means the model is not running, which is a thing to say in the conversation
 * and offer to redo -- not a toast that vanishes while the athlete reads it.
 */
export function FailedTurn({
  message,
  retryable,
  onRetry,
}: {
  message: string;
  retryable: boolean;
  onRetry: () => void;
}) {
  return (
    <MessageBubble role="assistant">
      <Stack gap="2">
        <Text color="fg.error">{message}</Text>
        {retryable && (
          <Text fontSize="xs" color="fg.muted">
            The language model runs on your own machine. Start it with <code>ollama serve</code>.
          </Text>
        )}
        <Button size="xs" variant="outline" alignSelf="flex-start" onClick={onRetry}>
          Try again
        </Button>
      </Stack>
    </MessageBubble>
  );
}
