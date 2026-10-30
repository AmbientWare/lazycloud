import { Link } from "@tanstack/react-router";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";

import { ConfirmButton } from "@/components/shared/ConfirmButton";
import { ContentTransition } from "@/components/shared/ContentTransition";
import { DiskFailure } from "@/components/shared/DiskFailure";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { formatBytes } from "@/lib/format";
import { deleteDisk, disksQueryOptions } from "@/lib/queries/storage";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

const STATUS_LABELS: Record<Schemas["DiskStatus"], string> = {
  attached: "In use",
  saving: "Saving",
  detached: "Idle",
};

export function DisksTab({ workspace }: { workspace: string }) {
  const query = useInfiniteQuery(disksQueryOptions(workspace));
  const disks = query.data?.pages.flatMap((page) => page.disks) ?? [];
  const nextCursor = query.hasNextPage ? query.data?.pages.at(-1)?.next_cursor : undefined;

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
            {disks.map((disk) => (
              <DiskRow key={disk.id} workspace={workspace} disk={disk} />
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

function DiskRow({ workspace, disk }: { workspace: string; disk: Schemas["Disk"] }) {
  const queryClient = useQueryClient();
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
        <DiskWorkload workspace={workspace} holder={disk.holder} />
        <span className="mono text-right tabular-nums">{formatBytes(disk.size_bytes)}</span>
        <span className="mono text-right tabular-nums text-muted-foreground">
          {formatBytes(disk.stored_bytes)}
        </span>
        <span className="text-muted-foreground">{STATUS_LABELS[disk.status]}</span>
        <span
          className="justify-self-end"
          title={deletable ? undefined : "Stop the workload using this disk to delete it"}
        >
          <ConfirmButton
            label={`Delete disk ${disk.name} and every file on it`}
            icon={Trash2}
            disabled={!deletable}
            pending={remove.isPending}
            onConfirm={() => remove.mutate()}
            className="opacity-70 focus-within:opacity-100 data-[armed=true]:opacity-100 md:opacity-0 md:group-hover:opacity-100"
          />
        </span>
      </div>
      {disk.failure ? <DiskFailure failure={disk.failure} /> : null}
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
  holder: Schemas["WorkloadRef"] | undefined;
}) {
  if (!holder) return <span className="text-muted-foreground">None</span>;
  return (
    <Link
      to="/w/$workspace/apps/$app/workloads/$kind/$name"
      params={{ workspace, app: holder.app, kind: holder.kind, name: holder.name }}
      className="min-w-0 truncate hover:text-foreground hover:underline"
    >
      <span className="mono text-foreground">{holder.name}</span>{" "}
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
