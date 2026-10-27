import { Button, HStack, Stack, Textarea, Wrap } from "@chakra-ui/react";
import { useState, type KeyboardEvent } from "react";
import { SUGGESTED_PROMPTS } from "./model";

interface Props {
  pending: boolean;
  /** True before anything has been said, when the suggestions are worth the room. */
  empty: boolean;
  onSend: (message: string) => void;
}

export function ChatInput({ pending, empty, onSend }: Props) {
  const [text, setText] = useState("");
  const ready = text.trim().length > 0 && !pending;

  const send = () => {
    if (!ready) return;
    onSend(text.trim());
    setText("");
  };

  /** Enter sends; Shift+Enter is a new line. IME composition must not send. */
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    send();
  };

  return (
    <Stack gap="2">
      {empty && (
        <Wrap gap="2">
          {SUGGESTED_PROMPTS.map((prompt) => (
            <Button
              key={prompt}
              size="xs"
              variant="outline"
              disabled={pending}
              onClick={() => onSend(prompt)}
            >
              {prompt}
            </Button>
          ))}
        </Wrap>
      )}
      <HStack gap="2" align="flex-end">
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about your training…"
          aria-label="Message"
          rows={2}
          resize="none"
          disabled={pending}
        />
        <Button onClick={send} disabled={!ready} loading={pending}>
          Send
        </Button>
      </HStack>
    </Stack>
  );
}
