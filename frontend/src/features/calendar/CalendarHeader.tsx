import { Box, Text } from "@chakra-ui/react";
import { WEEKDAYS } from "@/lib/lifts";
import { GRID_COLUMNS } from "./CalendarWeekRow";
import { WEEKDAY_COLUMNS } from "./model";

/** The grid's column headings. Mon–Sun, with the training days named in bold. */
export function CalendarHeader({ trainingDays }: { trainingDays: ReadonlySet<number> }) {
  return (
    <Box display="grid" gridTemplateColumns={GRID_COLUMNS} gap="1" px="1" pb="1">
      <Text fontSize="2xs" color="fg.muted">
        Week
      </Text>
      {WEEKDAY_COLUMNS.map((weekday) => (
        <Text
          key={weekday}
          fontSize="2xs"
          minW="0"
          color={trainingDays.has(weekday) ? "fg" : "fg.muted"}
          fontWeight={trainingDays.has(weekday) ? "bold" : "normal"}
        >
          {WEEKDAYS[weekday]?.slice(0, 3)}
        </Text>
      ))}
    </Box>
  );
}
