import { afterEach, describe, expect, it, vi } from "vitest";
import { clearConversationId, loadConversationId, saveConversationId } from "../conversationStore";

/**
 * The conversation id is a convenience: a reload continues the same thread. A browser
 * that refuses storage -- a private window, blocked site data -- must cost the athlete
 * a new thread and nothing else, so none of these three may ever throw.
 */

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("the remembered conversation", () => {
  it("gives back what was saved for that athlete", () => {
    saveConversationId("athlete-1", "conv-abc");
    expect(loadConversationId("athlete-1")).toBe("conv-abc");
  });

  it("is null before anything has been saved", () => {
    expect(loadConversationId("athlete-1")).toBeNull();
  });

  it("keeps two athletes in separate threads", () => {
    saveConversationId("athlete-1", "conv-one");
    saveConversationId("athlete-2", "conv-two");
    expect(loadConversationId("athlete-1")).toBe("conv-one");
    expect(loadConversationId("athlete-2")).toBe("conv-two");
  });

  it("forgets only the athlete it was asked to forget", () => {
    saveConversationId("athlete-1", "conv-one");
    saveConversationId("athlete-2", "conv-two");
    clearConversationId("athlete-1");
    expect(loadConversationId("athlete-1")).toBeNull();
    expect(loadConversationId("athlete-2")).toBe("conv-two");
  });

  it("replaces the thread rather than keeping both", () => {
    saveConversationId("athlete-1", "conv-one");
    saveConversationId("athlete-1", "conv-two");
    expect(loadConversationId("athlete-1")).toBe("conv-two");
  });
});

describe("a localStorage that refuses", () => {
  it("reads as no remembered thread instead of throwing", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(loadConversationId("athlete-1")).toBeNull();
  });

  it("swallows a write it cannot make", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(() => saveConversationId("athlete-1", "conv-abc")).not.toThrow();
  });

  it("swallows a removal it cannot make", () => {
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(() => clearConversationId("athlete-1")).not.toThrow();
  });

  it("works again once storage comes back", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(loadConversationId("athlete-1")).toBeNull();
    getItem.mockRestore();
    saveConversationId("athlete-1", "conv-abc");
    expect(loadConversationId("athlete-1")).toBe("conv-abc");
  });
});
