import { Center, Spinner, Text, VStack } from "@chakra-ui/react";

interface Props {
  label?: string;
  /** Fill the viewport: for the moment before the app knows anything at all. */
  fullPage?: boolean;
}

export function LoadingState({ label = "Loading…", fullPage = false }: Props) {
  return (
    <Center minH={fullPage ? "100vh" : "40vh"} role="status" aria-live="polite">
      <VStack gap="3">
        <Spinner size="lg" />
        <Text color="fg.muted">{label}</Text>
      </VStack>
    </Center>
  );
}
