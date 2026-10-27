/**
 * Exact arithmetic on the decimal strings the backend sends.
 *
 * Weights, intensities and plate increments are kept exact end to end and must
 * never pass through `Number()` or `parseFloat` (CLAUDE.md non-negotiable 4): a
 * 2.5 kg increment is not representable in binary floating point, so
 * `117.5 % 2.5` is not 0 and a perfectly loadable bar would be called invalid.
 *
 * Everything here scales to a fixed number of decimal places and works in
 * `BigInt`, so no float ever exists. `lib/format.ts` does the same for display;
 * this is the arithmetic half. Anything that is not a plain decimal returns
 * `null` rather than a number that would be wrong.
 */

/** Four places: more than a kilogram plate, an RPE or an intensity ever needs. */
const DP = 4;
const SCALE = 10n ** BigInt(DP);

/** An optionally signed plain decimal. No exponents: the backend never sends one. */
const DECIMAL = /^-?\d+(\.\d+)?$/;

/**
 * A decimal string as an integer at four places: `"87.5"` → `875000n`.
 *
 * Further places are truncated rather than rounded, which is safe here because
 * nothing the backend sends carries more than four.
 */
export function scaled(decimal: string): bigint | null {
  if (!DECIMAL.test(decimal)) return null;
  const negative = decimal.startsWith("-");
  const [whole = "0", fraction = ""] = (negative ? decimal.slice(1) : decimal).split(".");
  const value = BigInt(whole + fraction.slice(0, DP).padEnd(DP, "0"));
  return negative ? -value : value;
}

/** A scaled integer back to a canonical decimal string: `875000n` → `"87.5"`. */
export function unscaled(value: bigint): string {
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const whole = absolute / SCALE;
  const fraction = (absolute % SCALE).toString().padStart(DP, "0").replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${fraction ? `.${fraction}` : ""}`;
}

/**
 * `a` against `b`: negative, zero or positive, as a comparator wants. `null`
 * when either side cannot be read, so a caller can tell "equal" from "unknown".
 */
export function compareDecimal(a: string, b: string): number | null {
  const left = scaled(a);
  const right = scaled(b);
  if (left === null || right === null) return null;
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/** `a - b`, exactly. Used for "what changed since the last block". */
export function subDecimal(a: string, b: string): string | null {
  const left = scaled(a);
  const right = scaled(b);
  if (left === null || right === null) return null;
  return unscaled(left - right);
}

/**
 * Whether a weight sits on a step the bar can actually be loaded to.
 *
 * The athlete's `plate_increment_kg` is the step (ADR-0008), and it is never
 * assumed: a gym with microplates sets 1.25 and this follows. A step of zero or
 * less has no multiples, so it reads `null` rather than dividing by it.
 */
export function isMultipleOf(value: string, step: string): boolean | null {
  const amount = scaled(value);
  const increment = scaled(step);
  if (amount === null || increment === null || increment <= 0n) return null;
  return amount % increment === 0n;
}
