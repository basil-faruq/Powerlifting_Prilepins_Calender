import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/renderWithProviders";
import { TestSingleButtons } from "../TestSingleButtons";
import { toLogIn, type LiftEntry } from "../model";

const single = (over: Partial<LiftEntry> = {}): LiftEntry => ({
  liftCode: "squat",
  setsDone: "1",
  repsDone: ["1"],
  weightKg: "142.50",
  rpe: "",
  isTest: true,
  prescribed: { sets: 1, reps: 1, weightKg: "142.50" },
  ...over,
});

function render(over: Partial<LiftEntry> = {}) {
  const onChange = vi.fn();
  renderWithProviders(<TestSingleButtons entry={single(over)} onChange={onChange} />);
  return onChange;
}

describe("TestSingleButtons", () => {
  it("records a made attempt as one rep at the weight on the bar", async () => {
    const user = userEvent.setup();
    const onChange = render({ setsDone: "0", repsDone: ["0"] });

    await user.click(screen.getByRole("button", { name: "Made it" }));

    const next = onChange.mock.calls.at(-1)?.[0] as LiftEntry;
    expect(next).toMatchObject({ setsDone: "1", repsDone: ["1"] });
    // The weight is the athlete's claim about what they lifted, so pressing a
    // button must never quietly change it.
    expect(next.weightKg).toBe("142.50");
  });

  it("records a miss as the attempt it was, not as an absence", async () => {
    const user = userEvent.setup();
    const onChange = render();

    await user.click(screen.getByRole("button", { name: "Missed it" }));

    // There is no "missed" field on the wire. Zero reps is how a failed attempt
    // is stated, and it is what stops the backend recording a new max.
    const next = onChange.mock.calls.at(-1)?.[0] as LiftEntry;
    expect(next).toMatchObject({ setsDone: "0", repsDone: ["0"] });
    expect(next.weightKg).toBe("142.50");
  });

  it("says a new max is coming only when one actually is", () => {
    render();
    expect(screen.getByText(/records a new tested squat max of 142.5/)).toBeInTheDocument();
  });

  it("says what it would take, when the weight is under the prescription", () => {
    render({ weightKg: "140" });
    expect(screen.getByText(/only at or above the prescribed 142.5/)).toBeInTheDocument();
  });

  it("counts a heavier single than prescribed as the new max", () => {
    render({ weightKg: "145" });
    expect(screen.getByText(/records a new tested squat max of 145/)).toBeInTheDocument();
  });

  it("marks which of the two was chosen, for a reader who cannot see colour", () => {
    render();
    expect(screen.getByRole("button", { name: "Made it" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Missed it" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("sends a miss as zero reps at the prescribed weight", () => {
    // What the API actually receives, end to end.
    const body = toLogIn([single({ setsDone: "0", repsDone: ["0"] })], "2026-11-02", "");
    expect(body.entries[0]).toEqual({
      lift_code: "squat",
      sets_done: 0,
      reps_done: 0,
      weight_kg: "142.50",
    });
  });
});
