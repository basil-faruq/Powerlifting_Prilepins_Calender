import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { createQueryClient } from "@/api/queryClient";
import { Provider } from "@/components/ui/provider";
import { App } from "./App";

const queryClient = createQueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Provider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </Provider>
  </StrictMode>,
);
