import { Area, CartesianGrid, ComposedChart, Line, LineChart, XAxis, YAxis } from "recharts";
import type { TooltipValueType } from "recharts";

import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  emptyAxis,
  type ChartConfig,
} from "@/components/ui/chart";
import type { Schemas } from "@/lib/api/client";
import { formatCpu } from "@/lib/format";
import { cn } from "@/lib/utils";

import {
  buildMetricData,
  formatBytes,
  formatBytesPerSecond,
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

/**
 * A container's compute samples. Every chart keeps its size while samples
 * are pending or absent.
 */
export function ContainerMetricsCharts({
  metrics,
  pending = false,
  empty = "No compute samples",
  gpu = false,
  showIo = true,
  className,
}: {
  metrics: Schemas["ContainerMetrics"] | undefined;
  pending?: boolean;
  /** What the charts say while there are no samples to draw. */
  empty?: string;
  /** Draw GPU memory before any sample reports it. */
  gpu?: boolean;
  /** Network and disk throughput; off where a panel shows only what the container holds. */
  showIo?: boolean;
  className?: string;
}) {
  const data = metrics ? buildMetricData(metrics) : [];
  const emptyLabel = pending || data.length > 0 ? undefined : empty;
  const hasGpu = gpu || data.some((point) => point.gpuMemoryTotal > 0);
  const readout = latestComputeReadout(data);
  const latest = data.at(-1);
  const gpuReadout = latest?.gpuMemoryTotal ? formatBytes(latest.gpuMemoryUsed) : undefined;
  const diskReadout =
    latest && latest.diskReadRate !== null && latest.diskWriteRate !== null
      ? `Read ${formatBytesPerSecond(latest.diskReadRate)} · write ${formatBytesPerSecond(latest.diskWriteRate)}`
      : undefined;

  return (
    <div
      aria-busy={pending}
      className={cn("grid grid-cols-1 gap-x-6 gap-y-5 lg:grid-cols-2", className)}
    >
      <CpuChart data={data} readout={readout?.cpu} empty={emptyLabel} />
      <MemoryChart
        title="Memory"
        data={data}
        usedKey="memoryUsed"
        totalKey="memoryTotal"
        usedLabel="RSS"
        color="var(--chart-2)"
        readout={readout?.memory}
        empty={emptyLabel}
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
          empty={emptyLabel}
        />
      ) : null}
      {showIo ? (
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
            empty={emptyLabel}
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
            empty={emptyLabel}
          />
        </>
      ) : null}
    </div>
  );
}

/** Charts with no samples draw 0 to 1 CPU, 0 to 1 GiB and 0 to 1 MiB/s. */
const EMPTY_CPU_TICKS = [0, 250, 500, 750, 1000];
const EMPTY_BYTE_TICKS = [0, 256, 512, 768, 1024].map((mebibytes) => mebibytes * 1024 ** 2);
const EMPTY_RATE_TICKS = [0, 256, 512, 768, 1024].map((kibibytes) => kibibytes * 1024);

/** Metric name and latest-sample readout. */
function ChartHeader({ title, readout }: { title: string; readout?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <div className="text-xs font-medium text-muted-foreground">{title}</div>
      {readout ? <div className="mono text-xs tabular-nums text-foreground">{readout}</div> : null}
    </div>
  );
}

/** CPU used against a dashed reservation line on one CPU axis; bursts above the reservation show. */
function CpuChart({
  data,
  readout,
  empty,
}: {
  data: MetricDatum[];
  readout?: string;
  empty?: string;
}) {
  const config: ChartConfig = {
    cpuUsed: { label: "Used", color: "var(--chart-1)" },
    cpuTotal: { label: "Reserved", color: "var(--muted-foreground)" },
  };

  return (
    <section className="space-y-2" aria-label="CPU">
      <ChartHeader title="CPU" readout={readout} />
      <ChartContainer config={config} className="h-44 w-full" empty={empty}>
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
            width={76}
            {...emptyAxis(data.length === 0, EMPTY_CPU_TICKS)}
            tickFormatter={(value: number | string) => formatAxisValue(value, formatCpu)}
            tick={{ fontSize: 10 }}
          />
          <ChartTooltip content={<ChartTooltipContent formatter={cpuTooltipFormatter} />} />
          <Area
            type="monotone"
            dataKey="cpuUsed"
            name="Used"
            stroke="var(--color-cpuUsed)"
            fill="var(--color-cpuUsed)"
            fillOpacity={0.18}
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="cpuTotal"
            name="Reserved"
            stroke="var(--color-cpuTotal)"
            strokeWidth={1.5}
            dot={false}
            strokeDasharray="4 3"
            isAnimationActive={false}
          />
          <ChartLegend content={<ChartLegendContent />} />
        </ComposedChart>
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
  readout,
  empty,
}: {
  title: string;
  data: MetricDatum[];
  usedKey: BytesKey;
  totalKey: BytesKey;
  usedLabel: string;
  /** Per-metric hue, beam-style; the capacity line stays muted. */
  color: string;
  readout?: string;
  empty?: string;
}) {
  const config: ChartConfig = {
    [usedKey]: { label: usedLabel, color },
    [totalKey]: { label: "Total", color: "var(--muted-foreground)" },
  };

  return (
    <section className="space-y-2" aria-label={title}>
      <ChartHeader title={title} readout={readout} />
      <ChartContainer config={config} className="h-44 w-full" empty={empty}>
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
            width={76}
            {...emptyAxis(data.length === 0, EMPTY_BYTE_TICKS)}
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
          <ChartLegend content={<ChartLegendContent />} />
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
  readout,
  empty,
}: {
  title: string;
  data: MetricDatum[];
  primaryKey: RateKey;
  secondaryKey: RateKey;
  primaryLabel: string;
  secondaryLabel: string;
  color: string;
  readout?: string;
  empty?: string;
}) {
  const config: ChartConfig = {
    [primaryKey]: { label: primaryLabel, color },
    [secondaryKey]: { label: secondaryLabel, color },
  };

  return (
    <section className="space-y-2" aria-label={title}>
      <ChartHeader title={title} readout={readout} />
      <ChartContainer config={config} className="h-44 w-full" empty={empty}>
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
            width={76}
            {...emptyAxis(data.length === 0, EMPTY_RATE_TICKS)}
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
          <ChartLegend content={<ChartLegendContent />} />
        </LineChart>
      </ChartContainer>
    </section>
  );
}

function cpuTooltipFormatter(
  value: TooltipValueType | undefined,
  name: number | string | undefined,
) {
  return tooltipRow(name, formatTooltipValue(value, formatCpu));
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
