import { Alert, Box } from "@chakra-ui/react";
import type { ReactNode } from "react";

interface Props {
  title: string;
  /** One line each, as the server worded them (cell references, violations). */
  lines?: readonly string[];
  status?: "error" | "warning" | "info";
  children?: ReactNode;
}

export function ProblemAlert({ title, lines = [], status = "error", children }: Props) {
  return (
    <Alert.Root status={status} alignItems="flex-start">
      <Alert.Indicator />
      <Alert.Content minW="0">
        <Alert.Title>{title}</Alert.Title>
        {(lines.length > 0 || children) && (
          <Alert.Description>
            {lines.length > 0 && (
              <Box as="ul" ps="5" wordBreak="break-word">
                {lines.map((line, i) => (
                  <li key={`${i}-${line}`}>{line}</li>
                ))}
              </Box>
            )}
            {children}
          </Alert.Description>
        )}
      </Alert.Content>
    </Alert.Root>
  );
}
