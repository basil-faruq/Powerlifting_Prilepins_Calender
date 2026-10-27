import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ChatInput } from "../ChatInput";
import { SUGGESTED_PROMPTS } from "../model";

function renderInput(over: Partial<Parameters<typeof ChatInput>[0]> = {}) {
  const onSend = vi.fn();
  renderWithProviders(<ChatInput pending={false} empty={false} onSend={onSend} {...over} />);
  // Chakra takes a loading button's label out of the accessibility tree, so Send is
  // found by its own text rather than by its accessible name.
  const send = screen.getByText("Send").closest("button");
  if (send === null) throw new Error("no send button rendered");
  return { onSend, box: screen.getByLabelText("Message"), send };
}

describe("sending", () => {
  it("sends on Enter and empties the box, so the next message starts clean", async () => {
    const { onSend, box } = renderInput();
    await userEvent.type(box, "what's on this week?{Enter}");
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("what's on this week?");
    expect(box).toHaveValue("");
  });

  it("sends what the button is for too", async () => {
    const { onSend, box, send } = renderInput();
    await userEvent.type(box, "hello");
    await userEvent.click(send);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("trims the message rather than sending the athlete's stray spaces", async () => {
    const { onSend, box } = renderInput();
    await userEvent.type(box, "   I can't train tomorrow   {Enter}");
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("I can't train tomorrow");
  });

  it("takes Shift+Enter as a new line and sends nothing", async () => {
    const { onSend, box } = renderInput();
    await userEvent.type(box, "first{Shift>}{Enter}{/Shift}second");
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("first\nsecond");
  });
});

describe("what cannot be sent", () => {
  it("will not send an empty box", () => {
    const { send } = renderInput();
    expect(send).toBeDisabled();
  });

  it("will not send spaces alone", async () => {
    const { onSend, box, send } = renderInput();
    await userEvent.type(box, "    ");
    expect(send).toBeDisabled();
    await userEvent.type(box, "{Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("closes the box while an answer is in flight", () => {
    const { box, send } = renderInput({ pending: true });
    expect(box).toBeDisabled();
    expect(send).toBeDisabled();
  });

});

describe("the suggested prompts", () => {
  it("are offered before anything has been said", () => {
    renderInput({ empty: true });
    for (const prompt of SUGGESTED_PROMPTS) {
      expect(screen.getByRole("button", { name: prompt })).toBeInTheDocument();
    }
  });

  it("take up no room once the conversation has started", () => {
    renderInput({ empty: false });
    for (const prompt of SUGGESTED_PROMPTS) {
      expect(screen.queryByRole("button", { name: prompt })).not.toBeInTheDocument();
    }
  });

  it("send exactly the prompt that was clicked", async () => {
    const { onSend } = renderInput({ empty: true });
    await userEvent.click(screen.getByRole("button", { name: SUGGESTED_PROMPTS[1] }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith(SUGGESTED_PROMPTS[1]);
  });

  it("are not clickable while an answer is in flight", () => {
    renderInput({ empty: true, pending: true });
    expect(screen.getByRole("button", { name: SUGGESTED_PROMPTS[0] })).toBeDisabled();
  });
});
