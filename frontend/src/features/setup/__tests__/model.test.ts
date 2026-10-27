import { describe, expect, it } from "vitest";
import type { TemplateEntryOut } from "@/api/types";
import {
  isMonday,
  isValidKg,
  maxProblem,
  programmedLifts,
  slotsByLift,
  slotsNeeded,
  todayIso,
  trimDecimal,
  upcomingMondays,
  weekdayOf,
} from "../model";

const entry = (lift_code: string, slot: number) => ({ lift_code, slot }) as TemplateEntryOut;

// CONTEXT.md §4: squat and bench train Day 1 and 3, deadlift and press Day 2, row Day 3.
const ENTRIES = [
  entry("squat", 1),
  entry("squat", 3),
  entry("squat", 1),
  entry("bench", 1),
  entry("bench", 3),
  entry("deadlift", 2),
  entry("press", 2),
  entry("row", 3),
];

describe("which days a lift uses", () => {
  const byLift = slotsByLift(ENTRIES);

  it("collects each lift's distinct days, in order", () => {
    expect(byLift.squat).toEqual([1, 3]);
    expect(byLift.deadlift).toEqual([2]);
  });

  it("asks for Day 1, 2 and 3 for squat and deadlift", () => {
    expect(slotsNeeded(["squat", "deadlift"], byLift)).toEqual([1, 2, 3]);
  });

  it("asks for Day 2 alone for deadlift alone", () => {
    expect(slotsNeeded(["deadlift"], byLift)).toEqual([2]);
  });

  it("lists the lifts a template programmes in the app's order", () => {
    expect(programmedLifts(ENTRIES)).toEqual(["squat", "bench", "deadlift", "press", "row"]);
    expect(programmedLifts([entry("deadlift", 2)])).toEqual(["deadlift"]);
  });
});

describe("dates", () => {
  it("numbers weekdays from Monday = 0, like the backend", () => {
    expect(weekdayOf("2026-10-19")).toBe(0); // a Monday
    expect(weekdayOf("2026-10-25")).toBe(6); // a Sunday
  });

  it("offers the next Monday, strictly after today", () => {
    expect(upcomingMondays("2026-10-18", 2)).toEqual(["2026-10-19", "2026-10-26"]); // from a Sunday
    expect(upcomingMondays("2026-10-19", 1)).toEqual(["2026-10-26"]); // on a Monday: not today
    expect(upcomingMondays("2026-10-21", 1)).toEqual(["2026-10-26"]); // mid-week
  });

  it("offers only Mondays, across a month and a year boundary", () => {
    const mondays = upcomingMondays("2026-12-20", 6);
    expect(mondays.every(isMonday)).toBe(true);
    expect(mondays[0]).toBe("2026-12-21");
    expect(mondays).toContain("2027-01-04");
  });

  it("reads today in the athlete's zone, not the browser's", () => {
    // 23:30 UTC on 18 Oct is already 01:30 on the 19th in Berlin (CEST, UTC+2).
    const instant = new Date("2026-10-18T23:30:00Z");
    expect(todayIso("Europe/Berlin", instant)).toBe("2026-10-19");
    expect(todayIso("UTC", instant)).toBe("2026-10-18");
  });
});

describe("a max", () => {
  it("accepts a positive weight with at most two decimals", () => {
    expect(isValidKg("142.5")).toBe(true);
    expect(isValidKg("100")).toBe(true);
    expect(isValidKg("92.25")).toBe(true);
  });

  it("refuses zero, negatives, junk and too many decimals", () => {
    for (const bad of ["0", "0.0", "-5", "abc", "", "1.234", "1e3"]) expect(isValidKg(bad)).toBe(false);
  });

  it("trims trailing zeros as text, without parsing a number", () => {
    expect(trimDecimal("150.00")).toBe("150");
    expect(trimDecimal("92.50")).toBe("92.5");
    expect(trimDecimal("100")).toBe("100");
    expect(trimDecimal("5.00")).toBe("5");
  });

  it("treats a blank row as not entered, and a half-filled one as a problem", () => {
    expect(maxProblem({ kg: "", setOn: "" }, "2026-10-19")).toBeNull();
    expect(maxProblem({ kg: "140", setOn: "" }, "2026-10-19")?.setOn).toMatch(/date/);
    expect(maxProblem({ kg: "", setOn: "2026-10-01" }, "2026-10-19")?.kg).toBeDefined();
  });

  it("refuses a date in the future, but accepts today", () => {
    expect(maxProblem({ kg: "140", setOn: "2026-10-20" }, "2026-10-19")?.setOn).toMatch(/future/);
    expect(maxProblem({ kg: "140", setOn: "2026-10-19" }, "2026-10-19")).toBeNull();
  });
});
