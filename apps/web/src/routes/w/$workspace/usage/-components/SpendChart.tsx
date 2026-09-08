import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { ChartContainer, ChartTooltip, type ChartConfig } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { billedDimensions, type BilledDimension, type UsageCostBucket } from "@/lib/api/schemas";
import { exactDollars, formatCostNanos } from "@/lib/money";
import { accountCostSeriesQueryOptions, type UsageCostWindow } from "@/lib/queries/usage";

import { intervalLabel } from "./ranges";
import { COST_DIMENSIONS } from "./cost-colors";

const CHART_CONFIG: ChartConfig = {
  cost: { label: "Cost", color: "var(--brand)" },
  ...COST_DIMENSIONS,
};

type Interval = Record<BilledDimension, number> & {
  started_at: string;
  cost: number;
  dimensions: { dimension: BilledDimension; cost_nanos: number }[];
};

export function SpendChart({
  window,
  bucket,
  caption,
  byCategory,
}: {
  window: UsageCostWindow;
  bucket: UsageCostBucket;
  caption: string;
  byCategory: boolean;
}) {
  const series = useQuery(accountCostSeriesQueryOptions(window, bucket));

  if (series.isPending) {
    return <ChartSkeleton />;
  }
  if (series.isError) {
    return <PanelError message={series.error.message} layout="centered" />;
  }
  if (series.data.cost_nanos === 0) {
    return (
      <PanelEmpty
        message={`Nothing was billed during ${caption}`}
        detail="Spend appears a few minutes after a workload runs."
        className="h-full min-h-40"
      />
    );
  }

  const currency = series.data.currency;
  const data: Interval[] = series.data.data.map((interval) => {
    const row: Interval = {
      started_at: interval.started_at,
      cost: interval.cost_nanos,
      dimensions: interval.dimensions,
      compute_runtime: 0,
      volume_storage: 0,
      network_egress: 0,
    };
    for (const total of interval.dimensions) row[total.dimension] = total.cost_nanos;
    return row;
  });

  return (
    <ChartContainer
      config={CHART_CONFIG}
      className="aspect-auto h-full w-full"
      aria-label={byCategory ? "Spend over time by category" : "Total spend over time"}
    >
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis
          dataKey="started_at"
          tickFormatter={(value: string) => intervalLabel(value, bucket)}
          stroke="var(--muted-foreground)"
          tickLine={false}
          axisLine={false}
          minTickGap={20}
          tickMargin={8}
          tick={{ fontSize: 10 }}
        />
        <YAxis
          stroke="var(--muted-foreground)"
          tickLine={false}
          axisLine={false}
          width={62}
          tick={{ fontSize: 10 }}
          tickFormatter={(value: number | string) => exactDollars(Number(value) || 0)}
        />
        <ChartTooltip
          cursor={{ fill: "var(--accent)", opacity: 0.5 }}
          content={({ active, label }) => {
            const interval = data.find((row) => row.started_at === label);
            if (!active || !interval) return null;
            return (
              <div className="grid min-w-48 gap-2 rounded-lg border border-border/50 bg-background px-2.5 py-2 text-xs shadow-xl">
                <p className="text-muted-foreground">
                  {intervalLabel(interval.started_at, bucket)}
                </p>
                <IntervalBreakdown
                  cost={interval.cost}
                  dimensions={interval.dimensions}
                  currency={currency}
                />
              </div>
            );
          }}
        />
        {byCategory ? (
          billedDimensions.map((dimension) => (
            <Bar
              key={dimension}
              dataKey={dimension}
              name={COST_DIMENSIONS[dimension].label}
              stackId="spend"
              fill={`var(--color-${dimension})`}
              maxBarSize={26}
              isAnimationActive={false}
            />
          ))
        ) : (
          <Bar
            dataKey="cost"
            name="Cost"
            fill="var(--color-cost)"
            radius={[3, 3, 0, 0]}
            maxBarSize={26}
            isAnimationActive={false}
          />
        )}
      </BarChart>
    </ChartContainer>
  );
}

/** What one interval cost, and which invoice lines it was charged under. */
function IntervalBreakdown({
  cost,
  dimensions,
  currency,
}: {
  cost: number;
  dimensions: Interval["dimensions"];
  currency: string;
}) {
  return (
    <div className="flex flex-1 flex-col gap-1">
      <div className="flex items-center justify-between gap-6 leading-none">
        <span className="text-muted-foreground">Cost</span>
        <span className="mono font-medium tabular-nums text-foreground">
          {formatCostNanos(cost, currency)}
        </span>
      </div>
      {dimensions.map((total) => (
        <div
          key={total.dimension}
          className="flex items-center justify-between gap-6 leading-none text-muted-foreground"
        >
          <span className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-[2px]"
              style={{ backgroundColor: COST_DIMENSIONS[total.dimension].color }}
            />
            {COST_DIMENSIONS[total.dimension].label}
          </span>
          <span className="mono tabular-nums">{formatCostNanos(total.cost_nanos, currency)}</span>
        </div>
      ))}
    </div>
  );
}

function ChartSkeleton() {
  return (
    <div className="flex h-full min-h-0 items-end gap-1 px-4 pb-6 pt-4" aria-hidden="true">
      {[38, 62, 24, 80, 46, 70, 33, 55, 28, 64].map((height, index) => (
        <Skeleton key={index} className="min-w-0 flex-1" style={{ height: `${height}%` }} />
      ))}
    </div>
  );
}
