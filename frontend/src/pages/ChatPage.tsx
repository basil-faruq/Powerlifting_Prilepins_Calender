import { Stack } from "@chakra-ui/react";
import { useSearchParams } from "react-router";
import { ChatPanel } from "@/features/chat/ChatPanel";

export function ChatPage() {
  // `?proposal=` is how the calendar's banner points at the one it was showing.
  const [params] = useSearchParams();

  return (
    <Stack h="full" minH="0">
      <ChatPanel highlightId={params.get("proposal")} />
    </Stack>
  );
}
