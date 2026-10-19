import { Badge } from "@chakra-ui/react";
import type { LiftCode } from "@/api/types";
import { ACCESSORY_LIFTS, LIFT_LABEL } from "@/lib/lifts";

interface Props {
  lift: LiftCode;
  /** Append "accessory" for lifts the template never tests. */
  showAccessory?: boolean;
}

/** The one place a lift gets its colour: `colorPalette` is the lift code (theme.ts). */
export function LiftBadge({ lift, showAccessory = false }: Props) {
  const accessory = showAccessory && ACCESSORY_LIFTS.has(lift);
  return (
    <Badge colorPalette={lift} variant="solid">
      {LIFT_LABEL[lift]}
      {accessory ? " · accessory" : ""}
    </Badge>
  );
}
