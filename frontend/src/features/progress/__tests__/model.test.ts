import { describe, expect, it } from "vitest";
import type { BlockOut, LiftCode, OneRepMaxOut } from "@/api/types";
import { blockRows, hasSynthetic, liftsPresent, type MaxPoint, seriesByLift } from "../model";

/**
 * The progress screen shows two numbers that are not the same number: a max the
 * athlete tested, and the snapshot a block was COMPUTED from. These readers keep
 * them apart, keep every kilogram a string, and let `is_synthetic` -- not
 * `source` -- decide whether a point is real.
 */

const max = (over: Partial<OneRepMaxOut> = {}): OneRepMaxOut => ({
  id: "m1",
  lift_code: "squat",
  kg: "123.5",
  set_on: "2026-01-05",
  source: "tested",
  is_synthetic: false,
  block_id: null,
  ...over,
});

const block = (over: Partial<BlockOut> = {}): BlockOut => ({
  id: "b1",
  athlete_id: "a1",
  template_id: "t1",
  number: 1,
  start_date: "2026-01-05",
  status: "done",
  lifts: ["squat", "bench"],
  one_rm_snapshot: { squat: "123.5" },
  workflow_id: null,
  ...over,
});

describe("seriesByLift", () => {
  it("gives each lift its own series", () => {
    const series = seriesByLift([
      max({ lift_code: "squat" }),
      max({ lift_code: "bench", kg: "97.5" }),
      max({ lift_code: "squat", set_on: "2026-03-23", kg: "128" }),
    ]);
    expect([...series.keys()].sort()).toEqual(["bench", "squat"]);
    expect(series.get("squat")).toHaveLength(2);
  });

  it("puts a series in order, oldest first, whatever order the rows arrived in", () => {
    const series = seriesByLift([
      max({ set_on: "2026-06-08", kg: "142.5" }),
      max({ set_on: "2026-01-05", kg: "123.5" }),
      max({ set_on: "2026-03-23", kg: "128" }),
    ]);
    expect(series.get("squat")?.map((p) => p.setOn)).toEqual([
      "2026-01-05",
      "2026-03-23",
      "2026-06-08",
    ]);
  });

  it("carries the weight through as the exact string it arrived as", () => {
    const series = seriesByLift([max({ kg: "123.50" })]);
    expect(series.get("squat")?.[0]?.kg).toBe("123.50");
  });

  it("skips a lift code this build has no series for", () => {
    const series = seriesByLift([max(), max({ lift_code: "curl", kg: "27.5" })]);
    expect([...series.keys()]).toEqual(["squat"]);
  });

  it("reads a synthetic row as synthetic even when its source says tested", () => {
    // `is_synthetic` is the authoritative "is this real?" and every consumer
    // filters on it (ADR-0004 rule 4). A seeded row claiming `tested` is still
    // development data, and the chart must mark it as such.
    const series = seriesByLift([max({ source: "tested", is_synthetic: true })]);
    expect(series.get("squat")?.[0]?.kind).toBe("synthetic");
  });

  it("keeps the source as the kind when the row is real", () => {
    const series = seriesByLift([
      max({ source: "tested" }),
      max({ lift_code: "bench", source: "estimated", kg: "97.5" }),
      max({ lift_code: "press", source: "imported", kg: "62.5" }),
    ]);
    expect(series.get("squat")?.[0]?.kind).toBe("tested");
    expect(series.get("bench")?.[0]?.kind).toBe("estimated");
    expect(series.get("press")?.[0]?.kind).toBe("imported");
  });
});

describe("hasSynthetic", () => {
  it("is true as soon as one row is development data", () => {
    expect(hasSynthetic([max(), max({ is_synthetic: true })])).toBe(true);
  });

  it("is false when every row came from the athlete", () => {
    expect(hasSynthetic([max(), max({ source: "estimated" })])).toBe(false);
    expect(hasSynthetic([])).toBe(false);
  });
});

describe("blockRows", () => {
  it("puts the blocks in the order they were trained", () => {
    const rows = blockRows([block({ id: "b2", number: 2 }), block({ id: "b1", number: 1 })]);
    expect(rows.map((r) => r.number)).toEqual([1, 2]);
    expect(rows.map((r) => r.id)).toEqual(["b1", "b2"]);
  });

  it("gives the first block no delta at all, for any lift", () => {
    const rows = blockRows([
      block({ one_rm_snapshot: { squat: "123.5", bench: "97.5" } }),
      block({ id: "b2", number: 2, one_rm_snapshot: { squat: "128", bench: "99" } }),
    ]);
    expect([...(rows[0]?.planned.values() ?? [])].map((p) => p.delta)).toEqual([null, null]);
  });

  it("gives a later block the exact change from the one before it", () => {
    const rows = blockRows([
      block({ one_rm_snapshot: { squat: "123.5" } }),
      block({ id: "b2", number: 2, one_rm_snapshot: { squat: "128" } }),
    ]);
    expect(rows[1]?.planned.get("squat")).toEqual({ kg: "128", delta: "4.5" });
  });

  it("shows a restart as the drop it is, rather than hiding it", () => {
    // A block restarted after time off is planned at reduced numbers on purpose
    // (ADR-0010 mod 6): the delta goes negative and says so.
    const rows = blockRows([
      block({ one_rm_snapshot: { squat: "123.5" } }),
      block({ id: "b2", number: 2, one_rm_snapshot: { squat: "117" } }),
    ]);
    expect(rows[1]?.planned.get("squat")?.delta).toBe("-6.5");
  });

  it("has no delta for a lift the previous block never planned", () => {
    // Not a gain of the whole weight: there is nothing to compare it against.
    const rows = blockRows([
      block({ one_rm_snapshot: { squat: "123.5" } }),
      block({ id: "b2", number: 2, one_rm_snapshot: { squat: "128", bench: "97.5" } }),
    ]);
    expect(rows[1]?.planned.get("bench")).toEqual({ kg: "97.5", delta: null });
  });

  it("leaves a lift this block did not plan out of the row entirely", () => {
    const rows = blockRows([
      block({ one_rm_snapshot: { squat: "123.5", press: "62.5" } }),
      block({ id: "b2", number: 2, one_rm_snapshot: { squat: "128" } }),
    ]);
    expect(rows[1]?.planned.has("press")).toBe(false);
    expect([...(rows[1]?.planned.keys() ?? [])]).toEqual(["squat"]);
  });

  it("ignores a snapshot key that is not one of the five lifts", () => {
    const rows = blockRows([block({ one_rm_snapshot: { squat: "123.5", curl: "27.5" } })]);
    expect([...(rows[0]?.planned.keys() ?? [])]).toEqual(["squat"]);
  });
});

describe("liftsPresent", () => {
  it("offers the chart's lifts and the table's, each once", () => {
    const series = seriesByLift([max({ lift_code: "squat" }), max({ lift_code: "bench" })]);
    const rows = blockRows([block({ one_rm_snapshot: { bench: "97.5", press: "62.5" } })]);
    const lifts = liftsPresent(series, rows);
    expect([...lifts].sort()).toEqual(["bench", "press", "squat"]);
    expect(new Set(lifts).size).toBe(lifts.length);
  });

  it("offers nothing when there is nothing on the screen", () => {
    expect(liftsPresent(new Map<LiftCode, MaxPoint[]>(), [])).toEqual([]);
  });
});
