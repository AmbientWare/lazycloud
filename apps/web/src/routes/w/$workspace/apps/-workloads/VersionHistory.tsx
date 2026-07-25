import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Loader2, Pause, Play, Trash2 } from "lucide-react";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import type { Deployment } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";
import {
  deleteDeploymentMutationOptions,
  startDeploymentMutationOptions,
  stopDeploymentMutationOptions,
} from "@/lib/queries/apps";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import type { WorkloadGroup } from "./grouping";

export function VersionHistory({
  group,
  appId,
  workspaceId,
  workspaceName,
  nextCursor,
  loadingMore,
  loadMoreError,
  onLoadMore,
}: {
  group: WorkloadGroup;
  appId: string;
  workspaceId: string;
  workspaceName: string;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  onLoadMore: () => void;
}) {
  return (
    <div className="min-w-0 p-4 lg:p-5">
      <div className="divide-y divide-border">
        {group.deployments.map((deployment) => (
          <VersionRow
            key={deployment.id}
            deployment={deployment}
            latest={deployment.id === group.latest.id}
            lastDeployment={group.deployments.length === 1}
            appId={appId}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
          />
        ))}
      </div>
      <InfiniteScrollBoundary
        nextCursor={nextCursor}
        loading={loadingMore}
        error={loadMoreError}
        onLoadMore={onLoadMore}
        resourceLabel="deployment versions"
      />
    </div>
  );
}

function VersionRow({
  deployment,
  latest,
  lastDeployment,
  appId,
  workspaceId,
  workspaceName,
}: {
  deployment: Deployment;
  latest: boolean;
  lastDeployment: boolean;
  appId: string;
  workspaceId: string;
  workspaceName: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.deployments.root(workspaceId) }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId) }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.apps.summaries(workspaceId) }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.containers.root(workspaceId) }),
    ]);
  };
  const start = useMutation({
    ...startDeploymentMutationOptions(workspaceId, deployment.id),
    onSuccess: refresh,
  });
  const stop = useMutation({
    ...stopDeploymentMutationOptions(workspaceId, deployment.id),
    onSuccess: refresh,
  });
  const remove = useMutation({
    ...deleteDeploymentMutationOptions(workspaceId, deployment.id),
    onSuccess: async () => {
      await refresh();
      if (lastDeployment) {
        await navigate({
          to: "/w/$workspace/apps/$appId",
          params: { workspace: workspaceName, appId },
        });
      }
    },
  });
  const pending = start.isPending || stop.isPending || remove.isPending;
  const error = start.error ?? stop.error ?? remove.error;
  const hasActions =
    deployment.actions.can_start ||
    deployment.actions.can_stop ||
    deployment.actions.can_delete;

  return (
    <div className="flex min-h-11 flex-wrap items-center gap-2 py-2 text-sm">
      <span className="mono w-8 shrink-0 font-medium">v{deployment.version}</span>
      <StatusChip status={deployment.active ? "active" : "stopped"} live={deployment.active} />
      {latest ? <span className="micro-label text-muted-foreground">Latest</span> : null}
      <span className="ml-auto flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
        <time dateTime={deployment.created_at} title={deployment.created_at}>
          {relativeTime(deployment.created_at)}
        </time>
        <Link
          to="/w/$workspace/tasks"
          params={{ workspace: workspaceName }}
          search={{ app: appId, deployment: deployment.id }}
          className="text-brand hover:underline"
        >
          Tasks
        </Link>
      </span>
      {hasActions ? (
        <span className="flex shrink-0 items-center gap-1 border-l border-border pl-2">
          {confirmingDelete ? (
            <>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                disabled={pending}
                onClick={() => remove.mutate()}
              >
                {remove.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
                Delete v{deployment.version}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={pending}
                onClick={() => setConfirmingDelete(false)}
              >
                Keep
              </Button>
            </>
          ) : (
            <>
              {deployment.actions.can_start ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Resume deployment v${deployment.version}`}
                  title="Resume deployment"
                  disabled={pending}
                  onClick={() => start.mutate()}
                >
                  {start.isPending ? <Loader2 className="animate-spin" /> : <Play />}
                </Button>
              ) : null}
              {deployment.actions.can_stop ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Pause deployment v${deployment.version}`}
                  title="Pause deployment"
                  disabled={pending}
                  onClick={() => stop.mutate()}
                >
                  {stop.isPending ? <Loader2 className="animate-spin" /> : <Pause />}
                </Button>
              ) : null}
              {deployment.actions.can_delete ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Delete deployment v${deployment.version}`}
                  title="Delete deployment"
                  disabled={pending}
                  onClick={() => setConfirmingDelete(true)}
                >
                  <Trash2 className="text-destructive" />
                </Button>
              ) : null}
            </>
          )}
        </span>
      ) : null}
      {error ? (
        <span className="basis-full text-right text-xs text-destructive" role="alert">
          {error.message}
        </span>
      ) : null}
    </div>
  );
}
