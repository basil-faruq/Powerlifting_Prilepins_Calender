import { Stack, Text } from "@chakra-ui/react";
import { useRef, useState } from "react";
import { useConversationMessages, useSendMessage } from "@/api/hooks/chat";
import { ApiError, errorMessage, isNetworkError } from "@/api/errors";
import type { ChatResponse } from "@/api/types";
import { useAthleteContext } from "@/components/AthleteProvider";
import { EmptyState } from "@/components/states/EmptyState";
import { LoadingState } from "@/components/states/LoadingState";
import { PendingTray } from "@/features/proposals/PendingTray";
import { clearConversationId, loadConversationId } from "@/lib/conversationStore";
import { ChatInput } from "./ChatInput";
import { AssistantTurn, BubbleText, FailedTurn, MessageBubble } from "./MessageBubble";
import type { OpenTurn } from "./model";

/**
 * The conversation, with whatever is awaiting a decision pinned above it.
 *
 * Shared by `/chat` and the drawer the calendar's banner opens, so a proposal can be
 * decided without leaving the grid it would change.
 *
 * The transcript has ONE source: the conversation's cached message list, which the
 * send mutation appends to. Anything a turn produced beyond its text -- the route it
 * took, the proposals it raised, the simulation it ran -- is held alongside in
 * `details`, keyed on the decision id the messages carry.
 */
export function ChatPanel({ highlightId = null }: { highlightId?: string | null }) {
  const { athleteId } = useAthleteContext();
  const send = useSendMessage();

  const [conversationId, setConversationId] = useState<string | null>(() =>
    athleteId === null ? null : loadConversationId(athleteId),
  );
  const [details, setDetails] = useState<ReadonlyMap<string, ChatResponse>>(new Map());
  const [open, setOpen] = useState<OpenTurn | null>(null);
  const nextId = useRef(0);

  const history = useConversationMessages(conversationId, athleteId);

  if (athleteId === null) {
    return <EmptyState title="No athlete yet" description="Set up a block before chatting." />;
  }

  const ask = (question: string) => {
    const id = `turn-${nextId.current++}`;
    setOpen({ id, question, error: null });

    send.mutate(
      { athleteId, message: question, conversationId },
      {
        onSuccess: (response) => {
          if (response === undefined) return;
          // The mutation has already written both messages into this conversation's
          // cache and persisted the id, so the question leaves the "open" slot and
          // reappears as part of the transcript.
          setConversationId(response.conversation_id);
          setDetails((previous) => new Map(previous).set(response.decision_id, response));
          setOpen(null);
        },
        onError: (error) => {
          // A conversation the server does not know is one worth forgetting: the next
          // message then starts a fresh thread instead of failing the same way again.
          if (error instanceof ApiError && error.status === 404 && conversationId !== null) {
            clearConversationId(athleteId);
            setConversationId(null);
          }
          const retryable =
            isNetworkError(error) || (error instanceof ApiError && error.status === 503);
          setOpen({ id, question, error: { message: errorMessage(error), retryable } });
        },
      },
    );
  };

  const past = history.data ?? [];
  // The tray lists what is waiting; a card already drawn under its own turn is not
  // listed a second time with a second set of buttons.
  const shownInTranscript = new Set(
    [...details.values()].flatMap((response) => response.proposals.map((p) => p.id)),
  );

  return (
    <Stack gap="4" h="full" minH="0">
      <PendingTray athleteId={athleteId} highlightId={highlightId} excludeIds={shownInTranscript} />

      <Stack gap="3" flex="1" minH="0" overflowY="auto">
        {history.isPending && conversationId !== null && (
          <LoadingState label="Loading the conversation…" />
        )}

        {past.map((message) => {
          const detail =
            message.role === "assistant" && message.decision_id !== null
              ? details.get(message.decision_id)
              : undefined;
          // A reply from THIS session shows everything its turn produced; one read
          // back from an earlier session is text, because that is all history keeps.
          return detail === undefined ? (
            <MessageBubble key={message.id} role={message.role}>
              <BubbleText>{message.content}</BubbleText>
            </MessageBubble>
          ) : (
            <AssistantTurn key={message.id} response={detail} />
          );
        })}

        {open !== null && (
          <Stack gap="2">
            <MessageBubble role="user">
              <BubbleText>{open.question}</BubbleText>
            </MessageBubble>
            {open.error === null ? (
              <Text fontSize="sm" color="fg.muted">
                Thinking…
              </Text>
            ) : (
              <FailedTurn
                message={open.error.message}
                retryable={open.error.retryable}
                onRetry={() => ask(open.question)}
              />
            )}
          </Stack>
        )}
      </Stack>

      <ChatInput
        pending={send.isPending}
        empty={past.length === 0 && open === null}
        onSend={ask}
      />
    </Stack>
  );
}
