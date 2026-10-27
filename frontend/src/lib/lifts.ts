import type { LiftCode } from "@/api/types";

export const LIFTS = ["squat", "bench", "deadlift", "press", "row"] as const satisfies readonly LiftCode[];

export const LIFT_LABEL: Record<LiftCode, string> = {
  squat: "Squat",
  bench: "Bench",
  deadlift: "Deadlift",
  press: "Press",
  row: "Row",
};

/**
 * Two-or-three letter forms, for the calendar's narrow day columns where the full
 * label would wrap. The grid is seven columns wide on a desktop, so "Deadlift" does
 * not fit; spec 18 §4B writes its own example as `SQ 3×10 @ 81`.
 */
export const LIFT_SHORT: Record<LiftCode, string> = {
  squat: "SQ",
  bench: "BP",
  deadlift: "DL",
  press: "OHP",
  row: "ROW",
};

/** Lifts the template programmes but never tests: shown with an "accessory" label. */
export const ACCESSORY_LIFTS: ReadonlySet<LiftCode> = new Set<LiftCode>(["row"]);

/** `0 = Monday … 6 = Sunday`, the same numbering as the backend. */
export const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

export function isLiftCode(value: string): value is LiftCode {
  return (LIFTS as readonly string[]).includes(value);
}
