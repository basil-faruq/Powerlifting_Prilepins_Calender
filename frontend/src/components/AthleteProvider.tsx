import { createContext, useContext, useMemo, type ReactNode } from "react";
import { API_URL } from "@/api/client";
import { errorMessage, isNetworkError } from "@/api/errors";
import { useAthletes } from "@/api/hooks/athletes";
import { useCurrentBlock } from "@/api/hooks/blocks";
import type { AthleteOut, BlockOut } from "@/api/types";
import { ErrorState } from "./states/ErrorState";
import { LoadingState } from "./states/LoadingState";

interface AthleteContextValue {
  /** `null` on a fresh database: the setup wizard creates the athlete. */
  athlete: AthleteOut | null;
  athleteId: string | null;
  /** The active or paused block. A draft awaiting approval is deliberately not one. */
  currentBlock: BlockOut | null;
  currentBlockId: string | null;
}

const AthleteContext = createContext<AthleteContextValue | null>(null);

/**
 * Single-athlete app (CLAUDE.md): the first athlete the API lists is the one.
 * Nothing renders until both the athlete and their current block are known, so no
 * page ever has to handle "I don't know yet whether a block exists".
 */
export function AthleteProvider({ children }: { children: ReactNode }) {
  const athletes = useAthletes();
  const athlete = athletes.data?.[0] ?? null;
  const athleteId = athlete?.id ?? null;
  const block = useCurrentBlock(athleteId);

  const value = useMemo<AthleteContextValue>(
    () => ({
      athlete,
      athleteId,
      currentBlock: block.data ?? null,
      currentBlockId: block.data?.id ?? null,
    }),
    [athlete, athleteId, block.data],
  );

  const failure = athletes.error ?? block.error;
  if (failure) {
    const unreachable = isNetworkError(failure);
    return (
      <ErrorState
        fullPage
        title={unreachable ? "Backend not reachable" : "The backend returned an error"}
        message={unreachable ? `Backend not reachable at ${API_URL}` : errorMessage(failure)}
        onRetry={() => {
          void athletes.refetch();
          void block.refetch();
        }}
      />
    );
  }
  if (athletes.isPending || (athleteId !== null && block.isPending)) {
    return <LoadingState fullPage label="Connecting to the backend…" />;
  }

  return <AthleteContext value={value}>{children}</AthleteContext>;
}

export function useAthleteContext(): AthleteContextValue {
  const value = useContext(AthleteContext);
  if (value === null) throw new Error("useAthleteContext must be used inside <AthleteProvider>");
  return value;
}
