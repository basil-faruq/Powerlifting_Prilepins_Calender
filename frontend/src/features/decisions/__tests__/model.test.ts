import { describe, expect, it } from "vitest";
import type { DecisionOut } from "@/api/types";
import {
  badgeProps,
  decisionTitle,
  decisionView,
  matchesOutcome,
  outcomeView,
  proposalIds,
  readRouter,
  readToolCalls,
  serverOutcome,
  statLines,
  toolLabel,
  traceHref,
  unsupportedClaims,
  verdictTone,
  verdicts,
  WORKFLOW_TITLE,
} from "../model";

/**
 * The narrowing layer, tested without a DOM.
 *
 * `tool_calls` is a JSONB column with no generated type inside it, so every reader is
 * fed what an older build, a future build and a corrupt row could put there. These are
 * the assertions that keep "the drawer survives a record it does not recognise" true.
 */

const decision = (over: Partial<DecisionOut> = {}): DecisionOut => ({
  id: "d1",
  run_id: "chat:c1",
  source: "chat",
  route: "plan",
  filter_fired: null,
  outcome: "answered",
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

describe("readToolCalls", () => {
  it("drops an entry that is not a record, and one with no name", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          null as unknown as Record<string, unknown>,
          "nope" as unknown as Record<string, unknown>,
          { duration_ms: 5 },
          call({ name: "router" }),
        ],
      }),
    );
    expect(calls).toHaveLength(1);
    expect(calls[0]?.name).toBe("router");
  });

  it("keeps the order the calls ran in, as the index", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "router" }), call({ name: "plan_block" })] }),
    );
    expect(calls.map((c) => [c.name, c.index])).toEqual([
      ["router", 0],
      ["plan_block", 1],
    ]);
  });

  it("reads a zero duration as zero, and an unreadable one as null", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({ name: "a", duration_ms: 0 }),
          call({ name: "b", duration_ms: "420" }),
          call({ name: "c", duration_ms: null }),
        ],
      }),
    );
    // Zero is a real measurement -- a sub-millisecond call. Only an unreadable
    // figure is null, because "not stated" is not the same as "took no time".
    expect(calls.map((c) => c.durationMs)).toEqual([0, null, null]);
  });

  it("falls back to empty objects when input or output is not a record", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "x", input: "oops", output_summary: 7 })] }),
    );
    expect(calls[0]?.input).toEqual({});
    expect(calls[0]?.outputSummary).toEqual({});
  });

  it("keeps the error a tool raised", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "x", error: "IntegrityError: duplicate key" })] }),
    );
    expect(calls[0]?.error).toBe("IntegrityError: duplicate key");
  });
});

describe("readRouter", () => {
  it("finds the router by name, not by position", () => {
    // A workflow row has no entries at all, and a failed narrowing would otherwise
    // promote the next tool to "the route".
    const calls = readToolCalls(
      decision({
        tool_calls: [call({ name: "get_block" }), call({ name: "router", output_summary: { kind: "plan" } })],
      }),
    );
    expect(readRouter(calls)?.kind).toBe("plan");
  });

  it("is null when nothing routed", () => {
    expect(readRouter(readToolCalls(decision({ tool_calls: [] })))).toBeNull();
  });

  it("reads a fired filter and the clarify reason", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({
            name: "router",
            output_summary: { kind: "clarify", filter_fired: "no_one_rms", clarify_reason: "no lifts" },
          }),
        ],
      }),
    );
    const router = readRouter(calls);
    expect(router?.filterFired).toBe("no_one_rms");
    expect(router?.clarifyReason).toBe("no lifts");
  });

  // --- `exclude_defaults=True`: an ABSENT key is the default, not missing data ----

  it("has a null choice when the router recorded none", () => {
    const calls = readToolCalls(decision({ tool_calls: [call({ name: "router", output_summary: { kind: "safety" } })] }));
    expect(readRouter(calls)?.choice).toBeNull();
  });

  it("lists only the parameters that are present", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({ name: "router", output_summary: { choice: { kind: "plan", start_date: "2026-11-02", weeks: 8 } } }),
        ],
      }),
    );
    const choice = readRouter(calls)?.choice ?? [];
    expect(choice.map((p) => p.key)).toEqual(["kind", "start_date", "weeks"]);
    // Nothing invents a row for a key the sentence never set.
    expect(choice.find((p) => p.key === "test_week_lift")).toBeUndefined();
  });

  it("has an empty choice list when the router extracted nothing", () => {
    const calls = readToolCalls(decision({ tool_calls: [call({ name: "router", output_summary: { choice: {} } })] }));
    expect(readRouter(calls)?.choice).toEqual([]);
  });

  it("shows a parameter this build has never heard of, under its own name", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "router", output_summary: { choice: { deload_weeks: 2 } } })] }),
    );
    const choice = readRouter(calls)?.choice ?? [];
    expect(choice).toEqual([{ key: "deload_weeks", label: "deload_weeks", value: "2" }]);
  });

  it("renders a flag as a word and a list as a joined line, and drops an empty list", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({ name: "router", output_summary: { choice: { injury: false, lifts: ["squat", "bench"] } } }),
        ],
      }),
    );
    const choice = readRouter(calls)?.choice ?? [];
    expect(choice.find((p) => p.key === "lifts")?.value).toBe("squat, bench");
    expect(choice.find((p) => p.key === "injury")?.value).toBe("no");

    const empty = readToolCalls(
      decision({ tool_calls: [call({ name: "router", output_summary: { choice: { lifts: [] } } })] }),
    );
    expect(readRouter(empty)?.choice).toEqual([]);
  });
});

describe("the validator verdict", () => {
  it("is absent when nothing was flagged", () => {
    const calls = readToolCalls(decision({ tool_calls: [call({ name: "plan_block" })] }));
    expect(verdicts(calls)).toEqual([]);
  });

  it("reads the rules it named and the warning count", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({
            name: "plan_block",
            validation: { ok: false, violation_rules: ["no_two_sessions_same_day"], warning_count: 2 },
          }),
        ],
      }),
    );
    const entry = verdicts(calls)[0];
    expect(entry?.from).toBe("plan_block");
    expect(entry?.verdict.violationRules).toEqual(["no_two_sessions_same_day"]);
    expect(entry?.verdict.warningCount).toBe(2);
    expect(verdictTone(entry!.verdict)).toBe("violated");
  });

  it("calls warnings alone a pass with warnings", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "plan_block", validation: { ok: true, warning_count: 3 } })] }),
    );
    expect(verdictTone(verdicts(calls)[0]!.verdict)).toBe("warned");
  });

  it("will not guess when no boolean was recorded", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "plan_block", validation: { warning_count: 1 } })] }),
    );
    // "We do not know whether it passed" is not "it failed".
    expect(verdicts(calls)[0]!.verdict.ok).toBeNull();
    expect(verdictTone(verdicts(calls)[0]!.verdict)).toBe("unknown");
  });

  it("counts the answer checker's unsupported claims as a validator result", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "answer_check", output_summary: { unsupported: ["num:1"] } })] }),
    );
    expect(unsupportedClaims(calls)).toEqual(["num:1"]);
  });

  // Verified against a live API: an `answer` turn records `answer_check` and a `plan`
  // turn records `explainer`. Reading only one name empties this for half the routes.
  it("reads the claims off the explainer too, which is what a plan turn records", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "explainer", output_summary: { unsupported: ["num:2"] } })] }),
    );
    expect(unsupportedClaims(calls)).toEqual(["num:2"]);
  });

  it("merges both checkers without repeating a claim", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({ name: "explainer", output_summary: { unsupported: ["num:1"] } }),
          call({ name: "answer_check", output_summary: { unsupported: ["num:1", "num:3"] } }),
        ],
      }),
    );
    expect(unsupportedClaims(calls)).toEqual(["num:1", "num:3"]);
  });

  // The real shape of a clean plan: 49 warnings, no violated rule (CLAUDE.md #5 --
  // Prilepin ranges and the volume trend are warnings, so the author's own plan passes).
  it("calls a plan with warnings but no violated rule a pass", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({ name: "plan_block", validation: { ok: true, warning_count: 49, violation_rules: [] } }),
        ],
      }),
    );
    expect(verdictTone(verdicts(calls)[0]!.verdict)).toBe("warned");
  });
});

describe("statLines", () => {
  it("reads CP-SAT's figures off the envelope and names the tool", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({
            name: "reschedule",
            solver_stats: { status: "OPTIMAL", objective: 3, wall_time_s: 0.4217, num_booleans: 180 },
          }),
        ],
      }),
    );
    const lines = statLines(calls);
    expect(lines.map((l) => [l.label, l.value])).toEqual([
      ["Solver status", "OPTIMAL"],
      ["Objective", "3"],
      ["Wall time", "0.42 s"],
      ["Boolean variables", "180"],
    ]);
    expect(lines.every((l) => l.from === "reschedule")).toBe(true);
  });

  it("reads the simulator's figures out of its output summary instead", () => {
    const calls = readToolCalls(
      decision({
        tool_calls: [
          call({
            name: "simulate_block",
            output_summary: { runs: 200, seed: 7, wall_time_s: 1.5, finish_p50: "2027-01-18", p_restart: 0.12 },
          }),
        ],
      }),
    );
    const labels = statLines(calls).map((l) => l.label);
    expect(labels).toContain("Simulated runs");
    expect(labels).toContain("Seed");
    expect(labels).toContain("Finishes by (p50)");
    expect(labels).toContain("Restarts");
  });

  it("says plainly that no run finished the block", () => {
    const calls = readToolCalls(
      decision({ tool_calls: [call({ name: "simulate_block", output_summary: { runs: 200, finish_p50: null } })] }),
    );
    const p50 = statLines(calls).find((l) => l.label === "Finishes by (p50)");
    expect(p50?.value).toBe("no run finished the block");
  });

  it("reports nothing for a tier that never ran the solver", () => {
    // Reporting zeros would put a fabricated wall time in the record (ADR-0016).
    const calls = readToolCalls(decision({ tool_calls: [call({ name: "reschedule", solver_stats: null })] }));
    expect(statLines(calls)).toEqual([]);
  });

  it("reports nothing for a tool it has never heard of", () => {
    const calls = readToolCalls(decision({ tool_calls: [call({ name: "teleport", output_summary: { runs: 9 } })] }));
    expect(statLines(calls)).toEqual([]);
  });
});

describe("outcomeView", () => {
  it("names each decided outcome", () => {
    expect(outcomeView(decision({ outcome: "approved" })).tag).toBe("approved");
    expect(outcomeView(decision({ outcome: "rejected" })).tag).toBe("rejected");
    expect(outcomeView(decision({ outcome: "answered" })).tag).toBe("answered");
    expect(outcomeView(decision({ outcome: "failed" })).tag).toBe("failed");
  });

  it("calls a null outcome pending only while a proposal waits", () => {
    const waiting = outcomeView(decision({ outcome: null, proposal_id: "p1" }));
    expect(waiting.tag).toBe("pending");
    expect(waiting.detail).toMatch(/waiting on your decision/i);
  });

  it("says a turn that raised nothing has no outcome, rather than calling it pending", () => {
    const settled = outcomeView(decision({ outcome: null, proposal_id: null }));
    expect(settled.tag).toBe("settled");
    expect(settled.detail).toMatch(/raised nothing to decide/i);
  });

  it("shows an outcome newer than this screen as itself", () => {
    const future = outcomeView(decision({ outcome: "escalated" as DecisionOut["outcome"] }));
    expect(future.tag).toBe("unknown");
    expect(future.label).toBe("escalated");
  });

  it("never paints an outcome red, which is reserved", () => {
    // Red marks a written-off session and a test single (ADR-0020).
    const tags: DecisionOut["outcome"][] = ["approved", "rejected", "answered", "failed", null];
    for (const outcome of tags) {
      expect(outcomeView(decision({ outcome })).palette).not.toBe("red");
    }
  });
});

describe("the outcome filter", () => {
  it("asks the route for nothing when the filter is pending", () => {
    // A query string cannot ask for a null column, and an omitted `outcome` already
    // means "every".
    expect(serverOutcome("pending")).toBeNull();
    expect(serverOutcome(null)).toBeNull();
    expect(serverOutcome("approved")).toBe("approved");
  });

  it("narrows pending on the client", () => {
    expect(matchesOutcome(decision({ outcome: null }), "pending")).toBe(true);
    expect(matchesOutcome(decision({ outcome: "approved" }), "pending")).toBe(false);
    expect(matchesOutcome(decision({ outcome: "approved" }), null)).toBe(true);
  });
});

describe("proposalIds", () => {
  it("lists every option, not just the one on the column", () => {
    const d = decision({
      proposal_id: "p1",
      tool_calls: [call({ name: "reschedule", output_summary: { proposal_ids: ["p1", "p2", "p3"] } })],
    });
    expect(proposalIds(d, readToolCalls(d))).toEqual(["p1", "p2", "p3"]);
  });

  it("keeps the ranked order and de-duplicates", () => {
    const d = decision({
      proposal_id: "p9",
      tool_calls: [call({ name: "plan_block", output_summary: { proposal_id: "p9" } })],
    });
    expect(proposalIds(d, readToolCalls(d))).toEqual(["p9"]);
  });

  it("is empty when the turn raised nothing", () => {
    const d = decision({ proposal_id: null });
    expect(proposalIds(d, readToolCalls(d))).toEqual([]);
  });
});

describe("decisionTitle", () => {
  it("shows the athlete's own first line", () => {
    expect(decisionTitle(decision({ message: "move Wednesday\nplease" }))).toBe("move Wednesday");
  });

  it("skips leading blank lines", () => {
    expect(decisionTitle(decision({ message: "\n\n  plan me a block  " }))).toBe("plan me a block");
  });

  it("clips a very long line", () => {
    const title = decisionTitle(decision({ message: "x".repeat(400) }));
    expect(title).toHaveLength(121);
    expect(title.endsWith("…")).toBe(true);
  });

  it("gives a workflow decision neutral wording, never 'missed'", () => {
    // `missed` means written off by a human and is never written (ADR-0017).
    const title = decisionTitle(decision({ source: "workflow", message: null }));
    expect(title).toBe(WORKFLOW_TITLE);
    expect(title).not.toMatch(/missed/i);
  });

  it("says a chat turn lost its message rather than blaming the workflow", () => {
    expect(decisionTitle(decision({ source: "chat", message: null }))).toMatch(/no message was recorded/i);
  });
});

describe("traceHref", () => {
  it("passes an http or https trace through", () => {
    const url = "https://cloud.langfuse.com/project/p1/traces/t1";
    expect(traceHref(url)).toBe(url);
  });

  it("refuses anything that is not a web URL", () => {
    expect(traceHref("javascript:alert(1)")).toBeNull();
    expect(traceHref("/project/p1/traces/t1")).toBeNull();
    expect(traceHref("not a url")).toBeNull();
  });

  it("is null when tracing recorded nothing", () => {
    expect(traceHref(null)).toBeNull();
    expect(traceHref(undefined)).toBeNull();
    expect(traceHref("")).toBeNull();
  });
});

describe("decisionView", () => {
  it("sums only the calls that were timed, and says when none were", () => {
    const timed = decisionView(
      decision({
        tool_calls: [call({ name: "router", duration_ms: 12 }), call({ name: "plan_block", duration_ms: 430 })],
      }),
    );
    expect(timed.recordedMs).toBe(442);
    expect(decisionView(decision({ tool_calls: [] })).recordedMs).toBeNull();
  });

  it("flags a decision with no readable calls, which is normal for the workflow", () => {
    const view = decisionView(decision({ source: "workflow", route: "reschedule", tool_calls: [] }));
    expect(view.noCalls).toBe(true);
    expect(view.router).toBeNull();
    expect(view.stats).toEqual([]);
  });
});

describe("badgeProps", () => {
  it("takes the filter from the column and the clarify detail from the router call", () => {
    const d = decision({
      route: "clarify",
      filter_fired: "no_weekday_map",
      tool_calls: [call({ name: "router", output_summary: { unparseable: true, clarify_reason: "unclear" } })],
    });
    const props = badgeProps(d, readRouter(readToolCalls(d)));
    expect(props).toEqual({
      kind: "clarify",
      filterFired: "no_weekday_map",
      unparseable: true,
      clarifyReason: "unclear",
    });
  });
});

describe("toolLabel", () => {
  it("names the tools it knows and passes through the ones it does not", () => {
    expect(toolLabel("plan_block")).toBe("Planned a block");
    expect(toolLabel("teleport")).toBe("teleport");
  });
});
