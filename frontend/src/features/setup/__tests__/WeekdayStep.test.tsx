import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LiftCode } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { WeekdayStep } from "../WeekdayStep";

const put = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ API_URL: "http://test", api: { PUT: put } }));

// The authored template's days: squat and bench train Day 1 and 3, deadlift and press
// Day 2, row Day 3 (CONTEXT.md §4).
const BY_LIFT = { squat: [1, 3], bench: [1, 3], deadlift: [2], press: [2], row: [3] };

const verdict = (problems: string[]) =>
  put.mockResolvedValue({ data: { slots: [], problems }, response: new Response(null, { status: 200 }) });

function Harness({ lifts, initial }: { lifts: LiftCode[]; initial: Record<number, number | undefined> }) {
  const [weekdays, setWeekdays] = useState(initial);
  return (
    <WeekdayStep
      athleteId="athlete-1"
      templateId="template-1"
      lifts={lifts}
      byLift={BY_LIFT}
      weekdays={weekdays}
      locked={false}
      onChange={(slot, weekday) => setWeekdays((w) => ({ ...w, [slot]: weekday }))}
      onBack={() => undefined}
      onNext={() => undefined}
    />
  );
}

const next = () => screen.getByRole("button", { name: "Next" });

beforeEach(() => {
  put.mockReset();
});

describe("WeekdayStep", () => {
  it("shows the server's problem inline and keeps Next disabled", async () => {
    verdict(["Squat on Sunday and Monday is only 1 day apart"]);
    // Monday / Wednesday / Sunday: the spec's own example of a bad choice.
    renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{ 1: 0, 2: 2, 3: 6 }} />);

    expect(await screen.findByText(/only 1 day apart/)).toBeInTheDocument();
    expect(next()).toBeDisabled();
  });

  it("enables Next once the server finds nothing wrong", async () => {
    verdict([]);
    renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{ 1: 0, 2: 2, 3: 4 }} />);

    await waitFor(() => expect(next()).toBeEnabled());
    expect(screen.queryByText("These days will not work")).not.toBeInTheDocument();
  });

  it("sends the chosen lifts, so the server judges only the days they use", async () => {
    verdict([]);
    renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{ 1: 0, 2: 2, 3: 4 }} />);

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const [path, options] = put.mock.calls[0] as [string, { params: { query: { dry_run: boolean } }; body: unknown }];
    expect(path).toBe("/athletes/{athlete_id}/weekday-map");
    expect(options.params.query.dry_run).toBe(true);
    expect(options.body).toEqual({
      slots: { "1": 0, "2": 2, "3": 4 },
      template_id: "template-1",
      lifts: ["squat", "deadlift"],
    });
  });

  it("asks for Day 1, 2 and 3 for squat and deadlift, and only Day 2 for deadlift alone", () => {
    verdict([]);
    const { unmount } = renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{}} />);
    expect(screen.getByLabelText("Day 1 weekday")).toBeInTheDocument();
    expect(screen.getByLabelText("Day 2 weekday")).toBeInTheDocument();
    expect(screen.getByLabelText("Day 3 weekday")).toBeInTheDocument();
    unmount();

    renderWithProviders(<Harness lifts={["deadlift"]} initial={{}} />);
    expect(screen.getByLabelText("Day 2 weekday")).toBeInTheDocument();
    expect(screen.queryByLabelText("Day 1 weekday")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Day 3 weekday")).not.toBeInTheDocument();
  });

  it("does not ask the server until every day has a weekday", async () => {
    verdict([]);
    renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{ 1: 0 }} />);

    // Past the 300 ms debounce, so a request would have gone out by now.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450));
    });
    expect(put).not.toHaveBeenCalled();
    expect(next()).toBeDisabled();
  });

  it("waits for the changes to stop and asks once, about the last one", async () => {
    verdict([]);
    // Start with Day 1 missing, so nothing is requested on mount and every request
    // below belongs to the changes. `fireEvent` is synchronous: all three land well
    // inside one debounce window, which a slow `userEvent` run in jsdom cannot promise.
    renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{ 2: 2, 3: 4 }} />);

    const day1 = screen.getByLabelText("Day 1 weekday");
    fireEvent.change(day1, { target: { value: "1" } }); // Tuesday
    fireEvent.change(day1, { target: { value: "2" } }); // Wednesday
    fireEvent.change(day1, { target: { value: "3" } }); // Thursday

    await waitFor(() => expect(put).toHaveBeenCalled());
    // Long enough that a request per change would all have gone out by now.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450));
    });
    expect(put).toHaveBeenCalledTimes(1);
    const options = put.mock.calls[0]?.[1] as { body: { slots: Record<string, number> } };
    expect(options.body.slots["1"]).toBe(3);
  });

  it("keeps Next off while a verdict on the latest choice is still on its way", async () => {
    verdict([]);
    const user = userEvent.setup();
    renderWithProviders(<Harness lifts={["squat", "deadlift"]} initial={{ 1: 0, 2: 2, 3: 4 }} />);
    await waitFor(() => expect(next()).toBeEnabled());

    await user.selectOptions(screen.getByLabelText("Day 3 weekday"), "Saturday");
    // Inside the debounce window the old "fine" is about a different choice.
    expect(next()).toBeDisabled();
    await waitFor(() => expect(next()).toBeEnabled());
  });
});
