import { describe, expect, it } from "vitest";
import { compareDecimal, isMultipleOf, scaled, subDecimal, unscaled } from "../decimal";

/**
 * This module exists so that a kilogram never passes through a float. So most of
 * what is below is a case binary floating point gets wrong: `0.3 - 0.1`, and a
 * 117.5 kg bar that `%` says is not a multiple of 2.5. Everything else is the
 * other half of the contract -- anything that is not a plain decimal reads as
 * `null` rather than as a number that would be wrong.
 */

/** `scaled`, for the cases where a null would be a test failure, not a result. */
function mustScale(decimal: string): bigint {
  const value = scaled(decimal);
  if (value === null) throw new Error(`not a decimal: ${decimal}`);
  return value;
}

describe("scaled", () => {
  it("turns a decimal string into an integer at four places", () => {
    expect(scaled("87.5")).toBe(875000n);
    expect(scaled("2.5")).toBe(25000n);
  });

  it("reads a whole number as itself rather than as the wrong magnitude", () => {
    expect(scaled("123")).toBe(1230000n);
    expect(scaled("0")).toBe(0n);
  });

  it("keeps the sign, because a delta between two blocks can go down", () => {
    expect(scaled("-6.5")).toBe(-65000n);
    expect(scaled("-123")).toBe(-1230000n);
  });

  it("truncates beyond four places instead of rounding, which would invent precision", () => {
    expect(scaled("1.23456")).toBe(12345n);
    expect(scaled("0.99999")).toBe(9999n);
  });

  it("refuses anything that is not a plain decimal", () => {
    for (const bad of ["", "abc", "1.2.3", "1e3", "NaN", " 1"]) {
      expect(scaled(bad)).toBeNull();
    }
  });
});

describe("unscaled", () => {
  it("round-trips with scaled", () => {
    for (const decimal of ["87.5", "123.5", "0.25", "97.5", "-6.5"]) {
      expect(unscaled(mustScale(decimal))).toBe(decimal);
    }
  });

  it("trims the zeros the backend pads its weights with", () => {
    expect(unscaled(mustScale("5.00"))).toBe("5");
    expect(unscaled(mustScale("142.50"))).toBe("142.5");
  });

  it("keeps a negative negative", () => {
    expect(unscaled(-65000n)).toBe("-6.5");
    expect(unscaled(-1230000n)).toBe("-123");
  });

  it("renders zero as zero, not as an empty string or a signed one", () => {
    expect(unscaled(0n)).toBe("0");
  });
});

describe("compareDecimal", () => {
  it("orders two weights", () => {
    expect(compareDecimal("97.5", "123.5")).toBe(-1);
    expect(compareDecimal("123.5", "97.5")).toBe(1);
    expect(compareDecimal("123.5", "123.5")).toBe(0);
  });

  it("calls two spellings of the same weight equal", () => {
    // The backend sends "87.50"; the athlete types "87.5". They are one weight.
    expect(compareDecimal("87.50", "87.5")).toBe(0);
    expect(compareDecimal("87.5", "87.50")).toBe(0);
  });

  it("says 'unknown' rather than 'equal' when either side cannot be read", () => {
    const unreadable = compareDecimal("eighty seven", "87.5");
    expect(unreadable).toBeNull();
    // A caller branching on `=== 0` must not mistake one for the other: `null`
    // means the comparison never happened.
    expect(unreadable).not.toBe(0);
    expect(compareDecimal("87.5", "")).toBeNull();
  });
});

describe("subDecimal", () => {
  it("subtracts exactly, where a float would not", () => {
    // `0.3 - 0.1` is 0.19999999999999998 as a float.
    expect(subDecimal("0.3", "0.1")).toBe("0.2");
  });

  it("gives the change between two planning maxes", () => {
    expect(subDecimal("128", "123.5")).toBe("4.5");
    expect(subDecimal("142.5", "97.5")).toBe("45");
  });

  it("goes negative when a block was planned at reduced numbers", () => {
    expect(subDecimal("117", "123.5")).toBe("-6.5");
    expect(subDecimal("-2.5", "4")).toBe("-6.5");
  });

  it("is zero, not nothing, when a max did not move", () => {
    expect(subDecimal("123.50", "123.5")).toBe("0");
  });

  it("reads an unreadable side as no answer at all", () => {
    expect(subDecimal("1e3", "123.5")).toBeNull();
    expect(subDecimal("123.5", "about 120")).toBeNull();
  });
});

describe("isMultipleOf", () => {
  it("accepts a bar that can actually be loaded, which `%` on floats does not", () => {
    // `117.5 % 2.5` is 2.4999999999999996 in binary floating point: the whole
    // reason this module exists.
    expect(isMultipleOf("117.5", "2.5")).toBe(true);
    expect(isMultipleOf("100", "5")).toBe(true);
    expect(isMultipleOf("123.75", "1.25")).toBe(true);
  });

  it("rejects a weight between two steps", () => {
    expect(isMultipleOf("118", "2.5")).toBe(false);
    expect(isMultipleOf("97.5", "5")).toBe(false);
  });

  it("has no answer for a step that has no multiples", () => {
    expect(isMultipleOf("117.5", "0")).toBeNull();
    expect(isMultipleOf("117.5", "-2.5")).toBeNull();
    expect(isMultipleOf("117.5", "two and a half")).toBeNull();
  });

  it("has no answer for a weight it cannot read", () => {
    expect(isMultipleOf("", "2.5")).toBeNull();
  });
});
