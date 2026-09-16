import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { accountCostSeriesQueryOptions } from "@/lib/queries/usage";

import { UsageCostBreakdown } from "./-components/UsageCostBreakdown";
import { usageRange, usageRangeKeys, type UsageRangeKey } from "./-components/ranges";
import { SpendChart } from "./-components/SpendChart";
import { SpendTotals } from "./-components/SpendTotals";
import { UsageRangeControl } from "./-components/UsageRangeControl";

type UsageSearch = {
  range: UsageRangeKey;
};

export const Route = createFileRoute("/w/$workspace/usage/")({
  validateSearch: (search: Record<string, unknown>): UsageSearch => ({
    range: usageRangeKeys.includes(search.range as UsageRangeKey)
      ? (search.range as UsageRangeKey)
      : "month",
  }),
  component: UsagePage,
  errorComponent: RouteErrorFallback,
});

function UsagePage() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  // Keep the window fixed while paging so every section reads the same interval.
  const range = useMemo(() => usageRange(search.range, new Date()), [search.range]);
  const series = useQuery(accountCostSeriesQueryOptions(range.window, range.bucket));

  return (
    <WorkspacePage
      title="Usage"
      actions={
        <UsageRangeControl
          value={range.key}
          onChange={(next) => void navigate({ search: { range: next }, replace: true })}
        />
      }
      contentClassName="flex min-h-0 flex-col gap-3 overflow-y-auto lg:overflow-hidden"
    >
      <section aria-label="Spend" className="panel shrink-0 overflow-hidden rounded-md">
        <header className="flex flex-wrap items-end justify-between gap-x-8 gap-y-3 px-4 pt-3 pb-2">
          <SpendTotals series={series.data} error={series.error} />
        </header>
        <div className="h-40 px-3 pb-2 sm:h-44">
          <SpendChart window={range.window} bucket={range.bucket} caption={range.caption} />
        </div>
      </section>
      <Panel
        title="Usage by app"
        className="min-h-0 shrink-0 lg:flex-1"
        contentClassName="flex min-h-0 flex-col overflow-hidden"
      >
        <UsageCostBreakdown
          key={`${range.window.start}:${range.window.end}`}
          window={range.window}
          caption={range.caption}
        />
      </Panel>
    </WorkspacePage>
  );
}
