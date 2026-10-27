import { Box, HStack, Stack, Text } from "@chakra-ui/react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LiftCode } from "@/api/types";
import { formatDayMonth, formatKg } from "@/lib/format";
import { LIFT_LABEL } from "@/lib/lifts";
import { system } from "@/theme";
import type { MaxPoint, PointKind } from "./model";

/**
 * A max per lift over time.
 *
 * The y-coordinate is the ONE place a weight becomes a number: an SVG needs a
 * pixel. Every figure the athlete reads -- the tooltip, the axis ticks -- is
 * formatted from the exact decimal string the backend sent, so no displayed
 * number has been through a float (CLAUDE.md non-negotiable 4).
 *
 * Colours come from the lift palettes as CSS variables, so the chart follows
 * dark mode without knowing anything about it.
 */

/** A row per date, with a column per lift: what recharts wants. */
interface Row {
  setOn: string;
  [key: string]: string | number | undefined;
}

/**
 * The lift's own colour as a CSS variable, e.g. `var(--chakra-colors-squat-solid)`.
 *
 * Handed straight to SVG, so the chart follows dark mode without knowing the
 * mode exists. `system.token.var` is untyped, hence the explicit `String`.
 */
const liftColor = (lift: LiftCode): string => String(system.token.var(`colors.${lift}.solid`));

export function OneRmChart({
  series,
  lifts,
}: {
  series: ReadonlyMap<LiftCode, readonly MaxPoint[]>;
  lifts: readonly LiftCode[];
}) {
  const rows = toRows(series, lifts);

  if (rows.length === 0) {
    return (
      <Text color="fg.muted" fontSize="sm">
        No maxes recorded yet. They appear here as you test them.
      </Text>
    );
  }

  return (
    <Stack gap="2">
      <Box h="72" w="full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
            <CartesianGrid strokeOpacity={0.2} />
            <XAxis dataKey="setOn" tickFormatter={formatDayMonth} fontSize={12} />
            <YAxis
              width={56}
              fontSize={12}
              // A tick is a coordinate, so it is already a number here; it is
              // formatted rather than computed.
              tickFormatter={(value: number) => `${value} kg`}
              domain={["dataMin - 5", "dataMax + 5"]}
            />
            <Tooltip
              labelFormatter={(label) =>
                typeof label === "string" ? formatDayMonth(label) : String(label ?? "")
              }
              formatter={(_value, name, item) => {
                const row = (item as { payload?: Row }).payload;
                const lift = String(name) as LiftCode;
                // The exact string, never the plotted number.
                const exact = row?.[`${lift}_kg`];
                return [
                  typeof exact === "string" ? formatKg(exact) : "—",
                  LIFT_LABEL[lift] ?? lift,
                ];
              }}
            />
            {lifts.map((lift) => (
              <Line
                key={lift}
                type="monotone"
                dataKey={lift}
                name={lift}
                stroke={liftColor(lift)}
                strokeWidth={2}
                connectNulls
                dot={(props: unknown) => <SourceDot {...(props as DotProps)} lift={lift} />}
                activeDot={{ r: 5 }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </Box>
      <SourceLegend />
    </Stack>
  );
}

interface DotProps {
  cx?: number;
  cy?: number;
  payload?: Row;
  key?: string;
}

/**
 * The marker says where the number came from.
 *
 * Shape, not colour: colour already means "which lift", and a tested max must
 * be distinguishable from an estimate without relying on either (spec 18 §11's
 * rule, which holds across the app).
 */
function SourceDot({ cx, cy, payload, lift }: DotProps & { lift: LiftCode }) {
  if (cx === undefined || cy === undefined || payload === undefined) return null;
  const kind = payload[`${lift}_kind`] as PointKind | undefined;
  if (kind === undefined) return null;

  const color = liftColor(lift);
  const label = `${LIFT_LABEL[lift]} ${kind}`;

  if (kind === "imported") {
    return <rect x={cx - 4} y={cy - 4} width={8} height={8} fill={color} aria-label={label} />;
  }
  if (kind === "synthetic") {
    return (
      <g aria-label={label} stroke={color} strokeWidth={2}>
        <line x1={cx - 4} y1={cy - 4} x2={cx + 4} y2={cy + 4} />
        <line x1={cx - 4} y1={cy + 4} x2={cx + 4} y2={cy - 4} />
      </g>
    );
  }
  // Tested is filled, an estimate is hollow: the difference between a number
  // the athlete lifted and one the app worked out.
  return (
    <circle
      cx={cx}
      cy={cy}
      r={4}
      fill={kind === "tested" ? color : "var(--chakra-colors-bg)"}
      stroke={color}
      strokeWidth={2}
      aria-label={label}
    />
  );
}

function SourceLegend() {
  return (
    <HStack gap="4" wrap="wrap" fontSize="xs" color="fg.muted">
      <Text>● tested</Text>
      <Text>○ estimated</Text>
      <Text>■ imported</Text>
      <Text>✕ synthetic (development data)</Text>
    </HStack>
  );
}

/**
 * The series merged onto a shared date axis.
 *
 * Each row carries, per lift, the plotted number AND the exact string beside
 * it, so the tooltip never has to reverse the coordinate back into a weight.
 */
export function toRows(
  series: ReadonlyMap<LiftCode, readonly MaxPoint[]>,
  lifts: readonly LiftCode[],
): Row[] {
  const byDate = new Map<string, Row>();

  for (const lift of lifts) {
    for (const point of series.get(lift) ?? []) {
      const row = byDate.get(point.setOn) ?? { setOn: point.setOn };
      row[lift] = Number(point.kg);
      row[`${lift}_kg`] = point.kg;
      row[`${lift}_kind`] = point.kind;
      byDate.set(point.setOn, row);
    }
  }

  return [...byDate.values()].sort((a, b) => a.setOn.localeCompare(b.setOn));
}
