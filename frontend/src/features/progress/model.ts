import type { BlockOut, LiftCode, OneRepMaxOut } from "@/api/types";
import { subDecimal } from "@/lib/decimal";
import { isLiftCode } from "@/lib/lifts";

/**
 * The progress screen's two halves: a max per lift over time, and what each
 * block was planned against.
 *
 * They are not the same number and will often disagree. A 1RM is something the
 * athlete tested; a block's snapshot is what the plan was COMPUTED from, and a
 * restart deliberately reduces it (ADR-0010 mod 6). The table says so rather
 * than quietly showing a drop the athlete never lifted.
 */

/** How a max came to be recorded. `synthetic` is not a source -- it is a flag. */
export type PointKind = "tested" | "estimated" | "imported" | "synthetic";

export interface MaxPoint {
  lift: LiftCode;
  /** ISO day, for the axis. */
  setOn: string;
  /** The exact decimal string, for every label. */
  kg: string;
  kind: PointKind;
}

/**
 * One series per lift, oldest first.
 *
 * `is_synthetic` decides, not `source`: the flag is the authoritative "is this
 * real?" and every consumer filters on it (ADR-0004 rule 4). A synthetic row
 * therefore reads `synthetic` here whatever its `source` says.
 */
export function seriesByLift(history: readonly OneRepMaxOut[]): Map<LiftCode, MaxPoint[]> {
  const byLift = new Map<LiftCode, MaxPoint[]>();
  for (const row of history) {
    if (!isLiftCode(row.lift_code)) continue;
    const point: MaxPoint = {
      lift: row.lift_code,
      setOn: row.set_on,
      kg: row.kg,
      kind: row.is_synthetic ? "synthetic" : sourceKind(row.source),
    };
    const existing = byLift.get(row.lift_code);
    if (existing === undefined) byLift.set(row.lift_code, [point]);
    else existing.push(point);
  }
  for (const points of byLift.values()) {
    points.sort((a, b) => a.setOn.localeCompare(b.setOn));
  }
  return byLift;
}

function sourceKind(source: OneRepMaxOut["source"]): PointKind {
  return source === "tested" || source === "estimated" || source === "imported"
    ? source
    : "synthetic";
}

/** Whether anything on this screen is development data, for the banner. */
export function hasSynthetic(history: readonly OneRepMaxOut[]): boolean {
  return history.some((row) => row.is_synthetic);
}

/** One row of the history table: a block, and what it was planned against. */
export interface BlockRow {
  id: string;
  number: number;
  startDate: string;
  status: BlockOut["status"];
  /** lift → the planning max, and the exact change from the block before it. */
  planned: Map<LiftCode, { kg: string; delta: string | null }>;
}

/**
 * A row per block, oldest first, with the change from the previous block.
 *
 * The delta is computed with exact decimal arithmetic, never `Number()`: these
 * are kilograms (CLAUDE.md non-negotiable 4). A lift absent from either block
 * has no delta at all rather than a delta of zero.
 */
export function blockRows(blocks: readonly BlockOut[]): BlockRow[] {
  const ordered = [...blocks].sort((a, b) => a.number - b.number);

  return ordered.map((block, index) => {
    const previous = index === 0 ? undefined : ordered[index - 1];
    const planned = new Map<LiftCode, { kg: string; delta: string | null }>();

    for (const [lift, kg] of Object.entries(block.one_rm_snapshot)) {
      if (!isLiftCode(lift)) continue;
      const before = previous?.one_rm_snapshot[lift];
      planned.set(lift, {
        kg,
        delta: before === undefined ? null : subDecimal(kg, before),
      });
    }

    return {
      id: block.id,
      number: block.number,
      startDate: block.start_date,
      status: block.status,
      planned,
    };
  });
}

/** The lifts that actually appear, so the filter offers nothing empty. */
export function liftsPresent(
  series: ReadonlyMap<LiftCode, readonly MaxPoint[]>,
  rows: readonly BlockRow[],
): LiftCode[] {
  const lifts = new Set<LiftCode>(series.keys());
  for (const row of rows) for (const lift of row.planned.keys()) lifts.add(lift);
  return [...lifts];
}
