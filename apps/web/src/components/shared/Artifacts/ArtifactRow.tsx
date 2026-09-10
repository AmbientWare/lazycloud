import { Download, File, FileImage, FileText, Loader2, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { TableRow, TableCell } from "@/components/ui/table";
import { exactTime, formatBytes } from "@/lib/format";
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

type PreviewKind = "image" | "pdf" | "text" | "none";

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
function previewKind(contentType: string): PreviewKind {
  if (contentType.startsWith("image/")) return "image";
  if (contentType === "application/pdf") return "pdf";
  if (contentType.startsWith("text/") || TEXTUAL_CONTENT_TYPES.has(contentType)) return "text";
  return "none";
}

/**
 * Renders the fetched bytes according to what they are. Every branch fills the
 * dialog's fixed body and scrolls inside it, so the dialog is the same size
 * whatever the artifact turns out to be.
 */
function PreviewBody({
  artifact,
  workspaceId,
  kind,
}: {
  artifact: ArtifactSummary;
  workspaceId: string;
  kind: PreviewKind;
}): ReactNode {
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [undecodable, setUndecodable] = useState(false);
  // Depend on the identifying fields rather than the artifact object: the
  // list query hands back a fresh object on every refetch, and re-running
  // this effect would revoke a URL the rendered element is still showing.
  const { id, task_id: taskId, filename } = artifact;

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    void (async () => {
      try {
        const blob = await fetchArtifactBlob(workspaceId, { id, task_id: taskId, filename });
        const decoded = kind === "text" ? await blob.text() : null;
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          // Cleanup already ran, so nothing else will revoke this one.
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
          return;
        }
        if (decoded !== null) setText(decoded);
        setUrl(objectUrl);
      } catch (error) {
        if (!cancelled) setFailure(error instanceof Error ? error.message : "unknown error");
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [workspaceId, id, taskId, filename, kind]);

  if (failure) {
    return (
      <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
        {failure}
      </div>
    );
  }
  if (!url) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (kind === "image") {
    // A file can carry an image content type and still not decode. Saying so
    // beats an empty dialog that looks like the fetch silently failed.
    if (undecodable) {
      return (
        <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
          This image could not be decoded. Download it to inspect the file.
        </div>
      );
    }
    return (
      <div className="flex flex-1 items-center justify-center overflow-auto rounded bg-muted p-3">
        <img
          src={url}
          alt={artifact.filename}
          onError={() => setUndecodable(true)}
          className="max-h-full max-w-full object-contain"
        />
      </div>
    );
  }
  if (kind === "pdf") {
    return <iframe src={url} title={artifact.filename} className="flex-1 rounded border-0" />;
  }
  return (
    <pre className="mono flex-1 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap">
      {text ?? ""}
    </pre>
  );
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
  const [downloading, setDownloading] = useState(false);
  const now = useLiveNow(true);
  const kind = previewKind(artifact.content_type);
  const unavailable =
    artifact.deleting ||
    new Date(artifact.expires_at).getTime() <= now;
  const FileIcon = kind === "image" ? FileImage : kind === "text" ? FileText : File;

  async function download(): Promise<void> {
    setDownloading(true);
    let url: string | null = null;
    try {
      url = URL.createObjectURL(await fetchArtifactBlob(workspaceId, artifact));
      const link = document.createElement("a");
      link.href = url;
      link.download = artifact.filename;
      link.click();
    } catch (error) {
      toast.error("Download failed", {
        description: error instanceof Error ? error.message : "unknown error",
      });
    } finally {
      if (url) URL.revokeObjectURL(url);
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
        {/* One size for every artifact: the content scrolls or scales inside
            it rather than the dialog resizing around the content. */}
        <DialogContent className="flex h-[80vh] flex-col gap-3 sm:max-w-3xl">
          <DialogHeader className="shrink-0">
            <DialogTitle className="mono truncate pr-6 text-sm">{artifact.filename}</DialogTitle>
            <DialogDescription className="text-xs">
              {artifact.content_type} · {formatBytes(artifact.size)}
            </DialogDescription>
          </DialogHeader>
          {open && <PreviewBody artifact={artifact} workspaceId={workspaceId} kind={kind} />}
        </DialogContent>
      </Dialog>
    </>
  );
}
