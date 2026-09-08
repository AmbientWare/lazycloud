import { PanelError } from "@/components/shared/PanelError";
import { Skeleton } from "@/components/ui/skeleton";
import { billedDimensions, type UsageCostSeries } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";

import { COST_DIMENSIONS } from "./cost-colors";

export function SpendTotals({
  series,
  error,
}: {
  series: UsageCostSeries | undefined;
  error: Error | null;
}) {
  if (error) return <PanelError message={error.message} />;

  return (
    <dl
      aria-label="Spend by category"
      className="panel flex shrink-0 flex-wrap gap-x-8 gap-y-3 rounded-md px-4 py-3"
    >
      {billedDimensions.map((dimension) => {
        const totals = series?.data.flatMap((interval) =>
          interval.dimensions.filter((total) => total.dimension === dimension),
        );
        return (
          <div key={dimension} className="min-w-32 flex-1">
            <dt className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <span
                aria-hidden="true"
                className="size-2 shrink-0 rounded-[2px]"
                style={{ backgroundColor: COST_DIMENSIONS[dimension].color }}
              />
              {COST_DIMENSIONS[dimension].label}
            </dt>
            <dd className="mt-1">
              {!series ? (
                <Skeleton className="h-5 w-20" />
              ) : totals?.length ? (
                <span className="readout text-sm tabular-nums">
                  {formatCostNanos(
                    totals.reduce((sum, total) => sum + total.cost_nanos, 0),
                    series.currency,
                  )}
                </span>
              ) : (
                <span className="text-xs text-muted-foreground">No metered usage</span>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
