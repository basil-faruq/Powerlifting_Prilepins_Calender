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
export type ProposalOut = S["ProposalOut"];
export type Violation = S["Violation"];
export type LiftCode = DraftLift["lift_code"];
