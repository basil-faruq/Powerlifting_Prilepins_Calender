import { describe, expect, it, vi } from "vitest";
import { waitForDecision } from "../approval";

// Tiny intervals: these are about the order of events, not about real waiting.
const fast = { intervalMs: 1, timeoutMs: 200 };

describe("waitForDecision", () => {
  it("returns at once when the proposal is already approved", async () => {
    const read = vi.fn().mockResolvedValue("approved");
    expect(await waitForDecision(read, fast)).toBe("approved");
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("keeps asking while the workflow has not committed yet, then succeeds", async () => {
    // A first approval answers 200 before the plan lands: pending, pending, approved.
    const read = vi.fn().mockResolvedValueOnce("pending").mockResolvedValueOnce("pending").mockResolvedValue("approved");
    expect(await waitForDecision(read, fast)).toBe("approved");
    expect(read).toHaveBeenCalledTimes(3);
  });

  it("gives up as pending when nothing ever commits it (no worker running)", async () => {
    const read = vi.fn().mockResolvedValue("pending");
    expect(await waitForDecision(read, { intervalMs: 5, timeoutMs: 40 })).toBe("pending");
    expect(read.mock.calls.length).toBeGreaterThan(1);
  });

  it("does not call a rejected or superseded proposal a success", async () => {
    expect(await waitForDecision(() => Promise.resolve("rejected"), fast)).toBe("other");
    expect(await waitForDecision(() => Promise.resolve("superseded"), fast)).toBe("other");
  });

  it("lets a failed read surface rather than swallowing it", async () => {
    const read = vi.fn().mockRejectedValue(new Error("backend down"));
    await expect(waitForDecision(read, fast)).rejects.toThrow("backend down");
  });
});
