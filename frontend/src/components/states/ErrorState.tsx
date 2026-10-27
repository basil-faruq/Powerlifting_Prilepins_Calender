import { Button, Center, Heading, Text, VStack } from "@chakra-ui/react";

interface Props {
  title?: string;
  message: string;
  onRetry?: () => void;
  /** Fill the viewport: for a failure that leaves nothing else on screen to use. */
  fullPage?: boolean;
}

export function ErrorState({ title = "Something went wrong", message, onRetry, fullPage = false }: Props) {
  return (
    <Center minH={fullPage ? "100vh" : "40vh"} px="4" role="alert">
      <VStack gap="3" textAlign="center" maxW="md">
        <Heading size="md" color="fg.error">
          {title}
        </Heading>
        <Text color="fg.muted" wordBreak="break-word">
          {message}
        </Text>
        {onRetry && (
          <Button onClick={onRetry} variant="outline">
            Try again
          </Button>
        )}
      </VStack>
    </Center>
  );
}
