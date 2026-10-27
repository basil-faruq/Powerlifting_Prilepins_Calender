import type { AthleteDetail, LiftCode, TemplateEntryOut } from "@/api/types";
import { addDays, weekdayOf } from "@/lib/date";
import { LIFTS } from "@/lib/lifts";

export const STEP_TITLES = [
  "Your template",
  "Your maxes",
  "Your lifts",
  "Your training days",
  "Start date",
] as const;

export const LAST_STEP = STEP_TITLES.length;

/**
 * Plate steps are the TOTAL bar jump. A bar loads in pairs, so 2.5 kg is a 1.25 kg
 * plate a side, which a gym does not own; 5 kg is the smallest honest step (ADR-0008).
 * 1.25 and 2.5 as the spec once listed them move the bar by amounts nothing can load.
 */
export const PLATE_INCREMENTS = ["5", "2.5", "1"] as const;
export const DEFAULT_INCREMENT = "5";
export const DEFAULT_ROUNDING = "nearest";

export type Rounding = "floor" | "nearest";

export interface MaxEntry {
  /** Typed text, kept as a string so no float ever stands in for it. */
  kg: string;
  /** ISO `YYYY-MM-DD` the athlete tested it. */
  setOn: string;
}

/**
 * What the athlete has typed so far. Every field is `undefined` until touched, and an
 * untouched field falls back to what the server already holds -- so there is nothing
 * to "default", and in particular no maximum is ever invented (non-negotiable 9).
 */
export interface SetupDraft {
  maxes: Partial<Record<LiftCode, MaxEntry>>;
  increment?: string;
  rounding?: Rounding;
  lifts?: LiftCode[];
  /** slot → weekday, 0 = Monday. */
  weekdays: Record<number, number | undefined>;
  startDate?: string;
}

export const EMPTY_DRAFT: SetupDraft = { maxes: {}, weekdays: {} };

// --- numbers --------------------------------------------------------------

/** A positive amount with at most two decimals, the same shape the API stores. */
export const KG_PATTERN = /^\d{1,4}(\.\d{1,2})?$/;

export function isValidKg(text: string): boolean {
  return KG_PATTERN.test(text) && Number(text) > 0;
}

/** `"150.00"` → `"150"`, `"92.50"` → `"92.5"`. Text only; nothing is parsed to a number. */
export function trimDecimal(text: string): string {
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

// --- dates ----------------------------------------------------------------
// The generic three now live in `lib/date.ts`, because the block calendar needs
// the same arithmetic. Re-exported here so every existing caller is unaffected.

export { addDays, todayIso, weekdayOf } from "@/lib/date";

export function isMonday(iso: string): boolean {
  return weekdayOf(iso) === 0;
}

/** The first Monday strictly after `today`, then `count - 1` more. */
export function upcomingMondays(today: string, count: number): string[] {
  const first = addDays(today, 7 - weekdayOf(today));
  return Array.from({ length: count }, (_, i) => addDays(first, i * 7));
}

// --- the template ---------------------------------------------------------

/** Which template days (slots) each lift trains on. */
export function slotsByLift(entries: readonly TemplateEntryOut[]): Record<string, number[]> {
  const found: Record<string, Set<number>> = {};
  for (const entry of entries) (found[entry.lift_code] ??= new Set()).add(entry.slot);
  return Object.fromEntries(
    Object.entries(found).map(([lift, slots]) => [lift, [...slots].sort((a, b) => a - b)]),
  );
}

/** The lifts the template actually programmes, in the app's fixed order. */
export function programmedLifts(entries: readonly TemplateEntryOut[]): LiftCode[] {
  const present = new Set(entries.map((entry) => entry.lift_code));
  return LIFTS.filter((lift) => present.has(lift));
}

/** Template days these lifts need pinned to a weekday: squat + deadlift only → 1, 2, 3. */
export function slotsNeeded(chosen: readonly LiftCode[], byLift: Record<string, number[]>): number[] {
  const slots = new Set<number>();
  for (const lift of chosen) for (const slot of byLift[lift] ?? []) slots.add(slot);
  return [...slots].sort((a, b) => a - b);
}

// --- reading the draft against the server ----------------------------------

export function effectiveMax(draft: SetupDraft, detail: AthleteDetail | undefined, lift: LiftCode): MaxEntry {
  const typed = draft.maxes[lift];
  if (typed !== undefined) return typed;
  const saved = detail?.one_rep_maxes.find((row) => row.lift_code === lift);
  return saved === undefined ? { kg: "", setOn: "" } : { kg: trimDecimal(saved.kg), setOn: saved.set_on };
}

export function effectiveIncrement(draft: SetupDraft, detail: AthleteDetail | undefined): string {
  if (draft.increment !== undefined) return draft.increment;
  return detail === undefined ? DEFAULT_INCREMENT : trimDecimal(detail.plate_increment_kg);
}

export function effectiveRounding(draft: SetupDraft, detail: AthleteDetail | undefined): Rounding {
  return draft.rounding ?? detail?.rounding ?? DEFAULT_ROUNDING;
}

export function effectiveWeekday(
  draft: SetupDraft,
  detail: AthleteDetail | undefined,
  slot: number,
): number | undefined {
  return draft.weekdays[slot] ?? detail?.weekday_map.find((row) => row.slot === slot)?.weekday;
}

/** A lift's max as the server holds it, or `undefined` if the athlete never entered one. */
export function savedMax(detail: AthleteDetail | undefined, lift: LiftCode) {
  return detail?.one_rep_maxes.find((row) => row.lift_code === lift);
}

/** Does this entry differ from what the server holds? Only those are re-sent. */
export function maxChanged(detail: AthleteDetail | undefined, lift: LiftCode, entry: MaxEntry): boolean {
  const saved = savedMax(detail, lift);
  return saved === undefined || trimDecimal(saved.kg) !== trimDecimal(entry.kg) || saved.set_on !== entry.setOn;
}

export type MaxProblem = { kg?: string; setOn?: string };

/** What is wrong with one lift's entry, if anything. An entirely blank row is simply not entered. */
export function maxProblem(entry: MaxEntry, today: string): MaxProblem | null {
  if (entry.kg === "" && entry.setOn === "") return null;
  const problem: MaxProblem = {};
  if (!isValidKg(entry.kg)) problem.kg = "Enter a weight in kg, up to two decimals.";
  if (entry.setOn === "") problem.setOn = "Enter the date you tested it.";
  else if (entry.setOn > today) problem.setOn = "A max cannot be tested in the future.";
  return Object.keys(problem).length > 0 ? problem : null;
}
