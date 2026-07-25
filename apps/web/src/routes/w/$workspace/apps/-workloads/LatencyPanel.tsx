import { useQuery } from "@tanstack/react-query";
import { format, parseISO } from "date-fns";
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import type { TooltipValueType } from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration } from "@/lib/format";
import { taskLatencyQueryOptions } from "@/lib/queries/stubs";

/**
 * Function-level latency: task-duration p50/p95 per hour over the last 24h
 * from the SQL-windowed rollup, with a cold-start frequency readout counting
 * container creations for the function's stubs in the same window.
 */
export function LatencyPanel({
  workspaceId,
  stubIds,
  kind,
}: {
  workspaceId: string;
  stubIds: string[];
  kind: string;
}) {
  const latency = useQuery(taskLatencyQueryOptions(workspaceId, stubIds));

  if (latency.isPending) {
    return <LatencySkeleton />;
  }
  if (latency.isError) {
    return (
      <div className="flex h-full items-center text-sm text-destructive">
        {latency.error.message}
      </div>
    );
  }

  const buckets = latency.data.buckets;
  const tasks = buckets.reduce((total, bucket) => total + bucket.count, 0);
  const coldStarts = buckets.reduce((total, bucket) => total + bucket.cold_starts, 0);
  const failures = buckets.reduce(
    (total, bucket) => total + (bucket.status_counts.failed ?? 0),
    0,
  );
  const latest = [...buckets].reverse().find((bucket) => bucket.count > 0);

  if (tasks === 0 && coldStarts === 0) {
    return (
      <div className="flex h-full min-h-32 items-center justify-center text-sm text-muted-foreground">
        No tasks in the last 24 hours
      </div>
    );
  }

  const data = buckets.map((bucket) => ({
    label: formatBucketTime(bucket.timestamp),
    p50: bucket.p50_ms,
    p95: bucket.p95_ms,
  }));
  const config: ChartConfig = {
    p50: { label: "p50", color: "var(--muted-foreground)" },
    p95: { label: "p95", color: "var(--chart-3)" },
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div className="grid shrink-0 grid-cols-2 gap-x-5 gap-y-2">
        <LatencyReadout
          label="Latest p50"
          value={latest?.p50_ms == null ? "—" : formatDuration(latest.p50_ms)}
          series="p50"
        />
        <LatencyReadout
          label="Latest p95"
          value={latest?.p95_ms == null ? "—" : formatDuration(latest.p95_ms)}
          series="p95"
        />
        <dl className="col-span-2 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
          <LatencyFact label={volumeLabel(kind)} value={Intl.NumberFormat().format(tasks)} />
          {kind === "task-queue" ? (
            <LatencyFact label="Average throughput" value={`${(tasks / 24).toFixed(1)}/hr`} />
          ) : (
            <LatencyFact
              label={kind === "endpoint" || kind === "asgi" ? "Errors (24h)" : "Failed tasks"}
              value={Intl.NumberFormat().format(failures)}
              danger={failures > 0}
            />
          )}
          <LatencyFact label="Cold starts" value={Intl.NumberFormat().format(coldStarts)} />
        </dl>
      </div>
      <ChartContainer config={config} className="min-h-16 w-full flex-1 aspect-auto">
        <LineChart data={data} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
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
            dot={false}
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
            dot={false}
            activeDot={{ r: 3 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ChartContainer>
    </div>
  );
}

function LatencySkeleton() {
  return (
    <div className="flex h-full min-h-0 flex-col gap-2" aria-hidden="true">
      <div className="grid shrink-0 grid-cols-2 gap-x-5 gap-y-2">
        {["p50", "p95"].map((series) => (
          <div key={series} className="space-y-1">
            <Skeleton className="h-2.5 w-16" />
            <Skeleton className="h-4 w-12" />
          </div>
        ))}
        <Skeleton className="col-span-2 h-3 w-4/5" />
      </div>
      <Skeleton className="min-h-16 flex-1" />
    </div>
  );
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

function volumeLabel(kind: string): string {
  if (kind === "endpoint" || kind === "asgi") return "Requests (24h)";
  if (kind === "task-queue") return "Processed (24h)";
  return "Tasks (24h)";
}

function formatBucketTime(timestamp: string): string {
  try {
    return format(parseISO(timestamp), "HH:mm");
  } catch {
    return timestamp;
  }
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
