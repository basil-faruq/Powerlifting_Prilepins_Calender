import type { LiftCode } from "@/api/types";

export const LIFTS = ["squat", "bench", "deadlift", "press", "row"] as const satisfies readonly LiftCode[];

export const LIFT_LABEL: Record<LiftCode, string> = {
  squat: "Squat",
  bench: "Bench",
  deadlift: "Deadlift",
  press: "Press",
  row: "Row",
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
