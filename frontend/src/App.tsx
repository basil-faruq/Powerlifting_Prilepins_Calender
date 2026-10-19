import { Box } from "@chakra-ui/react";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { AthleteProvider } from "@/components/AthleteProvider";
import { Toaster } from "@/components/ui/toaster";
import { AppRoutes } from "@/router";

export function App() {
  return (
    <>
      <AthleteProvider>
        <AppRoutes />
      </AthleteProvider>
      <Toaster />
      {/* Dev-only, and hidden on a phone, where its floating button would sit on top of the first tab. */}
      <Box display={{ base: "none", md: "block" }}>
        <ReactQueryDevtools initialIsOpen={false} buttonPosition="bottom-left" />
      </Box>
    </>
  );
}
