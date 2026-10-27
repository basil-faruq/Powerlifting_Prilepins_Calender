import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Route, RouteKind } from "@/api/types";
import { renderWithProviders } from "@/test/renderWithProviders";
import { RouteBadge } from "../RouteBadge";

const route = (over: Partial<Route> & { kind: RouteKind }): Route => ({
  lifts: [],
  unparseable: false,
  ...over,
});

describe("RouteBadge", () => {
  it("names the tool that answered", () => {
    const kinds: RouteKind[] = ["answer", "plan", "reschedule", "simulate", "safety"];
    for (const kind of kinds) {
      const { unmount } = renderWithProviders(<RouteBadge route={route({ kind })} />);
      expect(screen.getByText(kind)).toBeInTheDocument();
      unmount();
    }
  });

  // --- the two unlike things `clarify` covers (ADR-0015 mod 12) ---------------

  it("says a hard filter stopped the message, naming the check that fired", () => {
    renderWithProviders(<RouteBadge route={route({ kind: "clarify", filter_fired: "pain" })} />);
    expect(screen.getByText(/stopped by the pain check/)).toBeInTheDocument();
  });

  it("says a message it could not read, rather than blaming a filter", () => {
    renderWithProviders(<RouteBadge route={route({ kind: "clarify", unparseable: true })} />);
    expect(screen.getByText(/could not be read/)).toBeInTheDocument();
    expect(screen.queryByText(/check/)).not.toBeInTheDocument();
  });

  it("shows the router's own reason when it has one", () => {
    const asked = route({ kind: "clarify", clarify_reason: "which lift did you mean?" });
    renderWithProviders(<RouteBadge route={asked} />);
    expect(screen.getByText(/which lift did you mean\?/)).toBeInTheDocument();
  });

  it("prefers the fired filter over the router's guess at why", () => {
    const both = route({
      kind: "clarify",
      filter_fired: "injury",
      unparseable: true,
      clarify_reason: "could not tell",
    });
    renderWithProviders(<RouteBadge route={both} />);
    expect(screen.getByText(/stopped by the injury check/)).toBeInTheDocument();
    expect(screen.queryByText(/could not tell/)).not.toBeInTheDocument();
  });

  it("says just 'clarify' when none of the three explains itself", () => {
    renderWithProviders(<RouteBadge route={route({ kind: "clarify" })} />);
    expect(screen.getByText("clarify")).toBeInTheDocument();
  });

  it("carries the detail in a tooltip as well as in the text", () => {
    renderWithProviders(<RouteBadge route={route({ kind: "clarify", filter_fired: "pain" })} />);
    expect(screen.getByTitle("stopped by the pain check")).toBeInTheDocument();
  });

  it("gives a badge with nothing to explain no tooltip at all", () => {
    renderWithProviders(<RouteBadge route={route({ kind: "answer" })} />);
    expect(screen.getByText("answer")).not.toHaveAttribute("title");
  });
});
