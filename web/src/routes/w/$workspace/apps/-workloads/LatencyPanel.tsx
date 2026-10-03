import { format } from "date-fns";
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import type { TooltipValueType } from "recharts";

import { PanelError } from "@/components/shared/PanelError";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { activityHourIndex, activityHours } from "@/lib/activity-window";
import type { Schemas } from "@/lib/api/client";
import { formatDuration } from "@/lib/format";
import { servesRequests } from "@/lib/queries/tasks";

const config: ChartConfig = {
  p50: { label: "p50", color: "var(--muted-foreground)" },
  p95: { label: "p95", color: "var(--chart-3)" },
};

/**
 * The duration axis, in milliseconds, of a plot with no points. Recharts draws
 * no ticks for an axis without data unless its domain may overflow.
 */
const EMPTY_AXIS_TICKS = [0, 250, 500, 750, 1_000];

/**
 * Workload latency: run time p50/p95 per hour over the drawn activity window
 * from the SQL-windowed rollup, with volume, failure and cold-start counts
 * over the same window. The plot keeps its frame and hours while the read is
 * pending or empty, so the panel never changes size as data arrives.
 */
export function LatencyPanel({
  buckets,
  pending,
  error,
  kind,
}: {
  buckets: Schemas["PerformanceBucket"][] | undefined;
  pending: boolean;
  error: Error | null;
  kind: Schemas["WorkloadKind"];
}) {
  if (error) {
    return <PanelError message={error.message} layout="centered" />;
  }

  const requests = servesRequests(kind);
  const known = buckets ?? [];
  const tasks = known.reduce((total, bucket) => total + bucket.count, 0);
  const coldStarts = known.reduce((total, bucket) => total + bucket.cold_starts, 0);
  const failures = known.reduce((total, bucket) => total + bucket.status_counts.failed, 0);
  const latest = [...known].reverse().find((bucket) => bucket.count > 0);
  const count = (value: number) => (pending ? "—" : Intl.NumberFormat().format(value));
  const duration = (value: number | undefined) =>
    value === undefined ? "—" : formatDuration(value);
  const rows = latencyRows(known);
  const plotted = rows.some((row) => row.p50 !== null || row.p95 !== null);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      {/* One line so the plot keeps the height: the readings qualify the chart,
          they are not a second panel above it. */}
      <div className="flex shrink-0 flex-wrap items-end gap-x-8 gap-y-2">
        <LatencyReadout label="Latest p50" value={duration(latest?.p50_ms)} series="p50" />
        <LatencyReadout label="Latest p95" value={duration(latest?.p95_ms)} series="p95" />
        <dl className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 pb-0.5 text-[11px] text-muted-foreground">
          <LatencyFact label={requests ? "Requests (24h)" : "Tasks (24h)"} value={count(tasks)} />
          <LatencyFact
            label={requests ? "Errors (24h)" : "Failed tasks"}
            value={count(failures)}
            danger={failures > 0}
          />
          <LatencyFact label="Cold starts" value={count(coldStarts)} />
        </dl>
      </div>
      <ChartContainer
        config={config}
        className="min-h-16 w-full flex-1 aspect-auto"
        empty={
          pending || latest
            ? undefined
            : `No ${requests ? "requests" : "finished tasks"} in the last 24 hours`
        }
      >
        <LineChart data={rows} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="label"
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            minTickGap={24}
            tickMargin={6}
            tick={{ fontSize: 10 }}
          />
          <YAxis
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            width={46}
            domain={plotted ? undefined : [0, EMPTY_AXIS_TICKS.at(-1) ?? 0]}
            ticks={plotted ? undefined : EMPTY_AXIS_TICKS}
            allowDataOverflow={!plotted}
            tick={{ fontSize: 10 }}
            tickFormatter={(value: number | string) => formatAxisDuration(value)}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                formatter={(value, name) => (
                  <div className="flex flex-1 items-center justify-between gap-2 leading-none">
                    <span className="text-muted-foreground">{name}</span>
                    <span className="font-mono font-medium tabular-nums text-foreground">
                      {formatTooltipDuration(value)}
                    </span>
                  </div>
                )}
              />
            }
          />
          <Line
            type="monotone"
            dataKey="p50"
            name="p50"
            stroke="var(--color-p50)"
            strokeWidth={2}
            dot={isolatedDot(rows, "p50")}
            activeDot={{ r: 3 }}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="p95"
            name="p95"
            stroke="var(--color-p95)"
            strokeWidth={2}
            strokeDasharray="5 3"
            dot={isolatedDot(rows, "p95")}
            activeDot={{ r: 3 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ChartContainer>
    </div>
  );
}

type LatencyRow = { label: string; p50: number | null; p95: number | null };

/** One row per drawn hour; an hour the server reported nothing for plots no point. */
function latencyRows(buckets: Schemas["PerformanceBucket"][]): LatencyRow[] {
  const hours = activityHours();
  const rows: LatencyRow[] = hours.map((hour) => ({
    label: format(hour, "HH:mm"),
    p50: null,
    p95: null,
  }));
  for (const bucket of buckets) {
    const index = activityHourIndex(bucket.timestamp, hours);
    if (index < 0) continue;
    rows[index].p50 = bucket.p50_ms ?? null;
    rows[index].p95 = bucket.p95_ms ?? null;
  }
  return rows;
}

/** A line needs two neighbouring hours, so an hour alone between gaps is drawn as a dot. */
function isolatedDot(rows: LatencyRow[], series: "p50" | "p95") {
  return function IsolatedDot({ cx, cy, index }: { cx?: number; cy?: number; index?: number }) {
    const alone =
      index !== undefined &&
      rows[index]?.[series] != null &&
      rows[index - 1]?.[series] == null &&
      rows[index + 1]?.[series] == null;
    if (!alone || cx === undefined || cy === undefined) return <g key={index} />;
    return <circle key={index} cx={cx} cy={cy} r={2.5} fill={`var(--color-${series})`} />;
  };
}

function LatencyReadout({
  label,
  value,
  series,
}: {
  label: string;
  value: string;
  series: "p50" | "p95";
}) {
  return (
    <div>
      <div className="mb-0.5 flex items-center gap-1.5">
        <span
          className={`w-3 border-t-2 ${series === "p95" ? "border-dashed border-chart-3" : "border-muted-foreground"}`}
          aria-hidden="true"
        />
        <div className="micro-label">{label}</div>
      </div>
      <div className="mono text-base tabular-nums text-foreground">{value}</div>
    </div>
  );
}

function LatencyFact({
  label,
  value,
  danger = false,
}: {
  label: string;
  value: string;
  danger?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5">
      <dt className="truncate">{label}</dt>
      <dd
        className={`mono shrink-0 font-medium tabular-nums ${danger ? "text-destructive" : "text-foreground"}`}
      >
        {value}
      </dd>
    </div>
  );
}

function formatAxisDuration(value: number | string): string {
  const numberValue = Array.isArray(value) ? Number(value[0]) : Number(value);
  if (!Number.isFinite(numberValue)) return String(value);
  return formatDuration(numberValue);
}

function formatTooltipDuration(value: TooltipValueType | undefined): string {
  if (value === undefined || value === null) return "";
  const numberValue = Array.isArray(value) ? Number(value[0]) : Number(value);
  if (!Number.isFinite(numberValue)) return String(value);
  return formatDuration(numberValue);
}
