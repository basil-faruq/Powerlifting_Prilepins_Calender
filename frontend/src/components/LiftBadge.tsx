import { Badge } from "@chakra-ui/react";
import type { LiftCode } from "@/api/types";
import { ACCESSORY_LIFTS, LIFT_LABEL, LIFT_SHORT } from "@/lib/lifts";

interface Props {
  lift: LiftCode;
  /** Append "accessory" for lifts the template never tests. */
  showAccessory?: boolean;
  /** The abbreviated form ("DL"), for the calendar's narrow day columns. */
  short?: boolean;
}

/** The one place a lift gets its colour: `colorPalette` is the lift code (theme.ts). */
export function LiftBadge({ lift, showAccessory = false, short = false }: Props) {
  const accessory = showAccessory && ACCESSORY_LIFTS.has(lift);
  return (
    // The abbreviation needs the full name as well, or the only thing naming the
    // lift in a narrow cell is two letters and a colour.
    <Badge colorPalette={lift} variant="solid" title={short ? LIFT_LABEL[lift] : undefined}>
      {short ? LIFT_SHORT[lift] : LIFT_LABEL[lift]}
      {accessory ? " · accessory" : ""}
    </Badge>
  );
}
