import {
  keepPreviousData,
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "../client";
import { keys } from "../keys";
import type { LiftCode, MeasuredOneRm } from "../types";
import { unwrap } from "../unwrap";

export const athletesOptions = () =>
  queryOptions({
    queryKey: keys.athletes.list(),
    queryFn: () => unwrap(api.GET("/athletes")),
  });

export const athleteOptions = (athleteId: string) =>
  queryOptions({
    queryKey: keys.athletes.detail(athleteId),
    queryFn: () => unwrap(api.GET("/athletes/{athlete_id}", { params: { path: { athlete_id: athleteId } } })),
  });

/** Every athlete, oldest first. Single-athlete app: the first is the one. */
export const useAthletes = () => useQuery(athletesOptions());

/** The athlete with their current maxes, weekday map and `has_synthetic_one_rms`. */
export const useAthlete = (athleteId: string | null) =>
  useQuery({ ...athleteOptions(athleteId ?? ""), enabled: athleteId !== null });

/**
 * Would these weekdays be coherent for these lifts? A dry run: it writes nothing and
 * answers 200 with `problems`, plain strings the server has already worded. The UI
 * only displays them -- the rules live in the backend (spec 17 §11).
 *
 * It is a query, not a mutation, because it is idempotent and keyed by its inputs, so
 * going back to a combination already checked is free.
 */
export const weekdayCheckOptions = (
  athleteId: string,
  templateId: string,
  lifts: readonly LiftCode[],
  slots: Readonly<Record<string, number>>,
) =>
  queryOptions({
    queryKey: keys.athletes.weekdayCheck(athleteId, templateId, lifts, slots),
    queryFn: () =>
      unwrap(
        api.PUT("/athletes/{athlete_id}/weekday-map", {
          params: { path: { athlete_id: athleteId }, query: { dry_run: true } },
          // `lifts` matters: left out, the server judges every lift in the template.
          body: { slots: { ...slots }, template_id: templateId, lifts: [...lifts] },
        }),
      ),
    // Keep showing the last verdict while the next one loads, so it does not flicker.
    placeholderData: keepPreviousData,
  });

/** Get-or-create: a second call returns the athlete that already exists. */
export function useCreateAthlete() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => unwrap(api.POST("/athletes", { body: { name } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
  });
}

export function usePatchAthlete() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: { athleteId: string; plateIncrementKg?: string; rounding?: "floor" | "nearest" }) =>
      unwrap(
        api.PATCH("/athletes/{athlete_id}", {
          params: { path: { athlete_id: args.athleteId } },
          body: { plate_increment_kg: args.plateIncrementKg, rounding: args.rounding },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
  });
}

/** Appends one row per lift sent and returns what is current. Send only what changed. */
export function useSaveOneRepMaxes() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: { athleteId: string; rows: MeasuredOneRm[] }) =>
      unwrap(
        api.PUT("/athletes/{athlete_id}/one-rep-maxes", {
          params: { path: { athlete_id: args.athleteId } },
          body: args.rows,
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
  });
}

/** The real save. 409 while a block is running; 422 if the map is incoherent. */
export function useSaveWeekdayMap() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: {
      athleteId: string;
      templateId: string;
      lifts: readonly LiftCode[];
      slots: Readonly<Record<string, number>>;
    }) =>
      unwrap(
        api.PUT("/athletes/{athlete_id}/weekday-map", {
          params: { path: { athlete_id: args.athleteId } },
          body: { slots: { ...args.slots }, template_id: args.templateId, lifts: [...args.lifts] },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.athletes.all }),
  });
}

/**
 * Every max ever recorded for this athlete, newest first.
 *
 * The chart's whole input. Rows carry `source` and `is_synthetic` separately,
 * and the UI must filter and mark on `is_synthetic` rather than on
 * `source === "synthetic"` -- that flag is the authoritative "is this real?"
 * (ADR-0004 rule 4).
 */
export const oneRmHistoryOptions = (athleteId: string) =>
  queryOptions({
    queryKey: keys.athletes.oneRmHistory(athleteId),
    queryFn: () =>
      unwrap(
        api.GET("/athletes/{athlete_id}/one-rep-maxes/history", {
          params: { path: { athlete_id: athleteId } },
        }),
      ),
  });

export const useOneRepMaxHistory = (athleteId: string | null) =>
  useQuery({ ...oneRmHistoryOptions(athleteId ?? ""), enabled: athleteId !== null });
