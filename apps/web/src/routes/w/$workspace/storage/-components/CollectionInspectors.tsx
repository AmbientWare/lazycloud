import { useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";

import { Fact } from "@/components/shared/Fact";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Skeleton } from "@/components/ui/skeleton";
import { countLabel, formatBytes, formatDuration } from "@/lib/format";
import {
  mapCountQueryOptions,
  mapKeysQueryOptions,
  mapValueQueryOptions,
  queuePeekQueryOptions,
  queueSizeQueryOptions,
  deleteMapKey,
  popQueueMessage,
  refreshCollection,
} from "@/lib/queries/collections";

import { EncodedValuePreview } from "./EncodedValuePreview";
import { CollectionValueForm, editableJson, type MapEdit } from "./CollectionValueForm";
import { ConfirmCollectionAction, DeleteCollection } from "./CollectionActions";

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
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [removed, setRemoved] = useState<string | null>(null);
  const size = useQuery(queueSizeQueryOptions(workspaceId, name));
  const peek = useQuery(queuePeekQueryOptions(workspaceId, name));
  const error = size.error ?? peek.error;
  const depth = size.data?.size ?? 0;
  if (error && (!size.data || !peek.data)) return <PanelError message={error.message} />;

  if (size.isPending || peek.isPending) {
    return <InspectorSkeleton />;
  }

  return (
    <div className="content-transition min-w-0 px-4 py-3">
      <InspectorHeader label="Head message" count={depth} singular="message" />
      {error ? (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error.message}
        </p>
      ) : null}
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
      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" onClick={() => setAdding(true)} disabled={adding}>
          Add message
        </Button>
        <ConfirmCollectionAction
          label="Remove next message"
          disabled={depth === 0}
          description="This consumes the next message without running it. A consumer may take the previewed message before you confirm."
          action={async () => {
            const result = await popQueueMessage(workspaceId, name);
            setRemoved(result.value_base64);
            await refreshCollection(client, workspaceId, "queues", name);
          }}
        />
        <DeleteCollection workspaceId={workspaceId} kind="queues" name={name} />
      </div>
      {adding ? (
        <div className="mt-3">
          <CollectionValueForm
            workspaceId={workspaceId}
            kind="queues"
            name={name}
            onDone={() => setAdding(false)}
            onCancel={() => setAdding(false)}
          />
        </div>
      ) : null}
      {removed !== null ? (
        <div className="mt-3 rounded-md border border-border p-3" role="status">
          <p className="mb-2 text-xs font-medium">
            {removed ? "Removed message" : "The queue was already empty"}
          </p>
          {removed ? <EncodedValuePreview valueBase64={removed} /> : null}
        </div>
      ) : null}
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
  const [prefix, setPrefix] = useState("");
  const [adding, setAdding] = useState(false);
  const count = useQuery(mapCountQueryOptions(workspaceId, name));
  const keys = useInfiniteQuery(mapKeysQueryOptions(workspaceId, name, prefix));
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const allKeys = [...new Set(keys.data?.pages.flatMap((page) => page.data) ?? [])];
  const effectiveSelectedKey = selectedKey ?? allKeys[0] ?? null;
  const error = count.error;
  if (error && !count.data) return <PanelError message={error.message} />;

  if (count.isPending) {
    return <InspectorSkeleton withControl />;
  }

  const keyCount = count.data?.count ?? allKeys.length;
  return (
    <div className="content-transition min-w-0 px-4 py-3">
      <InspectorHeader label="Value" count={keyCount} singular="key" />
      {error ? (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error.message}
        </p>
      ) : null}
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
      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" onClick={() => setAdding(true)} disabled={adding}>
          Add key
        </Button>
        <DeleteCollection workspaceId={workspaceId} kind="maps" name={name} />
      </div>
      {adding ? (
        <div className="mt-3">
          <CollectionValueForm
            workspaceId={workspaceId}
            kind="maps"
            name={name}
            onDone={(_name, key) => {
              setAdding(false);
              setSelectedKey(key);
            }}
            onCancel={() => setAdding(false)}
          />
        </div>
      ) : null}
      <div className="mt-3 grid min-w-0 gap-4 border-t border-border/60 pt-3 sm:grid-cols-[minmax(10rem,1fr)_minmax(0,2fr)]">
        <div className="min-w-0">
          <Input
            aria-label="Filter keys by prefix"
            placeholder="Filter keys by prefix"
            value={prefix}
            onChange={(event) => setPrefix(event.target.value)}
            className="mono mb-2"
          />
          <div
            className="max-h-72 overflow-y-auto rounded-md border border-border"
            aria-label="Map keys"
          >
            {allKeys.map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={key === effectiveSelectedKey}
                onClick={() => setSelectedKey(key)}
                className={`mono block w-full truncate px-3 py-2 text-left text-xs hover:bg-muted ${key === effectiveSelectedKey ? "bg-muted font-medium" : ""}`}
              >
                {key === "" ? '""' : key}
              </button>
            ))}
            {keys.isPending ? (
              <div className="p-3">
                <PreviewSkeleton />
              </div>
            ) : null}
            {!keys.isPending && !allKeys.length && !keys.hasNextPage ? (
              <p className="p-3 text-xs text-muted-foreground">
                {prefix ? "No matching keys" : "Map is empty"}
              </p>
            ) : null}
            {keys.isError ? (
              <p role="alert" className="p-3 text-xs text-destructive">
                {keys.error.message}
              </p>
            ) : null}
            <InfiniteScrollBoundary
              key={prefix}
              nextCursor={keys.data?.pages.at(-1)?.next ?? undefined}
              loading={keys.isFetchingNextPage}
              error={keys.isFetchNextPageError}
              onLoadMore={() => {
                void keys.fetchNextPage();
              }}
              resourceLabel="keys"
            />
          </div>
        </div>
        {effectiveSelectedKey !== null ? (
          <MapEntryInspector
            key={effectiveSelectedKey}
            workspaceId={workspaceId}
            name={name}
            entryKey={effectiveSelectedKey}
            onEdit={() => setSelectedKey(effectiveSelectedKey)}
            onDeleted={() => setSelectedKey(null)}
          />
        ) : null}
      </div>
    </div>
  );
}

function MapEntryInspector({
  workspaceId,
  name,
  entryKey,
  onDeleted,
  onEdit,
}: {
  workspaceId: string;
  name: string;
  entryKey: string;
  onDeleted: () => void;
  onEdit: () => void;
}) {
  const client = useQueryClient();
  const value = useQuery(mapValueQueryOptions(workspaceId, name, entryKey));
  const [editing, setEditing] = useState<MapEdit | null>(null);
  if (editing)
    return (
      <Dialog
        open
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto" aria-describedby={undefined}>
          <DialogHeader>
            <DialogTitle>Edit value</DialogTitle>
          </DialogHeader>
          <CollectionValueForm
            key={editing.value.revision}
            workspaceId={workspaceId}
            kind="maps"
            name={name}
            entry={editing}
            onCancel={() => setEditing(null)}
            onDone={() => setEditing(null)}
            onReload={setEditing}
          />
        </DialogContent>
      </Dialog>
    );
  if (value.isPending) return <PreviewSkeleton />;
  if (value.isError) return <PanelError message={value.error.message} />;
  const current = value.data;
  const editable = editableJson(current) !== null;
  return (
    <div className="content-transition min-w-0">
      <p className="mono mb-2 break-all text-xs font-medium">{entryKey === "" ? '""' : entryKey}</p>
      <EncodedValuePreview valueBase64={current.value_base64} />
      <p className="mt-2 text-xs text-muted-foreground">
        {current.expires_at
          ? `Expires ${new Date(current.expires_at).toLocaleString()}`
          : "No expiry"}
      </p>
      {!editable ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Only JSON values can be edited in the dashboard.
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={!editable}
          onClick={() => {
            onEdit();
            setEditing({ key: entryKey, value: current });
          }}
        >
          Edit value
        </Button>
        <ConfirmCollectionAction
          label="Delete key"
          description={`Delete key "${entryKey}" from "${name}"? This cannot be undone.`}
          action={async () => {
            await deleteMapKey(workspaceId, name, entryKey, current.revision);
            await refreshCollection(client, workspaceId, "maps", name);
            onDeleted();
          }}
        />
      </div>
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
