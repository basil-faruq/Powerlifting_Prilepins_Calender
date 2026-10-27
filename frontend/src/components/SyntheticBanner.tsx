import { Box, Text } from "@chakra-ui/react";

/**
 * Shown while any current 1RM is development data (ADR-0004 rule 4). Every weight
 * on screen is computed from the maxes, so one invented max makes all of them so.
 * Driven by the API's `has_synthetic_one_rms`, never by a row's `source`.
 */
export function SyntheticBanner() {
  return (
    <Box bg="synthetic.bg" color="synthetic.fg" px="4" py="2" role="status">
      <Text fontSize="sm" fontWeight="medium">
        Development data: at least one 1RM was seeded for local testing, so every weight shown
        is synthetic.
      </Text>
    </Box>
  );
}
