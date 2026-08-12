import { useMemo } from "react";
import { format } from "date-fns";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { billableMetrics } from "@/lib/api/schemas/usage";
import type { UsageBillingOverview } from "@/lib/api/schemas";
import { formatCostNanos } from "./usage-report";

// Every billable metric belongs to exactly one band. A metric missing here is
// dropped from the chart while the headline total still counts it, so the bars
// quietly stop adding up to the number above them.
const chartSeries = [
  { metric: "cpu_seconds", label: "CPU", color: "var(--chart-1)", covers: ["cpu_seconds"] },
  {
    metric: "memory_gib_seconds",
    label: "Memory",
    color: "var(--chart-2)",
    covers: ["memory_gib_seconds"],
  },
  { metric: "gpu_seconds", label: "GPU", color: "var(--chart-3)", covers: ["gpu_seconds"] },
  {
    // One band for the fee rather than three: it is charged for managing a
    // fleet, not for the CPU, memory and GPU it is measured from, and the
    // report lines below still break it out by dimension.
    metric: "managed",
    label: "Managed",
    color: "var(--chart-4)",
    covers: ["managed_cpu_seconds", "managed_memory_gib_seconds", "managed_gpu_seconds"],
  },
] as const;

// Makes the comment above enforceable rather than aspirational: a billable
// metric no band covers fails the build here instead of vanishing from the bars.
type CoveredMetric = (typeof chartSeries)[number]["covers"][number];
type UncoveredMetric = Exclude<(typeof billableMetrics)[number], CoveredMetric>;
const _everyBillableMetricHasABand: UncoveredMetric extends never ? true : never = true;
void _everyBillableMetricHasABand;

const seriesByMetric = new Map<string, string>(
  chartSeries.flatMap((series) => series.covers.map((metric) => [metric, series.metric])),
);

const config = Object.fromEntries(
  chartSeries.map((series) => [series.metric, { label: series.label, color: series.color }]),
) satisfies ChartConfig;

export function CostActivityChart({ report }: { report: UsageBillingOverview }) {
  const { data, visibleSeries } = useMemo(() => {
    const totals = new Map<string, number>(chartSeries.map((series) => [series.metric, 0]));
    const rows = report.activity.map((bucket) => {
      const row: Record<string, number | string> = {
        label: format(
          new Date(bucket.start),
          report.activity.length > 32 ? "MMM d" : "MMM d HH:mm",
        ),
      };
      for (const series of chartSeries) row[series.metric] = 0;
      for (const line of bucket.lines) {
        const seriesMetric = seriesByMetric.get(line.metric);
        if (seriesMetric === undefined) continue;
        row[seriesMetric] = Number(row[seriesMetric]) + line.cost_nanos;
        totals.set(seriesMetric, (totals.get(seriesMetric) ?? 0) + line.cost_nanos);
      }
      return row;
    });
    return {
      data: rows,
      visibleSeries: chartSeries.filter((series) => (totals.get(series.metric) ?? 0) > 0),
    };
  }, [report]);

  if (!visibleSeries.length) {
    return (
      <div className="flex h-full min-h-48 items-center justify-center text-sm text-muted-foreground">
        No billable activity in this period
      </div>
    );
  }

  return (
    <ChartContainer config={config} className="h-full min-h-48 w-full p-3">
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 4 }}>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          minTickGap={28}
          stroke="var(--muted-foreground)"
        />
        <YAxis
          tickLine={false}
          axisLine={false}
          width={64}
          stroke="var(--muted-foreground)"
          tickFormatter={(value: number) => formatCostNanos(value, true, report.currency)}
        />
        <ChartTooltip
          content={
            <ChartTooltipContent
              formatter={(value, name, item) => (
                <div className="flex flex-1 items-center justify-between gap-4 leading-none">
                  <span className="flex items-center gap-1.5 text-muted-foreground">
                    <span className="size-2.5 shrink-0" style={{ backgroundColor: item.color }} />
                    {config[String(name)]?.label ?? name}
                  </span>
                  <span className="font-mono font-medium tabular-nums text-foreground">
                    {formatCostNanos(Number(value), false, report.currency)}
                  </span>
                </div>
              )}
            />
          }
        />
        {visibleSeries.length > 1 ? <ChartLegend content={<ChartLegendContent />} /> : null}
        {visibleSeries.map((series) => (
          <Bar
            key={series.metric}
            dataKey={series.metric}
            stackId="cost"
            fill={`var(--color-${series.metric})`}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ChartContainer>
  );
}
