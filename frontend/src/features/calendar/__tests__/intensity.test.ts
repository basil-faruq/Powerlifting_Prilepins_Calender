import { describe, expect, it } from "vitest";
import { intensityStep, scaled, shadeBg, shadeFg, topStep } from "../intensity";

describe("scaled", () => {
  it("turns a four-decimal string into an integer, so nothing is ever a float", () => {
    expect(scaled("0.7812")).toBe(7812n);
    expect(scaled("1.0500")).toBe(10500n);
  });

  it("pads a short fraction rather than reading it as the wrong magnitude", () => {
    expect(scaled("0.7")).toBe(7000n);
    expect(scaled("0.75")).toBe(7500n);
  });

  it("reads a whole number as itself", () => {
    expect(scaled("1")).toBe(10000n);
    expect(scaled("0")).toBe(0n);
  });

  it("truncates beyond four places instead of rounding, which would invent precision", () => {
    expect(scaled("0.999999")).toBe(9999n);
  });

  it("refuses anything that is not a plain decimal", () => {
    for (const bad of ["", "abc", "1e3", "-0.5", "1.2.3", "NaN", " 0.8"]) {
      expect(scaled(bad)).toBeNull();
    }
  });
});

describe("intensityStep", () => {
  it("puts a submaximal set below the first Prilepin band in step 1", () => {
    expect(intensityStep("0.5833")).toBe(1);
    expect(intensityStep("0.6999")).toBe(1);
  });

  it("steps up exactly on each band edge, not just past it", () => {
    expect(intensityStep("0.7000")).toBe(2);
    expect(intensityStep("0.8000")).toBe(3);
    expect(intensityStep("0.9000")).toBe(4);
    expect(intensityStep("1.0000")).toBe(5);
  });

  it("holds a step until the next edge", () => {
    expect(intensityStep("0.7999")).toBe(2);
    expect(intensityStep("0.8999")).toBe(3);
    expect(intensityStep("0.9999")).toBe(4);
  });

  it("keeps overreaching in the top step rather than off the end of the scale", () => {
    expect(intensityStep("1.0500")).toBe(5);
    expect(intensityStep("1.1000")).toBe(5);
    expect(intensityStep("1.3000")).toBe(5);
  });

  it("reads something unreadable as no step at all", () => {
    expect(intensityStep("")).toBeNull();
  });
});

describe("topStep", () => {
  it("shades a session by its hardest lift", () => {
    const lifts = [{ rel_intensity: "0.6500" }, { rel_intensity: "0.9200" }, { rel_intensity: "0.7100" }];
    expect(topStep(lifts)).toBe(4);
  });

  it("ignores a lift it cannot read instead of losing the session's shade", () => {
    expect(topStep([{ rel_intensity: "oops" }, { rel_intensity: "0.8100" }])).toBe(3);
  });

  it("has no shade for a session with no lifts", () => {
    expect(topStep([])).toBeNull();
  });
});

describe("shade tokens", () => {
  it("names a theme token per step, so the scale lives in theme.ts", () => {
    expect(shadeBg(3)).toBe("intensity.3.bg");
    expect(shadeFg(3)).toBe("intensity.3.fg");
  });

  it("falls back to the plain surface when there is nothing to shade", () => {
    expect(shadeBg(null)).toBe("bg.subtle");
    expect(shadeFg(null)).toBe("fg");
  });
});
