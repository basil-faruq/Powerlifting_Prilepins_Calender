import { Box, HStack, Stack, Text } from "@chakra-ui/react";
import { shadeBg, shadeFg, STEP_LABEL, type IntensityStep } from "./intensity";

const STEPS: readonly IntensityStep[] = [1, 2, 3, 4, 5];

/**
 * What the shading means.
 *
 * Needed because a shade on its own says nothing, and because the basis has to be
 * stated honestly: the steps read *relative* intensity -- the authored fraction
 * against what the reps themselves imply -- and not a percentage of the athlete's
 * current one-rep max. The template's own denominator is the max the block was
 * programmed against (ADR-0003), so calling the scale "%1RM" would be wrong.
 */
export function IntensityLegend() {
  return (
    <Stack gap="1" minW="0">
      <Text fontSize="xs" color="fg.muted">
        Shading is the hardest lift in the session, by relative intensity
      </Text>
      <HStack gap="1" wrap="wrap">
        {STEPS.map((step) => (
          <Box
            key={step}
            px="2"
            py="0.5"
            rounded="sm"
            borderWidth="1px"
            borderColor="border"
            bg={shadeBg(step)}
            color={shadeFg(step)}
          >
            <Text fontSize="2xs" fontWeight="medium">
              {STEP_LABEL[step]}
            </Text>
          </Box>
        ))}
      </HStack>
    </Stack>
  );
}
