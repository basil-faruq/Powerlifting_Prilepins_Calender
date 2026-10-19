import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { Provider } from "@/components/ui/provider";

/** Chakra + Query (no retries, so a failure shows at once) + a memory router. */
export function renderWithProviders(ui: ReactElement, { route = "/" } = {}): RenderResult & {
  queryClient: QueryClient;
} {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <Provider>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  );
  return { ...result, queryClient };
}
