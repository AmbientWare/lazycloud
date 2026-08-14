import { useMemo } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { Panel } from "@/components/shared/Panel";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { usageCostGroupKeys, type UsageCostGroupKey } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";
import { calendarMonthWindow, usageCostsQueryOptions } from "@/lib/queries/usage";
import { useWorkspace } from "@/lib/workspace-context";

import { AccountStandingPanel } from "./-components/AccountStandingPanel";
import { CostBreakdownTable } from "./-components/CostBreakdownTable";

const LEVEL_TITLES: Record<UsageCostGroupKey, string> = {
  app: "By app",
  workload: "By workload",
  task: "By task",
};

type UsageSearch = {
  view: UsageCostGroupKey;
};

export const Route = createFileRoute("/w/$workspace/usage/")({
  validateSearch: (search: Record<string, unknown>): UsageSearch => ({
    view: usageCostGroupKeys.includes(search.view as UsageCostGroupKey)
      ? (search.view as UsageCostGroupKey)
      : "app",
  }),
  component: UsagePage,
  errorComponent: RouteErrorFallback,
});

function UsagePage() {
  const { workspace } = useWorkspace();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  // The window is the current UTC month, computed once per mount so paging and
  // the total cannot straddle a boundary crossed mid-session.
  const window = useMemo(() => calendarMonthWindow(new Date()), []);
  const total = useInfiniteQuery(
    usageCostsQueryOptions(workspace.id, window, { groupBy: search.view }),
  );
  const first = total.data?.pages[0];

  return (
    <WorkspacePage
      title="Usage"
      description="What this workspace has run this month, and what it cost"
      contentClassName="flex flex-col gap-4 overflow-y-auto pb-1"
    >
      <AccountStandingPanel />
      <Panel
        title="Cost this period"
        description="Read from the priced ledger, dearest first"
        className="min-h-[24rem] flex-1"
        contentClassName="flex flex-col"
        action={
          first ? (
            <span className="font-mono text-sm" aria-label="Total cost this period">
              {formatCostNanos(first.cost_nanos, first.currency)}
            </span>
          ) : null
        }
      >
        <Tabs
          value={search.view}
          onValueChange={(view) =>
            void navigate({ search: { view: view as UsageCostGroupKey }, replace: true })
          }
          className="flex min-h-0 flex-1 flex-col overflow-hidden"
        >
          <LinearTabsList ariaLabel="Cost attribution level" listClassName="sm:flex-none">
            {usageCostGroupKeys.map((level) => (
              <LinearTab key={level} value={level}>
                {LEVEL_TITLES[level]}
              </LinearTab>
            ))}
          </LinearTabsList>
          {usageCostGroupKeys.map((level) => (
            <TabsContent
              key={level}
              value={level}
              className="flex min-h-0 flex-1 flex-col overflow-hidden"
            >
              <CostBreakdownTable workspaceId={workspace.id} window={window} groupBy={level} />
            </TabsContent>
          ))}
        </Tabs>
      </Panel>
    </WorkspacePage>
  );
}
