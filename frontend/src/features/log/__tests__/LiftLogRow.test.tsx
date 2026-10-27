import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/renderWithProviders";
import { LiftLogRow } from "../LiftLogRow";
import { validateEntry, type EntryProblems, type LiftEntry } from "../model";

/** `formatKg` emits non-breaking spaces; assertions need plain ones. */
const plain = (text: string) => text.replace(/\s/g, " ");

const entry = (over: Partial<LiftEntry> = {}): LiftEntry => ({
  liftCode: "squat",
  setsDone: "3",
  repsDone: ["8"],
  weightKg: "87.50",
  rpe: "",
  isTest: false,
  prescribed: { sets: 3, reps: 8, weightKg: "87.50" },
  ...over,
});

const NO_PROBLEMS: EntryProblems = { errors: [], warnings: [] };

/** The athlete's own step, never assumed (ADR-0008). */
const STEP = "2.5";

function row(over: Partial<LiftEntry> = {}, problems: EntryProblems = NO_PROBLEMS) {
  const onChange = vi.fn();
  renderWithProviders(
    <LiftLogRow entry={entry(over)} problems={problems} onChange={onChange} />,
  );
  return onChange;
}

describe("LiftLogRow", () => {
  it("shows what was prescribed, so there is something to log against", () => {
    row();
    expect(plain(screen.getByText(/3×8/).textContent ?? "")).toBe("3×8 @ 87.5 kg");
  });

  it("starts filled in with the prescription, so an as-written session is one tap", () => {
    row();
    expect(screen.getByLabelText("Sets done for squat")).toHaveValue("3");
    expect(screen.getByLabelText("Reps done for squat")).toHaveValue("8");
    // The weight is the exact string the server sent, not a reformatted number.
    expect(screen.getByLabelText("Weight for squat")).toHaveValue("87.50");
  });

  it("asks for a number pad without making the value a number", () => {
    // A number input would expose `valueAsNumber` and invite a float into a
    // weight; these stay text inputs with a numeric keyboard.
    row();
    expect(screen.getByLabelText("Sets done for squat")).toHaveAttribute("inputmode", "numeric");
    expect(screen.getByLabelText("Weight for squat")).toHaveAttribute("inputmode", "decimal");
    expect(screen.getByLabelText("Weight for squat")).not.toHaveAttribute("type", "number");
  });

  it("hands the weight back exactly as typed", async () => {
    const user = userEvent.setup();
    const onChange = row();

    await user.type(screen.getByLabelText("Weight for squat"), "1");

    const last = onChange.mock.calls.at(-1)?.[0] as LiftEntry;
    expect(last.weightKg).toBe("87.501");
  });

  it("can split the reps per set and put them back together", async () => {
    const user = userEvent.setup();
    const onChange = row();

    await user.click(screen.getByRole("button", { name: /Reps differed per set/ }));

    const split = onChange.mock.calls.at(-1)?.[0] as LiftEntry;
    // One box per set done, each seeded with what was prescribed.
    expect(split.repsDone).toEqual(["8", "8", "8"]);
  });

  it("gives every set in a per-set list its own labelled box", () => {
    row({ repsDone: ["8", "7", "5"] });
    expect(screen.getByLabelText("Reps in set 1 for squat")).toHaveValue("8");
    expect(screen.getByLabelText("Reps in set 3 for squat")).toHaveValue("5");
    // The single-box control is gone while the list is showing.
    expect(screen.queryByLabelText("Reps done for squat")).not.toBeInTheDocument();
  });

  it("offers RPE as the scale lifters use, halves included, and nothing by default", () => {
    row();
    const rpe = screen.getByLabelText("RPE for squat");
    expect(rpe).toHaveValue("");
    expect(screen.getByRole("option", { name: "8.5" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "5.5" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "10.5" })).not.toBeInTheDocument();
  });

  it("shows an error where the athlete can see it", () => {
    row({}, { errors: ["Sets must be between 0 and 10."], warnings: [] });
    expect(screen.getByText("Sets must be between 0 and 10.")).toBeInTheDocument();
  });

  it("treats a weight off the plate step as a warning, not a refusal", () => {
    // ADR-0008: the increment is a setting, and a gym with microplates is real.
    // Refusing the weight actually lifted would falsify the record.
    const problems = validateEntry(entry({ weightKg: "117.5" }), "5");
    expect(problems.errors).toEqual([]);
    expect(problems.warnings).toHaveLength(1);

    row({ weightKg: "117.5" }, problems);
    expect(screen.getByText(/microplates/)).toBeInTheDocument();
  });

  it("accepts a weight that only exact arithmetic recognises as on-step", () => {
    // 117.5 / 2.5 is exact here; as a float the remainder is not zero.
    expect(validateEntry(entry({ weightKg: "117.5" }), STEP).warnings).toEqual([]);
  });

  it("swaps the inputs for two buttons on a test single", () => {
    row({ isTest: true, prescribed: { sets: 1, reps: 1, weightKg: "142.50" } });

    expect(screen.getByRole("button", { name: "Made it" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Missed it" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Sets done for squat")).not.toBeInTheDocument();
  });

  it("disables every control while the log is being saved", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <LiftLogRow entry={entry()} problems={NO_PROBLEMS} disabled onChange={onChange} />,
    );
    expect(screen.getByLabelText("Sets done for squat")).toBeDisabled();
    expect(screen.getByLabelText("Weight for squat")).toBeDisabled();
  });
});
