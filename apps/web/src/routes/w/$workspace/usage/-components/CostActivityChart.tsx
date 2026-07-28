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
import type { UsageBillingOverview } from "@/lib/api/schemas";
import { formatCostNanos } from "./usage-report";

const chartSeries = [
  { metric: "cpu_seconds", label: "CPU", color: "var(--chart-1)" },
  { metric: "memory_gib_seconds", label: "Memory", color: "var(--chart-2)" },
  { metric: "gpu_seconds", label: "GPU", color: "var(--chart-3)" },
  { metric: "recorded_compute", label: "Recorded compute", color: "var(--chart-5)" },
  {
    metric: "managed_compute_reservation_seconds",
    label: "Managed compute",
    color: "var(--chart-5)",
  },
  {
    metric: "customer_cloud_management_seconds",
    label: "Customer cloud management",
    color: "var(--chart-4)",
  },
] as const;

const config = Object.fromEntries(
  chartSeries.map((series) => [series.metric, { label: series.label, color: series.color }]),
) satisfies ChartConfig;

export function CostActivityChart({ report }: { report: UsageBillingOverview }) {
  const { data, visibleSeries } = useMemo(() => {
    const totals = new Map(chartSeries.map((series) => [series.metric, 0]));
    const rows = report.activity.map((bucket) => {
      const row: Record<string, number | string> = {
        label: format(
          new Date(bucket.start),
          report.activity.length > 32 ? "MMM d" : "MMM d HH:mm",
        ),
      };
      for (const series of chartSeries) row[series.metric] = 0;
      for (const line of bucket.lines) {
        row[line.metric] = Number(row[line.metric]) + line.cost_nanos;
        totals.set(line.metric, (totals.get(line.metric) ?? 0) + line.cost_nanos);
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
