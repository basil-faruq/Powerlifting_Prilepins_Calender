/**
 * The cell shade, from the exact relative intensity the backend computed.
 *
 * `rel_intensity` arrives as a decimal STRING ("0.7812") and must never pass through
 * `Number()` -- see the banner in `lib/format.ts` and CLAUDE.md non-negotiable 4.
 * Everything here compares scaled `BigInt`s instead, so no float ever exists.
 *
 * The five steps are not invented. Their edges are the Prilepin bands the validator
 * already works in (spec 05 §10) plus the crossing of the max itself, where a set
 * stops being submaximal. The scale is described as running from 55% to 110%
 * (spec 18 §4B); both of those fall inside the outer steps, so nothing is clamped.
 */

import { scaled as scaledDecimal } from "@/lib/decimal";

/** 1 is the lightest, 5 is at or over the max. */
export type IntensityStep = 1 | 2 | 3 | 4 | 5;

/** The ends of the scale as spec 18 §4B words them, for the legend only. */
export const SHADE_FLOOR = "0.55";
export const SHADE_CAP = "1.10";

/** What each step means, for the legend and for the cell's `title`. */
export const STEP_LABEL: Record<IntensityStep, string> = {
  1: "under 70%",
  2: "70–80%",
  3: "80–90%",
  4: "90–100%",
  5: "100% and over",
};

/**
 * A non-negative decimal string as an integer at four decimal places:
 * `"0.7812"` → `7812n`, `"1"` → `10000n`.
 *
 * The scaling itself lives in `lib/decimal` now, shared with the logger's plate
 * check (spec 20). An intensity below zero is not something to shade by, so a
 * signed value is refused here rather than there.
 */
export function scaled(decimal: string): bigint | null {
  const value = scaledDecimal(decimal);
  return value === null || value < 0n ? null : value;
}

/** Which of the five steps one lift's relative intensity falls in. */
export function intensityStep(relIntensity: string): IntensityStep | null {
  const value = scaled(relIntensity);
  if (value === null) return null;
  if (value >= 10_000n) return 5;
  if (value >= 9_000n) return 4;
  if (value >= 8_000n) return 3;
  if (value >= 7_000n) return 2;
  return 1;
}

/** A session is shaded by its hardest lift. `null` when none of them can be read. */
export function topStep(lifts: readonly { rel_intensity: string }[]): IntensityStep | null {
  let top: IntensityStep | null = null;
  for (const lift of lifts) {
    const step = intensityStep(lift.rel_intensity);
    if (step !== null && (top === null || step > top)) top = step;
  }
  return top;
}

/**
 * The background token for a step. Both halves of the pair are defined in
 * `theme.ts` in each colour mode, because spec 18 §11 requires the scale to come
 * from the theme and to work in the dark.
 */
export function shadeBg(step: IntensityStep | null): string {
  return step === null ? "bg.subtle" : `intensity.${step}.bg`;
}

/** The foreground to pair with `shadeBg`, so text stays legible on every step. */
export function shadeFg(step: IntensityStep | null): string {
  return step === null ? "fg" : `intensity.${step}.fg`;
}
