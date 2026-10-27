import type { LiftCode, LoggedLiftIn, PlannedLiftOut, SessionLogIn, SessionOut } from "@/api/types";
import { compareDecimal, isMultipleOf } from "@/lib/decimal";
import { isLiftCode } from "@/lib/lifts";

/**
 * Which session the logger opens on, what it starts filled with, and what
 * counts as a sane entry.
 *
 * Nothing here computes a weight. The prescription arrives as decimal strings
 * and is carried through as strings all the way back to the API (CLAUDE.md
 * non-negotiable 4); the only arithmetic is the plate check, and that is exact.
 */

/** The bounds the form offers, mirrored by the API since spec 20. */
export const LIMITS = {
  sets: { min: 0, max: 10 },
  reps: { min: 0, max: 20 },
  weightMaxKg: "500",
  noteMax: 2000,
  rpe: { min: 6, max: 10 },
} as const;

/** What the logger is about to send for one lift, held as the athlete typed it. */
export interface LiftEntry {
  liftCode: LiftCode;
  setsDone: string;
  /** One value for every set, or one per set when they differed. */
  repsDone: string[];
  /** A decimal string, never a number: this is a weight. */
  weightKg: string;
  rpe: string;
  isTest: boolean;
  /** The prescription, kept to show beside the inputs and to judge a test by. */
  prescribed: { sets: number; reps: number; weightKg: string };
}

/**
 * The session to log.
 *
 * Today's, if there is one. Otherwise the next one still to come, which the page
 * offers to log anyway -- a session done a day early or late is still that
 * session, and refusing it would push the athlete to log nothing at all.
 */
export function pickSession(
  sessions: readonly SessionOut[],
  today: string,
): { session: SessionOut; isToday: boolean } | null {
  const todays = sessions.find((s) => s.date === today);
  if (todays !== undefined) return { session: todays, isToday: true };

  const upcoming = [...sessions]
    .filter((s) => s.date > today && s.status !== "done")
    .sort((a, b) => a.date.localeCompare(b.date))[0];
  if (upcoming !== undefined) return { session: upcoming, isToday: false };

  // Nothing ahead: offer the most recent one that was never logged, so a
  // session done yesterday can still be recorded.
  const behind = [...sessions]
    .filter((s) => s.date <= today && s.status !== "done" && s.status !== "skipped")
    .sort((a, b) => b.date.localeCompare(a.date))[0];
  return behind === undefined ? null : { session: behind, isToday: false };
}

/**
 * The form, filled in with exactly what was prescribed.
 *
 * This is what makes a session done as written a single tap: the athlete
 * changes nothing and presses Save (spec 20 §11).
 */
export function prefill(lifts: readonly PlannedLiftOut[]): LiftEntry[] {
  const entries: LiftEntry[] = [];
  for (const lift of lifts) {
    // `PlannedLiftOut.lift_code` is a bare `string` on the wire while the log
    // route takes the five-lift enum, so the narrowing happens once, here. The
    // backend only ever sends those five; a sixth would be a lift this build
    // cannot log, and dropping it is better than sending a code that is refused.
    if (!isLiftCode(lift.lift_code)) continue;
    entries.push({
      liftCode: lift.lift_code,
      setsDone: String(lift.sets),
      repsDone: [String(lift.reps)],
      weightKg: lift.weight_kg,
      rpe: "",
      isTest: lift.is_test,
      prescribed: { sets: lift.sets, reps: lift.reps, weightKg: lift.weight_kg },
    });
  }
  return entries;
}

/** A test single made: one rep at the weight shown, which the athlete may raise. */
export function madeIt(entry: LiftEntry): LiftEntry {
  return { ...entry, setsDone: "1", repsDone: ["1"] };
}

/**
 * A test single missed: attempted, nothing completed.
 *
 * There is no "missed" field on the wire. The backend records a new max only
 * when the weight is at or above the prescription AND at least one rep was
 * done, so zero reps is precisely how a failed attempt is stated (ADR-0005
 * mod 5). The attempt is still logged; it is the max that is not.
 */
export function missedIt(entry: LiftEntry): LiftEntry {
  return { ...entry, setsDone: "0", repsDone: ["0"] };
}

/**
 * Whether logging this entry would record a new tested max.
 *
 * Used only to tell the athlete what is about to happen before they press the
 * button. The backend decides; this mirrors its rule and never writes anything.
 */
export function wouldSetNewMax(entry: LiftEntry): boolean {
  if (!entry.isTest) return false;
  const sets = Number.parseInt(entry.setsDone, 10);
  const reps = entry.repsDone.map((value) => Number.parseInt(value, 10));
  if (!Number.isFinite(sets) || sets < 1) return false;
  if (!reps.some((value) => Number.isFinite(value) && value >= 1)) return false;
  // The one comparison that matters is between two weights, so it is exact.
  const against = compareDecimal(entry.weightKg, entry.prescribed.weightKg);
  return against !== null && against >= 0;
}

export interface EntryProblems {
  /** Refused by the API too, so Save stays disabled. */
  errors: string[];
  /** Worth saying, but the athlete may know better than the setting. */
  warnings: string[];
}

/**
 * What is wrong with one entry.
 *
 * The plate increment is a WARNING, never an error. `plate_increment_kg` is a
 * setting (ADR-0008), and a gym that keeps a pair of 0.5 kg plates in a drawer
 * is a real thing: refusing the weight the athlete actually lifted would make
 * the record wrong in order to keep the setting right.
 */
export function validateEntry(entry: LiftEntry, plateIncrementKg: string): EntryProblems {
  const errors: string[] = [];
  const warnings: string[] = [];

  const sets = Number.parseInt(entry.setsDone, 10);
  if (!Number.isFinite(sets) || sets < LIMITS.sets.min || sets > LIMITS.sets.max) {
    errors.push(`Sets must be between ${LIMITS.sets.min} and ${LIMITS.sets.max}.`);
  }

  if (entry.repsDone.length === 0) errors.push("Give the reps for at least one set.");
  for (const value of entry.repsDone) {
    const reps = Number.parseInt(value, 10);
    if (!Number.isFinite(reps) || reps < LIMITS.reps.min || reps > LIMITS.reps.max) {
      errors.push(`Reps must be between ${LIMITS.reps.min} and ${LIMITS.reps.max}.`);
      break;
    }
  }

  const overCeiling = compareDecimal(entry.weightKg, LIMITS.weightMaxKg);
  const positive = compareDecimal(entry.weightKg, "0");
  if (positive === null || overCeiling === null) errors.push("Give the weight in kilograms.");
  else if (positive <= 0) errors.push("The weight has to be more than zero.");
  else if (overCeiling > 0) errors.push(`${LIMITS.weightMaxKg} kg is the heaviest the form takes.`);

  if (entry.rpe.trim() !== "") {
    const rpe = Number.parseFloat(entry.rpe);
    if (!Number.isFinite(rpe) || rpe < LIMITS.rpe.min || rpe > LIMITS.rpe.max) {
      errors.push(`RPE runs from ${LIMITS.rpe.min} to ${LIMITS.rpe.max}.`);
    }
  }

  if (errors.length === 0 && isMultipleOf(entry.weightKg, plateIncrementKg) === false) {
    warnings.push(`Not a multiple of your ${plateIncrementKg} kg step — microplates?`);
  }

  return { errors, warnings };
}

/** Everything wrong with the form, by entry index. */
export function validateAll(
  entries: readonly LiftEntry[],
  plateIncrementKg: string,
  note: string,
): { byEntry: EntryProblems[]; noteError: string | null; ok: boolean } {
  const byEntry = entries.map((entry) => validateEntry(entry, plateIncrementKg));
  const noteError =
    note.length > LIMITS.noteMax ? `Keep the note under ${LIMITS.noteMax} characters.` : null;
  const ok = noteError === null && byEntry.every((problems) => problems.errors.length === 0);
  return { byEntry, noteError, ok };
}

/**
 * The form as the API wants it.
 *
 * Weights go out as the STRINGS they came in as. `reps_done` collapses to a
 * single number when every set matched, which is both what the athlete meant
 * and what the simulator reads most easily.
 */
export function toLogIn(
  entries: readonly LiftEntry[],
  performedOn: string,
  note: string,
): SessionLogIn {
  return {
    performed_on: performedOn,
    entries: entries.map((entry): LoggedLiftIn => {
      const reps = entry.repsDone.map((value) => Number.parseInt(value, 10));
      const uniform = reps.every((value) => value === reps[0]);
      const trimmed = entry.rpe.trim();
      return {
        lift_code: entry.liftCode,
        sets_done: Number.parseInt(entry.setsDone, 10),
        reps_done: uniform ? (reps[0] ?? 0) : reps,
        weight_kg: entry.weightKg,
        ...(trimmed === "" ? {} : { rpe: trimmed }),
      };
    }),
    ...(note.trim() === "" ? {} : { note: note.trim() }),
  };
}
