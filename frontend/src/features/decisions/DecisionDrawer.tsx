import {
  Accordion,
  Badge,
  Box,
  Button,
  CloseButton,
  Drawer,
  HStack,
  Portal,
  SimpleGrid,
  Spinner,
  Stack,
  Text,
  VisuallyHidden,
} from "@chakra-ui/react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import type { DecisionOut, ProposalOut } from "@/api/types";
import { RouteBadge } from "@/components/RouteBadge";
import { formatCount, formatDateTime, formatDuration } from "@/lib/format";
import { ToolCallItem } from "./ToolCallItem";
import { badgeProps, decisionView, verdictTone, WORKFLOW_TITLE } from "./model";

interface Props {
  /** Null closes it. The page owns the `?id=` param, the fetch and the bad-id toast. */
  decision: DecisionOut | null;
  /** A deep link that arrived without its list row is still loading. */
  isLoading: boolean;
  /**
   * The first proposal, when the page has it. A decision records no decision TIME of
   * its own, so §4B's "final outcome and time" can only come from the proposal.
   */
  proposal: ProposalOut | null;
  onClose: () => void;
}

const TONE_PALETTE: Record<string, string> = {
  // Red stays reserved for written-off sessions and test singles (ADR-0020).
  violated: "orange",
  warned: "yellow",
  unknown: "gray",
};

/**
 * Everything that was recorded about one decision.
 *
 * Read-only throughout (spec 21 §11): nothing here approves, rejects or edits, and the
 * one write path stays `approve_proposal` (ADR-0007). Every number is printed as the
 * server recorded it.
 *
 * Takes plain props rather than fetching. `SessionDrawer` fetches because the grid's
 * copy of a session is a strict subset of the detail; here the list returns the
 * identical full shape, `tool_calls` included, so the only fetch is the deep-link one
 * and it belongs to the page along with the toast it can fail with.
 */
export function DecisionDrawer({ decision, isLoading, proposal, onClose }: Props) {
  const view = decision === null ? null : decisionView(decision);

  return (
    <Drawer.Root open={decision !== null} onOpenChange={(e) => !e.open && onClose()} size="md">
      <Portal>
        <Drawer.Backdrop />
        <Drawer.Positioner>
          <Drawer.Content>
            {decision !== null && view !== null && (
              <>
                <Drawer.Header>
                  <Stack gap="1" minW="0">
                    <Drawer.Title>{formatDateTime(decision.created_at)}</Drawer.Title>
                    <HStack gap="2" wrap="wrap">
                      <RouteBadge {...badgeProps(decision, view.router)} />
                      <Badge variant="outline">{decision.source}</Badge>
                      <Badge colorPalette={view.outcome.palette} variant="subtle">
                        {view.outcome.label}
                      </Badge>
                      {isLoading && <Spinner size="xs" />}
                    </HStack>
                    {view.outcome.detail !== null && (
                      <Text fontSize="sm" color="fg.muted">
                        {view.outcome.detail}
                      </Text>
                    )}
                    {/* How you find the same turn in the worker's own logs. */}
                    <Text fontSize="xs" color="fg.muted" fontFamily="mono" wordBreak="break-all">
                      {decision.run_id}
                    </Text>
                  </Stack>
                  <Drawer.CloseTrigger asChild>
                    <CloseButton size="sm" />
                  </Drawer.CloseTrigger>
                </Drawer.Header>

                <Drawer.Body>
                  <Stack gap="5" minW="0">
                    <Section title="Input">
                      {decision.message ? (
                        <Text whiteSpace="pre-wrap" wordBreak="break-word">
                          {decision.message}
                        </Text>
                      ) : (
                        <Stack gap="1">
                          <Text>{WORKFLOW_TITLE}</Text>
                          <Muted>
                            The workflow records no message: it ran on a timer, not on a sentence.
                          </Muted>
                        </Stack>
                      )}
                    </Section>

                    <Section title="Routing">
                      <Stack gap="2" minW="0">
                        {/* The filter result comes FIRST: it decides whether a model
                            was involved at all (CLAUDE.md #3). */}
                        {decision.filter_fired !== null ? (
                          <Text>
                            The <strong>{decision.filter_fired}</strong> check fired, so no model was
                            asked.
                          </Text>
                        ) : (
                          <Text>No filter fired; the router chose.</Text>
                        )}

                        {view.router === null ? (
                          <Muted>No routing was recorded for this decision.</Muted>
                        ) : view.router.choice === null ? (
                          <Muted>No model was asked, so there are no extracted parameters.</Muted>
                        ) : view.router.choice.length === 0 ? (
                          <Muted>The router extracted no parameters from the message.</Muted>
                        ) : (
                          <Stack gap="1" minW="0">
                            {view.router.choice.map((param) => (
                              <HStack key={param.key} gap="2" align="start" minW="0">
                                <Text fontSize="sm" color="fg.muted" minW="36" flexShrink="0">
                                  {param.label}
                                </Text>
                                <Text fontSize="sm" wordBreak="break-word">
                                  {param.value}
                                </Text>
                              </HStack>
                            ))}
                          </Stack>
                        )}

                        {view.router?.unparseable === true && (
                          <Muted>The router&rsquo;s answer could not be read.</Muted>
                        )}
                      </Stack>
                    </Section>

                    <Section
                      title="Tool calls"
                      note={
                        view.calls.length === 0
                          ? undefined
                          : `${formatCount(view.calls.length)} ${view.calls.length === 1 ? "call" : "calls"}${
                              view.recordedMs === null
                                ? ""
                                : ` · ${formatDuration(view.recordedMs)} in recorded tools`
                            }`
                      }
                    >
                      {view.noCalls ? (
                        <Stack gap="1">
                          <Muted>No tool calls were recorded.</Muted>
                          {decision.source === "workflow" && (
                            <Muted>
                              The block workflow does not record its steps individually; the
                              proposals it raised are below.
                            </Muted>
                          )}
                        </Stack>
                      ) : (
                        <Accordion.Root multiple lazyMount unmountOnExit variant="outline">
                          {view.calls.map((call) => (
                            <ToolCallItem key={`${String(call.index)}-${call.name}`} call={call} />
                          ))}
                        </Accordion.Root>
                      )}
                    </Section>

                    {/* Omitted entirely when empty rather than shown as zeros: a
                        fabricated wall time is the same mistake as an invented
                        weight (ADR-0016). */}
                    {view.stats.length > 0 && (
                      <Section title="Solver / simulator">
                        <SimpleGrid columns={{ base: 1, md: 2 }} gap="2" minW="0">
                          {view.stats.map((line) => (
                            <Stack key={`${line.from}-${line.label}`} gap="0" minW="0">
                              <Text fontSize="xs" color="fg.muted">
                                {line.label}
                              </Text>
                              <Text fontSize="sm" wordBreak="break-word">
                                {line.value}
                              </Text>
                              <Text fontSize="xs" color="fg.muted" fontFamily="mono">
                                {line.from}
                              </Text>
                            </Stack>
                          ))}
                        </SimpleGrid>
                      </Section>
                    )}

                    <Section title="Validator">
                      {view.verdicts.length === 0 && view.unsupported.length === 0 ? (
                        <Muted>Nothing was flagged for this decision.</Muted>
                      ) : (
                        <Stack gap="3" minW="0">
                          {view.verdicts.map((entry) => (
                            <Stack key={entry.from} gap="1" minW="0">
                              <HStack gap="2" wrap="wrap">
                                <Badge
                                  colorPalette={TONE_PALETTE[verdictTone(entry.verdict)] ?? "gray"}
                                  variant="subtle"
                                >
                                  {verdictTone(entry.verdict) === "violated"
                                    ? "rules violated"
                                    : verdictTone(entry.verdict) === "warned"
                                      ? "passed with warnings"
                                      : "verdict not recorded"}
                                </Badge>
                                <Text fontSize="xs" color="fg.muted" fontFamily="mono">
                                  {entry.from}
                                </Text>
                              </HStack>
                              {entry.verdict.violationRules.length > 0 && (
                                <Box as="ul" ps="5" fontSize="sm" wordBreak="break-word">
                                  {entry.verdict.violationRules.map((rule) => (
                                    <li key={rule}>{rule}</li>
                                  ))}
                                </Box>
                              )}
                              {entry.verdict.warningCount !== null && (
                                <Text fontSize="sm">
                                  {formatCount(entry.verdict.warningCount)}{" "}
                                  {entry.verdict.warningCount === 1 ? "warning" : "warnings"}
                                </Text>
                              )}
                              <Muted>
                                The trace records which rules were violated and how many warnings
                                there were, not their text.
                              </Muted>
                            </Stack>
                          ))}

                          {view.unsupported.length > 0 && (
                            <Stack gap="1" minW="0">
                              <Text fontSize="sm">
                                The reply check could not support{" "}
                                {formatCount(view.unsupported.length)}{" "}
                                {view.unsupported.length === 1 ? "claim" : "claims"} from the context
                                it was given.
                              </Text>
                              <Box as="ul" ps="5" fontSize="sm" fontFamily="mono" wordBreak="break-word">
                                {view.unsupported.map((claim) => (
                                  <li key={claim}>{claim}</li>
                                ))}
                              </Box>
                              {/* The checker's own markers -- `num:1` names a number in
                                  the reply. Shown as recorded: the explainer's answer is
                                  discarded rather than repaired (ADR-0015), so there is
                                  nothing more readable to show. */}
                              <Muted>
                                These are the check&rsquo;s own markers for what it flagged.
                              </Muted>
                            </Stack>
                          )}
                        </Stack>
                      )}
                    </Section>

                    <Section title="Proposal">
                      {view.proposalIds.length === 0 ? (
                        <Muted>This decision raised no proposal.</Muted>
                      ) : (
                        <Stack gap="2" minW="0">
                          {view.proposalIds.map((id) => (
                            <HStack key={id} gap="2" wrap="wrap" minW="0">
                              <Text fontSize="xs" fontFamily="mono" wordBreak="break-all">
                                {id}
                              </Text>
                              {/* Offered only while pending: the tray lists pending
                                  proposals only, and this screen decides nothing. */}
                              {view.outcome.tag === "pending" && (
                                <Button asChild variant="outline" size="xs">
                                  <Link to={`/chat?proposal=${id}`}>Decide it in Chat</Link>
                                </Button>
                              )}
                            </HStack>
                          ))}
                          <Text fontSize="sm">
                            {view.outcome.label}
                            {proposal?.decided_at ? ` · ${formatDateTime(proposal.decided_at)}` : ""}
                          </Text>
                          {proposal?.decided_at == null && view.outcome.tag !== "pending" && (
                            <Muted>
                              The decision records no time of its own; the proposal carries it.
                            </Muted>
                          )}
                        </Stack>
                      )}
                    </Section>
                  </Stack>
                </Drawer.Body>

                <Drawer.Footer>
                  {view.traceHref !== null ? (
                    <Button asChild variant="solid">
                      {/* New tab (§11). `rel` carries both tokens: `noreferrer`
                          implies `noopener` in current browsers, and the pair is
                          what keeps an older one from handing this app's `window`
                          to another origin. */}
                      <a href={view.traceHref} target="_blank" rel="noopener noreferrer">
                        Open in Langfuse
                        <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                      </a>
                    </Button>
                  ) : (
                    <Stack gap="0" minW="0">
                      <Text color="fg.muted">Tracing disabled</Text>
                      {/* A null `trace_url` has three causes -- tracing off, no
                          project id, or a row written before it was enabled -- and
                          the API sends no flag telling them apart (ADR-0016). So
                          this says what is true of all three rather than guessing. */}
                      <Muted>
                        No trace was recorded for this decision. Tracing may have been off when it
                        ran.
                      </Muted>
                    </Stack>
                  )}
                </Drawer.Footer>
              </>
            )}
          </Drawer.Content>
        </Drawer.Positioner>
      </Portal>
    </Drawer.Root>
  );
}

function Muted({ children }: { children: ReactNode }) {
  return (
    <Text fontSize="xs" color="fg.muted">
      {children}
    </Text>
  );
}

function Section({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <Stack gap="2" minW="0">
      <HStack gap="2" wrap="wrap">
        <Text fontWeight="semibold">{title}</Text>
        {note !== undefined && (
          <Text fontSize="xs" color="fg.muted">
            {note}
          </Text>
        )}
      </HStack>
      {children}
    </Stack>
  );
}
