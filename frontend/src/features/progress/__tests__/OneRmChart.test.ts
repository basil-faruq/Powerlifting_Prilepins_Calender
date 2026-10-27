import { describe, expect, it } from "vitest";
import type { LiftCode } from "@/api/types";
import type { MaxPoint } from "../model";
import { toRows } from "../OneRmChart";

/**
 * Only `toRows` is exercised here. The chart itself is recharts inside a
 * `ResponsiveContainer`, which measures a real layout box and renders nothing
 * at all under jsdom -- so rendering it would assert on an empty SVG.
 *
 * The row is where the one float in the feature lives: `squat` is a y-
 * coordinate, because an SVG needs a pixel. `squat_kg` beside it is the exact
 * string every number the athlete READS is formatted from.
 */

const point = (over: Partial<MaxPoint> = {}): MaxPoint => ({
  lift: "squat",
  setOn: "2026-01-05",
  kg: "123.5",
  kind: "tested",
  ...over,
});

const series = (): Map<LiftCode, MaxPoint[]> =>
  new Map<LiftCode, MaxPoint[]>([
    [
      "squat",
      [
        point({ setOn: "2026-06-08", kg: "142.5", kind: "estimated" }),
        point({ setOn: "2026-01-05", kg: "123.5" }),
      ],
    ],
    ["bench", [point({ lift: "bench", setOn: "2026-01-05", kg: "97.5" })]],
    ["press", [point({ lift: "press", setOn: "2026-01-05", kg: "62.5" })]],
  ]);

describe("toRows", () => {
  it("merges the lifts onto one shared date axis, a row per date", () => {
    const rows = toRows(series(), ["squat", "bench"]);
    expect(rows.map((r) => r.setOn)).toEqual(["2026-01-05", "2026-06-08"]);
  });

  it("puts both lifts on the date they share", () => {
    const rows = toRows(series(), ["squat", "bench"]);
    expect(rows[0]?.squat).toBe(123.5);
    expect(rows[0]?.bench).toBe(97.5);
  });

  it("carries the exact string and the source beside the plotted coordinate", () => {
    const rows = toRows(series(), ["squat", "bench"]);
    expect(rows[0]?.squat_kg).toBe("123.5");
    expect(rows[0]?.squat_kind).toBe("tested");
    expect(rows[1]?.squat_kg).toBe("142.5");
    expect(rows[1]?.squat_kind).toBe("estimated");
  });

  it("leaves a date a lift has no point on empty rather than filling it in", () => {
    const rows = toRows(series(), ["squat", "bench"]);
    expect(rows[1]).not.toHaveProperty("bench");
  });

  it("leaves out a lift the filter is not asking for", () => {
    const rows = toRows(series(), ["squat"]);
    for (const row of rows) {
      expect(row).not.toHaveProperty("bench");
      expect(row).not.toHaveProperty("press");
      expect(row).not.toHaveProperty("press_kg");
    }
  });

  it("has no rows at all when nothing has been recorded", () => {
    expect(toRows(new Map<LiftCode, MaxPoint[]>(), ["squat"])).toEqual([]);
  });
});
