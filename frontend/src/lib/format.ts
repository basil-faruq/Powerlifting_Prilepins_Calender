// Weights and intensities reach the browser as decimal STRINGS ("93.00"), because
// the backend keeps them exact. They must never pass through `Number()`/`parseFloat`:
// `Intl.NumberFormat#format` takes a string and formats its exact value, so no float
// ever exists. Do not "simplify" any of these to `format(Number(v))`.

const LOCALE = "en-GB";
const TIME_ZONE = "Europe/Berlin";

const plain = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 0, maximumFractionDigits: 4 });
const kg = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const pct = new Intl.NumberFormat(LOCALE, { style: "percent", maximumFractionDigits: 1 });
const seconds = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 });

const date = new Intl.DateTimeFormat(LOCALE, {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: TIME_ZONE,
});
const dateTime = new Intl.DateTimeFormat(LOCALE, {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: TIME_ZONE,
});

const dayMonth = new Intl.DateTimeFormat(LOCALE, {
  day: "numeric",
  month: "short",
  timeZone: TIME_ZONE,
});

const DECIMAL = /^-?\d+(\.\d+)?$/;

/**
 * `Intl.NumberFormat#format` takes a decimal string and formats its exact value,
 * but TypeScript only admits the numeric-literal type for it, not a plain string.
 * This is the one place that gap is bridged, and only for something that really is
 * a decimal; anything else is shown as received rather than as "NaN".
 */
function exact(value: string): `${number}` | null {
  return DECIMAL.test(value) ? (value as `${number}`) : null;
}

/** `"93.00"` → `"93 kg"`, `"92.5"` → `"92.5 kg"`. */
export function formatKg(value: string): string {
  const n = exact(value);
  return n === null ? value : `${kg.format(n)} kg`;
}

/** The authored intensity pair as written: `("70", "120")` → `"70/120"` (ADR-0003 rule 5). */
export function formatIntensity(num: string, den: string): string {
  const n = exact(num);
  const d = exact(den);
  return n === null || d === null ? `${num}/${den}` : `${plain.format(n)}/${plain.format(d)}`;
}

/** A fraction string as a percentage: `"0.5833"` → `"58.3%"`. Display only. */
export function formatPct(fraction: string): string {
  const n = exact(fraction);
  return n === null ? fraction : pct.format(n);
}

/**
 * An ISO `YYYY-MM-DD` as a calendar day. Pinned to noon UTC so no zone, however
 * far from UTC, can slide it onto the neighbouring day before it is formatted.
 */
export function formatDate(iso: string): string {
  return date.format(new Date(`${iso}T12:00:00Z`));
}

/**
 * An ISO `YYYY-MM-DD` as just the day and month: `"2026-10-21"` → `"21 Oct"`. The
 * calendar's columns already name the weekday and its rows the week, so a cell has
 * room for nothing more.
 */
export function formatDayMonth(iso: string): string {
  return dayMonth.format(new Date(`${iso}T12:00:00Z`));
}

/** An ISO timestamp as a date and time in Europe/Berlin. */
export function formatDateTime(iso: string): string {
  return dateTime.format(new Date(iso));
}

/**
 * Machine timings, which are the one family of figures in this app that really are
 * floats: a recorded `duration_ms` int and a solver's own `wall_time_s`. The warning
 * at the top of this file is about the decimal STRINGS a weight or an intensity
 * arrives as, and none of them reaches these three -- so they take `number`, and need
 * no trip through `exact`.
 */
export function formatDuration(ms: number): string {
  return ms < 1000 ? `${plain.format(ms)} ms` : `${seconds.format(ms / 1000)} s`;
}

/** A solver's or the simulator's own wall time: `0.4217` -> `"0.42 s"`. */
export function formatSeconds(value: number): string {
  return `${seconds.format(value)} s`;
}

/** A plain count with the locale's grouping: `12480` -> `"12,480"`. */
export function formatCount(value: number): string {
  return plain.format(value);
}
