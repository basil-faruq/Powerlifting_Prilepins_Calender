import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { PlannedLiftOut, SessionOut } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { SessionCell } from "../SessionCell";

/** `formatKg` and `formatDate` emit non-breaking spaces; assertions need plain ones. */
const plain = (text: string) => text.replace(/\s/g, " ");

const lift = (over: Partial<PlannedLiftOut> = {}): PlannedLiftOut => ({
  lift_code: "squat",
  variation: null,
  sets: 3,
  reps: 10,
  pct_num: "70",
  pct_den: "120",
  weight_kg: "81.00",
  rel_intensity: "0.7800",
  is_test: false,
  ...over,
});

const session = (over: Partial<SessionOut> = {}): SessionOut =>
  ({
    id: "s1",
    block_id: "b1",
    week: 2,
    slot: 1,
    seq: 4,
    date: "2026-10-21",
    original_date: "2026-10-21",
    status: "planned",
    lifts: [lift()],
    ...over,
  });

const TODAY = "2026-10-21";

describe("SessionCell", () => {
  it("shows each lift as sets by reps at the weight the server computed", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} />);
    expect(plain(screen.getByText(/3×10/).textContent ?? "")).toBe("3×10 @ 81 kg");
  });

  it("names the lift, so two letters and a colour are never the only identification", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} />);
    expect(screen.getByText("SQ")).toHaveAttribute("title", "Squat");
  });

  it("lists every lift in the session", () => {
    const lifts = [lift(), lift({ lift_code: "bench", sets: 3, reps: 8, weight_kg: "60.00" })];
    renderWithProviders(<SessionCell session={session({ lifts })} today={TODAY} />);
    expect(screen.getByText("SQ")).toBeInTheDocument();
    expect(screen.getByText("BP")).toBeInTheDocument();
  });

  it("tags a test single", () => {
    const lifts = [lift({ is_test: true })];
    renderWithProviders(<SessionCell session={session({ lifts })} today={TODAY} />);
    expect(screen.getByText("TEST")).toBeInTheDocument();
  });

  // --- statuses -----------------------------------------------------------
  // Every one carries a WORD as well as a glyph: colour is never the only signal.

  it("marks a logged session done", () => {
    renderWithProviders(<SessionCell session={session({ status: "done" })} today={TODAY} />);
    expect(screen.getByText("done")).toBeInTheDocument();
  });

  it("says 'not logged', not 'missed', for a past session nobody logged", () => {
    // ADR-0017: `missed` is never written, and means written off. A session the
    // athlete may still make up stays `planned` with a date in the past.
    renderWithProviders(<SessionCell session={session({ date: "2026-10-14" })} today={TODAY} />);
    expect(screen.getByText("not logged")).toBeInTheDocument();
    expect(screen.queryByText("missed")).not.toBeInTheDocument();
  });

  it("does not call today's session unlogged", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} />);
    expect(screen.queryByText("not logged")).not.toBeInTheDocument();
  });

  it("still says missed for a session a human wrote off", () => {
    renderWithProviders(<SessionCell session={session({ status: "missed" })} today={TODAY} />);
    expect(screen.getByText("missed")).toBeInTheDocument();
  });

  it("strikes through a skipped session and says so", () => {
    renderWithProviders(<SessionCell session={session({ status: "skipped" })} today={TODAY} />);
    expect(screen.getByText("skipped")).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveStyle({ textDecoration: "line-through" });
  });

  it("says where a moved session came from, in words and in its tooltip", () => {
    const moved = session({ status: "moved", date: "2026-10-22", original_date: "2026-10-21" });
    renderWithProviders(<SessionCell session={moved} today={TODAY} />);
    expect(screen.getByText("moved")).toBeInTheDocument();
    expect(plain(screen.getByRole("button").getAttribute("title") ?? "")).toBe("Moved from Wed, 21 Oct 2026");
  });

  // --- the ghost overlay ---------------------------------------------------

  it("labels a cell drawn at a proposed new date as proposed", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} ghost="arriving" />);
    expect(screen.getByText("proposed")).toBeInTheDocument();
  });

  it("says in the cell's own label that a session would move away", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} ghost="leaving" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toContain("would move away");
  });

  // --- the whole cell in one sentence --------------------------------------

  it("names the week, the date, the prescription and the intensity band in its label", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} />);
    const spoken = plain(screen.getByRole("button").getAttribute("aria-label") ?? "");
    expect(spoken).toContain("Week 2");
    expect(spoken).toContain("Wed, 21 Oct 2026");
    expect(spoken).toContain("Squat 3×10 at 81 kg");
    expect(spoken).toContain("top intensity 70–80%");
  });

  // --- opening the drawer --------------------------------------------------

  it("hands the session back when it is clicked", async () => {
    const onOpen = vi.fn();
    renderWithProviders(<SessionCell session={session()} today={TODAY} onOpen={onOpen} />);
    await userEvent.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "s1" }));
  });

  it("is not clickable when there is nothing to open", () => {
    renderWithProviders(<SessionCell session={session()} today={TODAY} />);
    expect(screen.getByRole("button")).toBeDisabled();
  });
});
