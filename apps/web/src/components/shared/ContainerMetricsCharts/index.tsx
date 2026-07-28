import { Area, CartesianGrid, ComposedChart, Line, LineChart, XAxis, YAxis } from "recharts";
import type { TooltipValueType } from "recharts";

import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import type { ContainerMetricsPoint } from "@/lib/api/schemas";
import { cn } from "@/lib/utils";

import {
  buildMetricData,
  formatBytes,
  formatBytesPerSecond,
  hasIoSamples,
  latestComputeReadout,
  type MetricDatum,
} from "./metrics";

/** One hover cursor across every sample chart on the page. */
const METRICS_SYNC_ID = "container-metrics";

type RateKey = keyof Pick<
  MetricDatum,
  "networkRecvRate" | "networkSentRate" | "diskReadRate" | "diskWriteRate"
>;
type BytesKey = keyof Pick<
  MetricDatum,
  "memoryUsed" | "memoryTotal" | "gpuMemoryUsed" | "gpuMemoryTotal"
>;

export function ContainerMetricsCharts({
  points,
  variant = "full",
  className,
}: {
  points: ContainerMetricsPoint[] | undefined;
  /** `compact` fits a drawer tab; `full` is a responsive detail-page grid. */
  variant?: "full" | "compact";
  className?: string;
}) {
  const samples = points ?? [];
  const data = buildMetricData(samples);

  if (!data.length) {
    return (
      <div
        className={cn(
          "flex items-center justify-center text-sm text-muted-foreground",
          variant === "compact" ? "h-36" : "h-44",
        )}
      >
        No compute samples
      </div>
    );
  }

  const hasGpu = data.some((point) => point.gpuMemoryTotal > 0);
  const hasIo = hasIoSamples(samples);

  if (variant === "compact") {
    const readout = latestComputeReadout(data);
    return (
      <div className={cn("grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2", className)}>
        <CpuChart data={data} compact readout={readout?.cpu} />
        <MemoryChart
          title="Memory"
          data={data}
          usedKey="memoryUsed"
          totalKey="memoryTotal"
          usedLabel="RSS"
          color="var(--chart-2)"
          compact
          readout={readout?.memory}
        />
        {hasIo ? (
          <RatePairChart
            title="Network"
            data={data}
            primaryKey="networkRecvRate"
            secondaryKey="networkSentRate"
            primaryLabel="Received"
            secondaryLabel="Sent"
            color="var(--chart-5)"
            compact
            readout={readout?.network ?? undefined}
            className="sm:col-span-2"
          />
        ) : null}
      </div>
    );
  }

  const readout = latestComputeReadout(data);
  const latest = data[data.length - 1];
  const gpuReadout = latest?.gpuMemoryTotal ? formatBytes(latest.gpuMemoryUsed) : undefined;
  const diskReadout =
    latest && latest.diskReadRate !== null && latest.diskWriteRate !== null
      ? `Read ${formatBytesPerSecond(latest.diskReadRate)} · write ${formatBytesPerSecond(latest.diskWriteRate)}`
      : undefined;

  return (
    <div className={cn("grid grid-cols-1 gap-x-6 gap-y-5 lg:grid-cols-2", className)}>
      <CpuChart data={data} readout={readout?.cpu} />
      <MemoryChart
        title="Memory"
        data={data}
        usedKey="memoryUsed"
        totalKey="memoryTotal"
        usedLabel="RSS"
        color="var(--chart-2)"
        readout={readout?.memory}
      />
      {hasGpu ? (
        <MemoryChart
          title="GPU memory"
          data={data}
          usedKey="gpuMemoryUsed"
          totalKey="gpuMemoryTotal"
          usedLabel="Used"
          color="var(--chart-3)"
          readout={gpuReadout}
        />
      ) : null}
      {hasIo ? (
        <>
          <RatePairChart
            title="Network"
            data={data}
            primaryKey="networkRecvRate"
            secondaryKey="networkSentRate"
            primaryLabel="Received"
            secondaryLabel="Sent"
            color="var(--chart-5)"
            readout={readout?.network ?? undefined}
          />
          <RatePairChart
            title="Disk I/O"
            data={data}
            primaryKey="diskReadRate"
            secondaryKey="diskWriteRate"
            primaryLabel="Read"
            secondaryLabel="Written"
            color="var(--chart-4)"
            readout={diskReadout}
          />
        </>
      ) : null}
    </div>
  );
}

/** Layout-matched loading placeholder for one compute chart panel. */
export function ChartSkeleton({
  className,
  compact = false,
}: {
  className?: string;
  compact?: boolean;
}) {
  return (
    <div className={cn("space-y-2", className)} aria-hidden="true">
      <Skeleton className="h-3.5 w-16" />
      <Skeleton className={cn("w-full", compact ? "h-36" : "h-44")} />
    </div>
  );
}

/** Chart section header: metric name, plus the latest-sample readout in compact mode. */
function ChartHeader({ title, readout }: { title: string; readout?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <div className="text-xs font-medium text-muted-foreground">{title}</div>
      {readout ? <div className="mono text-xs tabular-nums text-foreground">{readout}</div> : null}
    </div>
  );
}

/** CPU utilization as a percentage of the container allocation on a fixed 0-100% axis. */
function CpuChart({
  data,
  compact = false,
  readout,
}: {
  data: MetricDatum[];
  compact?: boolean;
  readout?: string;
}) {
  const config: ChartConfig = {
    cpuPercent: { label: "Used", color: "var(--chart-1)" },
  };

  return (
    <section className="space-y-2" aria-label="CPU">
      <ChartHeader title="CPU" readout={readout} />
      <ChartContainer config={config} className={cn("w-full", compact ? "h-36" : "h-44")}>
        <LineChart
          data={data}
          syncId={METRICS_SYNC_ID}
          margin={{ top: 8, right: 8, bottom: 4, left: 0 }}
        >
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="label"
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            minTickGap={28}
            tickMargin={8}
            tick={{ fontSize: 10 }}
          />
          <YAxis
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            width={compact ? 36 : 44}
            domain={[0, 100]}
            ticks={compact ? [0, 50, 100] : [0, 25, 50, 75, 100]}
            tickFormatter={(value: number | string) => `${value}%`}
            tick={{ fontSize: 10 }}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                formatter={(value, name, item) => (
                  <div className="flex flex-1 items-center justify-between gap-2 leading-none">
                    <span className="text-muted-foreground">{name}</span>
                    <span className="font-mono font-medium tabular-nums text-foreground">
                      {formatCpuValue(value, item.payload as MetricDatum | undefined)}
                    </span>
                  </div>
                )}
              />
            }
          />
          <Line
            type="monotone"
            dataKey="cpuPercent"
            name="Used"
            stroke="var(--color-cpuPercent)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ChartContainer>
    </section>
  );
}

/** Working-set area against a dashed total-capacity line on one bytes axis. */
function MemoryChart({
  title,
  data,
  usedKey,
  totalKey,
  usedLabel,
  color,
  compact = false,
  readout,
}: {
  title: string;
  data: MetricDatum[];
  usedKey: BytesKey;
  totalKey: BytesKey;
  usedLabel: string;
  /** Per-metric hue, beam-style; the capacity line stays muted. */
  color: string;
  compact?: boolean;
  readout?: string;
}) {
  const config: ChartConfig = {
    [usedKey]: { label: usedLabel, color },
    [totalKey]: { label: "Total", color: "var(--muted-foreground)" },
  };

  return (
    <section className="space-y-2" aria-label={title}>
      <ChartHeader title={title} readout={readout} />
      <ChartContainer config={config} className={cn("w-full", compact ? "h-36" : "h-44")}>
        <ComposedChart
          data={data}
          syncId={METRICS_SYNC_ID}
          margin={{ top: 8, right: 8, bottom: 4, left: 0 }}
        >
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="label"
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            minTickGap={28}
            tickMargin={8}
            tick={{ fontSize: 10 }}
          />
          <YAxis
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            width={compact ? 56 : 76}
            tickCount={compact ? 3 : undefined}
            tickFormatter={(value: number | string) => formatAxisValue(value, formatBytes)}
            tick={{ fontSize: 10 }}
          />
          <ChartTooltip content={<ChartTooltipContent formatter={bytesTooltipFormatter} />} />
          <Area
            type="monotone"
            dataKey={usedKey}
            name={usedLabel}
            stroke={`var(--color-${usedKey})`}
            fill={`var(--color-${usedKey})`}
            fillOpacity={0.18}
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey={totalKey}
            name="Total"
            stroke={`var(--color-${totalKey})`}
            strokeWidth={1.5}
            dot={false}
            strokeDasharray="4 3"
            isAnimationActive={false}
          />
          {compact ? null : <ChartLegend content={<ChartLegendContent />} />}
        </ComposedChart>
      </ChartContainer>
    </section>
  );
}

/**
 * A directional throughput pair (received/sent, read/written) as byte/s rates
 * derived from the worker's per-interval counters, on one shared axis. The
 * secondary direction shares the hue and is dashed, so the pair stays
 * distinguishable without relying on color.
 */
function RatePairChart({
  title,
  data,
  primaryKey,
  secondaryKey,
  primaryLabel,
  secondaryLabel,
  color,
  compact = false,
  readout,
  className,
}: {
  title: string;
  data: MetricDatum[];
  primaryKey: RateKey;
  secondaryKey: RateKey;
  primaryLabel: string;
  secondaryLabel: string;
  color: string;
  compact?: boolean;
  readout?: string;
  className?: string;
}) {
  const config: ChartConfig = {
    [primaryKey]: { label: primaryLabel, color },
    [secondaryKey]: { label: secondaryLabel, color },
  };

  return (
    <section className={cn("space-y-2", className)} aria-label={title}>
      <ChartHeader title={title} readout={readout} />
      <ChartContainer config={config} className={cn("w-full", compact ? "h-36" : "h-44")}>
        <LineChart
          data={data}
          syncId={METRICS_SYNC_ID}
          margin={{ top: 8, right: 8, bottom: 4, left: 0 }}
        >
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="label"
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            minTickGap={28}
            tickMargin={8}
            tick={{ fontSize: 10 }}
          />
          <YAxis
            stroke="var(--muted-foreground)"
            tickLine={false}
            axisLine={false}
            width={compact ? 56 : 76}
            tickCount={compact ? 3 : undefined}
            tickFormatter={(value: number | string) => formatAxisValue(value, formatBytesPerSecond)}
            tick={{ fontSize: 10 }}
          />
          <ChartTooltip content={<ChartTooltipContent formatter={rateTooltipFormatter} />} />
          <Line
            type="monotone"
            dataKey={primaryKey}
            name={primaryLabel}
            stroke={`var(--color-${primaryKey})`}
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey={secondaryKey}
            name={secondaryLabel}
            stroke={`var(--color-${secondaryKey})`}
            strokeWidth={2}
            strokeDasharray="5 3"
            dot={false}
            isAnimationActive={false}
          />
          {compact ? null : <ChartLegend content={<ChartLegendContent />} />}
        </LineChart>
      </ChartContainer>
    </section>
  );
}

function formatCpuValue(
  value: TooltipValueType | undefined,
  point: MetricDatum | undefined,
): string {
  const numberValue = toNumber(value);
  if (numberValue === null) return String(value ?? "");
  const percent = `${numberValue.toFixed(1)}%`;
  if (!point || point.cpuTotal <= 0) return percent;
  return `${percent} (${Math.round(point.cpuUsed)}m / ${Math.round(point.cpuTotal)}m)`;
}

function bytesTooltipFormatter(
  value: TooltipValueType | undefined,
  name: number | string | undefined,
) {
  return tooltipRow(name, formatTooltipValue(value, formatBytes));
}

function rateTooltipFormatter(
  value: TooltipValueType | undefined,
  name: number | string | undefined,
) {
  return tooltipRow(name, formatTooltipValue(value, formatBytesPerSecond));
}

function tooltipRow(name: number | string | undefined, formatted: string) {
  return (
    <div className="flex flex-1 items-center justify-between gap-2 leading-none">
      <span className="text-muted-foreground">{name}</span>
      <span className="font-mono font-medium tabular-nums text-foreground">{formatted}</span>
    </div>
  );
}

function formatAxisValue(value: number | string, formatValue: (value: number) => string): string {
  const numberValue = toNumber(value);
  if (numberValue === null) return String(value);
  return formatValue(numberValue);
}

function formatTooltipValue(
  value: TooltipValueType | undefined,
  formatValue: (value: number) => string,
): string {
  if (value === undefined) return "";
  const numberValue = toNumber(value);
  if (numberValue === null) return String(value);
  return formatValue(numberValue);
}

function toNumber(value: TooltipValueType | number | string | undefined): number | null {
  const numberValue = Array.isArray(value) ? Number(value[0]) : Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}
