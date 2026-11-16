import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import { Fact } from "@/components/shared/Fact";
import { FactGrid } from "@/components/shared/Fact/FactGrid";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { RangeControl } from "@/components/shared/RangeControl";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  emptyAxis,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { formatDuration, shareLabel } from "@/lib/format";
import { adminMetricsQueryOptions } from "@/lib/queries/spend";

type Range = Schemas["AdminMetricsRange"];
type Bucket = Schemas["AdminMetricsBucket"];

type Metrics = Schemas["AdminMetrics"];

const RANGES: { key: Range; label: string }[] = [
  { key: "24h", label: "24 hours" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
];

/** A count as a share of its total, or null where there was nothing to fail. */
const share = (part: number, whole: number) => (whole > 0 ? part / whole : null);

type Unit = "count" | "duration" | "percent";
type Series = {
  key: string;
  label: string;
  color: string;
  value: (b: Bucket) => number | null;
  /** The series over the whole range, where one number says it. */
  total?: (m: Metrics) => number | null;
};
type Chart = { title?: string; unit: Unit; stacked?: boolean; series: Series[] };

const sum = (m: Metrics, value: (b: Bucket) => number) =>
  m.buckets.reduce((total, b) => total + value(b), 0);

/**
 * How much the platform is used on the first row, how well it runs on the
 * second. Counts that add up to one total stack as bars; the rest are lines.
 * A chart has a title only where its series names do not say what is measured.
 */
const CHARTS: Chart[] = [
  {
    unit: "count",
    series: [
      {
        key: "in",
        label: "Signed in",
        color: "var(--chart-1)",
        value: (b) => b.sign_ins,
        total: (m) => m.signed_in,
      },
      {
        key: "up",
        label: "Signed up",
        color: "var(--chart-2)",
        value: (b) => b.sign_ups,
        total: (m) => sum(m, (b) => b.sign_ups),
      },
    ],
  },
  {
    unit: "count",
    stacked: true,
    series: [
      {
        key: "tasks",
        label: "Function calls",
        color: "var(--chart-1)",
        value: (b) => b.tasks,
        total: (m) => sum(m, (b) => b.tasks),
      },
      {
        key: "requests",
        label: "Requests",
        color: "var(--chart-4)",
        value: (b) => b.requests,
        total: (m) => sum(m, (b) => b.requests),
      },
    ],
  },
  {
    unit: "count",
    series: [
      {
        key: "active",
        label: "Active workspaces",
        color: "var(--chart-1)",
        value: (b) => b.active_workspaces,
      },
      {
        key: "deploys",
        label: "Deploys",
        color: "var(--chart-2)",
        value: (b) => b.deployments,
        total: (m) => sum(m, (b) => b.deployments),
      },
    ],
  },
  {
    title: "Container start time",
    unit: "duration",
    series: [
      {
        key: "p50",
        label: "p50",
        color: "var(--muted-foreground)",
        value: (b) => b.start_p50_ms ?? null,
      },
      { key: "p95", label: "p95", color: "var(--chart-3)", value: (b) => b.start_p95_ms ?? null },
    ],
  },
  {
    title: "Call queue wait",
    unit: "duration",
    series: [
      {
        key: "p50",
        label: "p50",
        color: "var(--muted-foreground)",
        value: (b) => b.queue_p50_ms ?? null,
      },
      { key: "p95", label: "p95", color: "var(--chart-3)", value: (b) => b.queue_p95_ms ?? null },
    ],
  },
  {
    title: "Failure rate",
    unit: "percent",
    series: [
      {
        key: "tasks",
        label: "Function calls",
        color: "var(--chart-1)",
        value: (b) => share(b.failed_tasks, b.tasks),
        total: (m) =>
          share(
            sum(m, (b) => b.failed_tasks),
            sum(m, (b) => b.tasks),
          ),
      },
      {
        key: "requests",
        label: "Requests",
        color: "var(--chart-4)",
        value: (b) => share(b.failed_requests, b.requests),
        total: (m) =>
          share(
            sum(m, (b) => b.failed_requests),
            sum(m, (b) => b.requests),
          ),
      },
      {
        key: "starts",
        label: "Container starts",
        color: "var(--chart-3)",
        value: (b) => share(b.failed_starts, b.container_starts),
        total: (m) =>
          share(
            sum(m, (b) => b.failed_starts),
            sum(m, (b) => b.container_starts),
          ),
      },
    ],
  },
];

const EMPTY_TICKS: Record<Unit, number[]> = {
  count: [0, 5, 10],
  duration: [0, 500, 1_000],
  percent: [0, 0.05, 0.1],
};

const count = (value: number) => Intl.NumberFormat().format(value);

function formatValue(unit: Unit, value: unknown): string {
  const n = Number(value);
  if (unit === "duration") return formatDuration(n);
  if (unit === "percent") return shareLabel(n);
  return count(n);
}

/** Hours read in local time; days start at UTC midnight, so they are labelled in UTC. */
function bucketLabel(timestamp: string, hourly: boolean): string {
  const at = new Date(timestamp);
  return hourly
    ? at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })
    : at.toLocaleDateString(undefined, { timeZone: "UTC", month: "short", day: "numeric" });
}

/** What exists now, then one chart per question over the chosen range. */
export function MetricsSettings() {
  const [range, setRange] = useState<Range>("7d");
  const metrics = useQuery(adminMetricsQueryOptions(range));
  const m = metrics.data;

  return (
    <Panel
      title="Metrics"
      className="min-h-0 flex-1"
      contentClassName="flex min-h-0 flex-col overflow-y-auto lg:overflow-hidden"
      action={<RangeControl options={RANGES} value={range} onChange={setRange} />}
    >
      {metrics.error ? <PanelError message={metrics.error.message} /> : null}
      {metrics.isPending ? <Skeleton className="m-4 h-40" /> : null}
      {m ? (
        <>
          <FactGrid columns={4} className="shrink-0 border-b border-border p-4">
            <Fact label="Users" mono value={count(m.users)} />
            <Fact label="Workspaces" mono value={count(m.workspaces)} />
            <Fact label="Apps" mono value={count(m.apps)} />
            <Fact label="Live containers" mono value={count(m.live_containers)} />
          </FactGrid>
          <div className="grid gap-x-8 gap-y-5 p-4 lg:min-h-0 lg:flex-1 lg:grid-cols-3 lg:grid-rows-2">
            {CHARTS.map((chart) => (
              <BucketChart
                key={chart.series[0].label + (chart.title ?? "")}
                metrics={m}
                {...chart}
              />
            ))}
          </div>
        </>
      ) : null}
    </Panel>
  );
}

function BucketChart({
  title,
  unit,
  stacked = false,
  series,
  metrics,
}: Chart & { metrics: Metrics }) {
  const buckets = metrics.buckets;
  const hourly = metrics.window_seconds < 86_400;
  const config: ChartConfig = Object.fromEntries(
    series.map((s) => [s.key, { label: s.label, color: s.color }]),
  );
  const rows = buckets.map((bucket) => ({
    label: bucketLabel(bucket.timestamp, hourly),
    ...Object.fromEntries(series.map((s) => [s.key, s.value(bucket)])),
  }));
  const plotted = buckets.some((bucket) => series.some((s) => (s.value(bucket) ?? 0) > 0));
  const Chart = stacked ? BarChart : LineChart;

  return (
    <section
      aria-label={title ?? series.map((s) => s.label).join(", ")}
      className="flex min-h-0 min-w-0 flex-col"
    >
      <div className="mb-2 flex shrink-0 flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
        {title ? <h3 className="font-medium text-foreground">{title}</h3> : null}
        {series.map((s) => {
          const total = s.total?.(metrics);
          return (
            <span key={s.key} className="flex items-baseline gap-1.5 text-muted-foreground">
              <span className="h-2 w-2 shrink-0" style={{ backgroundColor: s.color }} />
              {s.label}
              {total == null ? null : (
                <span className="mono tabular-nums text-foreground">
                  {formatValue(unit, total)}
                </span>
              )}
            </span>
          );
        })}
      </div>
      <ChartContainer
        config={config}
        className="aspect-auto h-44 w-full lg:h-auto lg:min-h-0 lg:flex-1"
      >
        <Chart data={rows} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
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
            allowDecimals={unit === "percent"}
            {...emptyAxis(!plotted, EMPTY_TICKS[unit])}
            tick={{ fontSize: 10 }}
            tickFormatter={(value: number) => formatValue(unit, value)}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                formatter={(value, name) => (
                  <div className="flex flex-1 items-center justify-between gap-2 leading-none">
                    <span className="text-muted-foreground">{name}</span>
                    <span className="font-mono font-medium tabular-nums text-foreground">
                      {formatValue(unit, value)}
                    </span>
                  </div>
                )}
              />
            }
          />
          {series.map((s) =>
            stacked ? (
              <Bar
                key={s.key}
                dataKey={s.key}
                name={s.label}
                stackId="stack"
                fill={`var(--color-${s.key})`}
                maxBarSize={26}
                isAnimationActive={false}
              />
            ) : (
              <Line
                key={s.key}
                type="linear"
                dataKey={s.key}
                name={s.label}
                stroke={`var(--color-${s.key})`}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 3 }}
                connectNulls
                isAnimationActive={false}
              />
            ),
          )}
        </Chart>
      </ChartContainer>
    </section>
  );
}
