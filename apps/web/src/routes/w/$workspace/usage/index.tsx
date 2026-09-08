import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { Panel } from "@/components/shared/Panel";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { countLabel } from "@/lib/format";
import { formatCostNanos } from "@/lib/money";
import { accountCostSeriesQueryOptions } from "@/lib/queries/usage";
import { useWorkspace } from "@/lib/workspace-context";
import { cn } from "@/lib/utils";

import { AccountCeilingLine } from "./-components/AccountCeilingLine";
import { AppCostAccordion } from "./-components/AppCostAccordion";
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
  const { workspaces } = useWorkspace();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const [byCategory, setByCategory] = useState(false);
  // Keep the window fixed while paging so every section reads the same interval.
  const range = useMemo(() => usageRange(search.range, new Date()), [search.range]);
  const series = useQuery(accountCostSeriesQueryOptions(range.window, range.bucket));

  return (
    <WorkspacePage
      title="Usage"
      description={
        <>
          <PageFacts
            items={[
              series.data ? formatCostNanos(series.data.cost_nanos, series.data.currency) : null,
              countLabel(workspaces.length, "workspace"),
              range.caption,
            ]}
          />
          <AccountCeilingLine />
        </>
      }
      actions={
        <UsageRangeControl
          value={range.key}
          onChange={(next) => void navigate({ search: { range: next }, replace: true })}
        />
      }
      contentClassName="flex min-h-0 flex-col gap-3 overflow-y-auto lg:overflow-hidden"
    >
      <Panel
        title="Spend over time"
        description={`${range.bucket === "hour" ? "Hourly" : "Daily"}, UTC`}
        action={
          <div
            role="group"
            aria-label="Chart breakdown"
            className="flex shrink-0 gap-0.5 rounded-md border border-input bg-card p-0.5"
          >
            {[false, true].map((category) => (
              <button
                key={String(category)}
                type="button"
                aria-pressed={byCategory === category}
                onClick={() => setByCategory(category)}
                className={cn(
                  "h-6 rounded-[3px] px-2 text-xs whitespace-nowrap outline-none transition-colors",
                  "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
                  byCategory === category
                    ? "bg-accent font-medium text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {category ? "By category" : "Total"}
              </button>
            ))}
          </div>
        }
        // The chart's flex content needs an explicit height on its parent.
        className="h-60 shrink-0 sm:h-72"
        contentClassName="overflow-hidden p-3"
      >
        <SpendChart
          window={range.window}
          bucket={range.bucket}
          caption={range.caption}
          byCategory={byCategory}
        />
      </Panel>
      <SpendTotals series={series.data} error={series.error} />
      <Panel
        title="Apps"
        description="Open an app to see its workloads"
        className="min-h-[22rem] flex-1 lg:min-h-0"
        contentClassName="flex min-h-0 flex-col overflow-hidden"
      >
        <AppCostAccordion window={range.window} caption={range.caption} />
      </Panel>
    </WorkspacePage>
  );
}
