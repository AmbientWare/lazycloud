import { Download, Loader2, Search } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fetchArtifactBlob } from "@/lib/queries/artifacts";
import type { ArtifactSummary } from "@/lib/api/schemas";

/** Bytes as something a person reads at a glance. */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
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
          This image could not be decoded — download it to inspect the file.
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
}: {
  artifact: ArtifactSummary;
  workspaceId: string;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const kind = previewKind(artifact.content_type);

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

  return (
    <div className="flex items-center gap-2 border-b border-border px-3 py-2">
      <span className="mono truncate text-xs" title={artifact.filename}>
        {artifact.filename}
      </span>
      <span className="shrink-0 text-xs text-muted-foreground">{formatSize(artifact.size)}</span>
      <span className="truncate text-xs text-muted-foreground">{artifact.content_type}</span>
      <div className="ml-auto flex shrink-0 items-center">
        {kind !== "none" && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Preview ${artifact.filename}`}
            title="Preview"
            onClick={() => setOpen(true)}
          >
            <Search className="size-4" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Download ${artifact.filename}`}
          title="Download"
          disabled={downloading}
          onClick={() => void download()}
        >
          {downloading ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <Download className="size-4" />
          )}
        </Button>
      </div>
      <Dialog open={open} onOpenChange={setOpen}>
        {/* One size for every artifact: the content scrolls or scales inside
            it rather than the dialog resizing around the content. */}
        <DialogContent className="flex h-[80vh] flex-col gap-3 sm:max-w-3xl">
          <DialogHeader className="shrink-0">
            <DialogTitle className="mono truncate pr-6 text-sm">{artifact.filename}</DialogTitle>
            <DialogDescription className="text-xs">
              {artifact.content_type} · {formatSize(artifact.size)}
            </DialogDescription>
          </DialogHeader>
          {open && <PreviewBody artifact={artifact} workspaceId={workspaceId} kind={kind} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}
