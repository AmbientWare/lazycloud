import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { File, RefreshCw, Search, SlidersHorizontal, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { formatCostNanos } from "@/lib/money";
import { countLabel, formatBytes } from "@/lib/format";
import { useWorkspace } from "@/lib/workspace-context";
import type { ArtifactSummary } from "@/lib/api/schemas/artifacts";
import {
  artifactsQuery,
  artifactStorageQuery,
  deleteArtifact,
  type ArtifactFilters,
} from "@/lib/queries/artifacts";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import { workspaceQueryKeys, accountQueryKeys } from "@/lib/queries/workspace-keys";
import { ArtifactRow } from "./ArtifactRow";

export function Artifacts({ workspaceId, taskId }: { workspaceId: string; taskId?: string }) {
  const { workspace } = useWorkspace();
  const client = useQueryClient();
  const [filters, setFilters] = useState<ArtifactFilters>({});
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [deleting, setDeleting] = useState<ArtifactSummary[] | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const query = useInfiniteQuery(
    artifactsQuery(workspaceId, { ...filters, ...(taskId ? { task_id: taskId } : {}) }),
  );
  const summary = useQuery({ ...artifactStorageQuery(workspaceId), enabled: !taskId });
  const apps = useQuery({ ...appSummariesQueryOptions(workspaceId), enabled: !taskId });
  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];
  const selected = rows.filter((row) => selectedIds.includes(row.id));
  const selectable = rows.filter((row) => !row.deleting).slice(0, 100);
  const allSelected =
    selectable.length > 0 && selectable.every((row) => selectedIds.includes(row.id));
  const hasFilters = Object.values(filters).some(Boolean);
  const extraFilters = Boolean(filters.task_id || filters.created_after || filters.created_before);
  const updateFilter = (key: keyof ArtifactFilters, value: string) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setSelectedIds([]);
  };
  const clearFilters = () => {
    setFilters({});
    setSelectedIds([]);
  };
  const invalidate = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: workspaceQueryKeys.storage.artifacts(workspaceId) }),
      client.invalidateQueries({ queryKey: accountQueryKeys.usage.root() }),
    ]);
  };
  const fileRow = (artifact: ArtifactSummary, compact = false) => (
    <ArtifactRow
      key={artifact.id}
      artifact={artifact}
      workspaceId={workspaceId}
      workspaceName={workspace.name}
      compact={compact}
      showSource={!taskId}
      onDelete={() => setDeleting([artifact])}
      selection={
        !taskId && (
          <Checkbox
            className="size-3.5"
            aria-label={`Select ${artifact.filename}`}
            checked={selectedIds.includes(artifact.id)}
            disabled={
              artifact.deleting || (selected.length >= 100 && !selectedIds.includes(artifact.id))
            }
            onCheckedChange={(checked) =>
              setSelectedIds(
                checked === true
                  ? [...selectedIds, artifact.id]
                  : selectedIds.filter((id) => id !== artifact.id),
              )
            }
          />
        )
      }
    />
  );
  return (
    <div className="@container flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2.5">
        <div className="relative min-w-32 flex-1 @2xl:max-w-72">
          <Search
            className="pointer-events-none absolute top-2 left-2.5 size-4 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            className="h-8 pl-8 text-xs"
            aria-label="Search artifact filenames"
            placeholder="Search files…"
            value={filters.search ?? ""}
            onChange={(event) => updateFilter("search", event.target.value)}
          />
        </div>
        {!taskId && (
          <>
            <Select
              value={filters.app_id || "all"}
              onValueChange={(value) => updateFilter("app_id", value === "all" ? "" : value)}
            >
              <SelectTrigger size="sm" className="max-w-44 text-xs" aria-label="Filter by app">
                <SelectValue placeholder="All apps" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All apps</SelectItem>
                {apps.data?.items.map(({ app }) => (
                  <SelectItem key={app.id} value={app.id}>
                    {app.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={filters.content_type || "all"}
              onValueChange={(value) => updateFilter("content_type", value === "all" ? "" : value)}
            >
              <SelectTrigger size="sm" className="text-xs" aria-label="Filter by file type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All types</SelectItem>
                <SelectItem value="image/">Images</SelectItem>
                <SelectItem value="application/pdf">PDF</SelectItem>
                <SelectItem value="text/">Text</SelectItem>
                <SelectItem value="application/json">JSON</SelectItem>
                <SelectItem value="application/zip">Archives</SelectItem>
              </SelectContent>
            </Select>
            <Button
              size="sm"
              variant={showFilters || extraFilters ? "secondary" : "ghost"}
              aria-expanded={showFilters}
              aria-controls="artifact-filters"
              onClick={() => setShowFilters(!showFilters)}
            >
              <SlidersHorizontal className="size-3.5" />
              Filters
            </Button>
          </>
        )}
        {hasFilters && (
          <Button size="sm" variant="ghost" onClick={clearFilters}>
            Clear
          </Button>
        )}
        <div className="ml-auto flex items-center gap-1">
          <Button
            size="icon"
            variant="ghost"
            className="size-8 text-muted-foreground"
            aria-label="Refresh artifacts"
            title="Refresh"
            disabled={query.isFetching}
            onClick={() => void invalidate()}
          >
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
      </div>
      {!taskId && showFilters && (
        <div
          id="artifact-filters"
          className="flex flex-wrap items-end gap-3 border-b bg-muted/20 px-3 py-3"
        >
          <label className="flex min-w-36 flex-1 flex-col gap-1.5 text-xs text-muted-foreground">
            Task ID
            <Input
              className="h-8 text-xs"
              placeholder="Any task"
              value={filters.task_id ?? ""}
              onChange={(event) => updateFilter("task_id", event.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            Saved from
            <Input
              className="h-8 w-40 text-xs"
              type="date"
              value={filters.created_after?.slice(0, 10) ?? ""}
              onChange={(event) =>
                updateFilter(
                  "created_after",
                  event.target.value ? `${event.target.value}T00:00:00Z` : "",
                )
              }
            />
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            Saved before
            <Input
              className="h-8 w-40 text-xs"
              type="date"
              value={filters.created_before?.slice(0, 10) ?? ""}
              onChange={(event) =>
                updateFilter(
                  "created_before",
                  event.target.value ? `${event.target.value}T00:00:00Z` : "",
                )
              }
            />
          </label>
        </div>
      )}
      {selected.length > 0 && !taskId && (
        <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 px-3 py-2 text-xs">
          <span className="mr-auto">
            {selected.length} selected{" "}
            <span className="ml-2 text-muted-foreground">
              {formatBytes(selected.reduce((sum, row) => sum + row.size, 0))}
            </span>
          </span>
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={() => setDeleting(selected)}
          >
            <Trash2 className="size-3.5" />
            Delete
          </Button>
          <Button
            size="icon"
            variant="ghost"
            className="size-7"
            aria-label="Clear selection"
            onClick={() => setSelectedIds([])}
          >
            <X className="size-3.5" />
          </Button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        {query.isPending ? (
          <div className="divide-y" aria-label="Loading artifacts">
            {[0, 1, 2, 3, 4].map((key) => (
              <div key={key} className="flex h-14 items-center gap-3 px-4">
                <div className="size-4 animate-pulse rounded-sm bg-muted" />
                <div className="h-3 w-1/3 animate-pulse rounded-sm bg-muted" />
                <div className="ml-auto h-3 w-20 animate-pulse rounded-sm bg-muted" />
              </div>
            ))}
          </div>
        ) : query.error && !query.data ? (
          <div role="alert" className="flex items-center justify-center gap-2 p-8 text-sm">
            Could not load artifacts.
            <Button size="sm" variant="outline" onClick={() => void query.refetch()}>
              Retry
            </Button>
          </div>
        ) : rows.length === 0 ? (
          <PanelEmpty
            icon={File}
            message={
              hasFilters
                ? "No files match your filters"
                : taskId
                  ? "This task has no artifacts"
                  : "No artifacts saved yet"
            }
            className="min-h-40 h-full"
          />
        ) : taskId ? (
          rows.map((artifact) => fileRow(artifact, true))
        ) : (
          <>
            <div className="@lg:hidden">{rows.map((artifact) => fileRow(artifact, true))}</div>
            <Table className="hidden table-fixed @lg:table">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10 pr-0">
                    <Checkbox
                      className="size-3.5"
                      aria-label="Select loaded artifacts"
                      checked={allSelected ? true : selected.length > 0 ? "indeterminate" : false}
                      onCheckedChange={(checked) =>
                        setSelectedIds(checked === true ? selectable.map((row) => row.id) : [])
                      }
                    />
                  </TableHead>
                  <TableHead className="normal-case tracking-normal">File</TableHead>
                  <TableHead className="hidden w-40 normal-case tracking-normal @3xl:table-cell">
                    App and task
                  </TableHead>
                  <TableHead className="hidden w-20 text-right normal-case tracking-normal @xl:table-cell">
                    Size
                  </TableHead>
                  <TableHead className="hidden w-32 normal-case tracking-normal @4xl:table-cell">
                    Saved
                  </TableHead>
                  <TableHead className="w-44 normal-case tracking-normal">Retention</TableHead>
                  <TableHead className="w-20">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>{rows.map((artifact) => fileRow(artifact))}</TableBody>
            </Table>
          </>
        )}
        <InfiniteScrollBoundary
          key={JSON.stringify(filters)}
          nextCursor={query.data?.pages.at(-1)?.next}
          loading={query.isFetchingNextPage}
          error={query.isFetchNextPageError}
          onLoadMore={() => void query.fetchNextPage()}
          resourceLabel="artifacts"
        />
      </div>
      {!taskId && (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t px-3 py-2 text-xs text-muted-foreground">
          {summary.data ? (
            <>
              <span>
                {countLabel(summary.data.count, "file")}
                <span className="ml-3 tabular-nums">{formatBytes(summary.data.size_bytes)}</span>
              </span>
              <span
                title={`Billed as volume storage. ${formatCostNanos(summary.data.accrued_nanos)} accrued since ${new Date(summary.data.accrued_since).toLocaleDateString()}.`}
              >
                {summary.data.estimated_monthly_nanos === null
                  ? "Storage rate unavailable"
                  : `Volume storage: ${formatCostNanos(summary.data.estimated_monthly_nanos)} / month`}
              </span>
              <span>
                New uploads expire after {countLabel(summary.data.retention_seconds / 86400, "day")}
              </span>
            </>
          ) : (
            <span>{summary.error ? "Storage totals unavailable" : "Loading storage totals…"}</span>
          )}
        </div>
      )}
      {deleting && (
        <ArtifactDeleteDialog
          artifacts={deleting}
          workspaceId={workspaceId}
          onClose={() => setDeleting(null)}
          onComplete={() => {
            setSelectedIds([]);
            setDeleting(null);
          }}
          invalidate={invalidate}
        />
      )}
    </div>
  );
}

function ArtifactDeleteDialog({
  artifacts,
  workspaceId,
  onClose,
  onComplete,
  invalidate,
}: {
  artifacts: ArtifactSummary[];
  workspaceId: string;
  onClose: () => void;
  onComplete: () => void;
  invalidate: () => Promise<void>;
}) {
  const mutation = useMutation({
    mutationFn: async () => {
      for (const artifact of artifacts) await deleteArtifact(workspaceId, artifact.id);
    },
    onSuccess: () => {
      toast.success("Deletion requested");
      onComplete();
    },
    onSettled: invalidate,
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {artifacts.length === 1 ? "Delete file?" : `Delete ${artifacts.length} files?`}
          </DialogTitle>
          <DialogDescription>
            This permanently removes the selected files. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-60 divide-y overflow-auto rounded-md border text-sm">
          {artifacts.map((item) => (
            <div key={item.id} className="flex items-center gap-3 px-3 py-2.5">
              <File className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{item.filename}</span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatBytes(item.size)}
              </span>
            </div>
          ))}
        </div>
        {mutation.error && (
          <p role="alert" className="text-sm text-destructive">
            {mutation.error.message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" disabled={mutation.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "Deleting…" : "Delete permanently"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
