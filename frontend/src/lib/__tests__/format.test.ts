import { describe, expect, it } from "vitest";
import { formatDate, formatDateTime, formatIntensity, formatKg, formatPct } from "../format";

// Intl output can carry non-breaking spaces and varies by ICU build, so compare
// after normalising rather than against byte-exact literals.
const plain = (text: string) => text.replace(/\s/g, " ");

describe("formatKg", () => {
  it("formats the decimal string it is given, without a float in between", () => {
    expect(plain(formatKg("93.00"))).toBe("93 kg");
    expect(plain(formatKg("92.50"))).toBe("92.5 kg");
  });

  it("keeps digits a float could not", () => {
    // 9007199254740993 is 2^53 + 1: `Number()` would hand back ...992.
    expect(plain(formatKg("9007199254740993"))).toContain("9,007,199,254,740,993");
  });

  it("shows something that is not a decimal as received, not as NaN", () => {
    expect(formatKg("n/a")).toBe("n/a");
  });
});

describe("formatIntensity", () => {
  it("shows the authored pair, unreduced", () => {
    expect(formatIntensity("70.00", "120.00")).toBe("70/120");
    expect(formatIntensity("112.5", "105")).toBe("112.5/105");
  });
});

describe("formatPct", () => {
  it("turns a fraction into a percentage", () => {
    expect(plain(formatPct("0.5833"))).toBe("58.3%");
  });
});

describe("formatDate", () => {
  it("names the same calendar day it was given", () => {
    expect(formatDate("2026-10-19")).toContain("19");
    expect(formatDate("2026-10-19")).toContain("Oct");
    expect(formatDate("2026-10-19")).toContain("Mon");
  });

  it("does not slide a date across a month boundary", () => {
    expect(formatDate("2026-11-01")).toContain("Nov");
    expect(formatDate("2026-10-31")).toContain("Oct");
  });
});

describe("formatDateTime", () => {
  it("reads a UTC timestamp in Europe/Berlin", () => {
    // 22:30Z on 31 Oct is 23:30 in Berlin (CET, UTC+1, after the clocks go back on 25 Oct).
    expect(formatDateTime("2026-10-31T22:30:00Z")).toContain("23:30");
  });
});
