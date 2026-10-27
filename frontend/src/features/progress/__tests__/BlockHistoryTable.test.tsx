import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LiftCode } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { BlockHistoryTable } from "../BlockHistoryTable";
import type { BlockRow } from "../model";

/**
 * The table's whole reason for existing is the misreading it prevents: a
 * block's snapshot is what the plan was computed FROM, so a drop is the plan
 * being careful rather than the athlete getting weaker. The footnote saying so
 * is asserted here as firmly as the numbers are.
 */

/** `formatKg` emits a non-breaking space; assertions want a plain one. */
const plain = (text: string) => text.replace(/\s/g, " ");

const row = (over: Partial<BlockRow> = {}): BlockRow => ({
  id: "b1",
  number: 1,
  startDate: "2026-01-05",
  status: "done",
  planned: new Map(),
  ...over,
});

const COLUMNS: LiftCode[] = ["squat", "bench"];

const ROWS: BlockRow[] = [
  row({
    planned: new Map([
      ["squat", { kg: "123.50", delta: null }],
      ["bench", { kg: "97.50", delta: null }],
    ]),
  }),
  row({
    id: "b2",
    number: 2,
    startDate: "2026-03-23",
    status: "active",
    planned: new Map([
      ["squat", { kg: "128.00", delta: "4.5" }],
      ["bench", { kg: "91.00", delta: "-6.5" }],
    ]),
  }),
  row({
    id: "b3",
    number: 3,
    startDate: "2026-06-08",
    status: "draft",
    planned: new Map([["squat", { kg: "142.50", delta: "14.5" }]]),
  }),
];

/**
 * The desktop table. The phone layout holds the same figures in cards, so every
 * query is scoped here rather than matching both. `hidden: true` is needed
 * because jsdom matches no media query, so Chakra's `{ base: "none", md:
 * "block" }` leaves the table `display: none` however wide the window claims
 * to be -- the table is the layout a desktop sees, not a hidden one.
 */
const table = () => within(screen.getByRole("table", { name: "Block history", hidden: true }));

/** One body row, by the block number in its first cell. */
function bodyRow(number: number) {
  const rows = table().getAllByRole("row", { hidden: true });
  const found = rows.find((element) => element.textContent?.startsWith(String(number)) === true);
  if (found === undefined) throw new Error(`no row for block ${number}`);
  return within(found);
}

describe("BlockHistoryTable", () => {
  it("shows a row per block, with the planning max it was computed from", () => {
    renderWithProviders(<BlockHistoryTable rows={ROWS} lifts={COLUMNS} />);
    // One header row plus one per block.
    expect(table().getAllByRole("row", { hidden: true })).toHaveLength(4);
    expect(plain(bodyRow(1).getByText(/123/).textContent ?? "")).toBe("123.5 kg");
    expect(plain(bodyRow(2).getByText(/128/).textContent ?? "")).toBe("128 kg");
  });

  it("marks a rise with a plus and leaves a drop to speak for itself", () => {
    renderWithProviders(<BlockHistoryTable rows={ROWS} lifts={COLUMNS} />);
    expect(bodyRow(2).getByText("+4.5 kg")).toBeInTheDocument();
    expect(bodyRow(2).getByText("-6.5 kg")).toBeInTheDocument();
  });

  it("shows no change beside the first block, which has nothing to change from", () => {
    renderWithProviders(<BlockHistoryTable rows={ROWS} lifts={COLUMNS} />);
    expect(bodyRow(1).queryAllByText(/^[+-]\d/)).toHaveLength(0);
    expect(bodyRow(1).queryByText("same")).not.toBeInTheDocument();
  });

  it("leaves a lift this block did not plan as a dash, not as a zero", () => {
    renderWithProviders(<BlockHistoryTable rows={ROWS} lifts={COLUMNS} />);
    expect(bodyRow(3).getByText("—")).toBeInTheDocument();
  });

  it("says in words that these are planning maxes and not maxes the athlete tested", () => {
    renderWithProviders(<BlockHistoryTable rows={ROWS} lifts={COLUMNS} />);
    expect(screen.getByText("planned against")).toBeInTheDocument();
  });

  it("labels a block that has not been approved yet", () => {
    renderWithProviders(<BlockHistoryTable rows={ROWS} lifts={COLUMNS} />);
    expect(bodyRow(3).getByText("draft")).toBeInTheDocument();
    expect(bodyRow(2).queryByText("draft")).not.toBeInTheDocument();
  });

  it("says so plainly when there are no blocks to show", () => {
    renderWithProviders(<BlockHistoryTable rows={[]} lifts={COLUMNS} />);
    expect(screen.getByText("No blocks yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { hidden: true })).not.toBeInTheDocument();
  });
});
