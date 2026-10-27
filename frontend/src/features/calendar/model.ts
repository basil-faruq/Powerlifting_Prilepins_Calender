import type { PlannedLiftOut, SessionOut, WeekdaySlotOut } from "@/api/types";
import { addDays, weekdayOf } from "@/lib/date";
import { isLiftCode } from "@/lib/lifts";

export type SessionStatus = SessionOut["status"];

/** The grid's columns, `0 = Monday … 6 = Sunday`. */
export const WEEKDAY_COLUMNS = [0, 1, 2, 3, 4, 5, 6] as const;

// --- dates ----------------------------------------------------------------

/** The Monday of the week containing `iso`. */
export function mondayOf(iso: string): string {
  return addDays(iso, -weekdayOf(iso));
}

// --- rows -----------------------------------------------------------------

/**
 * One row of the grid: a calendar week, with its sessions filed under the weekday
 * they fall on.
 *
 * Rows are keyed on the **Monday**, not on `week`. A repeated week (ADR-0010's
 * `repeat_week`) writes fresh seqs carrying the SAME week number at later dates, so
 * keying on `week` would collapse two real training weeks into a single row and lose
 * one of them. The week number is kept as the row's *label*, which is why spec 18 §4A
 * says "W1…W11, plus repeated weeks if any" -- "W4" can legitimately appear twice.
 */
export interface WeekRow {
  /** ISO `YYYY-MM-DD`. The row's identity. */
  monday: string;
  /** The week number its sessions carry, for the label. */
  week: number;
  /** Weekday `0..6` → the sessions on that day, in `seq` order. */
  byWeekday: ReadonlyMap<number, readonly SessionOut[]>;
}

/**
 * The sessions as calendar weeks, oldest first. Weeks the block does not touch are
 * absent rather than blank: the block's own weeks are consecutive, and inventing
 * empty rows between them would imply training that was never planned.
 */
export function buildWeekRows(sessions: readonly SessionOut[]): WeekRow[] {
  const rows = new Map<string, { week: number; byWeekday: Map<number, SessionOut[]> }>();

  for (const session of [...sessions].sort((a, b) => a.seq - b.seq)) {
    const monday = mondayOf(session.date);
    const row = rows.get(monday) ?? { week: session.week, byWeekday: new Map<number, SessionOut[]>() };
    // The lowest number labels a row that somehow carries more than one, so the
    // label is at least stable rather than dependent on iteration order.
    row.week = Math.min(row.week, session.week);
    const day = weekdayOf(session.date);
    const onDay = row.byWeekday.get(day) ?? [];
    onDay.push(session);
    row.byWeekday.set(day, onDay);
    rows.set(monday, row);
  }

  return [...rows.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([monday, row]) => ({ monday, week: row.week, byWeekday: row.byWeekday }));
}

/**
 * Which row to highlight and, on a phone, to expand.
 *
 * The week containing today, so the grid does not depend on `/blocks/{id}/state` --
 * that query 503s with no Temporal worker running, and the calendar has to render in
 * full without one (ADR-0018 mod 2). `nextSessionDate` is the fallback for a block
 * that has not started or is already over, and the first row the last resort.
 */
export function currentMonday(
  rows: readonly WeekRow[],
  today: string,
  nextSessionDate: string | null = null,
): string | null {
  if (rows.length === 0) return null;
  const thisWeek = mondayOf(today);
  if (rows.some((row) => row.monday === thisWeek)) return thisWeek;
  if (nextSessionDate !== null) {
    const nextWeek = mondayOf(nextSessionDate);
    if (rows.some((row) => row.monday === nextWeek)) return nextWeek;
  }
  return rows[0]?.monday ?? null;
}

/** The weekdays the athlete trains on, for the subtle column highlight. */
export function trainingWeekdays(map: readonly WeekdaySlotOut[] | undefined): ReadonlySet<number> {
  return new Set((map ?? []).map((slot) => slot.weekday));
}

// --- one cell -------------------------------------------------------------

/**
 * How a session should read, derived once so the cell and the drawer agree.
 *
 * `overdue` is **not** `missed`. ADR-0017: "`missed` is never written … `missed`
 * means written off, and only a human writes a session off." A session the athlete
 * may still make up stays `planned` with a date in the past, which is the state the
 * system actually produces and which spec 18 had no rendering for. Logging a session
 * is what sets `done` (`services.log_session`), so a past session still reading
 * `planned` is precisely one that was never logged.
 */
export interface CellState {
  status: SessionStatus;
  /** Past, not written off, and never logged. Shown as "not logged", never as "missed". */
  overdue: boolean;
  /** Where the session was first planned, when that is no longer where it sits. */
  movedFrom: string | null;
  /** Any lift in the session is a test single. */
  isTest: boolean;
}

export function cellState(session: SessionOut, today: string): CellState {
  const pending = session.status === "planned" || session.status === "moved";
  return {
    status: session.status,
    // ISO dates compare lexicographically in calendar order, so no parsing is needed.
    overdue: pending && session.date < today,
    movedFrom: session.original_date !== session.date ? session.original_date : null,
    isTest: session.lifts.some((lift) => lift.is_test),
  };
}

/**
 * The lifts of a session, with `lift_code` narrowed.
 *
 * `PlannedLiftOut.lift_code` is a plain `string` in the generated schema, while
 * `LiftBadge` needs the union. A code the frontend does not know is dropped rather
 * than rendered colourless, and the drawer lists the full prescription anyway.
 */
export function knownLifts(lifts: readonly PlannedLiftOut[]) {
  return lifts.flatMap((lift) =>
    isLiftCode(lift.lift_code) ? [{ ...lift, lift_code: lift.lift_code }] : [],
  );
}
