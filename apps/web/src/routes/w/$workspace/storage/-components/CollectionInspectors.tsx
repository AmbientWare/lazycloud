import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Fact } from "@/components/shared/Fact";
import { PanelError } from "@/components/shared/PanelError";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { countLabel, formatBytes, formatDuration } from "@/lib/format";
import {
  mapCountQueryOptions,
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
  if (error) return <PanelError message={error.message} />;

  if (size.isPending || peek.isPending) {
    return <InspectorSkeleton />;
  }

  return (
    <div className="min-w-0 px-4 py-3">
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
}: {
  workspaceId: string;
  name: string;
  sizeBytes: number;
  expiringKeys: number;
  nearestExpirySeconds: number | null;
}) {
  const count = useQuery(mapCountQueryOptions(workspaceId, name));
  const keys = useQuery(mapKeysQueryOptions(workspaceId, name));
  const [selectedKey, setSelectedKey] = useState("");
  const effectiveSelectedKey = keys.data?.keys.includes(selectedKey)
    ? selectedKey
    : (keys.data?.keys[0] ?? "");
  const value = useQuery(mapValueQueryOptions(workspaceId, name, effectiveSelectedKey));
  const error = count.error ?? keys.error;
  if (error) return <PanelError message={error.message} />;

  if (count.isPending || keys.isPending) {
    return <InspectorSkeleton withControl />;
  }

  const allKeys = keys.data?.keys ?? [];
  const visibleKeys = allKeys.slice(0, 100);
  const keyCount = count.data?.count ?? allKeys.length;
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
      {visibleKeys.length ? (
        <>
          <div className="mt-3 border-t border-border/60 pt-3">
            <Select value={effectiveSelectedKey} onValueChange={setSelectedKey}>
              <SelectTrigger size="sm" aria-label="Map key" className="mono w-full text-xs sm:w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                {visibleKeys.map((key) => (
                  <SelectItem key={key} value={key} className="mono text-xs">
                    {key}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {allKeys.length > visibleKeys.length ? (
              <p className="mt-2 text-[11px] text-muted-foreground">
                Showing first {visibleKeys.length.toLocaleString()} keys
              </p>
            ) : null}
          </div>
          <div className="mt-3 min-w-0 border-t border-border/60 pt-3">
            {value.isPending ? (
              <PreviewSkeleton />
            ) : value.isError ? (
              <p className="text-xs text-destructive">{value.error.message}</p>
            ) : (
              <EncodedValuePreview valueBase64={value.data?.value_base64} className="max-h-40" />
            )}
          </div>
        </>
      ) : (
        <p className="mt-3 border-t border-border/60 pt-3 text-xs text-muted-foreground">
          Map is empty
        </p>
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
      <p className="shrink-0 text-[11px] text-muted-foreground">
        {countLabel(count, singular)} · live
      </p>
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
