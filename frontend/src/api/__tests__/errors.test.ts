import { describe, expect, it } from "vitest";
import { ApiError, errorMessage, isNetworkError, parseProblem } from "../errors";

describe("parseProblem", () => {
  it("reads the API's own ProblemOut", () => {
    const problem = parseProblem(
      {
        detail: "proposal is already approved",
        violations: [{ rule: "r", message: "m", refs: ["seq:1"] }],
        warnings: [],
        problems: ["a", "b"],
      },
      409,
    );
    expect(problem.detail).toBe("proposal is already approved");
    expect(problem.violations).toHaveLength(1);
    expect(problem.problems).toEqual(["a", "b"]);
  });

  it("reads FastAPI's own validation 422, where detail is an array", () => {
    const problem = parseProblem(
      { detail: [{ loc: ["body", "start_date"], msg: "Input should be a valid date", type: "x" }] },
      422,
    );
    expect(problem.detail).toBe("start_date: Input should be a valid date");
    expect(problem.problems).toEqual(["start_date: Input should be a valid date"]);
  });

  it("falls back to the status for a body it does not recognise", () => {
    expect(parseProblem("<html>bad gateway</html>", 502).detail).toBe("HTTP 502");
    expect(parseProblem(null, 500).detail).toBe("HTTP 500");
  });
});

describe("errorMessage", () => {
  it("uses the server's own words for an ApiError", () => {
    const error = new ApiError(503, parseProblem({ detail: "temporal is down" }, 503));
    expect(errorMessage(error)).toBe("temporal is down");
  });

  it("recognises a fetch that never got an answer", () => {
    const error = new TypeError("Failed to fetch");
    expect(isNetworkError(error)).toBe(true);
    expect(errorMessage(error)).toMatch(/could not be reached/);
  });

  it("does not mistake an ApiError for a connection failure", () => {
    expect(isNetworkError(new ApiError(500, parseProblem({ detail: "x" }, 500)))).toBe(false);
  });
});
