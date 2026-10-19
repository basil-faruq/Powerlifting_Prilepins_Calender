import { describe, expect, it } from "vitest";
import { ApiError, parseProblem } from "@/api/errors";
import { classifyApproveError } from "../approve";

const failure = (status: number, body: unknown) => new ApiError(status, parseProblem(body, status));

describe("classifyApproveError", () => {
  it("treats 'already approved' as a block that started, not as an error", () => {
    // The first attempt committed and only its answer was lost (ADR-0017).
    expect(classifyApproveError(failure(409, { detail: "proposal is already approved" }))).toEqual({
      kind: "started",
    });
  });

  it("treats any other 409 as a proposal that moved on", () => {
    expect(classifyApproveError(failure(409, { detail: "proposal is already rejected" })).kind).toBe("stale");
    expect(classifyApproveError(failure(409, { detail: "the block changed since" })).kind).toBe("stale");
  });

  it("reads a 503 as the workflow service being unavailable", () => {
    const outcome = classifyApproveError(failure(503, { detail: "not reachable at localhost:7233" }));
    expect(outcome).toEqual({ kind: "unavailable", message: "not reachable at localhost:7233" });
  });

  it("reads a fetch that never got an answer as unavailable too", () => {
    expect(classifyApproveError(new TypeError("Failed to fetch")).kind).toBe("unavailable");
  });

  it("lists a 422's violations by their messages", () => {
    const outcome = classifyApproveError(
      failure(422, {
        detail: "x; y",
        violations: [
          { rule: "rest_gap", message: "squat W2 S1: too close to W2 S3", refs: ["seq:4"] },
          { rule: "off_weekday", message: "squat W3 S1: not on its weekday", refs: [] },
        ],
      }),
    );
    expect(outcome).toMatchObject({
      kind: "invalid",
      lines: ["squat W2 S1: too close to W2 S3", "squat W3 S1: not on its weekday"],
    });
  });

  it("falls back to the problems list when a 422 has no violations", () => {
    const outcome = classifyApproveError(failure(422, { detail: "bad", problems: ["a", "b"] }));
    expect(outcome).toMatchObject({ kind: "invalid", lines: ["a", "b"] });
  });

  it("keeps the server's words for anything else", () => {
    expect(classifyApproveError(failure(500, { detail: "boom" }))).toEqual({ kind: "other", message: "boom" });
    expect(classifyApproveError(new Error("odd"))).toEqual({ kind: "other", message: "odd" });
  });
});
