import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import type { BilledDimension, UsageCostBucket } from "@/lib/api/schemas";
import { exactDollars, formatCostNanos } from "@/lib/money";
import { accountCostSeriesQueryOptions, type UsageCostWindow } from "@/lib/queries/usage";

import { intervalLabel } from "./ranges";

const DIMENSION_LABELS: Record<BilledDimension, string> = {
  compute_runtime: "Compute",
  network_egress: "Egress",
  volume_storage: "Volume storage",
};

const CHART_CONFIG: ChartConfig = {
  cost: { label: "Cost", color: "var(--brand)" },
};

type Interval = {
  label: string;
  cost: number;
  dimensions: { dimension: BilledDimension; cost_nanos: number }[];
};

/**
 * What the account spent, interval by interval, over the range the page is set
 * to.
 *
 * One series rather than a bar stacked by invoice line: the page already answers
 * what the money went on, per app and per workload, in the region below this
 * one. Here the question is when, and a three-colour stack would spend the
 * page's only accent on a distinction it is not being read for — most of which
 * is one colour anyway on an account whose spend is compute. The composition is
 * still a hover away.
 */
export function SpendChart({
  window,
  bucket,
  caption,
}: {
  window: UsageCostWindow;
  bucket: UsageCostBucket;
  caption: string;
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
        message={`Nothing was billed over ${caption}`}
        detail="Spend appears here within a few minutes of a workload running."
        className="h-full min-h-40"
      />
    );
  }

  const currency = series.data.currency;
  const data: Interval[] = series.data.data.map((interval) => ({
    label: intervalLabel(interval.started_at, bucket),
    cost: interval.cost_nanos,
    dimensions: interval.dimensions,
  }));

  return (
    <ChartContainer config={CHART_CONFIG} className="aspect-auto h-full w-full">
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis
          dataKey="label"
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
          content={
            <ChartTooltipContent
              labelClassName="text-muted-foreground"
              formatter={(value, _name, item) => (
                <IntervalBreakdown
                  cost={Number(value) || 0}
                  dimensions={(item.payload as Interval | undefined)?.dimensions ?? []}
                  currency={currency}
                />
              )}
            />
          }
        />
        <Bar
          dataKey="cost"
          name="Cost"
          fill="var(--color-cost)"
          radius={[3, 3, 0, 0]}
          maxBarSize={26}
          isAnimationActive={false}
        />
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
          <span>{DIMENSION_LABELS[total.dimension]}</span>
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
