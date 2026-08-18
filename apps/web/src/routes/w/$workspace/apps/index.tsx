import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { countLabel } from "@/components/shared/WorkspacePage/countLabel";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import { Skeleton } from "@/components/ui/skeleton";
import type { AppSummary } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { AppCardActionsTrigger } from "./-components/AppCardActionsTrigger";
import { ActivitySparkline } from "./-components/ActivitySparkline";
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
        apps.isPending ? null : (
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
        )
      }
      contentClassName="overflow-y-auto pr-1"
    >
      {apps.isPending ? (
        <AppCardsSkeleton />
      ) : apps.isError ? (
        <div className="panel flex min-h-48 items-center justify-center rounded-md p-4 text-sm text-destructive">
          {apps.error.message}
        </div>
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
  const latest = item.latest_workload;
  const lastDeployedAt = item.last_deployed_at ?? item.app.updated_at;
  const workloadKinds = Object.entries(item.workload_kinds).sort(([left], [right]) =>
    left.localeCompare(right),
  );

  return (
    <article className="interactive-panel panel group relative min-w-0 rounded-md">
      <Link
        to="/w/$workspace/apps/$appId"
        params={{ workspace: workspaceName, appId: item.app.id }}
        aria-label={item.app.name}
        className="flex h-full min-h-[17.5rem] flex-col rounded-md p-4 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
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
          <div>
            <div>
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
                  {formatCount(item.failed_runs_24h, "failed", "failed")}
                </span>
              </div>
            </div>
          </div>
          <ActivitySparkline
            values={normalizedActivity(item.activity_24h)}
            failures={normalizedActivity(item.failures_24h)}
            label={`${item.app.name} task and failure activity over the last 24 hours`}
            className="mt-3 h-14 min-w-0"
          />
        </section>

        <div className="mt-5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span>
            {formatCount(item.running_containers, "running container", "running containers")}
          </span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0" title={exactTime(lastDeployedAt)}>
            Deployed <time dateTime={lastDeployedAt}>{relativeTime(lastDeployedAt)}</time>
          </span>
        </div>

        <div className="mt-3 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="micro-label shrink-0">
            {formatCount(item.workload_count, "workload", "workloads")}
          </span>
          {workloadKinds.map(([kind, count]) => (
            <span key={kind} className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <StubKindIcon kind={kind} className="size-3.5" />
              <span>{formatKind(kind)}</span>
              <span className="mono tabular-nums text-foreground">{count}</span>
            </span>
          ))}
          {workloadKinds.length === 0 ? (
            <span className="text-xs text-muted-foreground">None</span>
          ) : null}
        </div>
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

function normalizedActivity(values: number[]): number[] {
  return values.length === 24
    ? values
    : [...Array.from({ length: Math.max(24 - values.length, 0) }, () => 0), ...values].slice(-24);
}

function formatCount(value: number, singular: string, plural: string): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

function formatKind(kind: string): string {
  return kind
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function exactTime(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}
