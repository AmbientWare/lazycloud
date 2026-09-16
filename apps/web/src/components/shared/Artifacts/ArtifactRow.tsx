import { Download, File, FileImage, FileText, Loader2, Trash2 } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { TableRow, TableCell } from "@/components/ui/table";
import { exactTime, formatBytes } from "@/lib/format";
import { downloadBlob } from "@/lib/files";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { useLiveNow } from "@/hooks/use-live-now";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fetchArtifactBlob } from "@/lib/queries/artifacts";
import { ArtifactPreview, type PreviewKind } from "./ArtifactPreview";
import type { ArtifactSummary } from "@/lib/api/schemas";

export function ArtifactDeletionTime({ artifact }: { artifact: ArtifactSummary }) {
  const now = useLiveNow(true);
  if (artifact.deletion_failed) return <span className="text-destructive">Deletion failed</span>;
  if (artifact.deleting) return <span>Deleting…</span>;
  const date = new Date(artifact.expires_at);
  return (
    <time dateTime={artifact.expires_at} title={exactTime(artifact.expires_at)}>
      {date.getTime() <= now
        ? "Scheduled for deletion"
        : `Deletes ${date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(date.getFullYear() !== new Date(now).getFullYear() ? { year: "numeric" } : {}) })}`}
    </time>
  );
}

/**
 * Types that are plain text but are not spelled `text/*`. These are what
 * Python's `mimetypes` actually returns for the file extensions a task is
 * likely to save, so they belong in the text branch rather than falling
 * through to download-only.
 */
const TEXTUAL_CONTENT_TYPES = new Set([
  "application/javascript",
  "application/json",
  "application/toml",
  "application/x-yaml",
  "application/xml",
  "application/yaml",
]);

/**
 * How to render an artifact. The stored content type decides, which is why the
 * SDK infers it at save time rather than leaving everything octet-stream.
 */
function previewKind(contentType: string): PreviewKind | "none" {
  if (contentType.startsWith("image/")) return "image";
  if (contentType === "application/pdf") return "pdf";
  if (contentType.startsWith("text/") || TEXTUAL_CONTENT_TYPES.has(contentType)) return "text";
  return "none";
}

export function ArtifactRow({
  artifact,
  workspaceId,
  workspaceName,
  compact = false,
  showSource = true,
  selection,
  onDelete,
}: {
  artifact: ArtifactSummary;
  workspaceId: string;
  workspaceName: string;
  compact?: boolean;
  showSource?: boolean;
  selection?: ReactNode;
  onDelete: () => void;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const previewButton = useRef<HTMLButtonElement>(null);
  const previewTitle = useRef<HTMLHeadingElement>(null);
  const [downloading, setDownloading] = useState(false);
  const now = useLiveNow(true);
  const kind = previewKind(artifact.content_type);
  const unavailable = artifact.deleting || new Date(artifact.expires_at).getTime() <= now;
  const FileIcon = kind === "image" ? FileImage : kind === "text" ? FileText : File;

  async function download(): Promise<void> {
    setDownloading(true);
    try {
      downloadBlob(artifact.filename, await fetchArtifactBlob(workspaceId, artifact));
    } catch (error) {
      toast.error("Download failed", {
        description: error instanceof Error ? error.message : "unknown error",
      });
    } finally {
      setDownloading(false);
    }
  }

  const source = (
    <>
      {artifact.app_id ? (
        <Link
          className="interactive-link max-w-full truncate"
          to="/w/$workspace/apps/$appId"
          params={{ workspace: workspaceName, appId: artifact.app_id }}
        >
          {artifact.app_name || "App"}
        </Link>
      ) : (
        <span className="truncate">{artifact.app_name || "Unknown app"}</span>
      )}
      {artifact.task_id && (
        <Link
          className="interactive-link shrink-0"
          to="/w/$workspace/tasks/$taskId"
          params={{ workspace: workspaceName, taskId: artifact.task_id }}
          aria-label={`View task for ${artifact.filename}`}
        >
          View task
        </Link>
      )}
    </>
  );
  const filename = (
    <div className="flex min-w-0 items-center gap-2.5">
      <FileIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <button
        ref={previewButton}
        type="button"
        className="min-w-0 truncate rounded-sm text-left text-sm font-medium outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring disabled:no-underline disabled:opacity-60"
        title={artifact.filename}
        aria-label={`${kind === "none" ? "Download" : "Preview"} ${artifact.filename}`}
        disabled={unavailable || downloading}
        onClick={() => (kind === "none" ? void download() : setOpen(true))}
      >
        {artifact.filename}
      </button>
    </div>
  );
  const actions = (
    <div className="flex shrink-0 items-center justify-end gap-0.5">
      <Button
        variant="ghost"
        size="icon"
        className="size-7 text-muted-foreground"
        aria-label={`Download ${artifact.filename}`}
        title="Download"
        disabled={downloading || unavailable}
        onClick={() => void download()}
      >
        {downloading ? (
          <Loader2 className="size-4 animate-spin" />
        ) : (
          <Download className="size-4" />
        )}
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="size-7 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
        aria-label={`Delete ${artifact.filename}`}
        title={artifact.deletion_failed ? "Retry deletion" : "Delete"}
        onClick={onDelete}
        disabled={artifact.deleting && !artifact.deletion_failed}
      >
        <Trash2 className="size-3.5" />
      </Button>
    </div>
  );
  return (
    <>
      {compact ? (
        <div className="flex items-center gap-3 border-b px-4 py-3 last:border-0">
          {selection}
          <div className="min-w-0 flex-1">
            {filename}
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6.5 text-xs text-muted-foreground">
              <span>{formatBytes(artifact.size)}</span>
              <ArtifactDeletionTime artifact={artifact} />
              {showSource && source}
            </div>
          </div>
          {actions}
        </div>
      ) : (
        <TableRow>
          <TableCell className="w-10 pr-0">{selection}</TableCell>
          <TableCell className="max-w-0 py-3">
            {filename}
            {showSource && (
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 pl-6.5 text-xs text-muted-foreground @3xl:hidden">
                {source}
              </div>
            )}
          </TableCell>
          <TableCell className="hidden max-w-40 text-xs text-muted-foreground @3xl:table-cell">
            <div className="flex flex-col items-start gap-1">{source}</div>
          </TableCell>
          <TableCell
            className="hidden whitespace-nowrap text-right text-xs text-muted-foreground @xl:table-cell"
            title={artifact.content_type}
          >
            {formatBytes(artifact.size)}
          </TableCell>
          <TableCell className="hidden whitespace-nowrap text-xs text-muted-foreground @4xl:table-cell">
            {artifact.created_at ? <LiveRelativeTime value={artifact.created_at} /> : "Unknown"}
          </TableCell>
          <TableCell className="hidden whitespace-nowrap text-xs text-muted-foreground @lg:table-cell">
            <ArtifactDeletionTime artifact={artifact} />
          </TableCell>
          <TableCell className="w-20 pl-0">{actions}</TableCell>
        </TableRow>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          className="flex h-[min(40rem,80svh)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            previewTitle.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            previewButton.current?.focus();
          }}
        >
          <DialogHeader className="shrink-0 gap-1 border-b px-4 py-3 text-left">
            <DialogTitle
              ref={previewTitle}
              tabIndex={-1}
              className="mono truncate pr-6 text-sm outline-none"
            >
              {artifact.filename}
            </DialogTitle>
            <DialogDescription className="text-xs">
              {artifact.content_type} · {formatBytes(artifact.size)}
            </DialogDescription>
          </DialogHeader>
          {kind !== "none" && (
            <ArtifactPreview
              key={artifact.id}
              artifact={artifact}
              workspaceId={workspaceId}
              kind={kind}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
