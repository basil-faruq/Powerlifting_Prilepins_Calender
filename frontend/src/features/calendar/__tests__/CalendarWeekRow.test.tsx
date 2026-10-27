import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SessionOut } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { CalendarWeekRow } from "../CalendarWeekRow";
import { ghostPlan, NO_GHOSTS } from "../ghost";
import { buildWeekRows } from "../model";

const session = (over: Partial<SessionOut> & { date: string; seq: number }): SessionOut => ({
  id: `s${over.seq}`,
  block_id: "b1",
  week: 1,
  slot: 1,
  original_date: over.date,
  status: "planned",
  lifts: [
    {
      lift_code: "squat",
      variation: null,
      sets: 3,
      reps: 10,
      pct_num: "70.0000",
      pct_den: "120.0000",
      weight_kg: "75.00",
      rel_intensity: "0.7778",
      is_test: false,
    },
  ],
  ...over,
});

/** Monday, Wednesday, Friday of the week beginning 19 October 2026. */
const SESSIONS = [
  session({ date: "2026-10-19", seq: 1, slot: 1 }),
  session({ date: "2026-10-21", seq: 2, slot: 2 }),
  session({ date: "2026-10-23", seq: 3, slot: 3 }),
];

const TODAY = "2026-10-21";
const MON_WED_FRI = new Set([0, 2, 4]);

function renderRow(over: Partial<Parameters<typeof CalendarWeekRow>[0]> = {}) {
  const row = buildWeekRows(SESSIONS)[0];
  if (row === undefined) throw new Error("no row built");
  return renderWithProviders(
    <CalendarWeekRow
      row={row}
      today={TODAY}
      isCurrent={false}
      trainingDays={MON_WED_FRI}
      ghosts={NO_GHOSTS}
      showGhosts={false}
      sessionsBySeq={new Map(SESSIONS.map((s) => [s.seq, s]))}
      onOpen={() => {}}
      {...over}
    />,
  );
}

const dayCell = (day: string) => within(screen.getByLabelText(new RegExp(`^${day} `)));

describe("CalendarWeekRow", () => {
  it("lays out all seven weekdays, whether or not anything is planned on them", () => {
    renderRow();
    for (const day of ["Monday", "Tuesday", "Saturday", "Sunday"]) {
      expect(screen.getByLabelText(new RegExp(`^${day} `))).toBeInTheDocument();
    }
  });

  it("puts each session in the column of the weekday it falls on", () => {
    renderRow();
    expect(dayCell("Monday").getByRole("button")).toBeInTheDocument();
    expect(dayCell("Wednesday").getByRole("button")).toBeInTheDocument();
    expect(dayCell("Tuesday").queryByRole("button")).not.toBeInTheDocument();
  });

  it("labels the row with the week number and the Monday it starts on", () => {
    renderRow();
    expect(screen.getByText("W1")).toBeInTheDocument();
    // The Monday cell carries that date too, so the row's own label is the one
    // outside any session button.
    const dated = screen.getAllByText("19 Oct");
    expect(dated.some((el) => el.closest("button") === null)).toBe(true);
  });

  // --- the ghost overlay ---------------------------------------------------

  const moved = ghostPlan({
    changes: [{ seq: 1, action: "move", from_date: "2026-10-19", to_date: "2026-10-20" }],
  });

  it("draws nothing proposed while the overlay is off, even with a pending move", () => {
    renderRow({ ghosts: moved, showGhosts: false });
    expect(dayCell("Tuesday").queryByRole("button")).not.toBeInTheDocument();
    const monday = dayCell("Monday").getByRole("button");
    expect(monday.getAttribute("aria-label")).not.toContain("would move away");
  });

  it("draws the session dashed at its proposed new date when the overlay is on", () => {
    renderRow({ ghosts: moved, showGhosts: true });
    // Tuesday holds no real session; the cell there is the proposal's.
    const tuesday = dayCell("Tuesday").getByRole("button");
    expect(tuesday.getAttribute("aria-label")).toContain("proposed new date");
  });

  it("marks the session it would leave behind at the old date", () => {
    renderRow({ ghosts: moved, showGhosts: true });
    const monday = dayCell("Monday").getByRole("button");
    expect(monday.getAttribute("aria-label")).toContain("would move away");
  });

  it("leaves the sessions a proposal does not touch alone", () => {
    renderRow({ ghosts: moved, showGhosts: true });
    const friday = dayCell("Friday").getByRole("button");
    expect(friday.getAttribute("aria-label")).not.toContain("would move away");
  });

  it("ignores a move onto a date in some other week", () => {
    const elsewhere = ghostPlan({
      changes: [{ seq: 1, action: "move", from_date: "2026-10-19", to_date: "2026-11-30" }],
    });
    renderRow({ ghosts: elsewhere, showGhosts: true });
    for (const day of ["Tuesday", "Thursday", "Saturday", "Sunday"]) {
      expect(dayCell(day).queryByRole("button")).not.toBeInTheDocument();
    }
  });
});
