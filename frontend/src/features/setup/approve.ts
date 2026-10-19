import { ApiError, errorMessage, isNetworkError } from "@/api/errors";

export type ApproveOutcome =
  /** The block started, though this call did not hear it: carry on to the calendar. */
  | { kind: "started" }
  /** Temporal or its worker could not be reached. Nothing was lost; try again. */
  | { kind: "unavailable"; message: string }
  /**
   * The server answered 200 but the plan was not committed within our patience. A first
   * approval only *starts* the workflow; its first act is the commit, and without a
   * worker nothing performs it. Nothing is lost: the start is idempotent, and the block
   * begins by itself once a worker is running.
   */
  | { kind: "pending" }
  /** The plan no longer validates. Violations name the sessions. */
  | { kind: "invalid"; message: string; lines: string[] }
  /** The proposal moved on under us (stale, or decided the other way). */
  | { kind: "stale"; message: string }
  | { kind: "other"; message: string };

/**
 * What an approve that failed means for the wizard (ADR-0017). Approving a first
 * block starts a Temporal workflow, so it can fail in ways the old direct commit
 * could not -- and one of those failures is not a failure at all:
 *
 * a 409 "already approved" means the FIRST attempt committed and only its answer was
 * lost. The block is running; the wizard is done and must not call that an error.
 */
export function classifyApproveError(error: unknown): ApproveOutcome {
  if (isNetworkError(error)) {
    return { kind: "unavailable", message: "The backend could not be reached." };
  }
  if (!(error instanceof ApiError)) return { kind: "other", message: errorMessage(error) };

  const { status, problem } = error;
  if (status === 503) return { kind: "unavailable", message: problem.detail };
  if (status === 409) {
    return /already approved/i.test(problem.detail)
      ? { kind: "started" }
      : { kind: "stale", message: problem.detail };
  }
  if (status === 422) {
    const lines = problem.violations.length > 0 ? problem.violations.map((v) => v.message) : problem.problems;
    return { kind: "invalid", message: problem.detail, lines };
  }
  return { kind: "other", message: problem.detail };
}

/** What became of a proposal we asked to have approved. */
export type Decision = "approved" | "pending" | "other";

/**
 * Wait for a proposal to be decided.
 *
 * A 200 from approve is NOT proof of approval. A first approval starts the block's
 * workflow and answers at once, so the response can still say `pending` -- briefly
 * when a worker is running, indefinitely when none is. Only `approved` is success.
 */
export async function waitForDecision(
  read: () => Promise<string>,
  { intervalMs = 400, timeoutMs = 8000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<Decision> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const status = await read();
    if (status === "approved") return "approved";
    if (status !== "pending") return "other";
    if (Date.now() >= deadline) return "pending";
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
