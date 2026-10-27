import type { DraftSession, ProposalOut, SessionChange } from "@/api/types";

/**
 * Reading a proposal's `payload` and `cost`.
 *
 * Both are `{ [key: string]: unknown }` in the generated schema, on purpose: their
 * shape depends on `kind`, and a union there would generate a type that has to be
 * narrowed before it can be read at all. So this is the one place in the frontend
 * that narrows an API shape by hand (ADR-0020), and every reader goes through it.
 *
 * It is written to survive a payload it does not recognise. A proposal whose payload
 * cannot be read still has reasons, warnings and buttons that work; only the detail
 * drawn FROM the payload goes missing. Nothing here throws, and nothing here invents
 * a value -- an unreadable field reads `null`, never 0.
 */

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * An ISO date that is also a real day.
 *
 * The shape test alone is not enough: `"2026-13-99"` matches the pattern, and a date
 * that merely LOOKS right reaches `Intl.DateTimeFormat#format` as an `Invalid Date`,
 * which throws a RangeError -- taking down the card this module exists to keep
 * standing. The round-trip also rejects a day that silently rolls over.
 */
function isoDate(value: unknown): string | null {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return null;
  const parsed = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().startsWith(value) ? value : null;
}

/** A finite number, or null. `null` means "not stated", which is not the same as zero. */
function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * One entry of the payload's diff, narrowed.
 *
 * `action` is kept as the string the server sent rather than a union: a server that
 * grows a sixth action must not make this return nothing. Readers match on the
 * actions they know and ignore the rest.
 */
export interface PayloadChange {
  seq: number;
  action: string;
  from: string | null;
  to: string | null;
  intoSeq: number | null;
}

/**
 * Every change on a payload, in the order the solver emitted them.
 *
 * `calendar_solver.to_changes` emits one entry per session it actually touched, so
 * this is a diff rather than a comparison of two schedules.
 */
export function readChanges(payload: unknown): PayloadChange[] {
  if (!isRecord(payload) || !Array.isArray(payload.changes)) return [];

  const changes: PayloadChange[] = [];
  for (const change of payload.changes) {
    if (!isRecord(change)) continue;
    if (typeof change.seq !== "number" || typeof change.action !== "string") continue;
    changes.push({
      seq: change.seq,
      action: change.action,
      from: isoDate(change.from_date),
      to: isoDate(change.to_date),
      intoSeq: finite(change.into_seq),
    });
  }
  return lastPerSeq(changes);
}

/**
 * One entry per session, keeping the last.
 *
 * An edited proposal stores the solver's changes AND the hand edits appended after
 * them (`services.edit_proposal`), so a session moved twice appears twice. The server
 * resolves that by assigning into a dict keyed on `seq`, where the later entry wins;
 * this is the same rule read back. Without it a card lists one move as two and the
 * calendar draws a ghost at both destinations.
 */
function lastPerSeq<T extends { seq: number }>(changes: T[]): T[] {
  const bySeq = new Map<number, T>();
  for (const change of changes) bySeq.set(change.seq, change);

  // The latest entry, at the position of the FIRST: the hand edits are appended
  // after the solver's changes, so taking the last value in place keeps the plan's
  // own ordering instead of floating every edited session to the end of the list.
  const placed = new Set<number>();
  const collapsed: T[] = [];
  for (const change of changes) {
    if (placed.has(change.seq)) continue;
    placed.add(change.seq);
    const latest = bySeq.get(change.seq);
    if (latest !== undefined) collapsed.push(latest);
  }
  return collapsed;
}

/**
 * The changes as the API's own `SessionChange`, for sending back in a PATCH.
 *
 * `PATCH /proposals/{id}` rebuilds the whole plan from the changes it is given, so an
 * edit has to return them as they arrived, with only the dates altered. Entries that
 * do not carry the `week` and `slot` the route requires are dropped rather than
 * guessed at, and a session edited twice is sent once.
 */
export function readSessionChanges(payload: unknown): SessionChange[] {
  if (!isRecord(payload) || !Array.isArray(payload.changes)) return [];

  const changes: SessionChange[] = [];
  for (const change of payload.changes) {
    if (!isRecord(change)) continue;
    const { seq, week, slot, action } = change;
    if (typeof seq !== "number" || typeof week !== "number") continue;
    if (typeof slot !== "number" || typeof action !== "string") continue;
    changes.push(change as unknown as SessionChange);
  }
  return lastPerSeq(changes);
}

/**
 * The schedule a proposal would produce, when it carries one.
 *
 * A `new_block` payload holds the whole plan, which is what `BlockPreviewTable` draws.
 * A reschedule carries only its diff, so this is empty for one and that is correct.
 *
 * Every field a consumer dereferences is checked here, not just the date. `week`,
 * `slot` and `seq` are read as numbers and `lifts` is always an array afterwards:
 * `BlockPreviewTable` maps over it unguarded, so a session missing one would be a
 * blank screen rather than a degraded card.
 */
export function readPlan(payload: unknown): DraftSession[] {
  if (!isRecord(payload) || !Array.isArray(payload.plan)) return [];

  const plan: DraftSession[] = [];
  for (const session of payload.plan) {
    if (!isRecord(session)) continue;
    const { week, slot, seq } = session;
    if (typeof week !== "number" || typeof slot !== "number" || typeof seq !== "number") continue;
    const date = isoDate(session.date);
    if (date === null) continue;
    plan.push({ ...session, week, slot, seq, date, lifts: readLifts(session.lifts) });
  }
  return plan;
}

/**
 * The lifts of one planned session.
 *
 * A lift is kept only if it carries the code and the strings the UI prints. The
 * weights stay exactly as they arrived -- they are decimal strings and nothing here
 * parses one (CLAUDE.md non-negotiable 4).
 */
function readLifts(value: unknown): DraftSession["lifts"] {
  if (!Array.isArray(value)) return [];
  return value.filter((lift): lift is DraftSession["lifts"][number] => {
    if (!isRecord(lift)) return false;
    return typeof lift.lift_code === "string" && typeof lift.weight_kg === "string";
  });
}

/**
 * What a proposal costs, for display and nothing else.
 *
 * The solver ranks options by `total`; these are the parts it is made of (ADR-0006).
 * They are floats and stay floats -- no weight is computed here, so the exact-
 * arithmetic rule does not apply.
 */
export interface ProposalCost {
  delayDays: number | null;
  offWeekdayCount: number | null;
  endSlipDays: number | null;
}

export function readCost(cost: unknown): ProposalCost {
  if (!isRecord(cost)) return { delayDays: null, offWeekdayCount: null, endSlipDays: null };
  return {
    delayDays: finite(cost.delay_days),
    offWeekdayCount: finite(cost.off_weekday_count),
    endSlipDays: finite(cost.end_slip_days),
  };
}

/**
 * Whether a `new_block` is a restart rather than a first block or a rollover.
 *
 * A restart supersedes the block it replaces and plans against a REDUCED max
 * (ADR-0010 mod 6), so it must not be shown as ordinary new work.
 */
export function isRestart(proposal: ProposalOut): boolean {
  return isRecord(proposal.payload) && typeof proposal.payload.supersedes_block_id === "string";
}

/** The block a restart would replace, for a card that has to say what it supersedes. */
export function supersededBlockId(payload: unknown): string | null {
  if (!isRecord(payload)) return null;
  return typeof payload.supersedes_block_id === "string" ? payload.supersedes_block_id : null;
}

/**
 * The weekdays a `weekday_change` proposes, as slot → weekday (0 = Monday).
 *
 * Returns null when the payload carries no slot map at all, which is how a caller
 * tells "no change proposed" from "every slot moved to Monday".
 */
export function readSlots(payload: unknown): Map<number, number> | null {
  if (!isRecord(payload) || !isRecord(payload.slots)) return null;

  const slots = new Map<number, number>();
  for (const [slot, weekday] of Object.entries(payload.slots)) {
    const slotNumber = Number(slot);
    if (!Number.isInteger(slotNumber) || typeof weekday !== "number") continue;
    if (!Number.isInteger(weekday) || weekday < 0 || weekday > 6) continue;
    slots.set(slotNumber, weekday);
  }
  return slots.size > 0 ? slots : null;
}
