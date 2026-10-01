import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";

import { PanelError } from "@/components/shared/PanelError";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { ContentTransition } from "@/components/shared/ContentTransition";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Skeleton } from "@/components/ui/skeleton";
import {
  mapsQueryOptions,
  queuesQueryOptions,
  selectMaps,
  selectQueues,
  type CollectionKind,
} from "@/lib/queries/collections";
import { cn } from "@/lib/utils";

import { MapInspector, QueueInspector } from "./CollectionInspectors";
import { CollectionValueForm } from "./CollectionValueForm";

type CollectionRowData = { name: string; amount: number };

export function CollectionAccordion({
  kind,
  workspaceId,
  creating,
  onCreatingChange,
}: {
  kind: CollectionKind;
  workspaceId: string;
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
}) {
  const queues = useInfiniteQuery({
    ...queuesQueryOptions(workspaceId),
    enabled: kind === "queues",
  });
  const maps = useInfiniteQuery({ ...mapsQueryOptions(workspaceId), enabled: kind === "maps" });
  const query = kind === "queues" ? queues : maps;
  const queueList = selectQueues(queues.data, queues.hasNextPage);
  const mapList = selectMaps(maps.data, maps.hasNextPage);
  const rows: CollectionRowData[] =
    kind === "queues"
      ? queueList.items.map((queue) => ({ name: queue.name, amount: queue.size }))
      : mapList.items.map((map) => ({ name: map.name, amount: map.count }));
  const nextCursor = kind === "queues" ? queueList.nextCursor : mapList.nextCursor;
  const [openName, setOpenName] = useState<string | null>(null);
  const title = kind === "queues" ? "Queues" : "Maps";
  const singular = kind === "queues" ? "queue" : "map";

  return (
    <ContentTransition
      pending={query.isPending}
      className="flex min-h-full flex-col lg:h-full lg:min-h-0"
    >
      <section
        aria-label={`${title} collection`}
        data-collection-scroll=""
        className="min-h-[24rem] flex-1 overflow-visible lg:min-h-0 lg:overflow-y-auto"
      >
        <div className="flex min-h-11 items-center gap-2 border-b border-border px-3 py-2">
          <h2 className="text-sm font-medium">{title}</h2>
          <span className="mono text-xs text-muted-foreground">
            {rows.length}
            {nextCursor ? "+" : ""}
          </span>
        </div>
        {creating ? (
          <div className="p-4">
            <CollectionValueForm
              workspaceId={workspaceId}
              kind={kind}
              onCancel={() => onCreatingChange(false)}
              onDone={(name) => {
                setOpenName(name);
                onCreatingChange(false);
              }}
            />
          </div>
        ) : null}
        {query.isPending ? (
          <CollectionSkeleton />
        ) : query.isError && !query.data ? (
          <PanelError message={query.error.message} />
        ) : rows.length === 0 ? (
          <PanelEmpty message={`No ${title.toLowerCase()}`} className="p-8" />
        ) : (
          <div className="divide-y divide-border/70">
            {rows.map((row) => {
              const open = openName === row.name;
              return (
                <div key={row.name}>
                  <CollectionRow
                    kind={kind}
                    row={row}
                    open={open}
                    onToggle={(element) => {
                      setOpenName(open ? null : row.name);
                      if (!open) {
                        requestAnimationFrame(() => {
                          requestAnimationFrame(() => element.scrollIntoView({ block: "nearest" }));
                        });
                      }
                    }}
                  />
                  {open ? (
                    <div
                      role="region"
                      aria-label={`${row.name} ${singular} inspector`}
                      className="border-t border-border bg-background/35"
                    >
                      {kind === "queues" ? (
                        <QueueInspector workspaceId={workspaceId} name={row.name} />
                      ) : (
                        <MapInspector workspaceId={workspaceId} name={row.name} />
                      )}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
        <InfiniteScrollBoundary
          nextCursor={nextCursor}
          loading={query.isFetchingNextPage}
          error={query.isFetchNextPageError}
          onLoadMore={() => void query.fetchNextPage()}
          resourceLabel={title.toLowerCase()}
        />
      </section>
    </ContentTransition>
  );
}

function CollectionRow({
  kind,
  row,
  open,
  onToggle,
}: {
  kind: CollectionKind;
  row: CollectionRowData;
  open: boolean;
  onToggle: (element: HTMLElement) => void;
}) {
  const singular = kind === "queues" ? "message" : "key";
  return (
    <button
      type="button"
      aria-expanded={open}
      data-selected={open}
      className="interactive-row group flex w-full min-w-0 items-center gap-3 px-3 py-3 text-left"
      onClick={(event) => onToggle(event.currentTarget.parentElement ?? event.currentTarget)}
    >
      <ChevronRight
        className={cn(
          "interactive-row-indicator size-3.5 shrink-0 text-muted-foreground transition-transform",
          open && "rotate-90 text-brand",
        )}
      />
      <span className="mono min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">
        {row.name}
      </span>
      <span className="shrink-0 text-right">
        <span className="readout block text-sm text-foreground">{row.amount.toLocaleString()}</span>
        <span className="block text-[10px] text-muted-foreground">
          {row.amount === 1 ? singular : `${singular}s`}
        </span>
      </span>
    </button>
  );
}

function CollectionSkeleton() {
  return (
    <div aria-hidden="true" className="divide-y divide-border/60">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="flex items-center gap-3 px-3 py-3">
          <Skeleton className="size-3.5" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-7 w-10" />
        </div>
      ))}
    </div>
  );
}
