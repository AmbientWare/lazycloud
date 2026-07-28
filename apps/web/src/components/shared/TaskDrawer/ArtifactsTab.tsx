import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { artifactPublicUrl, taskArtifactsQuery } from "@/lib/queries/artifacts";
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

/**
 * How to show an artifact. The stored content type decides, which is why the
 * SDK infers it at save time rather than leaving everything octet-stream.
 */
function previewKind(contentType: string): "image" | "pdf" | "text" | "download" {
  if (contentType.startsWith("image/")) return "image";
  if (contentType === "application/pdf") return "pdf";
  if (contentType.startsWith("text/") || contentType === "application/json") return "text";
  return "download";
}

function ArtifactRow({
  artifact,
  workspaceId,
}: {
  artifact: ArtifactSummary;
  workspaceId: string;
}): ReactNode {
  const [url, setUrl] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const kind = previewKind(artifact.content_type);

  async function open(): Promise<void> {
    if (url) return;
    setPending(true);
    try {
      const response = await artifactPublicUrl(
        workspaceId,
        artifact.id,
        artifact.task_id,
        artifact.filename,
      );
      setUrl(response.public_url);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="border-b border-border px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="mono truncate text-xs" title={artifact.filename}>
          {artifact.filename}
        </span>
        <span className="text-xs text-muted-foreground">{formatSize(artifact.size)}</span>
        <span className="truncate text-xs text-muted-foreground">{artifact.content_type}</span>
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto"
          disabled={pending}
          onClick={() => void open()}
        >
          {kind === "download" ? "Download" : "Preview"}
        </Button>
      </div>
      {url && kind === "image" && (
        <img src={url} alt={artifact.filename} className="mt-2 max-h-96 max-w-full rounded" />
      )}
      {url && kind === "pdf" && <iframe src={url} title={artifact.filename} className="mt-2 h-96 w-full rounded" />}
      {url && (kind === "text" || kind === "download") && (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-block text-xs underline"
        >
          Open {artifact.filename}
        </a>
      )}
    </div>
  );
}

export function ArtifactsTab({
  workspaceId,
  taskId,
}: {
  workspaceId: string;
  taskId: string;
}): ReactNode {
  const { data, isLoading, error } = useQuery(taskArtifactsQuery(workspaceId, taskId));

  if (isLoading) {
    return <div className="px-3 py-2 text-xs text-muted-foreground">Loading artifacts…</div>;
  }
  if (error) {
    return <div className="px-3 py-2 text-xs text-muted-foreground">Could not load artifacts</div>;
  }
  const artifacts = data?.data ?? [];
  if (artifacts.length === 0) {
    return <div className="px-3 py-2 text-xs text-muted-foreground">No artifacts saved</div>;
  }
  return (
    <div className="flex min-h-full flex-col overflow-auto">
      {artifacts.map((artifact) => (
        <ArtifactRow key={artifact.id} artifact={artifact} workspaceId={workspaceId} />
      ))}
    </div>
  );
}
