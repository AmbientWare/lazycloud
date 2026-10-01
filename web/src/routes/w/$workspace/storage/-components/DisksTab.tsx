import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Trash2 } from "lucide-react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { formatBytes } from "@/lib/format";
import {
  deleteDisk,
  disksQueryOptions,
  selectDiskList,
  type HeldDisk,
} from "@/lib/queries/storage";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

const STATUS_LABELS: Record<Schemas["DiskStatus"], string> = {
  attached: "In use",
  saving: "Saving",
  detached: "Idle",
};

export function DisksTab({ workspace }: { workspace: string }) {
  const query = useInfiniteQuery(disksQueryOptions(workspace));
  const { items: disks, nextCursor } = selectDiskList(query.data, query.hasNextPage);

  return (
    <ContentTransition pending={query.isPending} className="min-h-full lg:h-full lg:min-h-0">
      {query.isPending ? (
        <DisksSkeleton />
      ) : query.isError && !query.isFetchNextPageError ? (
        <PanelError message={query.error.message} />
      ) : disks.length === 0 ? (
        <PanelEmpty
          message="No disks. A devbox or a pod with disks=[Disk(...)] creates its disks when it first starts."
          className="p-6"
        />
      ) : (
        <div className="min-h-0 overflow-y-auto">
          <div
            aria-hidden="true"
            className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_5rem_6rem_6rem_2rem] gap-3 border-b border-border/50 px-4 py-2 text-xs text-muted-foreground"
          >
            <span>Disk</span>
            <span>Used by</span>
            <span className="text-right">Size</span>
            <span className="text-right">Stored</span>
            <span>Status</span>
            <span />
          </div>
          <div className="divide-y divide-border/60">
            {disks.map((item) => (
              <DiskRow key={item.disk.id} workspace={workspace} item={item} />
            ))}
          </div>
          <InfiniteScrollBoundary
            nextCursor={nextCursor}
            loading={query.isFetchingNextPage}
            error={query.isFetchNextPageError}
            onLoadMore={() => void query.fetchNextPage()}
            resourceLabel="disks"
          />
        </div>
      )}
    </ContentTransition>
  );
}

function DiskRow({ workspace, item }: { workspace: string; item: HeldDisk }) {
  const { disk, holder } = item;
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const remove = useMutation({
    mutationFn: () => deleteDisk(workspace, disk.name),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.storage.disks(workspace) }),
  });
  const deletable = disk.status === "detached";

  return (
    <div className="group px-4 py-2.5">
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_5rem_6rem_6rem_2rem] items-center gap-3 text-xs">
        <span className="min-w-0">
          <span className="mono block truncate text-[13px] font-medium text-foreground">
            {disk.name}
          </span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">
            updated <LiveRelativeTime value={disk.updated_at} />
          </span>
        </span>
        <DiskWorkload workspace={workspace} holder={holder} />
        <span className="mono text-right tabular-nums">{formatBytes(disk.size_bytes)}</span>
        <span className="mono text-right tabular-nums text-muted-foreground">
          {formatBytes(disk.stored_bytes)}
        </span>
        <span className="text-muted-foreground">{STATUS_LABELS[disk.status]}</span>
        {!confirming ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-7 opacity-70 md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100"
            aria-label={`Delete disk ${disk.name}`}
            title={deletable ? "Delete disk" : "Stop the workload using this disk to delete it"}
            disabled={!deletable}
            onClick={() => setConfirming(true)}
          >
            <Trash2 />
          </Button>
        ) : (
          <span />
        )}
      </div>
      {confirming ? (
        <div className="mt-2 flex items-center gap-1.5">
          <span className="mr-1 text-xs text-muted-foreground">
            Deletes every file on the disk.
          </span>
          <Button
            variant="destructive"
            size="sm"
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? <Loader2 className="animate-spin" /> : "Delete"}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Keep
          </Button>
        </div>
      ) : null}
      {remove.isError ? (
        <p className="mt-2 text-xs text-destructive">{remove.error.message}</p>
      ) : null}
    </div>
  );
}

function DiskWorkload({
  workspace,
  holder,
}: {
  workspace: string;
  holder: Schemas["Container"] | null;
}) {
  if (!holder) return <span className="text-muted-foreground">None</span>;
  return (
    <Link
      to="/w/$workspace/apps/$appId/workloads/$kind/$name"
      params={{ workspace, appId: holder.app, kind: "function", name: holder.function }}
      className="min-w-0 truncate hover:text-foreground hover:underline"
    >
      <span className="mono text-foreground">{holder.function}</span>{" "}
      <span className="text-muted-foreground">pod in {holder.app}</span>
    </Link>
  );
}

function DisksSkeleton() {
  return (
    <div aria-hidden="true" className="divide-y divide-border/60">
      {Array.from({ length: 3 }, (_, index) => (
        <div key={index} className="space-y-2 px-4 py-3">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-20" />
        </div>
      ))}
    </div>
  );
}
