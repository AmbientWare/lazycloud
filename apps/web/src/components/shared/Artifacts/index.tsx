import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { formatBytes, relativeTime } from "@/lib/format";
import { useWorkspace } from "@/lib/workspace-context";
import type { ArtifactSummary, ArtifactRetentionPreview } from "@/lib/api/schemas/artifacts";
import {
  artifactsQuery,
  artifactStorageQuery,
  deleteArtifact,
  updateArtifactRetention,
  updateWorkspaceArtifactRetention,
  applyArtifactRetention,
  type ArtifactFilters,
} from "@/lib/queries/artifacts";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import { workspaceQueryKeys, accountQueryKeys } from "@/lib/queries/workspace-keys";
import { ArtifactRow } from "./ArtifactRow";

type Action =
  | { kind: "delete"; artifacts: ArtifactSummary[] }
  | { kind: "retention"; artifact: ArtifactSummary }
  | { kind: "workspace"; seconds: number | null }
  | { kind: "apply"; seconds: number | null; preview: ArtifactRetentionPreview };

const money = (nanos: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 4,
  }).format(nanos / 1e9);

export function Artifacts({ workspaceId, taskId }: { workspaceId: string; taskId?: string }) {
  const { workspace } = useWorkspace();
  const client = useQueryClient();
  const [filters, setFilters] = useState<ArtifactFilters>({});
  const [selected, setSelected] = useState<ArtifactSummary[]>([]);
  const [action, setAction] = useState<Action | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const query = useInfiniteQuery(
    artifactsQuery(workspaceId, { ...filters, ...(taskId ? { task_id: taskId } : {}) }),
  );
  const summary = useQuery({ ...artifactStorageQuery(workspaceId), enabled: !taskId });
  const apps = useQuery({ ...appSummariesQueryOptions(workspaceId), enabled: !taskId });
  const rows = query.data?.pages.flatMap((page) => page.data) ?? [];
  const updateFilter = (key: keyof ArtifactFilters, value: string) => {
    setFilters({ ...filters, [key]: value });
    setSelected([]);
  };
  const invalidate = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: workspaceQueryKeys.storage.artifacts(workspaceId) }),
      client.invalidateQueries({ queryKey: accountQueryKeys.usage.root() }),
    ]);
  };
  async function previewRetention() {
    if (!summary.data) return;
    setPreviewing(true);
    try {
      const seconds = summary.data.retention_seconds;
      const preview = await applyArtifactRetention(
        workspaceId,
        selected.map((item) => item.id),
        seconds,
      );
      setAction({ kind: "apply", seconds, preview });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not preview retention");
    } finally {
      setPreviewing(false);
    }
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!taskId && (
        <div className="flex flex-wrap items-center gap-4 border-b p-3 text-xs text-muted-foreground">
          {summary.data ? (
            <>
              <span>
                {summary.data.count} artifacts · {formatBytes(summary.data.size_bytes)}
              </span>
              <span>
                {summary.data.estimated_monthly_nanos === null
                  ? "Storage rate unavailable"
                  : `${money(summary.data.estimated_monthly_nanos)}/month at current size`}
              </span>
              <span title={`Since ${summary.data.accrued_since}`}>
                {money(summary.data.accrued_nanos)} accrued this month
              </span>
              <span>Billed as Volume storage · Artifacts</span>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  summary.data &&
                  setAction({ kind: "workspace", seconds: summary.data.retention_seconds })
                }
              >
                Default retention:{" "}
                {summary.data.retention_seconds === null
                  ? "Keep until deleted"
                  : `${summary.data.retention_seconds / 86400} days`}
              </Button>
            </>
          ) : (
            <span>
              {summary.error ? "Storage totals could not be loaded" : "Loading storage totals…"}
            </span>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-2 border-b p-3">
        <Input
          className="h-8 w-48"
          aria-label="Search artifact filenames"
          placeholder="Search filenames"
          value={filters.search ?? ""}
          onChange={(e) => updateFilter("search", e.target.value)}
        />
        {!taskId && (
          <>
            <select
              className="h-8 rounded border bg-background px-2 text-xs"
              aria-label="Filter by app"
              value={filters.app_id ?? ""}
              onChange={(e) => updateFilter("app_id", e.target.value)}
            >
              <option value="">All apps</option>
              {apps.data?.items.map(({ app }) => (
                <option key={app.id} value={app.id}>
                  {app.name}
                </option>
              ))}
            </select>
            <Input
              className="h-8 w-44"
              aria-label="Filter by task ID"
              placeholder="Task ID"
              value={filters.task_id ?? ""}
              onChange={(e) => updateFilter("task_id", e.target.value)}
            />
          </>
        )}
        <select
          className="h-8 rounded border bg-background px-2 text-xs"
          aria-label="Filter by file type"
          value={filters.content_type ?? ""}
          onChange={(e) => updateFilter("content_type", e.target.value)}
        >
          <option value="">All types</option>
          <option value="image/">Images</option>
          <option value="application/pdf">PDF</option>
          <option value="text/">Text</option>
          <option value="application/json">JSON</option>
          <option value="application/zip">Archives</option>
        </select>
        <label className="flex items-center gap-1 text-xs">
          From{" "}
          <input
            className="h-8 rounded border bg-background px-2"
            type="date"
            onChange={(e) =>
              updateFilter("created_after", e.target.value ? `${e.target.value}T00:00:00Z` : "")
            }
          />
        </label>
        <label className="flex items-center gap-1 text-xs">
          Before{" "}
          <input
            className="h-8 rounded border bg-background px-2"
            type="date"
            onChange={(e) =>
              updateFilter("created_before", e.target.value ? `${e.target.value}T00:00:00Z` : "")
            }
          />
        </label>
        <Button size="sm" variant="ghost" onClick={() => void invalidate()}>
          Refresh
        </Button>
      </div>
      {selected.length > 0 && (
        <div className="flex items-center gap-3 border-b px-3 py-2 text-xs">
          <span>
            {selected.length} selected ·{" "}
            {formatBytes(selected.reduce((sum, row) => sum + row.size, 0))}
          </span>
          <Button
            size="sm"
            variant="destructive"
            onClick={() => setAction({ kind: "delete", artifacts: selected })}
          >
            Delete selected
          </Button>
          {!taskId && (
            <Button
              size="sm"
              variant="outline"
              disabled={!summary.data || previewing}
              onClick={() => void previewRetention()}
            >
              Apply workspace retention…
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
            Clear selection
          </Button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        {query.isPending ? (
          <div className="animate-pulse space-y-3 p-3" aria-label="Loading artifacts">
            {[0, 1, 2].map((key) => (
              <div key={key} className="h-14 rounded bg-muted" />
            ))}
          </div>
        ) : query.error && !query.data ? (
          <div role="alert" className="p-3 text-sm">
            Could not load artifacts.{" "}
            <Button variant="ghost" onClick={() => void query.refetch()}>
              Retry
            </Button>
          </div>
        ) : rows.length === 0 ? (
          <p className="p-4 text-sm text-muted-foreground">No artifacts found</p>
        ) : (
          rows.map((artifact) => (
            <div key={artifact.id} className="border-b">
              <div className="flex items-center">
                <input
                  type="checkbox"
                  className="ml-3"
                  aria-label={`Select ${artifact.filename}`}
                  checked={selected.some((item) => item.id === artifact.id)}
                  disabled={
                    selected.length >= 100 && !selected.some((item) => item.id === artifact.id)
                  }
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, artifact]
                        : selected.filter((item) => item.id !== artifact.id),
                    )
                  }
                />
                <div className="min-w-0 flex-1">
                  {artifact.deleting ? (
                    <div className="p-3 text-xs">
                      {artifact.filename} ·{" "}
                      {artifact.deletion_failed ? "Deletion failed." : "Deletion pending."} Storage
                      is billed until removal is confirmed.
                    </div>
                  ) : (
                    <ArtifactRow artifact={artifact} workspaceId={workspaceId} />
                  )}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-3 px-3 py-2 text-xs text-muted-foreground">
                {artifact.created_at && (
                  <time dateTime={artifact.created_at} title={artifact.created_at}>
                    Saved {relativeTime(artifact.created_at)}
                  </time>
                )}
                {artifact.app_id && (
                  <Link
                    to="/w/$workspace/apps/$appId"
                    params={{ workspace: workspace.name, appId: artifact.app_id }}
                  >
                    {artifact.app_name || "App"}
                  </Link>
                )}
                {!taskId && (
                  <Link
                    to="/w/$workspace/tasks/$taskId"
                    params={{ workspace: workspace.name, taskId: artifact.task_id }}
                  >
                    View task
                  </Link>
                )}
                <span title={artifact.expires_at ?? undefined}>
                  {artifact.expires_at
                    ? `Expires ${new Date(artifact.expires_at).toLocaleString()}`
                    : "Keep until deleted"}{" "}
                  ·{" "}
                  {artifact.retention_source === "explicit"
                    ? "Artifact setting"
                    : "Workspace default at save"}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={artifact.deleting}
                  onClick={() => setAction({ kind: "retention", artifact })}
                >
                  Retention
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setAction({ kind: "delete", artifacts: [artifact] })}
                >
                  {artifact.deleting ? "Retry deletion" : "Delete"}
                </Button>
              </div>
            </div>
          ))
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
      {action && (
        <ArtifactActionDialog
          action={action}
          workspaceId={workspaceId}
          onClose={() => setAction(null)}
          onComplete={() => {
            setSelected([]);
            setAction(null);
          }}
          invalidate={invalidate}
        />
      )}
    </div>
  );
}

function ArtifactActionDialog({
  action,
  workspaceId,
  onClose,
  onComplete,
  invalidate,
}: {
  action: Action;
  workspaceId: string;
  onClose: () => void;
  onComplete: () => void;
  invalidate: () => Promise<void>;
}) {
  const initial =
    action.kind === "retention"
      ? action.artifact.retention_seconds
      : action.kind === "workspace"
        ? action.seconds
        : null;
  const [days, setDays] = useState(initial === null ? "" : String(initial / 86400));
  const seconds = days === "" ? null : Number(days) * 86400;
  const valid = seconds === null || (Number.isSafeInteger(seconds) && seconds > 0);
  const mutation = useMutation({
    mutationFn: async () => {
      if (action.kind === "delete") {
        for (const artifact of action.artifacts) await deleteArtifact(workspaceId, artifact.id);
      } else if (action.kind === "retention")
        await updateArtifactRetention(workspaceId, action.artifact.id, seconds);
      else if (action.kind === "workspace")
        await updateWorkspaceArtifactRetention(workspaceId, seconds);
      else
        await applyArtifactRetention(
          workspaceId,
          action.preview.data.map((item) => item.id),
          action.seconds,
          true,
        );
    },
    onSuccess: onComplete,
    onSettled: invalidate,
  });
  const title =
    action.kind === "delete"
      ? "Delete artifacts"
      : action.kind === "workspace"
        ? "Workspace artifact retention"
        : action.kind === "apply"
          ? "Apply retention to selected artifacts"
          : `Retention for ${action.artifact.filename}`;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {action.kind === "delete"
              ? "Deletion permanently removes these files and stops their storage charges after removal is confirmed."
              : action.kind === "workspace"
                ? "This default applies to future saves that omit retention. Existing files keep their current expiration."
                : "Retention is measured from when each file was saved. Files already older than this duration will expire immediately and be permanently deleted."}
          </DialogDescription>
        </DialogHeader>
        {action.kind === "delete" && (
          <div className="max-h-60 overflow-auto text-sm">
            <p>
              {action.artifacts.length} files ·{" "}
              {formatBytes(action.artifacts.reduce((sum, item) => sum + item.size, 0))}
            </p>
            {action.artifacts.map((item) => (
              <p key={item.id}>
                {item.filename} · {formatBytes(item.size)}
              </p>
            ))}
          </div>
        )}
        {action.kind === "apply" && (
          <div className="max-h-60 overflow-auto text-sm">
            <p>
              {action.preview.data.length} files · {formatBytes(action.preview.total_bytes)}.
              Explicit artifact settings are preserved.
            </p>
            {action.preview.data.map((item) => (
              <p key={item.id}>
                {item.filename} ·{" "}
                {item.expires_at
                  ? `Expires ${new Date(item.expires_at).toLocaleString()}`
                  : "Keep until deleted"}
              </p>
            ))}
          </div>
        )}
        {(action.kind === "workspace" || action.kind === "retention") && (
          <label className="space-y-2 text-sm">
            Delete after this many days
            <Input
              aria-label="Retention days"
              type="number"
              min="0"
              step="any"
              placeholder="Keep until deleted"
              value={days}
              onChange={(e) => setDays(e.target.value)}
            />
            <span className="text-xs text-muted-foreground">
              Leave empty to keep until deleted. Storage charges continue while files are stored.
            </span>
          </label>
        )}
        {mutation.error && (
          <p role="alert" className="text-sm text-destructive">
            {mutation.error.message}
          </p>
        )}
        {!valid && (
          <p role="alert" className="text-sm text-destructive">
            Enter a positive duration in whole seconds, or leave empty.
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" disabled={mutation.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant={action.kind === "delete" ? "destructive" : "default"}
            disabled={
              !valid ||
              mutation.isPending ||
              (action.kind === "apply" && action.preview.data.length === 0)
            }
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? "Saving…"
              : action.kind === "delete"
                ? "Delete permanently"
                : "Save retention"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
