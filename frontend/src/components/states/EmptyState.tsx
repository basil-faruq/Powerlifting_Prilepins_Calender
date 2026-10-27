import { Center, Heading, Text, VStack } from "@chakra-ui/react";
import type { ReactNode } from "react";

interface Props {
  title: string;
  description?: string;
  /** A button or link offering the way out of the empty state. */
  action?: ReactNode;
}

export function EmptyState({ title, description, action }: Props) {
  return (
    <Center minH="40vh" px="4">
      <VStack gap="3" textAlign="center" maxW="md">
        <Heading size="md">{title}</Heading>
        {description && <Text color="fg.muted">{description}</Text>}
        {action}
      </VStack>
    </Center>
  );
}
