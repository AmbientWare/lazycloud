import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { PanelError } from "@/components/shared/PanelError";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import type { AppSummary } from "@/lib/api/schemas";
import { countLabel } from "@/lib/format";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { AppCardActionsTrigger } from "./-components/AppCardActionsTrigger";
import { AppActivityChart } from "./-components/AppActivityChart";
import { appRunActivityFromSeries } from "./-components/app-activity-buckets";
import { QuickstartEmptyState } from "./-components/QuickstartEmptyState";

export const Route = createFileRoute("/w/$workspace/apps/")({
  component: AppsPage,
  errorComponent: RouteErrorFallback,
});

function AppsPage() {
  const { workspace } = useWorkspace();
  const apps = useQuery(appSummariesQueryOptions(workspace.id));
  const items = apps.data?.items ?? [];

  return (
    <WorkspacePage
      title="Apps"
      description={
        // Gated on the data rather than on pending alone: reported as three
        // zeroes, a failed read of the workspace reads as an empty workspace.
        apps.data ? (
          <PageFacts
            items={[
              countLabel(items.length, "app"),
              countLabel(
                items.reduce((total, item) => total + item.workload_count, 0),
                "workload",
              ),
              countLabel(
                items.reduce((total, item) => total + item.running_containers, 0),
                "running container",
              ),
            ]}
          />
        ) : null
      }
      contentClassName="overflow-y-auto pr-1"
    >
      {apps.isPending ? (
        <AppCardsSkeleton />
      ) : apps.isError ? (
        <PanelError message={apps.error.message} layout="framed" />
      ) : items.length === 0 ? (
        <QuickstartEmptyState />
      ) : (
        <AppsGrid items={items} workspaceId={workspace.id} workspaceName={workspace.name} />
      )}
    </WorkspacePage>
  );
}

function AppsGrid({
  items,
  workspaceId,
  workspaceName,
}: {
  items: AppSummary[];
  workspaceId: string;
  workspaceName: string;
}) {
  return (
    <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="Workspace apps">
      {items.map((item) => (
        <AppCard
          key={item.app.id}
          item={item}
          workspaceId={workspaceId}
          workspaceName={workspaceName}
        />
      ))}
    </section>
  );
}

function AppCard({
  item,
  workspaceId,
  workspaceName,
}: {
  item: AppSummary;
  workspaceId: string;
  workspaceName: string;
}) {
  return (
    <article className="interactive-panel panel group relative min-w-0 rounded-md">
      <Link
        to="/w/$workspace/apps/$appId"
        params={{ workspace: workspaceName, appId: item.app.id }}
        aria-label={item.app.name}
        className="flex h-full min-h-[13rem] flex-col rounded-md p-4 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <header className="flex min-w-0 items-start gap-4 pr-8">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h3 className="truncate text-base font-semibold text-foreground">{item.app.name}</h3>
              <span className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                <span
                  className={cn(
                    "size-1.5 rounded-full",
                    item.app.active ? "bg-positive" : "bg-muted-foreground",
                  )}
                  aria-hidden="true"
                />
                {item.app.active ? "Active" : "Inactive"}
              </span>
            </div>
          </div>
        </header>

        <section className="mt-6" aria-label="24 hour activity">
          <div className="micro-label">Tasks · 24 hours</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="readout text-xl leading-none text-foreground">
              {item.runs_24h.toLocaleString()}
            </span>
            <span
              className={cn(
                "text-xs",
                item.failed_runs_24h > 0 ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {countLabel(item.failed_runs_24h, "failed", "failed")}
            </span>
          </div>
          <AppActivityChart
            activity={appRunActivityFromSeries({
              activity: item.activity_24h,
              failures: item.failures_24h,
              pending: item.pending_24h,
              succeeded: item.succeeded_24h,
            })}
            label={`${item.app.name} task volume by outcome over the last 24 hours`}
            className="mt-3"
            chartClassName="h-14 min-w-0"
          />
        </section>

        <p className="mt-4 text-xs text-muted-foreground">
          {countLabel(item.workload_count, "workload")}
        </p>
      </Link>
      <div className="absolute right-3 top-3 z-10">
        <AppCardActionsTrigger app={item.app} workspaceId={workspaceId} />
      </div>
    </article>
  );
}

function AppCardsSkeleton() {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-hidden="true">
      {Array.from({ length: 6 }, (_, index) => (
        <div key={index} className="panel min-h-[13rem] rounded-md bg-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-2">
              <Skeleton className="h-4 w-36" />
              <Skeleton className="h-3 w-48" />
            </div>
            <Skeleton className="size-4" />
          </div>
          <div className="mt-6 space-y-3">
            <Skeleton className="h-8 w-28" />
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-3 w-full" />
          </div>
          <div className="mt-5 space-y-2">
            <Skeleton className="h-3 w-3/4" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        </div>
      ))}
    </div>
  );
}
