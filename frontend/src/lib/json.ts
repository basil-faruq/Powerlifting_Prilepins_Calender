/**
 * Reading values out of JSON the generated schema types only as `unknown`.
 *
 * The primitives the single-narrowing-point rule is built from (ADR-0020, extended by
 * ADR-0021): a `payload`, a `cost` and a decision's `tool_calls` are all
 * `{ [key: string]: unknown }` on purpose, because their shape depends on a sibling
 * field. These guards are what let one module narrow each of them and every reader go
 * through it.
 *
 * Nothing here throws and nothing here invents a value: an unreadable field reads
 * `null`, never 0 and never "". `extra="forbid"` upstream makes the shapes
 * predictable; it does not make them guaranteed, because a row written by an older
 * build is still in the table.
 */

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A finite number, or null. `null` means "not stated", which is not the same as zero. */
export function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A non-empty string, or null. An empty string carries no more than an absent one. */
export function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** Strictly `true`. A missing flag is false, not "maybe". */
export function flag(value: unknown): boolean {
  return value === true;
}

/** The string members of an array, in order. Anything else is an empty list. */
export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
