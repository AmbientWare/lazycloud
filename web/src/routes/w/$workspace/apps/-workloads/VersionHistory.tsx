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
  deleteWorkloadMutationOptions,
  selectVersionList,
  startWorkloadMutationOptions,
  stopWorkloadMutationOptions,
  versionsInfiniteQueryOptions,
  workloadRunning,
} from "@/lib/queries/deployments";

/** The workload's deployed versions, newest first, with the actions each offers. */
export function VersionHistory({
  workspace,
  workload,
}: {
  workspace: string;
  workload: Schemas["Workload"];
}) {
  const versions = useInfiniteQuery(versionsInfiniteQueryOptions(workspace, workload));
  const list = selectVersionList(versions.data, versions.hasNextPage);
  if (versions.isPending) return <RowsSkeleton rows={3} height="h-12" />;
  if (versions.isError && !versions.isFetchNextPageError) {
    return <PanelError message={versions.error.message} />;
  }
  const newest = list.items[0]?.version;
  // Deleting removes the workload with every version, so only its one
  // remaining version offers it.
  const onlyVersion = list.items.length === 1 && !list.nextCursor;

  return (
    <div className="@container min-w-0">
      <VersionListHeader />
      <div className="divide-y divide-border/70">
        {list.items.map((version) => (
          <VersionRow
            key={version.version}
            version={version}
            workload={workload}
            latest={version.version === newest}
            deletable={onlyVersion}
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
  workload,
  latest,
  deletable,
  workspace,
}: {
  version: Schemas["Version"];
  workload: Schemas["Workload"];
  latest: boolean;
  deletable: boolean;
  workspace: string;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const refresh = () => invalidateAppLists(queryClient, workspace);
  const start = useMutation({
    ...startWorkloadMutationOptions(workspace, workload, version.version),
    onSuccess: refresh,
  });
  const stop = useMutation({
    ...stopWorkloadMutationOptions(workspace, workload),
    onSuccess: refresh,
  });
  const remove = useMutation({
    ...deleteWorkloadMutationOptions(workspace, workload),
    onSuccess: async () => {
      await refresh();
      await navigate({
        to: "/w/$workspace/apps/$app",
        params: { workspace, app: workload.app },
      });
    },
  });
  const running = version.active && workloadRunning(workload);
  const canDelete = deletable && workload.state !== "deleted";
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
          search={{ app: workload.app, workload: workload.name, version: version.version }}
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
              Delete v{version.version}
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
            {running ? (
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
            ) : (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Resume deployment v${version.version}`}
                title="Resume deployment"
                disabled={pending}
                onClick={() => start.mutate()}
              >
                {start.isPending ? <Loader2 className="animate-spin" /> : <Play />}
              </Button>
            )}
            {canDelete ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Delete deployment v${version.version}`}
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
      {error ? (
        <span className="col-span-full text-xs text-destructive @2xl:text-right" role="alert">
          {error.message}
        </span>
      ) : null}
    </div>
  );
}
