import { describe, expect, it } from "vitest";
import type { DraftSession } from "@/api/types";
import { summarize } from "../summary";

const session = (date: string, lifts: { lift_code: string; is_test: boolean }[]) =>
  ({ date, week: 1, slot: 1, seq: 1, lifts }) as unknown as DraftSession;

describe("summarize", () => {
  const sessions = [
    session("2026-12-21", [{ lift_code: "squat", is_test: true }]),
    session("2026-10-26", [{ lift_code: "squat", is_test: false }]),
    session("2026-12-14", [
      { lift_code: "bench", is_test: true },
      { lift_code: "row", is_test: false },
    ]),
  ];

  it("counts sessions and finds the last day whatever order they arrive in", () => {
    const summary = summarize(sessions);
    expect(summary.sessionCount).toBe(3);
    expect(summary.endDate).toBe("2026-12-21");
  });

  it("lists each tested lift on its own date, earliest first", () => {
    expect(summarize(sessions).tests).toEqual([
      { lift: "bench", date: "2026-12-14" },
      { lift: "squat", date: "2026-12-21" },
    ]);
  });

  it("copes with a block that has no sessions", () => {
    expect(summarize([])).toEqual({ sessionCount: 0, endDate: null, tests: [] });
  });
});
