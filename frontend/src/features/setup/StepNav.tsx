import { Button, HStack, Stack, Text } from "@chakra-ui/react";

interface Props {
  onBack?: () => void;
  onNext: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  /** An action is running; the button shows a spinner and cannot be pressed twice. */
  busy?: boolean;
  /** Why Next is disabled, when it is not obvious. */
  hint?: string;
}

export function StepNav({ onBack, onNext, nextLabel = "Next", nextDisabled = false, busy = false, hint }: Props) {
  return (
    <Stack gap="2" pt="2">
      {hint && nextDisabled && (
        <Text fontSize="sm" color="fg.muted">
          {hint}
        </Text>
      )}
      <HStack justify="space-between">
        {onBack ? (
          <Button variant="outline" onClick={onBack} disabled={busy}>
            Back
          </Button>
        ) : (
          <span />
        )}
        <Button onClick={onNext} disabled={nextDisabled || busy} loading={busy}>
          {nextLabel}
        </Button>
      </HStack>
    </Stack>
  );
}
