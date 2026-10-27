import { Badge, Button, CloseButton, Drawer, Portal, Stack, Table, Text } from "@chakra-ui/react";
import { Link } from "react-router";
import { useSession } from "@/api/hooks/sessions";
import type { SessionOut } from "@/api/types";
import { LiftBadge } from "@/components/LiftBadge";
import { formatDate, formatIntensity, formatKg, formatPct } from "@/lib/format";
import { STEP_LABEL, topStep } from "./intensity";
import { cellState, knownLifts } from "./model";

interface Props {
  /** The grid's own copy, so the drawer has content before the detail call lands. */
  session: SessionOut | null;
  today: string;
  onClose: () => void;
  /**
   * Spec 20 owns the unavailable dialog and puts it on the logger. Left as a seam
   * here rather than built twice; the button explains itself until then.
   */
  onCantMakeIt?: (sessionId: string) => void;
}

/**
 * One session in full: what was prescribed, and what was logged if anything was.
 *
 * Read-only. Spec 18 §11 allows no mutation but the opening of a dialog, and the one
 * write path stays `approve_proposal` (ADR-0007). Nothing here derives a weight or a
 * percentage; every number is printed as the server computed it.
 */
export function SessionDrawer({ session, today, onClose, onCantMakeIt }: Props) {
  // Only the log is new here -- `SessionDetail` is `SessionOut` plus `log`.
  const detail = useSession(session?.id ?? null);
  const log = detail.data?.log ?? null;

  const state = session === null ? null : cellState(session, today);
  const step = session === null ? null : topStep(session.lifts);

  return (
    <Drawer.Root open={session !== null} onOpenChange={(e) => !e.open && onClose()} size="md">
      <Portal>
        <Drawer.Backdrop />
        <Drawer.Positioner>
          <Drawer.Content>
            {session !== null && state !== null && (
              <>
                <Drawer.Header>
                  <Stack gap="1" minW="0">
                    <Drawer.Title>
                      Week {session.week} · {formatDate(session.date)}
                    </Drawer.Title>
                    <Stack direction="row" gap="2" wrap="wrap">
                      <Badge variant="subtle">{session.status}</Badge>
                      {state.overdue && (
                        <Badge colorPalette="yellow" variant="subtle">
                          not logged
                        </Badge>
                      )}
                      {state.isTest && (
                        <Badge colorPalette="red" variant="outline">
                          test single
                        </Badge>
                      )}
                      {step !== null && <Badge variant="outline">top {STEP_LABEL[step]}</Badge>}
                    </Stack>
                    {state.movedFrom !== null && (
                      <Text fontSize="sm" color="fg.muted">
                        Moved from {formatDate(state.movedFrom)}
                      </Text>
                    )}
                  </Stack>
                  <Drawer.CloseTrigger asChild>
                    <CloseButton size="sm" />
                  </Drawer.CloseTrigger>
                </Drawer.Header>

                <Drawer.Body>
                  <Stack gap="5" minW="0">
                    <Stack gap="2" minW="0">
                      <Text fontWeight="semibold">Prescribed</Text>
                      <Table.Root size="sm" aria-label="Prescription">
                        <Table.Header>
                          <Table.Row>
                            <Table.ColumnHeader>Lift</Table.ColumnHeader>
                            <Table.ColumnHeader>Sets×reps</Table.ColumnHeader>
                            <Table.ColumnHeader>Intensity</Table.ColumnHeader>
                            <Table.ColumnHeader>Weight</Table.ColumnHeader>
                            <Table.ColumnHeader>Relative</Table.ColumnHeader>
                          </Table.Row>
                        </Table.Header>
                        <Table.Body>
                          {knownLifts(session.lifts).map((lift, i) => (
                            <Table.Row key={`${lift.lift_code}-${i}`}>
                              <Table.Cell>
                                <LiftBadge lift={lift.lift_code} showAccessory />
                              </Table.Cell>
                              <Table.Cell>
                                {lift.sets}×{lift.reps}
                              </Table.Cell>
                              {/* The pair exactly as the sheet authored it. Its
                                  denominator is the max the block was programmed
                                  against, not a 1RM (ADR-0003). */}
                              <Table.Cell>{formatIntensity(lift.pct_num, lift.pct_den)}</Table.Cell>
                              <Table.Cell>{formatKg(lift.weight_kg)}</Table.Cell>
                              <Table.Cell>{formatPct(lift.rel_intensity)}</Table.Cell>
                            </Table.Row>
                          ))}
                        </Table.Body>
                      </Table.Root>
                      <Text fontSize="xs" color="fg.muted">
                        Intensity is the fraction as written in the template; its denominator is
                        the max the block was programmed against. "Relative" is that fraction
                        against what the reps themselves imply.
                      </Text>
                    </Stack>

                    {log !== null && (
                      <Stack gap="2" minW="0">
                        <Text fontWeight="semibold">Logged {formatDate(log.performed_on)}</Text>
                        <Table.Root size="sm" aria-label="Log">
                          <Table.Header>
                            <Table.Row>
                              <Table.ColumnHeader>Lift</Table.ColumnHeader>
                              <Table.ColumnHeader>Sets</Table.ColumnHeader>
                              <Table.ColumnHeader>Reps</Table.ColumnHeader>
                              <Table.ColumnHeader>Weight</Table.ColumnHeader>
                              <Table.ColumnHeader>RPE</Table.ColumnHeader>
                            </Table.Row>
                          </Table.Header>
                          <Table.Body>
                            {log.entries.map((entry, i) => (
                              <Table.Row key={`${entry.lift_code}-${i}`}>
                                <Table.Cell>
                                  <LiftBadge lift={entry.lift_code} />
                                </Table.Cell>
                                <Table.Cell>{entry.sets_done}</Table.Cell>
                                <Table.Cell>
                                  {Array.isArray(entry.reps_done)
                                    ? entry.reps_done.join(", ")
                                    : entry.reps_done}
                                </Table.Cell>
                                <Table.Cell>{formatKg(entry.weight_kg)}</Table.Cell>
                                <Table.Cell>{entry.rpe ?? "—"}</Table.Cell>
                              </Table.Row>
                            ))}
                          </Table.Body>
                        </Table.Root>
                        {log.note !== null && log.note !== "" && (
                          <Stack gap="1">
                            <Text fontWeight="semibold" fontSize="sm">
                              Note
                            </Text>
                            <Text fontSize="sm" whiteSpace="pre-wrap">
                              {log.note}
                            </Text>
                          </Stack>
                        )}
                      </Stack>
                    )}

                    {detail.isError && (
                      <Text fontSize="sm" color="fg.muted">
                        The log for this session could not be loaded. The prescription above is
                        what the block holds.
                      </Text>
                    )}
                  </Stack>
                </Drawer.Body>

                <Drawer.Footer gap="2">
                  <Button asChild variant="solid">
                    <Link to={`/log?session=${session.id}`}>Log this session</Link>
                  </Button>
                  <Button
                    variant="outline"
                    disabled={onCantMakeIt === undefined}
                    title={
                      onCantMakeIt === undefined
                        ? "Rescheduling arrives with the session logger (spec 20)."
                        : undefined
                    }
                    onClick={onCantMakeIt === undefined ? undefined : () => onCantMakeIt(session.id)}
                  >
                    I cannot make it
                  </Button>
                </Drawer.Footer>
              </>
            )}
          </Drawer.Content>
        </Drawer.Positioner>
      </Portal>
    </Drawer.Root>
  );
}
