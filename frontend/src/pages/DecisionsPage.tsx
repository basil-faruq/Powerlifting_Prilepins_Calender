import { Button, Field, HStack, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { PAGE_LIMIT, useDecision, useDecisions } from "@/api/hooks/decisions";
import { proposalOptions } from "@/api/hooks/proposals";
import { errorMessage } from "@/api/errors";
import type { DecisionFilters } from "@/api/keys";
import type { DecisionRoute, DecisionSource } from "@/api/types";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorState } from "@/components/states/ErrorState";
import { LoadingState } from "@/components/states/LoadingState";
import { toaster } from "@/components/ui/toaster";
import { DecisionDrawer } from "@/features/decisions/DecisionDrawer";
import { DecisionRow } from "@/features/decisions/DecisionRow";
import { matchesOutcome, serverOutcome, type OutcomeFilter } from "@/features/decisions/model";

const ROUTES: DecisionRoute[] = ["answer", "plan", "reschedule", "simulate", "safety", "clarify"];
const SOURCES: DecisionSource[] = ["chat", "workflow"];
/** The four column values, plus the one a null column means. */
const OUTCOMES: OutcomeFilter[] = ["approved", "rejected", "answered", "failed", "pending"];

/**
 * The decision trail: every turn the agent took, and what it decided.
 *
 * Read-only (spec 21 §11). It does not use `useAthleteContext`, and that is
 * deliberate: `athlete_id` is never sent, because the route joins it through
 * `chat_message` and passing it would hide every workflow decision. So this page works
 * before setup, unlike every other one.
 */
export function DecisionsPage() {
  const [params, setParams] = useSearchParams();
  const openId = params.get("id");

  // Filters are LOCAL. `?id=` is the deep-link contract chat already builds, and spec
  // 21 §3 asks for no other URL param.
  const [route, setRoute] = useState<DecisionRoute | null>(null);
  const [outcome, setOutcome] = useState<OutcomeFilter | null>(null);
  const [source, setSource] = useState<DecisionSource | null>(null);

  const filters: DecisionFilters = { route, outcome: serverOutcome(outcome), source };
  const decisions = useDecisions(filters);

  const rows = useMemo(
    () => (decisions.data ?? []).filter((decision) => matchesOutcome(decision, outcome)),
    [decisions.data, outcome],
  );

  // The list returns the FULL shape, so a row already on screen opens with no request.
  const fromList = decisions.data?.find((decision) => decision.id === openId);
  const detail = useDecision(openId, fromList);
  const open = detail.data ?? null;

  // §4B wants the outcome AND its time; the decision has no time of its own.
  const firstProposal = open?.proposal_id ?? null;
  const proposal = useQuery({
    ...proposalOptions(firstProposal ?? ""),
    enabled: firstProposal !== null,
  });

  const clearId = useCallback(() => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("id");
        return next;
      },
      { replace: true },
    );
  }, [setParams]);

  const openDecision = useCallback(
    (decisionId: string) => {
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        next.set("id", decisionId);
        return next;
      });
    },
    [setParams],
  );

  // §13: a bad `?id=` is a toast and the list, not an error page. Clearing the id
  // disables the query, so this fires once per bad id rather than once per render.
  // The sentence is fixed rather than `errorMessage(error)`: a malformed uuid is a 422
  // whose body reads "decision_id: value is not a valid uuid", which is
  // developer-speak, and a missing one is a 404 -- both mean the same thing here.
  useEffect(() => {
    if (!detail.isError) return;
    toaster.create({
      type: "error",
      title: "That decision could not be found",
      description: "It may have been removed, or the link may be wrong.",
    });
    clearId();
  }, [detail.isError, clearId]);

  const filtered = route !== null || outcome !== null || source !== null;

  const clearFilters = () => {
    setRoute(null);
    setOutcome(null);
    setSource(null);
  };

  // `isPending` is only the FIRST load: `keepPreviousData` keeps the page on screen
  // while a changed filter is in flight.
  if (decisions.isPending) return <LoadingState label="Loading the decision trail…" />;

  if (decisions.isError) {
    return (
      <ErrorState
        title="Could not load the decision trail"
        message={errorMessage(decisions.error)}
        onRetry={() => void decisions.refetch()}
      />
    );
  }

  return (
    <Stack gap="4" minW="0">
      <Stack gap="1">
        <Text textStyle="2xl" fontWeight="semibold">
          Decisions
        </Text>
        <Text fontSize="sm" color="fg.muted">
          Every turn the agent took: which route and filter decided it, which tools ran, and what
          you did with what it proposed.
        </Text>
      </Stack>

      {/* The controls stay mounted above the branch below, so a filter that matches
          nothing still leaves a way to clear it. */}
      <Stack direction={{ base: "column", md: "row" }} gap="3" align={{ md: "flex-end" }}>
        <Field.Root maxW={{ md: "44" }}>
          <Field.Label fontSize="xs">Route</Field.Label>
          <NativeSelect.Root size="sm">
            <NativeSelect.Field
              value={route ?? ""}
              aria-label="Filter by route"
              onChange={(e) => setRoute(e.currentTarget.value === "" ? null : (e.currentTarget.value as DecisionRoute))}
            >
              <option value="">Every route</option>
              {ROUTES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
        </Field.Root>

        <Field.Root maxW={{ md: "52" }}>
          <Field.Label fontSize="xs">Outcome</Field.Label>
          <NativeSelect.Root size="sm">
            <NativeSelect.Field
              value={outcome ?? ""}
              aria-label="Filter by outcome"
              onChange={(e) => setOutcome(e.currentTarget.value === "" ? null : (e.currentTarget.value as OutcomeFilter))}
            >
              <option value="">Every outcome</option>
              {OUTCOMES.map((value) => (
                <option key={value} value={value}>
                  {/* "pending" is not a column value, so the route cannot filter on
                      it and this page narrows the page it already has. */}
                  {value === "pending" ? "pending (newest page only)" : value}
                </option>
              ))}
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
        </Field.Root>

        <Field.Root maxW={{ md: "40" }}>
          <Field.Label fontSize="xs">Source</Field.Label>
          <NativeSelect.Root size="sm">
            <NativeSelect.Field
              value={source ?? ""}
              aria-label="Filter by source"
              onChange={(e) => setSource(e.currentTarget.value === "" ? null : (e.currentTarget.value as DecisionSource))}
            >
              <option value="">Chat and workflow</option>
              {SOURCES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
        </Field.Root>

        {filtered && (
          <Button variant="outline" size="sm" onClick={clearFilters}>
            Clear filters
          </Button>
        )}
      </Stack>

      {rows.length === 0 ? (
        filtered ? (
          <EmptyState
            title="No decisions match these filters"
            description="Clear them to see the whole trail."
            action={
              <Button variant="outline" onClick={clearFilters}>
                Clear filters
              </Button>
            }
          />
        ) : (
          // Spec 21 §10, verbatim.
          <EmptyState
            title="No decisions yet."
            description="Ask the agent something in Chat, or wait for the workflow to notice a missed session."
            action={
              <Button asChild variant="solid">
                <Link to="/chat">Open Chat</Link>
              </Button>
            }
          />
        )
      ) : (
        <Stack gap="1" minW="0" opacity={decisions.isFetching ? 0.6 : 1}>
          {rows.map((decision) => (
            <DecisionRow
              key={decision.id}
              decision={decision}
              selected={decision.id === openId}
              onOpen={openDecision}
            />
          ))}
        </Stack>
      )}

      {decisions.data.length >= PAGE_LIMIT && (
        <HStack gap="2" wrap="wrap">
          <Text fontSize="xs" color="fg.muted">
            The newest {PAGE_LIMIT} decisions. There is no older-page link: the route takes a limit
            and no cursor.
          </Text>
        </HStack>
      )}

      <DecisionDrawer
        decision={open}
        isLoading={openId !== null && detail.isFetching}
        proposal={proposal.data ?? null}
        onClose={clearId}
      />
    </Stack>
  );
}
