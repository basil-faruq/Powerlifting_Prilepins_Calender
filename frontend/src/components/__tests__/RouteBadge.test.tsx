import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RouteKind } from "@/api/types";
import { RouteBadge } from "@/components/RouteBadge";
import { renderWithProviders } from "@/test/renderWithProviders";

describe("RouteBadge", () => {
  it("names the tool that answered", () => {
    const kinds: RouteKind[] = ["answer", "plan", "reschedule", "simulate", "safety"];
    for (const kind of kinds) {
      const { unmount } = renderWithProviders(<RouteBadge kind={kind} />);
      expect(screen.getByText(kind)).toBeInTheDocument();
      unmount();
    }
  });

  // --- the two unlike things `clarify` covers (ADR-0015 mod 12) ---------------

  it("says a hard filter stopped the message, naming the check that fired", () => {
    renderWithProviders(<RouteBadge kind="clarify" filterFired="pain" />);
    expect(screen.getByText(/stopped by the pain check/)).toBeInTheDocument();
  });

  it("says a message it could not read, rather than blaming a filter", () => {
    renderWithProviders(<RouteBadge kind="clarify" unparseable />);
    expect(screen.getByText(/could not be read/)).toBeInTheDocument();
    expect(screen.queryByText(/check/)).not.toBeInTheDocument();
  });

  it("shows the router's own reason when it has one", () => {
    renderWithProviders(<RouteBadge kind="clarify" clarifyReason="which lift did you mean?" />);
    expect(screen.getByText(/which lift did you mean\?/)).toBeInTheDocument();
  });

  it("prefers the fired filter over the router's guess at why", () => {
    renderWithProviders(
      <RouteBadge kind="clarify" filterFired="injury" unparseable clarifyReason="could not tell" />,
    );
    expect(screen.getByText(/stopped by the injury check/)).toBeInTheDocument();
    expect(screen.queryByText(/could not tell/)).not.toBeInTheDocument();
  });

  it("says just 'clarify' when none of the three explains itself", () => {
    renderWithProviders(<RouteBadge kind="clarify" />);
    expect(screen.getByText("clarify")).toBeInTheDocument();
  });

  it("carries the detail in a tooltip as well as in the text", () => {
    renderWithProviders(<RouteBadge kind="clarify" filterFired="pain" />);
    expect(screen.getByTitle("stopped by the pain check")).toBeInTheDocument();
  });

  it("gives a badge with nothing to explain no tooltip at all", () => {
    renderWithProviders(<RouteBadge kind="answer" />);
    expect(screen.getByText("answer")).not.toHaveAttribute("title");
  });

  // The decisions caller's exact shape (spec 21): a bare route column and a
  // `filter_fired` column, with no `Route` object anywhere.
  it("takes a decision's two columns with no Route object", () => {
    renderWithProviders(<RouteBadge kind="safety" filterFired="pain_keyword" />);
    expect(screen.getByText("safety")).toBeInTheDocument();
  });
});
