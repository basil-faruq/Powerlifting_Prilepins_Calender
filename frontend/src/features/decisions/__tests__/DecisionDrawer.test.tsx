import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { DecisionOut, ProposalOut } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { DecisionDrawer } from "../DecisionDrawer";

/**
 * Spec 21 §14: the drawer shows routing, tool calls, stats, the validator result and
 * the proposal outcome, with a Langfuse link when tracing is on.
 *
 * No API client mock is needed: the drawer takes plain props, because the list already
 * returns the full shape and the only fetch belongs to the page. Every assertion is
 * scoped with `within(dialog)` -- the drawer renders in a `Portal` beside the page, and
 * a figure can appear both in a stat line and in an expanded JSON view.
 */

const decision = (over: Partial<DecisionOut> = {}): DecisionOut => ({
  id: "d1",
  run_id: "chat:c1",
  source: "chat",
  route: "plan",
  filter_fired: null,
  outcome: "approved",
  proposal_id: null,
  created_at: "2026-10-21T17:04:00+00:00",
  athlete_id: "a1",
  message: "plan me eight weeks",
  tool_calls: [],
  trace_url: null,
  ...over,
});

const call = (over: Record<string, unknown> & { name: string }): Record<string, unknown> => ({
  input: {},
  output_summary: {},
  duration_ms: 0,
  solver_stats: null,
  validation: null,
  error: null,
  ...over,
});

const open = (over: Partial<DecisionOut> = {}, proposal: ProposalOut | null = null) =>
  renderWithProviders(
    <DecisionDrawer decision={decision(over)} isLoading={false} proposal={proposal} onClose={() => undefined} />,
  );

const body = async () => within(await screen.findByRole("dialog"));

describe("DecisionDrawer", () => {
  it("renders nothing when no decision is open", () => {
    renderWithProviders(
      <DecisionDrawer decision={null} isLoading={false} proposal={null} onClose={() => undefined} />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the question that was asked, in full", async () => {
    open({ message: "move Wednesday\nI am away that evening" });
    const dialog = await body();
    expect(dialog.getByText(/I am away that evening/)).toBeInTheDocument();
  });

  // --- routing ---------------------------------------------------------------

  it("names the filter that fired and says no model was asked", async () => {
    open({
      route: "safety",
      filter_fired: "pain_keyword",
      tool_calls: [call({ name: "router", output_summary: { kind: "safety", filter_fired: "pain_keyword" } })],
    });
    const dialog = await body();
    expect(dialog.getByText(/check fired, so no model was asked/)).toBeInTheDocument();
    expect(dialog.getAllByText("pain_keyword").length).toBeGreaterThan(0);
    // A safety route runs no LLM tool calls (spec 21 §12).
    expect(dialog.queryByText(/extracted no parameters/)).not.toBeInTheDocument();
  });

  it("lists the parameters the router extracted, and invents none it did not", async () => {
    open({
      tool_calls: [
        call({
          name: "router",
          output_summary: { choice: { kind: "plan", start_date: "2026-11-02", weeks: 8 } },
        }),
      ],
    });
    const dialog = await body();
    expect(dialog.getByText("start date")).toBeInTheDocument();
    expect(dialog.getByText("2026-11-02")).toBeInTheDocument();
    expect(dialog.getByText("weeks")).toBeInTheDocument();
    // `exclude_defaults=True`: an absent key is the default, not missing data.
    expect(dialog.queryByText("test week lift")).not.toBeInTheDocument();
  });

  it("says no model was asked rather than showing an empty parameter list", async () => {
    open({ tool_calls: [call({ name: "router", output_summary: { kind: "safety" } })] });
    const dialog = await body();
    expect(dialog.getByText(/no model was asked, so there are no extracted parameters/i)).toBeInTheDocument();
  });

  // --- tool calls ------------------------------------------------------------

  it("lists the tool calls in the order they ran, with how long each took", async () => {
    open({
      tool_calls: [
        call({ name: "router", duration_ms: 12 }),
        call({ name: "plan_block", duration_ms: 1200 }),
        call({ name: "explainer", duration_ms: 420 }),
      ],
    });
    const dialog = await body();
    const triggers = dialog.getAllByRole("button", { expanded: false });
    const labels = triggers.map((t) => t.textContent ?? "");
    expect(labels[0]).toContain("router");
    expect(labels[1]).toContain("plan_block");
    expect(labels[2]).toContain("explainer");
    expect(dialog.getByText("1.2 s")).toBeInTheDocument();
    expect(dialog.getByText("420 ms")).toBeInTheDocument();
  });

  it("keeps a tool call's input and output shut until it is opened", async () => {
    const user = userEvent.setup();
    open({
      tool_calls: [call({ name: "plan_block", output_summary: { session_count: 33 }, duration_ms: 5 })],
    });
    const dialog = await body();
    // Collapsed by default (§11), and unmounted rather than merely hidden.
    expect(dialog.queryByText("session_count")).not.toBeInTheDocument();

    await user.click(dialog.getByRole("button", { name: /plan_block/ }));
    expect(await dialog.findByText("session_count")).toBeInTheDocument();
  });

  it("shows a tool that raised as having raised, with no output to show", async () => {
    const user = userEvent.setup();
    open({ tool_calls: [call({ name: "log_session", error: "IntegrityError: duplicate key", duration_ms: 3 })] });
    const dialog = await body();
    expect(dialog.getByText("raised")).toBeInTheDocument();

    await user.click(dialog.getByRole("button", { name: /log_session/ }));
    expect(await dialog.findByText(/IntegrityError: duplicate key/)).toBeInTheDocument();
    expect(dialog.getByText(/raised before producing a summary/i)).toBeInTheDocument();
  });

  it("survives a tool it has never heard of", async () => {
    const user = userEvent.setup();
    open({ tool_calls: [call({ name: "teleport", output_summary: { nested: { deeper: [1, 2] } }, duration_ms: 3 })] });
    const dialog = await body();
    const trigger = dialog.getByRole("button", { name: /teleport/ });
    await user.click(trigger);
    expect(await dialog.findByText("nested")).toBeInTheDocument();
  });

  it("survives a malformed tool_calls array", async () => {
    open({
      route: "clarify",
      tool_calls: [
        null as unknown as Record<string, unknown>,
        "nope" as unknown as Record<string, unknown>,
        { duration_ms: 5 },
        call({ name: "router", output_summary: { kind: "clarify", choice: { question: "which lift?" } } }),
      ],
    });
    const dialog = await body();
    // The router is still found, and the three bad entries are simply gone.
    expect(dialog.getByText("which lift?")).toBeInTheDocument();
    expect(dialog.getAllByRole("button", { expanded: false })).toHaveLength(1);
  });

  // --- stats -----------------------------------------------------------------

  it("shows the solver's status, objective and wall time, attributed to its tool", async () => {
    open({
      route: "reschedule",
      tool_calls: [
        call({
          name: "reschedule",
          solver_stats: { status: "OPTIMAL", objective: 3, wall_time_s: 0.4217, num_booleans: 180 },
        }),
      ],
    });
    const dialog = await body();
    expect(dialog.getByText("Solver status")).toBeInTheDocument();
    expect(dialog.getByText("OPTIMAL")).toBeInTheDocument();
    expect(dialog.getByText("0.42 s")).toBeInTheDocument();
    expect(dialog.getByText("180")).toBeInTheDocument();
  });

  it("shows the simulator's figures in the same section", async () => {
    // They live in `output_summary`, not `solver_stats` -- this is what proves the
    // section reads both places.
    open({
      route: "simulate",
      tool_calls: [
        call({
          name: "simulate_block",
          output_summary: { runs: 200, seed: 7, wall_time_s: 1.5, finish_p50: "2027-01-18", p_restart: 0.12 },
        }),
      ],
    });
    const dialog = await body();
    expect(dialog.getByText("Simulated runs")).toBeInTheDocument();
    expect(dialog.getByText("200")).toBeInTheDocument();
    expect(dialog.getByText("Seed")).toBeInTheDocument();
    expect(dialog.getByText("Restarts")).toBeInTheDocument();
  });

  // --- validator -------------------------------------------------------------

  it("reports the validator by the rules it named, and counts the warnings", async () => {
    open({
      tool_calls: [
        call({
          name: "plan_block",
          validation: { ok: false, violation_rules: ["no_two_sessions_same_day"], warning_count: 2 },
        }),
      ],
    });
    const dialog = await body();
    expect(dialog.getByText("rules violated")).toBeInTheDocument();
    expect(dialog.getByText("no_two_sessions_same_day")).toBeInTheDocument();
    expect(dialog.getByText("2 warnings")).toBeInTheDocument();
    // The record holds rule names and a count, and the drawer says so.
    expect(dialog.getByText(/not their text/)).toBeInTheDocument();
  });

  it("shows the reply check's unsupported claims as a validator result", async () => {
    // The markers are the checker's own (`num:1` names a number in the reply), and a
    // plan turn records them on `explainer` rather than `answer_check`.
    open({
      route: "answer",
      tool_calls: [call({ name: "answer_check", output_summary: { unsupported: ["num:1"] } })],
    });
    const dialog = await body();
    expect(dialog.getByText(/could not support 1 claim/)).toBeInTheDocument();
    expect(dialog.getByText("num:1")).toBeInTheDocument();
  });

  it("shows them for a plan turn too, which records them on the explainer", async () => {
    open({ tool_calls: [call({ name: "explainer", output_summary: { unsupported: ["num:2"] } })] });
    const dialog = await body();
    expect(dialog.getByText("num:2")).toBeInTheDocument();
  });

  it("says nothing was flagged when the validator recorded nothing", async () => {
    open({ tool_calls: [call({ name: "plan_block" })] });
    const dialog = await body();
    expect(dialog.getByText(/nothing was flagged for this decision/i)).toBeInTheDocument();
  });

  // --- proposal and outcome --------------------------------------------------

  it("lists every proposal the turn raised, not just the one on the column", async () => {
    open({
      route: "reschedule",
      proposal_id: "p1",
      tool_calls: [call({ name: "reschedule", output_summary: { proposal_ids: ["p1", "p2", "p3"] } })],
    });
    const dialog = await body();
    expect(dialog.getByText("p1")).toBeInTheDocument();
    expect(dialog.getByText("p2")).toBeInTheDocument();
    expect(dialog.getByText("p3")).toBeInTheDocument();
  });

  it("calls it pending while a proposal waits, and offers Chat to decide it", async () => {
    open({ outcome: null, proposal_id: "p1" });
    const dialog = await body();
    expect(dialog.getAllByText("pending").length).toBeGreaterThan(0);
    expect(dialog.getByText(/waiting on your decision/i)).toBeInTheDocument();
    expect(dialog.getByRole("link", { name: /decide it in chat/i })).toHaveAttribute(
      "href",
      "/chat?proposal=p1",
    );
  });

  it("says nothing is waiting when the turn raised nothing to decide", async () => {
    open({ outcome: null, proposal_id: null });
    const dialog = await body();
    expect(dialog.getByText(/raised nothing to decide/i)).toBeInTheDocument();
    expect(dialog.queryByText(/waiting on your decision/i)).not.toBeInTheDocument();
    expect(dialog.queryByRole("link", { name: /decide it in chat/i })).not.toBeInTheDocument();
  });

  it("never offers to approve or reject: this screen decides nothing", async () => {
    // One write path only (ADR-0007), and spec 21 §11 makes this read-only.
    open({ outcome: null, proposal_id: "p1" });
    const dialog = await body();
    expect(dialog.queryByRole("button", { name: /^approve$/i })).not.toBeInTheDocument();
    expect(dialog.queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
  });

  // --- the Langfuse link -----------------------------------------------------

  it("offers the Langfuse trace in a new tab when tracing was on", async () => {
    const url = "https://cloud.langfuse.com/project/p1/traces/t1";
    open({ trace_url: url });
    const dialog = await body();
    const link = dialog.getByRole("link", { name: /open in langfuse/i });
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("refuses a trace url that is not a web address", async () => {
    open({ trace_url: "javascript:alert(1)" });
    const dialog = await body();
    expect(dialog.queryByRole("link", { name: /open in langfuse/i })).not.toBeInTheDocument();
    expect(dialog.getByText("Tracing disabled")).toBeInTheDocument();
  });

  it("does not claim tracing is off for certain when there is simply no trace", async () => {
    // A null `trace_url` has three causes and the API distinguishes none of them
    // (ADR-0016), so the second line states what is true of all three.
    open({ trace_url: null });
    const dialog = await body();
    expect(dialog.getByText("Tracing disabled")).toBeInTheDocument();
    expect(dialog.getByText(/may have been off when it ran/i)).toBeInTheDocument();
  });

  // --- the workflow row ------------------------------------------------------

  it("degrades to the bare record for a workflow decision, and never says 'missed'", async () => {
    open({
      source: "workflow",
      route: "reschedule",
      message: null,
      athlete_id: null,
      filter_fired: null,
      outcome: null,
      proposal_id: "p1",
      tool_calls: [],
    });
    const dialog = await body();
    expect(dialog.getByText(/raised automatically by the block workflow/i)).toBeInTheDocument();
    expect(dialog.getByText(/no tool calls were recorded/i)).toBeInTheDocument();
    expect(dialog.getByText(/does not record its steps individually/i)).toBeInTheDocument();
    expect(dialog.getByText(/no routing was recorded/i)).toBeInTheDocument();
    // `missed` means written off by a human, and is never written (ADR-0017).
    expect(dialog.queryByText(/missed/i)).not.toBeInTheDocument();
  });

  it("shows the run id, which is how the same turn is found in the worker logs", async () => {
    open({ run_id: "chat:conv-7" });
    const dialog = await body();
    expect(dialog.getByText("chat:conv-7")).toBeInTheDocument();
  });
});
