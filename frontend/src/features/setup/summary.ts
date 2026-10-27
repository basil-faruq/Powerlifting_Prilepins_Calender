import type { DraftSession, LiftCode } from "@/api/types";

export interface PreviewSummary {
  sessionCount: number;
  /** The last session's date, ISO. */
  endDate: string | null;
  /** The day each lift is tested, in date order. */
  tests: { lift: LiftCode; date: string }[];
}

/**
 * The preview carries no end date and no test dates of its own (`BlockDraft` is just
 * its sessions), so the summary is read off them. ISO dates sort as plain strings.
 */
export function summarize(sessions: readonly DraftSession[]): PreviewSummary {
  const tests = sessions.flatMap((session) =>
    session.lifts.filter((lift) => lift.is_test).map((lift) => ({ lift: lift.lift_code, date: session.date })),
  );
  tests.sort((a, b) => a.date.localeCompare(b.date));
  const dates = sessions.map((session) => session.date).sort();
  return { sessionCount: sessions.length, endDate: dates.at(-1) ?? null, tests };
}
