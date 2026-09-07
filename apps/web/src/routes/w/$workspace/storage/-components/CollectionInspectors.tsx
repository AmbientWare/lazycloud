import { useDeferredValue, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { Fact } from "@/components/shared/Fact";
import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { countLabel, formatBytes, formatDuration } from "@/lib/format";
import {
  mapKeysQueryOptions,
  mapValueQueryOptions,
  queuePeekQueryOptions,
  queueSizeQueryOptions,
} from "@/lib/queries/collections";

import { EncodedValuePreview } from "./EncodedValuePreview";

export function QueueInspector({
  workspaceId,
  name,
  oldestMessageAgeSeconds,
  putRatePerMinute,
}: {
  workspaceId: string;
  name: string;
  oldestMessageAgeSeconds: number | null;
  putRatePerMinute: number;
}) {
  const size = useQuery(queueSizeQueryOptions(workspaceId, name));
  const peek = useQuery(queuePeekQueryOptions(workspaceId, name));
  const error = size.error ?? peek.error;
  const depth = size.data?.size ?? 0;
  const retry = () => {
    void size.refetch();
    void peek.refetch();
  };
  if (error && (!size.data || !peek.data))
    return (
      <ApiErrorNotice
        error={error}
        title="Queue could not be loaded"
        onRetry={retry}
        retrying={size.isFetching || peek.isFetching}
      />
    );

  if (size.isPending || peek.isPending) {
    return <InspectorSkeleton />;
  }

  return (
    <div className="min-w-0 px-4 py-3">
      {error ? (
        <ApiErrorNotice
          compact
          error={error}
          title="Queue could not be refreshed"
          onRetry={retry}
          retrying={size.isFetching || peek.isFetching}
        />
      ) : null}
      <InspectorHeader label="Head message" count={depth} singular="message" />
      <CollectionStats
        items={[
          {
            label: "Oldest",
            value:
              oldestMessageAgeSeconds === null
                ? "Empty"
                : formatDuration(oldestMessageAgeSeconds * 1_000),
          },
          { label: "Writes", value: `${putRatePerMinute.toLocaleString()}/min` },
        ]}
      />
      <div className="mt-3 min-w-0 border-t border-border/60 pt-3">
        <EncodedValuePreview
          valueBase64={peek.data?.value_base64}
          emptyLabel={depth === 0 ? "Queue is empty" : "Message is empty"}
          className="max-h-40"
        />
      </div>
    </div>
  );
}

export function MapInspector({
  workspaceId,
  name,
  sizeBytes,
  expiringKeys,
  nearestExpirySeconds,
  keyCount,
}: {
  workspaceId: string;
  name: string;
  sizeBytes: number;
  expiringKeys: number;
  nearestExpirySeconds: number | null;
  keyCount: number;
}) {
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const keys = useInfiniteQuery(mapKeysQueryOptions(workspaceId, name, deferredSearch));
  const [selectedKey, setSelectedKey] = useState("");
  const allKeys = [...new Set(keys.data?.pages.flatMap((page) => page.data) ?? [])];
  const effectiveSelectedKey = allKeys.includes(selectedKey) ? selectedKey : (allKeys[0] ?? "");
  const value = useQuery(mapValueQueryOptions(workspaceId, name, effectiveSelectedKey));
  return (
    <div className="min-w-0 px-4 py-3">
      <InspectorHeader label="Value" count={keyCount} singular="key" />
      <CollectionStats
        items={[
          { label: "Stored", value: formatBytes(sizeBytes) },
          { label: "Expiring", value: expiringKeys.toLocaleString() },
          {
            label: "Next expiry",
            value:
              nearestExpirySeconds === null
                ? "Persistent"
                : formatDuration(nearestExpirySeconds * 1_000),
          },
        ]}
      />
      <div className="mt-3 flex gap-2 border-t border-border/60 pt-3">
        <Input
          aria-label="Search map keys"
          placeholder="Search keys"
          maxLength={240}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="h-8 font-mono"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={keys.isFetching}
          onClick={() => void keys.refetch()}
        >
          Refresh
        </Button>
      </div>
      {keys.isPending ? (
        <InspectorSkeleton withControl />
      ) : keys.isError && !keys.data ? (
        <ApiErrorNotice
          error={keys.error}
          title="Map keys could not be loaded"
          onRetry={() => void keys.refetch()}
          retrying={keys.isFetching}
        />
      ) : (
        <>
          {keys.isError && !keys.isFetchNextPageError ? (
            <ApiErrorNotice
              compact
              error={keys.error}
              title="Map keys could not be refreshed"
              onRetry={() => void keys.refetch()}
              retrying={keys.isFetching}
            />
          ) : null}
          <div className="mt-2 max-h-40 overflow-y-auto border border-border" aria-label="Map keys">
            {allKeys.map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={key === effectiveSelectedKey}
                data-selected={key === effectiveSelectedKey}
                className="interactive-row mono block w-full truncate px-3 py-2 text-left text-xs"
                onClick={() => setSelectedKey(key)}
              >
                {key}
              </button>
            ))}
            <InfiniteScrollBoundary
              key={deferredSearch}
              nextCursor={keys.data?.pages.at(-1)?.next}
              loading={keys.isFetchingNextPage}
              error={keys.isFetchNextPageError}
              onLoadMore={() => void keys.fetchNextPage()}
              resourceLabel="map keys"
            />
            {allKeys.length === 0 && !keys.hasNextPage ? (
              <p className="p-3 text-xs text-muted-foreground">
                {search ? "No matching keys" : "Map is empty"}
              </p>
            ) : null}
          </div>
          {effectiveSelectedKey ? (
            <div className="mt-3 min-w-0 border-t border-border/60 pt-3">
              {value.isPending ? (
                <PreviewSkeleton />
              ) : value.isError ? (
                <ApiErrorNotice
                  error={value.error}
                  title="Value could not be loaded"
                  onRetry={() => void value.refetch()}
                  retrying={value.isFetching}
                />
              ) : (
                <EncodedValuePreview valueBase64={value.data?.value_base64} className="max-h-40" />
              )}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

function InspectorHeader({
  label,
  count,
  singular,
}: {
  label: string;
  count: number;
  singular: string;
}) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-3">
      <p className="micro-label truncate">{label}</p>
      <p className="shrink-0 text-[11px] text-muted-foreground">{countLabel(count, singular)}</p>
    </div>
  );
}

function CollectionStats({ items }: { items: Array<{ label: string; value: string }> }) {
  return (
    <dl
      className="mt-3 grid gap-2 rounded-sm bg-muted/45 p-2.5 text-xs text-foreground"
      style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}
    >
      {items.map((item) => (
        <Fact key={item.label} label={item.label} value={item.value} mono />
      ))}
    </dl>
  );
}

function InspectorSkeleton({ withControl = false }: { withControl?: boolean }) {
  return (
    <div aria-hidden="true" className="px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-24" />
      </div>
      <div className="mt-3 border-t border-border/60 pt-3">
        {withControl ? <Skeleton className="mb-3 h-8 w-full sm:w-64" /> : null}
        <PreviewSkeleton />
      </div>
    </div>
  );
}

function PreviewSkeleton() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-3 w-4/5" />
      <Skeleton className="h-3 w-3/5" />
    </div>
  );
}
