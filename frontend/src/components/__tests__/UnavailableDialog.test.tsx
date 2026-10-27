import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/renderWithProviders";
import { UnavailableDialog } from "../UnavailableDialog";

const post = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ API_URL: "http://test", api: { POST: post } }));

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toaster", () => ({ toaster: { create: toast } }));

// The dialog reads the athlete only for its timezone and nothing else.
vi.mock("@/components/AthleteProvider", () => ({
  useAthleteContext: () => ({
    athlete: { timezone: "Europe/Berlin" },
    athleteId: "a1",
    currentBlock: null,
    currentBlockId: "b1",
  }),
}));

const ok = (data: unknown, status = 202) => ({ data, response: new Response(null, { status }) });
const fail = (status: number, body: unknown) => ({
  error: body,
  response: new Response(null, { status }),
});

function open(defaultFrom = "2026-11-09") {
  renderWithProviders(
    <UnavailableDialog open blockId="b1" defaultFrom={defaultFrom} onClose={vi.fn()} />,
  );
}

const body = () => post.mock.calls[0]?.[1] as { body: Record<string, unknown> };

beforeEach(() => {
  post.mockReset();
  toast.mockReset();
});

describe("UnavailableDialog", () => {
  it("starts from the day it was opened on, not from today", () => {
    open("2026-11-09");
    expect(screen.getByLabelText("From")).toHaveValue("2026-11-09");
  });

  it("sends the signal using the API's own field names", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(ok({ state: "ACTIVE", block_id: "b1" }));
    open();

    await user.click(screen.getByRole("button", { name: /Tell the planner/ }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    // `from` is an alias on the wire; sending `from_date` would be silently wrong.
    expect(body().body).toEqual({ from: "2026-11-09", injury: false });
  });

  it("includes an end date when one is given, and omits it when not", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(ok({ state: "ACTIVE", block_id: "b1" }));
    open();

    await user.type(screen.getByLabelText(/Until/), "2026-11-18");
    await user.click(screen.getByRole("button", { name: /Tell the planner/ }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body().body).toMatchObject({ until: "2026-11-18" });
  });

  it("says an injury restarts the block, before anything is sent", async () => {
    const user = userEvent.setup();
    open();

    expect(screen.getByText(/A short gap moves sessions/)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox"));

    // ADR-0010: an injury is a restart at reduced PLANNING maxes, and the
    // athlete's recorded 1RMs are untouched. Both halves have to be said.
    expect(screen.getByText(/proposes a restart/)).toBeInTheDocument();
    expect(screen.getByText(/recorded 1RMs do not change/)).toBeInTheDocument();
  });

  it("treats a signal nobody could confirm as saved, not as failed", async () => {
    const user = userEvent.setup();
    // ADR-0017 rule 10: the signal is durable; only the state read failed.
    post.mockResolvedValue(ok({ state: "unknown", block_id: "b1" }));
    open();

    await user.click(screen.getByRole("button", { name: /Tell the planner/ }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ type: "info", title: "Saved" }),
      );
    });
  });

  it("explains a 409 as the block not running", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(fail(409, { detail: "block is not running" }));
    open();

    await user.click(screen.getByRole("button", { name: /Tell the planner/ }));

    expect(await screen.findByText(/not running/)).toBeInTheDocument();
  });

  it("will not send a range that ends before it starts", async () => {
    const user = userEvent.setup();
    open("2026-11-09");

    await user.type(screen.getByLabelText(/Until/), "2026-11-02");

    expect(screen.getByRole("button", { name: /Tell the planner/ })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });
});
