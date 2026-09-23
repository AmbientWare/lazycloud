import { useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Trash2 } from "lucide-react";

import { ContentTransition } from "@/components/shared/ContentTransition";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Disk } from "@/lib/api/schemas";
import { formatBytes } from "@/lib/format";
import { deleteDisk, disksQueryOptions, selectDiskList } from "@/lib/queries/storage";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

const STATUS_LABELS: Record<Disk["status"], string> = {
  attached: "In use",
  detached: "Idle",
  deleting: "Deleting",
};

export function DisksTab({ workspaceId }: { workspaceId: string }) {
  const query = useInfiniteQuery(disksQueryOptions(workspaceId));
  const { items: disks, nextCursor } = selectDiskList(query.data, query.hasNextPage);

  return (
    <ContentTransition pending={query.isPending} className="min-h-full lg:h-full lg:min-h-0">
      {query.isPending ? (
        <DisksSkeleton />
      ) : query.isError && !query.isFetchNextPageError ? (
        <PanelError message={query.error.message} />
      ) : disks.length === 0 ? (
        <PanelEmpty
          message="No disks. A pod declares one with disks=[Disk(...)] and creates it on deploy."
          className="p-6"
        />
      ) : (
        <div className="min-h-0 overflow-y-auto">
          <div
            aria-hidden="true"
            className="grid grid-cols-[minmax(0,1fr)_5rem_6rem_6rem_2rem] gap-3 border-b border-border/50 px-4 py-2 text-xs text-muted-foreground"
          >
            <span>Disk</span>
            <span className="text-right">Size</span>
            <span className="text-right">Stored</span>
            <span>Status</span>
            <span />
          </div>
          <div className="divide-y divide-border/60">
            {disks.map((disk) => (
              <DiskRow key={disk.id} workspaceId={workspaceId} disk={disk} />
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

function DiskRow({ workspaceId, disk }: { workspaceId: string; disk: Disk }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const remove = useMutation({
    mutationFn: () => deleteDisk(workspaceId, disk.name),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.storage.disks(workspaceId) }),
  });
  const deletable = disk.status === "detached";

  return (
    <div className="group px-4 py-2.5">
      <div className="grid grid-cols-[minmax(0,1fr)_5rem_6rem_6rem_2rem] items-center gap-3 text-xs">
        <span className="min-w-0">
          <span className="mono block truncate text-[13px] font-medium text-foreground">
            {disk.name}
          </span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">
            updated <LiveRelativeTime value={disk.updated_at} />
          </span>
        </span>
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
            title={deletable ? "Delete disk" : "Stop the pod using this disk to delete it"}
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
