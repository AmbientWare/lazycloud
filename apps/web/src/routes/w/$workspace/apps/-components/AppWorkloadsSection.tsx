import { Link } from "@tanstack/react-router";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { WorkloadSummary } from "@/lib/api/schemas";
import { formatKind } from "@/lib/format";
import { WorkloadRowActions } from "./WorkloadRowActions";

export function AppWorkloadsSection({
  workspaceId,
  workspaceName,
  appId,
  workloads,
  kind,
  kinds,
  onKindChange,
  pending,
  error,
  nextCursor,
  loadingMore,
  loadMoreError,
  onLoadMore,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloads: WorkloadSummary[];
  kind: string | undefined;
  kinds: string[];
  onKindChange: (kind: string | undefined) => void;
  pending: boolean;
  error: string | undefined;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  onLoadMore: () => void;
}) {
  return (
    <Panel
      action={
        <Select
          value={kind ?? "all"}
          onValueChange={(next) => onKindChange(next === "all" ? undefined : next)}
        >
          <SelectTrigger aria-label="Workload type" size="sm" className="w-36 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent align="end">
            <SelectItem value="all">All types</SelectItem>
            {kinds.map((option) => (
              <SelectItem key={option} value={option}>
                {formatKind(option)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      }
      title="Workloads"
      className="min-h-[26rem] lg:h-full lg:min-h-0"
      contentClassName="p-0"
    >
      {pending ? (
        <RowsSkeleton rows={4} height="h-14" />
      ) : error ? (
        <PanelError message={error} />
      ) : workloads.length === 0 ? (
        <PanelEmpty
          message={kind ? "No workloads match this type" : "No deployed workloads for this app"}
          className="min-h-48"
        />
      ) : (
        <div className="divide-y divide-border/80">
          {workloads.map((workload) => {
            const deployment = workload.deployment;
            return (
              <div
                key={deployment.name}
                className="interactive-row flex min-w-0 items-center gap-2 px-3"
              >
                <Link
                  to="/w/$workspace/apps/$appId/workloads/$name"
                  params={{ workspace: workspaceName, appId, name: deployment.name }}
                  className="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-2 py-3 outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="flex min-w-0 flex-1 items-center gap-2.5">
                    <StubKindIcon kind={deployment.kind} className="size-3.5 shrink-0" />
                    <span className="min-w-0">
                      <span className="mono block truncate text-sm font-medium">
                        {deployment.name}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        {formatKind(deployment.kind)} · v{deployment.version}
                      </span>
                    </span>
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {workload.running_containers} running
                  </span>
                  <StatusChip status={deployment.active ? "deployed" : "inactive"} />
                  <LiveRelativeTime
                    value={deployment.created_at}
                    className="hidden text-xs text-muted-foreground xl:block"
                  />
                </Link>
                <WorkloadRowActions workload={workload} workspaceId={workspaceId} appId={appId} />
              </div>
            );
          })}
        </div>
      )}
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={loadingMore}
        error={loadMoreError}
        onLoadMore={onLoadMore}
        resourceLabel="workloads"
      />
    </Panel>
  );
}
