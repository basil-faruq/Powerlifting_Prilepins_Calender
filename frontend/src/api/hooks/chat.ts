import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../client";
import { keys } from "../keys";
import { saveConversationId } from "@/lib/conversationStore";
import type { ChatMessageOut, ChatResponse } from "../types";
import { unwrap } from "../unwrap";

/**
 * One conversation's messages, oldest first.
 *
 * Read once when the panel opens and then left alone: every later turn arrives as the
 * answer to the send that caused it, so refetching would only re-fetch what the panel
 * already has -- and would briefly show the turn twice. `athlete_id` is required by
 * the route, which 404s a conversation belonging to someone else.
 */
export const conversationOptions = (conversationId: string, athleteId: string) =>
  queryOptions({
    queryKey: keys.conversations.messages(conversationId),
    queryFn: () =>
      unwrap(
        api.GET("/conversations/{conversation_id}/messages", {
          params: {
            path: { conversation_id: conversationId },
            query: { athlete_id: athleteId },
          },
        }),
      ),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

export const useConversationMessages = (conversationId: string | null, athleteId: string | null) =>
  useQuery({
    ...conversationOptions(conversationId ?? "", athleteId ?? ""),
    enabled: conversationId !== null && athleteId !== null,
  });

/**
 * Send a message and get the whole turn back: the reply, the route that produced it,
 * any proposals it raised and any simulation it ran.
 *
 * Errors are silent because the panel draws them as a bubble with a retry -- a 503
 * means the model is down, which is a thing to say in the conversation rather than in
 * a toast that disappears.
 */
export function useSendMessage() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: { silent: true },
    mutationFn: ({
      athleteId,
      message,
      conversationId,
    }: {
      athleteId: string;
      message: string;
      conversationId: string | null;
    }) =>
      unwrap(
        api.POST("/chat", {
          body: {
            athlete_id: athleteId,
            message,
            conversation_id: conversationId,
          },
        }),
      ),
    /**
     * Write the exchange into the conversation's cache rather than re-reading it.
     *
     * The route commits both messages before it answers, so enabling the history
     * query on a conversation id this turn just created would fetch back the very
     * turn the panel is already showing -- the first exchange of every new
     * conversation, twice. Appending here keeps one copy, and keeps the transcript
     * when the panel unmounts and the (`staleTime: Infinity`) cache is reused.
     *
     * It lives in the hook, not at the call site: TanStack Query drops a `mutate`
     * caller's own callbacks if that component unmounted first, which is exactly
     * what happens when the calendar's drawer is closed while the model is thinking.
     */
    onSuccess: (response: ChatResponse | undefined, variables) => {
      if (response === undefined) return;
      saveConversationId(variables.athleteId, response.conversation_id);
      const key = keys.conversations.messages(response.conversation_id);
      const stamp = new Date().toISOString();
      queryClient.setQueryData<ChatMessageOut[]>(key, (existing) => [
        ...(existing ?? []),
        {
          id: `${response.decision_id}-q`,
          role: "user",
          content: variables.message,
          created_at: stamp,
          decision_id: response.decision_id,
        },
        {
          id: `${response.decision_id}-a`,
          role: "assistant",
          content: response.reply,
          created_at: stamp,
          decision_id: response.decision_id,
        },
      ]);
    },
  });
}
