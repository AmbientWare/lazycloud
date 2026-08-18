import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Check, ChevronRight, Loader2, Minus, Plus, Server } from "lucide-react";
import { toast } from "sonner";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
import { countLabel } from "@/components/shared/WorkspacePage/countLabel";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { Container, Deployment } from "@/lib/api/schemas";
import { scaleDeploymentMutationOptions } from "@/lib/queries/apps";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { podInstanceUptime } from "./pod-instance-format";

export const ACTIVE_POD_CONTAINER_STATUSES = ["pending", "running"] as const;
const ACTIVE_CONTAINER_STATUSES = new Set<string>(ACTIVE_POD_CONTAINER_STATUSES);
export type PodInstanceStatusFilter = "active" | "all";

export function PodInstances({
  workspaceId,
  workspaceName,
  appId,
  workloadName,
  deployment,
  statusFilter,
  onStatusFilterChange,
  containers,
  loading,
  error,
  nextCursor,
  loadingMore,
  loadMoreError,
  onLoadMore,
}: {
  workspaceId: string;
  workspaceName: string;
  appId: string;
  workloadName: string;
  deployment: Deployment;
  statusFilter: PodInstanceStatusFilter;
  onStatusFilterChange: (filter: PodInstanceStatusFilter) => void;
  containers: Container[];
  loading: boolean;
  error: Error | null;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  onLoadMore: () => void;
}) {
  const instances = useMemo(() => orderInstances(containers), [containers]);
  const active = instances.filter((container) =>
    ACTIVE_CONTAINER_STATUSES.has(container.status),
  ).length;
  const running = instances.filter((container) => container.status === "running").length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-11 shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-border/80 px-3 py-2">
        {/* The header already states how many are running; what it cannot say is
            how many the Pod was told to hold. */}
        <p className="text-[11px] text-muted-foreground">
          {countLabel(active, "active", "active")} · {configuredReplicaLabel(deployment)} configured
        </p>
        <InstanceControls
          workspaceId={workspaceId}
          appId={appId}
          deployment={deployment}
          running={running}
          statusFilter={statusFilter}
          onStatusFilterChange={onStatusFilterChange}
        />
      </div>
      <div className="flex min-h-0 flex-1 flex-col bg-muted/10">
        <InstanceListHeader />
        <div className="min-h-0 flex-1 overflow-y-auto" role="list" aria-label="Pod instances">
          {loading ? (
            <InstanceListSkeleton />
          ) : error ? (
            <PanelError message={error.message} />
          ) : instances.length ? (
            <>
              {instances.map((container) => (
                <InstanceRow
                  key={container.id}
                  workspaceName={workspaceName}
                  appId={appId}
                  workloadName={workloadName}
                  container={container}
                />
              ))}
              <InfiniteScrollBoundary
                nextCursor={nextCursor}
                loading={loadingMore}
                error={loadMoreError}
                onLoadMore={onLoadMore}
                resourceLabel="instances"
              />
            </>
          ) : (
            <PanelEmpty
              icon={Server}
              message={statusFilter === "active" ? "No active instances" : "No instances"}
              detail={
                statusFilter === "active"
                  ? "Scale this Pod above zero to start an instance."
                  : "This Pod has no recorded instances yet."
              }
              className="h-full min-h-36 px-6"
            />
          )}
        </div>
      </div>
    </div>
  );
}

function InstanceControls({
  workspaceId,
  appId,
  deployment,
  running,
  statusFilter,
  onStatusFilterChange,
}: {
  workspaceId: string;
  appId: string;
  deployment: Deployment;
  running: number;
  statusFilter: PodInstanceStatusFilter;
  onStatusFilterChange: (value: PodInstanceStatusFilter) => void;
}) {
  return (
    <div
      className="flex w-full min-w-0 items-center justify-end gap-1 sm:w-auto"
      aria-label="Instance controls"
    >
      <ReplicaControl
        workspaceId={workspaceId}
        appId={appId}
        deployment={deployment}
        running={running}
      />
      <InstanceStatusFilter value={statusFilter} onChange={onStatusFilterChange} />
    </div>
  );
}

function ReplicaControl({
  workspaceId,
  appId,
  deployment,
  running,
}: {
  workspaceId: string;
  appId: string;
  deployment: Deployment;
  running: number;
}) {
  const configured = deployment.scaling?.max_replicas ?? running;
  const [draft, setDraft] = useState<number | null>(null);
  const replicas = draft ?? configured;
  const queryClient = useQueryClient();
  const scale = useMutation({
    ...scaleDeploymentMutationOptions(workspaceId, deployment.id, replicas),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.deployments.root(workspaceId),
        }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.containers.root(workspaceId),
          refetchType: "active",
        }),
        queryClient.invalidateQueries({
          queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId),
        }),
      ]);
      setDraft(null);
      toast.success(`Configured ${replicas} ${replicas === 1 ? "replica" : "replicas"}`);
    },
    onError: (error) => toast.error("Pod could not be scaled", { description: error.message }),
  });

  if (!deployment.actions.can_scale) {
    return (
      <div className="text-right text-xs text-muted-foreground">
        <span className="mono text-foreground">{configuredReplicaLabel(deployment)}</span>{" "}
        configured
      </div>
    );
  }

  const changed = draft !== null && replicas !== configured;
  return (
    <div className="flex min-w-0 items-center gap-1" aria-label="Replica control">
      <span className="hidden text-xs text-muted-foreground xl:inline">Replicas</span>
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="size-7"
        aria-label="Decrease replicas"
        title="Decrease replicas"
        disabled={scale.isPending || replicas === 0}
        onClick={() => setDraft(Math.max(0, replicas - 1))}
      >
        <Minus />
      </Button>
      <output
        aria-label="Configured replicas"
        aria-live="polite"
        className="mono flex h-7 w-10 items-center justify-center rounded-md border border-input bg-background px-1 text-center text-xs tabular-nums"
      >
        {replicas}
      </output>
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="size-7"
        aria-label="Increase replicas"
        title="Increase replicas"
        disabled={scale.isPending}
        onClick={() => setDraft(replicas + 1)}
      >
        <Plus />
      </Button>
      <Button
        type="button"
        size="sm"
        className="w-16 gap-1 px-1"
        aria-label="Apply"
        disabled={!changed || scale.isPending}
        onClick={() => scale.mutate()}
      >
        {scale.isPending ? (
          <Loader2 className="animate-spin" />
        ) : scale.isSuccess ? (
          <Check />
        ) : null}
        Apply
      </Button>
    </div>
  );
}

function InstanceStatusFilter({
  value,
  onChange,
}: {
  value: PodInstanceStatusFilter;
  onChange: (value: PodInstanceStatusFilter) => void;
}) {
  return (
    <Select
      value={value}
      onValueChange={(next) => {
        if (next === "active" || next === "all") onChange(next);
      }}
    >
      <SelectTrigger
        aria-label="Instance status"
        size="sm"
        className="h-7 w-20 bg-background px-2 text-xs"
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="end">
        <SelectItem value="active">Active</SelectItem>
        <SelectItem value="all">All</SelectItem>
      </SelectContent>
    </Select>
  );
}

function InstanceListHeader() {
  return (
    <div className="hidden min-h-9 shrink-0 grid-cols-[minmax(7rem,1fr)_5rem_5.5rem_1rem] items-center gap-3 border-b border-border/80 px-3 text-[10px] font-medium text-muted-foreground lg:grid">
      <span>Instance</span>
      <span>State</span>
      <span>Uptime</span>
      <span className="sr-only">Open</span>
    </div>
  );
}

function InstanceRow({
  workspaceName,
  appId,
  workloadName,
  container,
}: {
  workspaceName: string;
  appId: string;
  workloadName: string;
  container: Container;
}) {
  const running = container.status === "running";
  return (
    <div role="listitem" className="border-b border-border/70 last:border-b-0">
      <Link
        to="/w/$workspace/apps/$appId/workloads/$name/instances/$containerId"
        params={{
          workspace: workspaceName,
          appId,
          name: workloadName,
          containerId: container.id,
        }}
        aria-label={`Open instance ${container.id}`}
        className="group grid min-h-12 grid-cols-[minmax(8rem,1fr)_auto] items-center gap-x-3 px-3 py-2 text-left outline-none transition-colors hover:bg-accent/55 focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring lg:grid-cols-[minmax(7rem,1fr)_5rem_5.5rem_1rem]"
      >
        <span className="min-w-0">
          <span className="mono block truncate text-sm font-medium">{container.id}</span>
          <span className="mt-1 block lg:hidden">
            <StatusChip status={container.status} live={running} />
          </span>
        </span>
        <span className="hidden lg:block">
          <StatusChip status={container.status} live={running} />
        </span>
        <span className="mono hidden truncate text-xs tabular-nums text-muted-foreground lg:block">
          {podInstanceUptime(container)}
        </span>
        <ChevronRight
          className="size-4 justify-self-end text-muted-foreground transition-colors group-hover:text-foreground"
          aria-hidden="true"
        />
      </Link>
    </div>
  );
}

function InstanceListSkeleton() {
  return (
    <div aria-hidden="true">
      {Array.from({ length: 3 }, (_, index) => (
        <div
          key={index}
          className="flex min-h-12 items-center gap-3 border-b border-border/70 px-3"
        >
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-3.5 w-24" />
            <Skeleton className="h-3 w-16" />
          </div>
          <Skeleton className="h-4 w-4" />
        </div>
      ))}
    </div>
  );
}

function orderInstances(containers: Container[]): Container[] {
  return [...containers].sort((left, right) => {
    const statusOrder = statusRank(left.status) - statusRank(right.status);
    if (statusOrder !== 0) return statusOrder;
    return right.created_at.localeCompare(left.created_at);
  });
}

function statusRank(status: string): number {
  if (status === "running") return 0;
  if (status === "pending") return 1;
  return 2;
}

function configuredReplicaLabel(deployment: Deployment): string {
  const scaling = deployment.scaling;
  if (!scaling) return "Not reported";
  if (scaling.min_replicas === scaling.max_replicas) return String(scaling.max_replicas);
  return `${scaling.min_replicas}–${scaling.max_replicas}`;
}
