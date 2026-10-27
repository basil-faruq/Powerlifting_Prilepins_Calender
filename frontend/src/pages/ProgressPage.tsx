import { Button, Heading, Stack, Text, Wrap } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { useOneRepMaxHistory } from "@/api/hooks/athletes";
import { useAthleteBlocks } from "@/api/hooks/blocks";
import type { LiftCode } from "@/api/types";
import { useAthleteContext } from "@/components/AthleteProvider";
import { LiftBadge } from "@/components/LiftBadge";
import { ProblemAlert } from "@/components/ProblemAlert";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorState } from "@/components/states/ErrorState";
import { LoadingState } from "@/components/states/LoadingState";
import { BlockHistoryTable } from "@/features/progress/BlockHistoryTable";
import { OneRmChart } from "@/features/progress/OneRmChart";
import { blockRows, hasSynthetic, liftsPresent, seriesByLift } from "@/features/progress/model";

/** A max per lift across every block, and what each block was planned against. */
export function ProgressPage() {
  const { athleteId } = useAthleteContext();
  const history = useOneRepMaxHistory(athleteId);
  const blocks = useAthleteBlocks(athleteId);
  const [hidden, setHidden] = useState<ReadonlySet<LiftCode>>(new Set());

  const rows = useMemo(() => blockRows(blocks.data ?? []), [blocks.data]);
  const series = useMemo(() => seriesByLift(history.data ?? []), [history.data]);
  const present = useMemo(() => liftsPresent(series, rows), [series, rows]);

  if (history.isPending || blocks.isPending) return <LoadingState label="Loading your history…" />;

  if (history.isError || blocks.isError) {
    const failed = history.isError ? history : blocks;
    return (
      <ErrorState
        title="Could not load your history"
        message={failed.error?.message ?? "Something went wrong."}
        onRetry={() => void failed.refetch()}
      />
    );
  }

  if (present.length === 0) {
    return (
      <EmptyState
        title="Nothing to show yet"
        description="Your maxes appear here once you have recorded some and run a block."
      />
    );
  }

  const shown = present.filter((lift) => !hidden.has(lift));
  const toggle = (lift: LiftCode) =>
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(lift)) next.delete(lift);
      else next.add(lift);
      return next;
    });

  return (
    <Stack gap="6" minW="0">
      <Stack gap="1">
        <Heading size="lg">Progress</Heading>
        <Text color="fg.muted">What you have lifted, and what each block was planned against.</Text>
      </Stack>

      {hasSynthetic(history.data) && (
        <ProblemAlert status="warning" title="Development data">
          Some of these maxes were seeded for local testing and marked with a cross on the
          chart. Nothing here describes a real athlete while they are present.
        </ProblemAlert>
      )}

      <Wrap gap="2">
        {present.map((lift) => {
          const on = !hidden.has(lift);
          return (
            <Button
              key={lift}
              size="sm"
              variant={on ? "solid" : "outline"}
              aria-pressed={on}
              onClick={() => toggle(lift)}
            >
              <LiftBadge lift={lift} short />
              {on ? "shown" : "hidden"}
            </Button>
          );
        })}
      </Wrap>

      <OneRmChart series={series} lifts={shown} />
      <BlockHistoryTable rows={rows} lifts={shown} />
    </Stack>
  );
}
