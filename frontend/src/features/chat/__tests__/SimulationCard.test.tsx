import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { LiftOutlook, SimulationOut } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { SimulationCard } from "../SimulationCard";

/**
 * The card's whole job is non-negotiable 8: percentiles and assumptions, never one
 * predicted number. These tests watch the two ways that could quietly be broken --
 * a band collapsing to a point, and a null being printed as a zero.
 */

const outlook = (over: Partial<LiftOutlook> = {}): LiftOutlook => ({
  test: "pending",
  p_test_made: 0.62,
  current_1rm_kg: "140.00",
  new_1rm: { p10: "142.50", p50: "147.50", p90: "150.00" },
  ...over,
});

const simulation = (over: Partial<SimulationOut> = {}): SimulationOut => ({
  planned_end_date: "2026-12-21",
  finish_date: { p10: "2026-12-21", p50: "2026-12-28", p90: "2027-01-11" },
  per_lift: { squat: outlook() },
  missed_sessions: { p10: 0, p50: 2, p90: 5 },
  missed_sessions_mean: 2.4,
  p_restart: 0.08,
  p_unplannable: 0,
  assumptions: ["Sessions are missed independently, at the rate seen so far."],
  stats: {
    runs: 1000,
    seed: 0,
    wall_time_s: 1.2,
    runs_finished: 980,
    runs_restart: 80,
    runs_unplannable: 0,
  },
  block_id: "b1",
  has_synthetic_one_rms: false,
  ...over,
});

describe("the finish date", () => {
  it("gives a band and its middle, never a single date", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    const line = screen.getByText(/Finishing between/);
    expect(line).toHaveTextContent(/21 Dec 2026/);
    expect(line).toHaveTextContent(/11 Jan 2027/);
    expect(line).toHaveTextContent(/28 Dec 2026/);
  });

  it("says no run finished rather than inventing a date when none did", () => {
    renderWithProviders(<SimulationCard simulation={simulation({ finish_date: null })} />);
    expect(screen.getByText(/No simulated run finished the block/)).toBeInTheDocument();
    expect(screen.queryByText(/Finishing between/)).not.toBeInTheDocument();
    // The PLANNED end still shows: it is the template's own arithmetic, not a
    // simulated outcome, so withholding it would hide something known.
    expect(screen.getByText(/Planned to end/)).toHaveTextContent(/21 Dec 2026/);
  });
});

describe("the per-lift outlook", () => {
  it("reads kg straight from the decimal strings, without passing through a float", () => {
    // `formatKg("142.50")` is "142.5 kg". Nothing here may go near `Number()`.
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    expect(screen.getByText(/142\.5 kg/)).toHaveTextContent(/150 kg/);
    expect(screen.getByText(/now 140 kg/)).toBeInTheDocument();
  });

  it("keeps a weight that no float could hold exactly", () => {
    const sim = simulation({
      per_lift: {
        squat: outlook({
          current_1rm_kg: "140.10",
          new_1rm: { p10: "142.55", p50: "147.55", p90: "150.05" },
        }),
      },
    });
    renderWithProviders(<SimulationCard simulation={sim} />);
    expect(screen.getByText(/142\.55 kg/)).toHaveTextContent(/150\.05 kg/);
  });

  it("gives the chance of making a test that is still ahead", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    expect(screen.getByText("62% chance of making the test")).toBeInTheDocument();
  });

  it("says there is no test this block rather than a 0% chance of making one", () => {
    const sim = simulation({
      per_lift: { row: outlook({ test: "none", p_test_made: null }) },
    });
    renderWithProviders(<SimulationCard simulation={sim} />);
    expect(screen.getByText("no test this block")).toBeInTheDocument();
    expect(screen.queryByText(/0%/)).not.toBeInTheDocument();
  });

  it("says a test is already behind you rather than a 0% chance of making it", () => {
    const sim = simulation({
      per_lift: { squat: outlook({ test: "behind", p_test_made: null }) },
    });
    renderWithProviders(<SimulationCard simulation={sim} />);
    expect(screen.getByText("test already behind you")).toBeInTheDocument();
    expect(screen.queryByText(/chance of making/)).not.toBeInTheDocument();
  });
});

describe("the not-done band", () => {
  it("is a range with a typical value, not a count", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    // "missed" is reserved for a session a human wrote off (ADR-0017); a simulated
    // absence is nobody's decision, so OUR line says "not done". The server's own
    // assumption sentences are quoted as data and are not ours to reword.
    expect(screen.queryByText(/^Sessions missed:/)).not.toBeInTheDocument();
    expect(screen.getByText(/Sessions not done:/)).toHaveTextContent(
      "Sessions not done: 0–5, typically 2",
    );
  });
});

describe("the assumptions", () => {
  it("lists what the simulator was told to believe", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    expect(screen.getByText(/Sessions are missed independently/)).toBeInTheDocument();
  });

  it("says the constants behind the numbers are starting values, not derived ones", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    expect(screen.getByText(/starting values chosen to be reasonable/)).toBeInTheDocument();
  });

  it("counts them on the button that opens them", () => {
    const sim = simulation({ assumptions: ["one", "two"] });
    renderWithProviders(<SimulationCard simulation={sim} />);
    expect(screen.getByRole("button", { name: /What this assumes \(2\)/ })).toBeInTheDocument();
  });

  it("still qualifies the numbers when the simulator stated no assumptions", async () => {
    // The caveat that the constants are guesses is the one line that may never be
    // conditional: bands shown with nothing qualifying them are what CLAUDE.md's
    // "label a guessed threshold as a starting value" exists to prevent.
    const user = userEvent.setup();
    renderWithProviders(<SimulationCard simulation={simulation({ assumptions: [] })} />);

    const trigger = screen.getByRole("button", { name: /What this assumes/ });
    expect(trigger).toBeInTheDocument();
    // No count, because the server offered none of its own.
    expect(trigger).toHaveTextContent(/^What this assumes$/);

    await user.click(trigger);
    expect(await screen.findByText(/starting values chosen to be reasonable/)).toBeInTheDocument();
  });
});

describe("the run itself", () => {
  it("names the number of runs and the seed, so the answer can be reproduced", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    expect(screen.getByText("1000 simulated runs, seed 0.")).toBeInTheDocument();
  });

  it("mentions an unplannable share only when there was one", () => {
    renderWithProviders(<SimulationCard simulation={simulation()} />);
    expect(screen.queryByText(/no workable plan/)).not.toBeInTheDocument();

    renderWithProviders(<SimulationCard simulation={simulation({ p_unplannable: 0.03 })} />);
    expect(screen.getByText(/no workable plan/)).toBeInTheDocument();
  });
});
