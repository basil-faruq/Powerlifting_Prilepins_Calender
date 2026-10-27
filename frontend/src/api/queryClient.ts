import { MutationCache, QueryClient } from "@tanstack/react-query";
import { toaster } from "@/components/ui/toaster";
import { ApiError, errorMessage } from "./errors";

declare module "@tanstack/react-query" {
  interface Register {
    mutationMeta: {
      /** The caller shows this failure itself (inline, or by navigating), so no toast. */
      silent?: boolean;
    };
  }
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        // A 4xx will answer the same way again; only a connection failure is worth another go.
        retry: (count, error) => !(error instanceof ApiError) && count < 1,
      },
    },
    mutationCache: new MutationCache({
      // Spec 16 §13: a failed mutation toasts the server's own message.
      onError: (error, _variables, _context, mutation) => {
        if (mutation.meta?.silent) return;
        toaster.create({ type: "error", title: "Request failed", description: errorMessage(error) });
      },
    }),
  });
}
