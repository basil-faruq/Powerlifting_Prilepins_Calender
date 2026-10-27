// Calendar-day arithmetic on ISO `YYYY-MM-DD` strings.
//
// ISO strings in, ISO strings out, pinned to noon UTC so no zone, however far from
// UTC, can slide a date onto the neighbouring day. Weekdays are `0 = Monday … 6 =
// Sunday`, matching the backend and never Postgres's DOW.
//
// Lifted out of the setup wizard when the calendar needed the same three functions;
// `features/setup/model.ts` re-exports them, so nothing that used them had to change.

/** Today in `timeZone`, as `YYYY-MM-DD`. */
export function todayIso(timeZone: string, now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

export function addDays(iso: string, days: number): string {
  const date = new Date(`${iso}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/** `0 = Monday … 6 = Sunday`, matching the backend. */
export function weekdayOf(iso: string): number {
  return (new Date(`${iso}T12:00:00Z`).getUTCDay() + 6) % 7;
}
