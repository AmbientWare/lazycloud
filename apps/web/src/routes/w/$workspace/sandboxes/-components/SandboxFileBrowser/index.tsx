import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  Download,
  File,
  Folder,
  Loader2,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { PodFileInfo } from "@/lib/api/schemas";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import {
  deleteSandboxFileMutationOptions,
  downloadSandboxFile,
  sandboxFilesQueryOptions,
  uploadSandboxFileMutationOptions,
} from "@/lib/queries/sandboxes";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

const ROOT_PATH = "/workspace";
const PREVIEW_LIMIT_BYTES = 256 * 1024;

export function SandboxFileBrowser({
  containerId,
  writable,
  className,
}: {
  containerId: string;
  writable: boolean;
  className?: string;
}) {
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState(ROOT_PATH);
  const [preview, setPreview] = useState<{ path: string; content: string } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const previewRequest = useRef<{ path: string; controller: AbortController } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  useEffect(() => () => previewRequest.current?.controller.abort(), []);

  const clearPreview = () => {
    previewRequest.current?.controller.abort();
    previewRequest.current = null;
    setPreview(null);
    setPreviewError(null);
    setPreviewing(false);
  };

  const navigateDirectory = (target: string) => {
    clearPreview();
    setPath(target);
    setDeleteTarget(null);
    setDownloadError(null);
  };

  const query = useQuery(sandboxFilesQueryOptions(workspace.id, containerId, path));
  const invalidateFiles = () =>
    queryClient.invalidateQueries({
      queryKey: workspaceQueryKeys.sandboxes.files(workspace.id, containerId),
    });
  const upload = useMutation({
    ...uploadSandboxFileMutationOptions(workspace.id, containerId),
    onSuccess: invalidateFiles,
  });
  const remove = useMutation({
    ...deleteSandboxFileMutationOptions(workspace.id, containerId),
    onMutate: (target) => {
      if (previewRequest.current?.path === target) clearPreview();
    },
    onSuccess: async (_, target) => {
      setDeleteTarget(null);
      if (previewRequest.current?.path === target) clearPreview();
      await invalidateFiles();
    },
  });

  const openFile = async (file: PodFileInfo) => {
    const target = joinPath(path, file.name);
    clearPreview();
    const controller = new AbortController();
    previewRequest.current = { path: target, controller };
    setPreviewing(true);
    try {
      const download = await downloadSandboxFile(
        workspace.id,
        containerId,
        target,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setPreview({ path: target, content: decodePreview(download.value_base64) });
    } catch (error) {
      if (controller.signal.aborted) return;
      setPreviewError(error instanceof Error ? error.message : "Failed to read file");
    } finally {
      if (!controller.signal.aborted) setPreviewing(false);
    }
  };

  const downloadFile = async (file: PodFileInfo) => {
    const target = joinPath(path, file.name);
    setDownloadError(null);
    try {
      const download = await downloadSandboxFile(workspace.id, containerId, target);
      const bytes = base64ToBytes(download.value_base64);
      downloadBlob(file.name, new Blob([bytes]));
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "Failed to download file");
    }
  };

  return (
    <div
      className={cn(
        "grid min-h-0 grid-rows-[minmax(12rem,1fr)_minmax(12rem,1fr)] overflow-hidden lg:grid-cols-2 lg:grid-rows-1",
        className,
      )}
    >
      <div className="flex min-h-0 flex-col overflow-hidden border-b border-border lg:border-b-0 lg:border-r">
        <div className="flex min-h-10 items-center gap-1 border-b border-border px-3 py-2 text-xs">
          <div className="flex min-w-0 flex-1 items-center overflow-x-auto">
            {breadcrumbs(path).map((crumb, index) => (
              <span key={crumb.path} className="flex items-center gap-1">
                {index > 0 ? <ChevronRight className="size-3 text-muted-foreground" /> : null}
                <button
                  type="button"
                  className="interactive-link mono text-muted-foreground hover:text-foreground"
                  onClick={() => navigateDirectory(crumb.path)}
                >
                  {crumb.label}
                </button>
              </span>
            ))}
          </div>
          {writable ? (
            <>
              <input
                ref={fileInput}
                type="file"
                className="hidden"
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  if (file) upload.mutate({ path: joinPath(path, file.name), file });
                  event.currentTarget.value = "";
                }}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-7"
                aria-label="Upload file"
                title="Upload file"
                disabled={upload.isPending}
                onClick={() => fileInput.current?.click()}
              >
                {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />}
              </Button>
            </>
          ) : null}
        </div>
        {upload.isError || remove.isError || downloadError ? (
          <p role="alert" className="border-b border-border px-3 py-2 text-xs text-destructive">
            {downloadError ?? (upload.error || remove.error)?.message}
          </p>
        ) : null}
        <div className="min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto">
          {query.isPending ? (
            <FileSkeleton />
          ) : query.isError ? (
            <PanelError message={query.error.message} />
          ) : query.data.files.length === 0 ? (
            <PanelEmpty message="Empty directory" className="p-4" />
          ) : (
            [...query.data.files].sort(byDirThenName).map((file) => {
              const target = joinPath(path, file.name);
              return (
                <div key={file.name} className="flex min-h-9 items-center gap-1 px-2">
                  <button
                    type="button"
                    className="interactive-row flex min-w-0 flex-1 items-center gap-2 rounded px-1 py-1.5 text-left text-sm"
                    onClick={() => (file.is_dir ? navigateDirectory(target) : void openFile(file))}
                  >
                    {file.is_dir ? (
                      <Folder className="size-3.5 shrink-0 text-brand" />
                    ) : (
                      <File className="size-3.5 shrink-0 text-muted-foreground" />
                    )}
                    <span className="mono truncate">{file.name}</span>
                    {!file.is_dir ? (
                      <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                        {formatBytes(file.size)}
                      </span>
                    ) : null}
                  </button>
                  {!file.is_dir ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-7"
                      aria-label={`Download ${file.name}`}
                      title="Download"
                      onClick={() => void downloadFile(file)}
                    >
                      <Download />
                    </Button>
                  ) : null}
                  {writable && !file.is_dir ? (
                    deleteTarget === target ? (
                      <div className="flex items-center">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-7 text-destructive"
                          aria-label={`Confirm delete ${file.name}`}
                          title="Delete"
                          disabled={remove.isPending}
                          onClick={() => remove.mutate(target)}
                        >
                          <Check />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-7"
                          aria-label="Cancel delete"
                          title="Cancel"
                          onClick={() => setDeleteTarget(null)}
                        >
                          <X />
                        </Button>
                      </div>
                    ) : (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        aria-label={`Delete ${file.name}`}
                        title="Delete"
                        onClick={() => setDeleteTarget(target)}
                      >
                        <Trash2 />
                      </Button>
                    )
                  ) : null}
                </div>
              );
            })
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-col overflow-hidden">
        <div className="border-b border-border px-3 py-2 text-xs text-muted-foreground">
          {preview ? <span className="mono text-foreground">{preview.path}</span> : "Preview"}
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-3">
          {previewing ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              Reading
            </div>
          ) : previewError ? (
            <PanelError message={previewError} />
          ) : preview ? (
            <pre className="mono whitespace-pre-wrap break-words text-xs text-foreground">
              {preview.content}
            </pre>
          ) : (
            <p className="text-sm text-muted-foreground">Select a file to preview.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function FileSkeleton() {
  return (
    <div aria-hidden="true">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="flex items-center gap-2 px-3 py-1.5">
          <Skeleton className="size-3.5" />
          <Skeleton className="h-3.5 w-40" />
        </div>
      ))}
    </div>
  );
}

function byDirThenName(a: PodFileInfo, b: PodFileInfo): number {
  if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
  return a.name.localeCompare(b.name);
}

function joinPath(base: string, name: string): string {
  return `${base.replace(/\/+$/, "")}/${name}`;
}

function breadcrumbs(path: string): Array<{ label: string; path: string }> {
  const parts = path.split("/").filter(Boolean);
  const crumbs: Array<{ label: string; path: string }> = [{ label: "/", path: "/" }];
  let current = "";
  for (const part of parts) {
    current += `/${part}`;
    crumbs.push({ label: part, path: current });
  }
  return crumbs;
}

function decodePreview(valueBase64: string): string {
  const bytes = base64ToBytes(valueBase64);
  if (isProbablyBinary(bytes)) return `[binary file, ${formatBytes(bytes.length)}]`;
  if (bytes.length > PREVIEW_LIMIT_BYTES) {
    const preview = new TextDecoder("utf-8", { fatal: false }).decode(
      bytes.subarray(0, PREVIEW_LIMIT_BYTES),
    );
    return `[file is ${formatBytes(bytes.length)}; preview truncated]\n${preview}`;
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

function isProbablyBinary(bytes: Uint8Array): boolean {
  return bytes.subarray(0, 512).some((byte) => byte === 0);
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
