// Names for the generated schema -- aliases, not new shapes (spec 16 §11: no
// hand-written response types). Anything reachable here changes when the backend does.
import type { components } from "./schema";

type S = components["schemas"];

export type AthleteOut = S["AthleteOut"];
export type AthleteDetail = S["AthleteDetail"];
export type OneRepMaxOut = S["OneRepMaxOut"];
export type MeasuredOneRm = S["MeasuredOneRm"];
export type WeekdayMapOut = S["WeekdayMapOut"];
export type TemplateSummaryOut = S["TemplateSummaryOut"];
export type TemplateOut = S["TemplateOut"];
export type TemplateEntryOut = S["TemplateEntryOut"];
export type BlockOut = S["BlockOut"];
export type BlockStateOut = S["BlockStateOut"];
export type BlockCreateBody = S["BlockCreate"];
export type BlockCreated = S["BlockCreated"];
export type BlockPreviewOut = S["BlockPreviewOut"];
export type BlockDraft = S["BlockDraft"];
export type DraftSession = BlockDraft["sessions"][number];
export type DraftLift = DraftSession["lifts"][number];
export type SessionOut = S["SessionOut"];
export type SessionDetail = S["SessionDetail"];
export type SessionLogOut = S["SessionLogOut"];
export type PlannedLiftOut = S["PlannedLiftOut"];
export type WeekdaySlotOut = S["WeekdaySlotOut"];
// What a log READ gives back. Spec 20 writes with `LoggedLiftIn`, which is the same
// shape plus the bounds the logger's form promises -- so the domain model is no
// longer used in both directions and Pydantic stopped splitting it in two.
export type LoggedLift = S["LoggedLift"];
export type LoggedLiftIn = S["LoggedLiftIn"];
export type SessionLogIn = S["SessionLogIn"];
export type UnavailableIn = S["UnavailableIn"];
export type ProposalOut = S["ProposalOut"];
export type ProposalKind = ProposalOut["kind"];
export type ProposalEdit = S["ProposalEdit"];
export type ProposalReject = S["ProposalReject"];
export type SessionChange = S["SessionChange"];
export type Violation = S["Violation"];
export type LiftCode = DraftLift["lift_code"];

// Chat (spec 19). `route` is an OBJECT, not the bare string the spec's prose implies:
// `clarify` covers both "the router asked a question" and "a hard filter stopped this
// before any model saw it" (ADR-0015 mod 12), and only these fields tell them apart.
export type ChatCreate = S["ChatCreate"];
export type ChatResponse = S["ChatResponse"];
export type ChatMessageOut = S["ChatMessageOut"];
export type Route = S["Route"];
export type RouteKind = Route["kind"];

export type SimulationOut = S["SimulationOut"];
export type LiftOutlook = S["LiftOutlook"];
// Percentiles, never a single number (CLAUDE.md non-negotiable 8). Dates and kg come
// over as strings; only the counts are numbers.
export type DatePercentiles = S["DatePercentiles"];
export type KgPercentiles = S["KgPercentiles"];
export type CountPercentiles = S["CountPercentiles"];

// Decision trace (spec 21). `route` here IS the bare string the chat `Route` object
// wraps -- a decision keeps the kind in one column and `filter_fired` in another, so
// the two never arrive as one shape. `outcome` is nullable on the wire and null means
// PENDING (ADR-0015: no new enum value, no migration), so the filter type strips the
// null: a query string has no way to ask for "column is null", where an omitted
// `outcome` already means "every".
export type DecisionOut = S["DecisionOut"];
export type DecisionSource = DecisionOut["source"];
export type DecisionRoute = DecisionOut["route"];
export type DecisionOutcome = NonNullable<DecisionOut["outcome"]>;
