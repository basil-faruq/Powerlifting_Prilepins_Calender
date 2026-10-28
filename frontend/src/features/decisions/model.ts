import type { DecisionOut, DecisionOutcome } from "@/api/types";
import { formatCount, formatDate, formatDateTime, formatPct, formatSeconds } from "@/lib/format";
import { finite, flag, isRecord, strings, text } from "@/lib/json";

/**
 * Reading a decision's `tool_calls`.
 *
 * `tool_calls` is a JSONB column and `list[dict]` in Python, so the generated schema
 * types it `{ [key: string]: unknown }[]` and nothing inside it is typed at all. That
 * matters more here than anywhere else in the frontend, because the routing
 * parameters, the solver stats and the validator verdict exist ONLY in there -- the
 * `decision` table has no column for any of them. So this is the second hand-narrowed
 * API shape (after `lib/proposalPayload.ts`, ADR-0020), and it lives in the feature
 * because exactly one screen reads it.
 *
 * Every reader is written to survive a record it does not recognise. A tool added to
 * the agent after this build must still appear; a malformed entry must not take the
 * drawer down with it. Nothing here throws and nothing invents a value.
 */

// --- the envelope -----------------------------------------------------------

export interface ToolCallView {
  /**
   * The recorder's own name (`@records("plan_block")`). A free string on purpose: a
   * tool added to the agent must not make this list unreadable.
   */
  name: string;
  /**
   * Position in `tool_calls`, which is the order the calls ran in. There are no
   * per-call timestamps, so this index and `durationMs` are the whole chronology.
   */
  index: number;
  input: Record<string, unknown>;
  outputSummary: Record<string, unknown>;
  /**
   * Null when the entry carries no readable duration -- not zero. Zero is a real
   * measurement: the recorder floors a sub-millisecond call at 0.
   */
  durationMs: number | null;
  solverStats: SolverStatsView | null;
  validation: ValidatorVerdict | null;
  /**
   * Set when the tool raised. The entry is still recorded, and `outputSummary` is
   * then `{}` because the summariser never ran.
   */
  error: string | null;
}

export function readToolCalls(decision: DecisionOut): ToolCallView[] {
  return decision.tool_calls.flatMap((entry, index) => readToolCall(entry, index) ?? []);
}

/**
 * One entry, or null if it is not one.
 *
 * `name` is the only field treated as required: it is what the drawer labels the row
 * with and what every by-tool reader below matches on. An entry without one is not a
 * tool call anyone can say anything about, so it is dropped rather than shown as
 * "unnamed". `isRecord` is redundant to the compiler here -- that is the point: the
 * generated type is a promise from a generator, and a `null` in a JSONB array
 * satisfies no guard.
 */
function readToolCall(entry: unknown, index: number): ToolCallView | null {
  if (!isRecord(entry)) return null;
  const name = text(entry.name);
  if (name === null) return null;
  return {
    name,
    index,
    input: isRecord(entry.input) ? entry.input : {},
    outputSummary: isRecord(entry.output_summary) ? entry.output_summary : {},
    durationMs: finite(entry.duration_ms),
    solverStats: readSolverStats(entry.solver_stats),
    validation: readValidation(entry.validation),
    error: text(entry.error),
  };
}

// --- the routing layer ------------------------------------------------------

export interface RouterView {
  /**
   * The route as the router itself recorded it. `decision.route` is the column
   * written from the same value; the column is what the screen shows, and this is
   * kept so a disagreement is visible rather than silently resolved.
   */
  kind: string | null;
  filterFired: string | null;
  unparseable: boolean;
  clarifyReason: string | null;
  lifts: string[];
  /**
   * The parameters the router extracted, in a fixed display order.
   *
   * `RouterChoice` is dumped with `exclude_defaults=True`, so an ABSENT key means
   * "the sentence did not say" -- it is not missing data and must never be drawn as a
   * blank row. Null means the router recorded no `choice` at all, which is the normal
   * state for `safety` and `clarify`: no model was asked, or its answer was unusable.
   */
  choice: ChoiceParam[] | null;
}

export interface ChoiceParam {
  /** The wire key, e.g. `test_week_lift`. Also the React key. */
  key: string;
  label: string;
  /** Already rendered: a list joined, a flag as yes/no. */
  value: string;
}

/**
 * `RouterChoice`'s fields in the order a reader wants them -- what was chosen, then
 * what was extracted for it -- rather than the order Pydantic declares them.
 */
const CHOICE_LABELS: ReadonlyArray<readonly [key: string, label: string]> = [
  ["kind", "chose"],
  ["question", "question"],
  ["lifts", "lifts"],
  ["start_date", "start date"],
  ["weeks", "weeks"],
  ["test_week_lift", "test week lift"],
  ["test_week_number", "test week"],
  ["missed_dates", "unlogged dates"],
  ["unavailable_from", "unavailable from"],
  ["unavailable_until", "unavailable until"],
  ["injury", "injury"],
  ["scenario_kind", "scenario"],
  ["scenario_start_week", "scenario starts week"],
  ["scenario_start_date", "scenario starts"],
  ["scenario_days", "scenario days"],
];

/**
 * The routing layer, from the entry that always holds it.
 *
 * Found by NAME, not by `tool_calls[0]`. The index is the documented contract for a
 * chat turn (ADR-0015) and matching on it would be shorter -- but a workflow row has
 * no entries at all, and an entry that failed to narrow would silently promote the
 * next tool to "the route", which is the one lie this screen must not tell.
 */
export function readRouter(calls: readonly ToolCallView[]): RouterView | null {
  const router = calls.find((call) => call.name === "router");
  if (router === undefined) return null;

  const out = router.outputSummary;
  return {
    kind: text(out.kind),
    filterFired: text(out.filter_fired),
    unparseable: flag(out.unparseable),
    clarifyReason: text(out.clarify_reason),
    lifts: strings(out.lifts),
    choice: readChoice(out.choice),
  };
}

function readChoice(value: unknown): ChoiceParam[] | null {
  if (!isRecord(value)) return null;

  const params: ChoiceParam[] = [];
  const known = new Set<string>();
  for (const [key, label] of CHOICE_LABELS) {
    known.add(key);
    // `in`, not a truthiness test: the dump excludes defaults, so a key that is
    // PRESENT was set deliberately. `injury: false` can only appear if the model
    // sent it.
    if (!(key in value)) continue;
    const shown = displayValue(value[key]);
    if (shown !== null) params.push({ key, label, value: shown });
  }
  // A field added to `RouterChoice` after this list was written still appears, under
  // its own wire name. The alternative is a parameter silently vanishing from the one
  // place the parameters live.
  for (const [key, raw] of Object.entries(value)) {
    if (known.has(key)) continue;
    const shown = displayValue(raw);
    if (shown !== null) params.push({ key, label: key, value: shown });
  }
  return params;
}

/**
 * One extracted parameter as a fragment of a sentence.
 *
 * Every value `RouterChoice` can hold is a date, a count, a flag or a word. Not one
 * is a weight or an intensity, so `String` on the numbers here is safe: non-negotiable
 * 4 is about the decimal strings, and none of them reaches this function. An empty
 * list reads as nothing at all rather than as "[]" -- the router sending `lifts: []`
 * and the router not mentioning lifts mean the same thing to a reader.
 */
function displayValue(raw: unknown): string | null {
  if (typeof raw === "boolean") return raw ? "yes" : "no";
  if (typeof raw === "string") return raw === "" ? null : raw;
  if (typeof raw === "number") return Number.isFinite(raw) ? String(raw) : null;
  if (Array.isArray(raw)) {
    const parts = raw.flatMap((item) => displayValue(item) ?? []);
    return parts.length === 0 ? null : parts.join(", ");
  }
  // An object parameter has no one-line form. The entry's own JSON view has it.
  return null;
}

// --- validator and solver ---------------------------------------------------

export interface ValidatorVerdict {
  /**
   * `false` only when a RULE was violated; warnings alone leave it true. Null when
   * the entry recorded no boolean, which this reader will not guess at: "we do not
   * know whether the plan passed" and "the plan failed" are different sentences.
   */
  ok: boolean | null;
  /** Rule NAMES, already sorted by the server. */
  violationRules: string[];
  /**
   * A COUNT. Spec 21 §4B asks for "violations and warnings"; the record holds neither
   * in full -- the server keeps rule names and a number -- so the drawer says how many
   * warnings there were and does not pretend it can list them.
   */
  warningCount: number | null;
}

/**
 * Found on the `plan_block` entry's `validation`, and null in the ordinary case: the
 * server records no verdict when there were no violations AND no warnings. So "no
 * verdict" means "nothing was flagged", which is the opposite of "unknown".
 */
export function readValidation(value: unknown): ValidatorVerdict | null {
  if (!isRecord(value)) return null;
  return {
    ok: typeof value.ok === "boolean" ? value.ok : null,
    violationRules: strings(value.violation_rules),
    warningCount: finite(value.warning_count),
  };
}

export type VerdictTone = "violated" | "warned" | "unknown";

export function verdictTone(verdict: ValidatorVerdict): VerdictTone {
  if (verdict.ok === false || verdict.violationRules.length > 0) return "violated";
  if (verdict.ok === true) return "warned";
  return "unknown";
}

export interface VerdictEntry {
  from: string;
  verdict: ValidatorVerdict;
}

export function verdicts(calls: readonly ToolCallView[]): VerdictEntry[] {
  return calls.flatMap((call) =>
    call.validation === null ? [] : [{ from: call.name, verdict: call.validation }],
  );
}

/**
 * The tools that post-check a written reply. BOTH record `unsupported`, and which one
 * runs depends on the route -- verified against a live API: an `answer` turn records
 * `answer_check`, a `plan` turn records `explainer`, and neither turn records the
 * other. Reading only one name would silently empty this section for half the routes.
 */
const CHECKERS = ["answer_check", "explainer"];

/**
 * Claims the post-check could not support from the context it was given.
 *
 * Also a validator result, and the only one the `answer` route ever produces --
 * `validation` is populated by `plan_block` alone, so without this the Validator
 * section is empty for five of the six routes.
 *
 * These are the checker's own terse markers (`"num:1"` names the second number in the
 * reply), not sentences. They are shown as recorded: this screen's job is to say what
 * the check actually flagged, and the explainer's reply is discarded rather than
 * repaired (ADR-0015), so there is nothing more readable to show.
 */
export function unsupportedClaims(calls: readonly ToolCallView[]): string[] {
  const found: string[] = [];
  for (const call of calls) {
    if (!CHECKERS.includes(call.name)) continue;
    for (const claim of strings(call.outputSummary.unsupported)) {
      if (!found.includes(claim)) found.push(claim);
    }
  }
  return found;
}

export interface SolverStatsView {
  /**
   * CP-SAT's own word: OPTIMAL, FEASIBLE, INFEASIBLE. Kept verbatim -- it is a solver
   * status, not a sentence to translate.
   */
  status: string | null;
  /** The ranked cost of the option returned. Null when the solve had no objective. */
  objective: number | null;
  wallTimeS: number | null;
  numBooleans: number | null;
}

/**
 * Found on the `reschedule` entry's `solver_stats`, and null when the tier it took
 * never ran the solver -- a greedy or direct repair reports nothing, and reporting
 * zeros there would put a fabricated wall time in the record (ADR-0016).
 */
export function readSolverStats(value: unknown): SolverStatsView | null {
  if (!isRecord(value)) return null;
  return {
    status: text(value.status),
    objective: finite(value.objective),
    wallTimeS: finite(value.wall_time_s),
    numBooleans: finite(value.num_booleans),
  };
}

export interface SimulatorRunView {
  runs: number | null;
  seed: number | null;
  wallTimeS: number | null;
  /** An ISO date as the server wrote it, or null when no run finished the block. */
  finishP50: string | null;
  /** A probability, as a fraction. */
  pRestart: number | null;
  hasSyntheticOneRms: boolean;
}

/** The simulator's figures, which ride in `output_summary` rather than `solver_stats`. */
export function readSimulator(calls: readonly ToolCallView[]): SimulatorRunView | null {
  const call = calls.find((entry) => entry.name === "simulate_block");
  if (call === undefined) return null;
  const out = call.outputSummary;
  return {
    runs: finite(out.runs),
    seed: finite(out.seed),
    wallTimeS: finite(out.wall_time_s),
    finishP50: text(out.finish_p50),
    pRestart: finite(out.p_restart),
    hasSyntheticOneRms: flag(out.has_synthetic_one_rms),
  };
}

export interface StatLine {
  label: string;
  /** Already formatted. The raw figure stays visible in the entry's own JSON. */
  value: string;
  /** The recorded tool it came from, so a reader is never told CP-SAT reported a seed. */
  from: string;
}

/**
 * Every machine figure this turn recorded, wherever it was stored.
 *
 * One list, not two sections, for three reasons: at most one of CP-SAT and the
 * simulator runs in a turn, so two sections means one is always an empty heading; the
 * storage split (`solver_stats` on the envelope, the simulator's inside
 * `output_summary`) is a recorder accident rather than a distinction a reader cares
 * about; and `wall_time_s` is common to both and wants one label and one formatter.
 *
 * A tool this build has never heard of contributes nothing here -- its raw envelope is
 * still rendered by `ToolCallItem`, so a future tool with stats degrades to JSON
 * rather than to nothing.
 */
export function statLines(calls: readonly ToolCallView[]): StatLine[] {
  const lines: StatLine[] = [];

  for (const call of calls) {
    const stats = call.solverStats;
    if (stats === null) continue;
    if (stats.status !== null) {
      lines.push({ label: "Solver status", value: stats.status, from: call.name });
    }
    if (stats.objective !== null) {
      lines.push({ label: "Objective", value: formatCount(stats.objective), from: call.name });
    }
    if (stats.wallTimeS !== null) {
      lines.push({ label: "Wall time", value: formatSeconds(stats.wallTimeS), from: call.name });
    }
    if (stats.numBooleans !== null) {
      lines.push({
        label: "Boolean variables",
        value: formatCount(stats.numBooleans),
        from: call.name,
      });
    }
  }

  const sim = readSimulator(calls);
  if (sim !== null) {
    const from = "simulate_block";
    if (sim.runs !== null) lines.push({ label: "Simulated runs", value: formatCount(sim.runs), from });
    if (sim.wallTimeS !== null) {
      lines.push({ label: "Wall time", value: formatSeconds(sim.wallTimeS), from });
    }
    // The seed is what makes a run reproducible, which is the whole reason it is
    // recorded. Printed as itself, never grouped.
    if (sim.seed !== null) lines.push({ label: "Seed", value: String(sim.seed), from });
    if (sim.runs !== null || sim.finishP50 !== null) {
      lines.push({
        label: "Finishes by (p50)",
        value: sim.finishP50 === null ? "no run finished the block" : formatDate(sim.finishP50),
        from,
      });
    }
    if (sim.pRestart !== null) {
      // `formatPct` takes the fraction as a string. This one is a float probability
      // from a Monte-Carlo run, not a stored decimal, so `String` is the bridge.
      lines.push({ label: "Restarts", value: formatPct(String(sim.pRestart)), from });
    }
  }
  return lines;
}

// --- outcome ----------------------------------------------------------------

export type OutcomeTag =
  | "approved"
  | "rejected"
  | "answered"
  | "failed"
  | "pending"
  | "settled"
  | "unknown";

export interface OutcomeView {
  tag: OutcomeTag;
  /** The word on the badge. */
  label: string;
  /**
   * Chakra palette. Red is reserved for written-off and test markers (ADR-0020), so a
   * failure is orange and a rejection is gray.
   */
  palette: string;
  /** The line under it. Null when the word says it all. */
  detail: string | null;
}

const DECIDED: Record<string, OutcomeView> = {
  approved: { tag: "approved", label: "approved", palette: "green", detail: null },
  rejected: { tag: "rejected", label: "rejected", palette: "gray", detail: null },
  answered: { tag: "answered", label: "answered", palette: "blue", detail: null },
  failed: {
    tag: "failed",
    label: "failed",
    palette: "orange",
    detail: "This turn produced nothing.",
  },
};

const WAITING: OutcomeView = {
  tag: "pending",
  label: "pending",
  palette: "yellow",
  detail: "Waiting on your decision.",
};

const NOTHING_TO_DECIDE: OutcomeView = {
  tag: "settled",
  label: "no outcome",
  palette: "gray",
  detail: "This turn raised nothing to decide.",
};

/**
 * The outcome tag, including the two the column has no value for.
 *
 * A null `outcome` means pending (ADR-0015: no new enum value, no migration). But
 * "pending" is only true when something IS pending, and two unlike rows arrive null:
 *
 *   - `proposal_id` set -- options exist and the athlete has not chosen. Pending.
 *   - `proposal_id` null -- the turn ended without raising one: a `clarify` that asked
 *     a question, a `safety` reply. Nothing is waiting, and calling it "pending" would
 *     park a row in the athlete's to-do list forever.
 *
 * Indexed by string rather than by the generated union, so an outcome this build has
 * never heard of shows as itself instead of rendering `undefined`.
 */
export function outcomeView(decision: DecisionOut): OutcomeView {
  if (decision.outcome === null || decision.outcome === undefined) {
    return decision.proposal_id === null ? NOTHING_TO_DECIDE : WAITING;
  }
  return (
    DECIDED[decision.outcome] ?? {
      tag: "unknown",
      label: decision.outcome,
      palette: "gray",
      detail: "This outcome is newer than this screen.",
    }
  );
}

/** What the outcome control can be set to. "pending" is not an outcome. */
export type OutcomeFilter = DecisionOutcome | "pending";

/**
 * The part of the outcome filter the route can answer.
 *
 * A null column cannot be expressed in a query string -- an omitted `outcome` already
 * means "every" -- so "pending" is asked for as "every" and narrowed on the client by
 * `matchesOutcome`. The list says it is only looking at the newest page, so the
 * narrowing never implies completeness it does not have.
 */
export function serverOutcome(filter: OutcomeFilter | null): DecisionOutcome | null {
  return filter === null || filter === "pending" ? null : filter;
}

export function matchesOutcome(decision: DecisionOut, filter: OutcomeFilter | null): boolean {
  if (filter === null) return true;
  if (filter === "pending") return decision.outcome === null || decision.outcome === undefined;
  return decision.outcome === filter;
}

// --- proposals, titles, labels, the trace link ------------------------------

/**
 * Every proposal this decision raised, first one first.
 *
 * `decision.proposal_id` is ONE column and an in-week move offers up to three, so the
 * column holds the first and the recorded summary holds the set (ADR-0015). A plan
 * turn records its single id in its own summary too, hence the de-duplication -- which
 * keeps arrival order rather than sorting, because that is the order the options were
 * ranked in.
 */
export function proposalIds(decision: DecisionOut, calls: readonly ToolCallView[]): string[] {
  const ids: string[] = [];
  const add = (id: string | null) => {
    // `includes` over a Set: there are never more than a handful, and this keeps the
    // recorded order.
    if (id !== null && !ids.includes(id)) ids.push(id);
  };

  add(decision.proposal_id);
  for (const call of calls) {
    add(text(call.outputSummary.proposal_id));
    for (const id of strings(call.outputSummary.proposal_ids)) add(id);
  }
  return ids;
}

/**
 * The neutral wording for a decision nobody asked for in words.
 *
 * It must NOT say "missed session". `missed` is never written and means written off by
 * a human (ADR-0017), and the workflow's real cause -- a deadline passing, or an
 * absence declared -- is not recorded on the row either way, so naming one would be
 * wrong about half of them.
 */
export const WORKFLOW_TITLE = "Raised automatically by the block workflow";

const NO_MESSAGE_TITLE = "No message was recorded for this turn";

const TITLE_CHARS = 120;

/**
 * The one line that says what this decision was about: the athlete's own first line,
 * never a summary of it. This screen's job is to show what was actually said.
 */
export function decisionTitle(decision: DecisionOut): string {
  const first = (decision.message ?? "")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line !== "");

  if (first !== undefined) {
    return first.length > TITLE_CHARS ? `${first.slice(0, TITLE_CHARS)}…` : first;
  }
  // Falls back on the source, not the other way round: a workflow row that somehow
  // carries a message shows it, and a chat row whose join found nothing says so
  // rather than claiming the workflow raised it.
  return decision.source === "workflow" ? WORKFLOW_TITLE : NO_MESSAGE_TITLE;
}

/**
 * What a recorded tool is called on screen. Indexed by string, so a tool this build
 * has never heard of is labelled with its own name rather than blank.
 */
const TOOL_LABEL: Record<string, string> = {
  router: "Routing",
  get_block: "Read the block",
  search_notes: "Searched the notes",
  plan_block: "Planned a block",
  reschedule: "Rescheduled",
  simulate_block: "Simulated the block",
  log_session: "Logged a session",
  explainer: "Wrote the reply",
  answer_check: "Checked the reply",
};

export const toolLabel = (name: string): string => TOOL_LABEL[name] ?? name;

/**
 * The `trace_url` as something safe to put in an `href`.
 *
 * The backend builds it from `LANGFUSE_HOST`, so this is belt-and-braces -- but an
 * `href` fed from the database is the one injection this read-only screen could have,
 * and refusing a `javascript:` URL costs four lines.
 */
export function traceHref(url: string | null | undefined): string | null {
  if (url === null || url === undefined || url === "") return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

// --- the aggregate the drawer takes ----------------------------------------

export interface DecisionView {
  title: string;
  outcome: OutcomeView;
  /** Null for a workflow decision, and for any chat row whose router entry is gone. */
  router: RouterView | null;
  calls: ToolCallView[];
  stats: StatLine[];
  verdicts: VerdictEntry[];
  unsupported: string[];
  proposalIds: string[];
  /**
   * Summed `duration_ms`. Labelled "time in recorded tools", never "turn duration":
   * only decorated calls are timed, and the gaps between them are not.
   */
  recordedMs: number | null;
  traceHref: string | null;
  /**
   * True when the row carries no readable tool calls at all. NORMAL for a workflow
   * decision: nothing in the missed-session activity is decorated, so the recorded
   * list comes back empty and there is genuinely nothing to show.
   */
  noCalls: boolean;
}

export function decisionView(decision: DecisionOut): DecisionView {
  const calls = readToolCalls(decision);
  const timed = calls.flatMap((call) => call.durationMs ?? []);
  return {
    title: decisionTitle(decision),
    outcome: outcomeView(decision),
    router: readRouter(calls),
    calls,
    stats: statLines(calls),
    verdicts: verdicts(calls),
    unsupported: unsupportedClaims(calls),
    proposalIds: proposalIds(decision, calls),
    recordedMs: timed.length === 0 ? null : timed.reduce((total, ms) => total + ms, 0),
    traceHref: traceHref(decision.trace_url),
    noCalls: calls.length === 0,
  };
}

/**
 * The badge's props, assembled from the column and the router entry -- the four values
 * `RouteBadge` takes live in two different places on a decision.
 */
export function badgeProps(decision: DecisionOut, router: RouterView | null) {
  return {
    kind: decision.route,
    // The COLUMN wins. It is what `?route=` and `?outcome=` filter on and what the row
    // was written from; the router entry's own copy stays visible in its JSON.
    filterFired: decision.filter_fired,
    unparseable: router?.unparseable ?? false,
    clarifyReason: router?.clarifyReason ?? null,
  };
}

/** The row's accessible name: everything the row shows, as one sentence. */
export function rowLabel(decision: DecisionOut, outcome: OutcomeView): string {
  const source = decision.source === "chat" ? "from chat" : "raised automatically";
  return `${formatDateTime(decision.created_at)}, ${decision.route}, ${source}, ${outcome.label}: ${decisionTitle(decision)}`;
}
