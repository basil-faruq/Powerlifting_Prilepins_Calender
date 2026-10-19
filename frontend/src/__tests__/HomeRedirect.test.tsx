import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/renderWithProviders";
import { HomeRedirect } from "../router";

const context = vi.hoisted(() => ({ currentBlockId: null as string | null }));

vi.mock("@/components/AthleteProvider", () => ({
  useAthleteContext: () => ({
    athlete: null,
    athleteId: null,
    currentBlock: null,
    currentBlockId: context.currentBlockId,
  }),
  AthleteProvider: ({ children }: { children: unknown }) => children,
}));

function renderHome() {
  renderWithProviders(
    <Routes>
      <Route path="/" element={<HomeRedirect />} />
      <Route path="/setup" element={<p>setup page</p>} />
      <Route path="/calendar" element={<p>calendar page</p>} />
    </Routes>,
  );
}

describe("HomeRedirect", () => {
  it("sends a fresh install, with no active block, to setup", () => {
    context.currentBlockId = null;
    renderHome();
    expect(screen.getByText("setup page")).toBeInTheDocument();
  });

  it("sends an athlete with an active block to the calendar", () => {
    context.currentBlockId = "block-1";
    renderHome();
    expect(screen.getByText("calendar page")).toBeInTheDocument();
  });
});
