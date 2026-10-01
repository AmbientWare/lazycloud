import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { SquareTerminal } from "lucide-react";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelError } from "@/components/shared/PanelError";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import { countLabel, formatKind } from "@/lib/format";
import { appSummariesQueryOptions, type AppSummary } from "@/lib/queries/apps";
import { deployedAt } from "@/lib/queries/deployments";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { AppCardActionsTrigger } from "./-components/AppCardActionsTrigger";
import { AppActivityChart } from "./-components/AppActivityChart";
import { appRunActivity } from "./-components/app-activity-buckets";
import { QuickstartEmptyState } from "./-components/QuickstartEmptyState";

export const Route = createFileRoute("/w/$workspace/apps/")({
  component: AppsPage,
  errorComponent: RouteErrorFallback,
});

function AppsPage() {
  const { workspace } = useWorkspace();
  const apps = useQuery(appSummariesQueryOptions(workspace.name));
  const items = apps.data ?? [];

  return (
    <WorkspacePage
      title="Apps"
      pending={apps.isPending}
      description={
        // Gated on the data rather than on pending alone: reported as three
        // zeroes, a failed read of the workspace reads as an empty workspace.
        apps.data ? (
          <PageFacts
            items={[
              countLabel(items.length, "app"),
              countLabel(
                items.reduce((total, item) => total + item.app.workloads, 0),
                "workload",
              ),
              countLabel(
                items.reduce((total, item) => total + item.app.running_containers, 0),
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
        <AppsGrid items={items} workspace={workspace.name} />
      )}
    </WorkspacePage>
  );
}

function AppsGrid({ items, workspace }: { items: AppSummary[]; workspace: string }) {
  return (
    <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="Workspace apps">
      {items.map((item) => (
        <AppCard key={item.app.id} item={item} workspace={workspace} />
      ))}
    </section>
  );
}

function AppCard({ item, workspace }: { item: AppSummary; workspace: string }) {
  const { app, workloads } = item;
  const latest = workloads[0];
  const lastDeployedAt = latest ? deployedAt(latest) : app.created_at;
  const active = app.state === "active";
  // Devboxes are counted on their own, not as pods.
  const kindCounts = new Map<string, number>();
  let devboxCount = 0;
  for (const workload of workloads) {
    if (workload.role === "devbox") devboxCount++;
    else kindCounts.set(workload.kind, (kindCounts.get(workload.kind) ?? 0) + 1);
  }
  const workloadKinds = [...kindCounts].sort(([left], [right]) => left.localeCompare(right));
  const activity = appRunActivity(item.activity ? [item.activity] : undefined);

  return (
    <article className="interactive-panel panel group relative min-w-0 rounded-md">
      <Link
        to="/w/$workspace/apps/$app"
        params={{ workspace, app: app.name }}
        aria-label={app.name}
        className="flex h-full min-h-[17.5rem] flex-col rounded-md p-4 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <header className="flex min-w-0 items-start gap-4 pr-8">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h3 className="truncate text-base font-semibold text-foreground">{app.name}</h3>
              <span className="flex shrink-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                <span
                  className={cn(
                    "size-1.5 rounded-full",
                    active ? "bg-positive" : "bg-muted-foreground",
                  )}
                  aria-hidden="true"
                />
                {active ? "Active" : "Inactive"}
              </span>
            </div>
            <div className="mt-1.5 flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
              <span className="shrink-0">Latest workload</span>
              <span aria-hidden="true">·</span>
              {latest ? (
                <span className="flex min-w-0 items-center gap-1.5 text-foreground">
                  <StubKindIcon kind={latest.kind} className="size-3.5" />
                  <span className="mono truncate">{latest.name}</span>
                </span>
              ) : (
                <span>None</span>
              )}
            </div>
          </div>
        </header>

        <section className="mt-6" aria-label="24 hour activity">
          <div className="micro-label">Tasks · 24 hours</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="readout text-xl leading-none text-foreground">
              {activity.total.toLocaleString()}
            </span>
            <span
              className={cn(
                "text-xs",
                activity.totals.failed > 0 ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {countLabel(activity.totals.failed, "failed", "failed")}
            </span>
          </div>
          <AppActivityChart
            activity={activity}
            label={`${app.name} task volume by outcome over the last 24 hours`}
            className="mt-3"
            chartClassName="h-14 min-w-0"
          />
        </section>

        <div className="mt-5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span>{countLabel(app.running_containers, "running container")}</span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0">
            Deployed <LiveRelativeTime value={lastDeployedAt} />
          </span>
        </div>

        <div className="mt-3 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          {workloadKinds.map(([kind, count]) => (
            <span key={kind} className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <StubKindIcon kind={kind} className="size-3.5" />
              <span>{formatKind(kind)}</span>
              <span className="mono tabular-nums text-foreground">{count}</span>
            </span>
          ))}
          {devboxCount > 0 ? (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <SquareTerminal className="size-3.5 shrink-0" aria-hidden="true" />
              <span>{devboxCount === 1 ? "Devbox" : "Devboxes"}</span>
              <span className="mono tabular-nums text-foreground">{devboxCount}</span>
            </span>
          ) : null}
          {workloadKinds.length === 0 && devboxCount === 0 ? (
            <span className="text-xs text-muted-foreground">None</span>
          ) : null}
        </div>
      </Link>
      <div className="absolute right-3 top-3 z-10">
        <AppCardActionsTrigger app={app} workspace={workspace} />
      </div>
    </article>
  );
}

function AppCardsSkeleton() {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-hidden="true">
      {Array.from({ length: 6 }, (_, index) => (
        <div key={index} className="panel min-h-[17.5rem] rounded-md bg-card p-4">
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
