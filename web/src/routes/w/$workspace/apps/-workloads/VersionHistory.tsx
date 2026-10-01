import { useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Loader2, Pause, Play, Trash2 } from "lucide-react";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelError } from "@/components/shared/PanelError";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import type { Schemas } from "@/lib/api/client";
import { invalidateAppLists } from "@/lib/queries/apps";
import {
  deleteDeployment,
  selectVersions,
  startDeployment,
  stopDeployment,
  versionsQueryOptions,
  type DeployedWorkload,
} from "@/lib/queries/deployments";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

/**
 * Every deployed version of a workload. The active version takes calls; pause
 * and resume act on the workload, and resuming an older version makes it the
 * active one again. Delete removes the workload with all its versions.
 */
export function VersionHistory({
  workload,
  workspace,
}: {
  workload: DeployedWorkload;
  workspace: string;
}) {
  const versions = useInfiniteQuery(versionsQueryOptions(workspace, workload.id));
  const list = selectVersions(versions.data, versions.hasNextPage);

  if (versions.isPending) return <RowsSkeleton rows={3} height="h-12" />;
  if (versions.isError && !versions.isFetchNextPageError) {
    return <PanelError message={versions.error.message} />;
  }
  return (
    <div className="@container min-w-0">
      <VersionListHeader />
      <div className="divide-y divide-border/70">
        {list.items.map((version) => (
          <VersionRow
            key={version.release_id}
            version={version}
            latest={version.version === list.items[0]?.version}
            workload={workload}
            workspace={workspace}
          />
        ))}
      </div>
      <InfiniteScrollBoundary
        nextCursor={list.nextCursor}
        loading={versions.isFetchingNextPage}
        error={versions.isFetchNextPageError}
        onLoadMore={() => void versions.fetchNextPage()}
        resourceLabel="deployment versions"
      />
    </div>
  );
}

const VERSION_COLUMNS = "@2xl:grid @2xl:grid-cols-[6rem_6rem_minmax(0,1fr)_auto] @2xl:items-center";

function VersionListHeader() {
  return (
    <div
      className={`hidden min-h-9 shrink-0 gap-3 border-b border-border/80 px-3 text-[10px] font-medium text-muted-foreground ${VERSION_COLUMNS}`}
      aria-hidden="true"
    >
      <span>Version</span>
      <span>State</span>
      <span>Deployed</span>
      <span />
    </div>
  );
}

function VersionRow({
  version,
  latest,
  workload,
  workspace,
}: {
  version: Schemas["Version"];
  latest: boolean;
  workload: DeployedWorkload;
  workspace: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const refresh = async () => {
    await Promise.all([
      invalidateAppLists(queryClient, workspace),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.workloads.root(workspace) }),
    ]);
  };
  const running = version.active && workload.state === "active";
  const start = useMutation({
    mutationFn: () =>
      startDeployment(workspace, workload.id, version.active ? undefined : version.version),
    onSuccess: refresh,
  });
  const stop = useMutation({
    mutationFn: () => stopDeployment(workspace, workload.id),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => deleteDeployment(workspace, workload.id),
    onSuccess: async () => {
      await refresh();
      await navigate({ to: "/w/$workspace/apps/$app", params: { workspace, app: workload.app } });
    },
  });
  const pending = start.isPending || stop.isPending || remove.isPending;
  const error = start.error ?? stop.error ?? remove.error;

  return (
    <div
      className={`grid min-h-12 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 px-3 py-3 text-sm ${VERSION_COLUMNS}`}
    >
      <span className="flex min-w-0 items-center gap-2">
        <span className="mono font-medium">v{version.version}</span>
        {latest ? <span className="micro-label text-muted-foreground">Latest</span> : null}
      </span>
      <span className="flex min-w-0 justify-end @2xl:justify-start">
        <StatusChip status={running ? "active" : "stopped"} />
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className="whitespace-nowrap">
          <LiveRelativeTime value={version.created_at} />
        </span>
        <Link
          to="/w/$workspace/tasks"
          params={{ workspace }}
          search={{ app: workload.app, workload: workload.name }}
          className="interactive-link text-brand"
        >
          Tasks
        </Link>
      </span>
      <span className="flex min-w-0 flex-wrap items-center justify-end gap-1">
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
              Delete {workload.name}
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
            {!running ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Resume deployment v${version.version}`}
                title={version.active ? "Resume deployment" : "Make this version active"}
                disabled={pending}
                onClick={() => start.mutate()}
              >
                {start.isPending ? <Loader2 className="animate-spin" /> : <Play />}
              </Button>
            ) : (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Pause deployment v${version.version}`}
                title="Pause deployment"
                disabled={pending}
                onClick={() => stop.mutate()}
              >
                {stop.isPending ? <Loader2 className="animate-spin" /> : <Pause />}
              </Button>
            )}
            {version.active ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Delete ${workload.name} and every version`}
                title="Delete workload"
                disabled={pending}
                onClick={() => setConfirmingDelete(true)}
              >
                <Trash2 className="text-destructive" />
              </Button>
            ) : null}
          </>
        )}
      </span>
      {error ? (
        <span className="col-span-full text-xs text-destructive @2xl:text-right" role="alert">
          {error.message}
        </span>
      ) : null}
    </div>
  );
}
