import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProposalOut } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ProposalCard } from "../ProposalCard";

const post = vi.hoisted(() => vi.fn());
const get = vi.hoisted(() => vi.fn());
const patch = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ API_URL: "http://test", api: { POST: post, GET: get, PATCH: patch } }));

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toaster", () => ({ toaster: { create: toast } }));

const ok = (data: unknown, status = 200) => ({ data, response: new Response(null, { status }) });
const fail = (status: number, body: unknown) => ({
  error: body,
  response: new Response(null, { status }),
});

const proposal = (over: Partial<ProposalOut> = {}): ProposalOut => ({
  id: "p1",
  block_id: "b1",
  kind: "reschedule",
  status: "pending",
  payload: {
    changes: [
      { seq: 4, week: 2, slot: 1, action: "move", from_date: "2026-10-19", to_date: "2026-10-20" },
    ],
    plan: [
      {
        week: 2,
        slot: 1,
        seq: 4,
        date: "2026-10-20",
        lifts: [
          {
            lift_code: "squat",
            variation: null,
            sets: 3,
            reps: 8,
            pct_num: "75",
            pct_den: "120",
            weight_kg: "87.50",
            rel_intensity: "0.8100",
            is_test: false,
          },
        ],
      },
    ],
  },
  cost: { delay_days: 1, off_weekday_count: 1, end_slip_days: 0 },
  reasons: ["Monday was not possible."],
  warnings: [],
  source: "workflow",
  decided_at: null,
  decision_reason: null,
  ...over,
});

beforeEach(() => {
  post.mockReset();
  get.mockReset();
  patch.mockReset();
  toast.mockReset();
});

describe("ProposalCard", () => {
  it("shows the reasons, the changes and what the option costs", () => {
    renderWithProviders(<ProposalCard proposal={proposal()} />);

    expect(screen.getByText("Reschedule")).toBeInTheDocument();
    expect(screen.getByText("Monday was not possible.")).toBeInTheDocument();
    // The lift is named from the payload's own plan, joined on seq.
    expect(screen.getByText("SQ")).toBeInTheDocument();
    expect(screen.getByText(/19 Oct.*→.*20 Oct/)).toBeInTheDocument();
    expect(screen.getByText(/1 day later/)).toBeInTheDocument();
    expect(screen.getByText(/1 session off their weekday/)).toBeInTheDocument();
  });

  it("marks a workflow proposal as automatic, and never as 'missed'", () => {
    // ADR-0017: `missed` is never written and means written off by a human. The
    // cause is in the reasons, which the server wrote; the tag claims none.
    renderWithProviders(<ProposalCard proposal={proposal({ source: "workflow" })} />);
    expect(screen.getByText("raised automatically")).toBeInTheDocument();
    expect(screen.queryByText(/missed/i)).not.toBeInTheDocument();
  });

  it("hides warnings behind a toggle and does not treat them as blocking", async () => {
    const user = userEvent.setup();
    const warned = proposal({
      warnings: [{ rule: "prilepin", message: "Above the Prilepin range for this intensity.", refs: [] }],
    });
    renderWithProviders(<ProposalCard proposal={warned} />);

    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: /1 warning/ }));
    expect(await screen.findByText(/Above the Prilepin range/)).toBeInTheDocument();
  });

  it("approves, and says so when the workflow has decided", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(ok({ ...proposal(), status: "approved" }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ type: "success", title: "Approved" }));
    });
    expect(post).toHaveBeenCalledWith(
      "/proposals/{proposal_id}/approve",
      expect.objectContaining({ params: { path: { proposal_id: "p1" } } }),
    );
  });

  it("disables both decisions while one is in flight, so nothing is approved twice", async () => {
    const user = userEvent.setup();
    // Never settles: the buttons must stay disabled for as long as it is pending.
    post.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled());
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("treats a 409 'already approved' as success, not as an error", async () => {
    const user = userEvent.setup();
    // The first attempt committed and only its answer was lost (ADR-0019).
    post.mockResolvedValue(fail(409, { detail: "This proposal is already approved." }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ type: "success", title: "Approved" }));
    });
    expect(screen.queryByText(/out of date/i)).not.toBeInTheDocument();
  });

  it("treats any other 409 as a stale proposal", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(fail(409, { detail: "This proposal was superseded." }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ type: "error", title: "This proposal is out of date" }),
      );
    });
  });

  it("explains a 503 as a missing worker rather than a lost approval", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(fail(503, { detail: "The workflow service is not reachable." }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Not approved: the workflow did not answer")).toBeInTheDocument();
    expect(screen.getByText(/make worker/)).toBeInTheDocument();
    // The server's sentence says the same thing as the heading, so it is not repeated.
    expect(screen.queryByText(/not reachable/i)).not.toBeInTheDocument();
  });

  it("greys out a superseded proposal and offers it no decision", () => {
    renderWithProviders(<ProposalCard proposal={proposal({ status: "superseded" })} />);

    expect(screen.getByText("Superseded by another option")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("labels an option when it is one of several", () => {
    renderWithProviders(<ProposalCard proposal={proposal()} optionLabel="Option 2 of 3" />);
    expect(screen.getByText("Option 2 of 3")).toBeInTheDocument();
  });

  it("shows an edit's violations and holds Approve until they are dismissed", async () => {
    const user = userEvent.setup();
    // 422: the plan was re-validated, it failed, and NOTHING was stored (ADR-0007 mod 7).
    patch.mockResolvedValue(
      fail(422, {
        detail: "That plan does not validate.",
        violations: [{ rule: "rest", message: "Two sessions on the same day.", refs: [] }],
      }),
    );

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(await screen.findByRole("button", { name: /Save and re-check/ }));

    expect(await screen.findByText("Two sessions on the same day.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();

    // The proposal itself is untouched, so dismissing restores the decision.
    await user.click(screen.getByRole("button", { name: "Discard edit" }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("sends an edited date back as a move the route will accept", async () => {
    const user = userEvent.setup();
    patch.mockResolvedValue(ok({ ...proposal(), reasons: ["edited: 1 change"] }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const field = await screen.findByLabelText("New date for session 4");
    await user.clear(field);
    await user.type(field, "2026-10-22");
    await user.click(screen.getByRole("button", { name: /Save and re-check/ }));

    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [, options] = patch.mock.calls[0] as [string, { body: { changes: unknown[] } }];
    expect(options.body.changes).toHaveLength(1);
    expect(options.body.changes[0]).toMatchObject({
      action: "move",
      to_date: "2026-10-22",
      seq: 4,
      week: 2,
    });
  });

  it("offers no Edit when there is no move to edit", () => {
    const weekdays = proposal({
      kind: "weekday_change",
      payload: { changes: [], slots: { 1: 0, 2: 2 } },
    });
    renderWithProviders(<ProposalCard proposal={weekdays} />);
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("rejects with the reason that was typed", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue(ok({ ...proposal(), status: "rejected" }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.type(await screen.findByRole("textbox"), "Travelling that week");
    await user.click(screen.getByRole("button", { name: "Reject", hidden: false }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/proposals/{proposal_id}/reject",
        expect.objectContaining({ body: { reason: "Travelling that week" } }),
      );
    });
  });

  it("survives a payload it cannot read, and still offers the decision", () => {
    const odd = proposal({ payload: { changes: "not a list" }, cost: {} });
    renderWithProviders(<ProposalCard proposal={odd} />);

    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByText("Monday was not possible.")).toBeInTheDocument();
  });

  it("survives a new_block whose plan is missing the fields the table reads", async () => {
    const user = userEvent.setup();
    // `BlockPreviewTable` maps over `lifts`; a session without one used to throw
    // during render, which is a blank screen rather than a degraded card.
    const ragged = proposal({
      kind: "new_block",
      payload: { plan: [{ week: 1, slot: 1, seq: 1, date: "2026-10-19" }], changes: [] },
    });
    renderWithProviders(<ProposalCard proposal={ragged} />);

    await user.click(screen.getByRole("button", { name: /Show the whole plan/ }));
    expect(await screen.findByLabelText("Block preview")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("names the days a weekday change proposes, rather than only that days change", () => {
    const weekdays = proposal({
      kind: "weekday_change",
      payload: { changes: [], slots: { 1: 0, 2: 2, 3: 4 } },
    });
    renderWithProviders(<ProposalCard proposal={weekdays} />);

    expect(screen.getByText("Day 1 → Monday")).toBeInTheDocument();
    expect(screen.getByText("Day 2 → Wednesday")).toBeInTheDocument();
    expect(screen.getByText("Day 3 → Friday")).toBeInTheDocument();
  });

  it("tells the athlete when rejecting something already approved", async () => {
    const user = userEvent.setup();
    // The approve path reads this 409 as success; for a reject there is nothing left
    // to reject, so it must not pass silently.
    post.mockResolvedValue(fail(409, { detail: "This proposal is already approved." }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ type: "error", title: "This proposal is out of date" }),
      );
    });
  });

  it("does not call a failed edit a validation failure, and keeps Approve usable", async () => {
    const user = userEvent.setup();
    patch.mockResolvedValue(fail(500, { detail: "Something broke." }));

    renderWithProviders(<ProposalCard proposal={proposal()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(await screen.findByRole("button", { name: /Save and re-check/ }));

    expect(await screen.findByText("Could not decide this proposal")).toBeInTheDocument();
    expect(screen.queryByText(/does not validate/)).not.toBeInTheDocument();
    // Nothing was stored, so the original proposal is still approvable.
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });
});
