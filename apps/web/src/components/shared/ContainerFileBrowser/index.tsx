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

import { ContentTransition } from "@/components/shared/ContentTransition";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { PodFileInfo } from "@/lib/api/schemas";
import { base64ToBytes, downloadBlob } from "@/lib/files";
import { exactTime, formatBytes, relativeTime } from "@/lib/format";
import {
  CONTAINER_FILE_DOWNLOAD_LIMIT_BYTES,
  CONTAINER_FILE_LIST_LIMIT,
  containerFilesQueryOptions,
  deleteContainerFileMutationOptions,
  downloadContainerFile,
  uploadContainerFileMutationOptions,
} from "@/lib/queries/container-files";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

const PREVIEW_LIMIT_BYTES = 256 * 1024;

type Preview =
  | { path: string; state: "reading" }
  | { path: string; state: "text"; content: string }
  | { path: string; state: "note"; message: string }
  | { path: string; state: "error"; message: string };

/** Browse a running container's filesystem through its pod file API. */
export function ContainerFileBrowser({
  containerId,
  rootPath,
  writable,
  className,
}: {
  containerId: string;
  rootPath: string;
  writable: boolean;
  className?: string;
}) {
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState(rootPath);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const previewRequest = useRef<{ path: string; controller: AbortController } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  useEffect(() => () => previewRequest.current?.controller.abort(), []);

  const clearPreview = () => {
    previewRequest.current?.controller.abort();
    previewRequest.current = null;
    setPreview(null);
  };

  const navigateDirectory = (target: string) => {
    clearPreview();
    setPath(target);
    setDeleteTarget(null);
    setDownloadError(null);
  };

  // Name, size, modified, then room for download and, when writable, delete.
  const columns = writable
    ? "grid-cols-[minmax(0,1fr)_4.5rem_7rem_3.75rem]"
    : "grid-cols-[minmax(0,1fr)_4.5rem_7rem_1.75rem]";
  const query = useQuery(containerFilesQueryOptions(workspace.id, containerId, path));
  const invalidateFiles = () =>
    queryClient.invalidateQueries({
      queryKey: workspaceQueryKeys.containers.files(workspace.id, containerId),
    });
  const upload = useMutation({
    ...uploadContainerFileMutationOptions(workspace.id, containerId),
    onSuccess: invalidateFiles,
  });
  const remove = useMutation({
    ...deleteContainerFileMutationOptions(workspace.id, containerId),
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
    if (file.size > CONTAINER_FILE_DOWNLOAD_LIMIT_BYTES) {
      setPreview({ path: target, state: "note", message: tooLargeMessage(file.size) });
      return;
    }
    const controller = new AbortController();
    previewRequest.current = { path: target, controller };
    setPreview({ path: target, state: "reading" });
    try {
      const download = await downloadContainerFile(
        workspace.id,
        containerId,
        target,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setPreview(decodePreview(target, download.value_base64));
    } catch (error) {
      if (controller.signal.aborted) return;
      setPreview({
        path: target,
        state: "error",
        message: error instanceof Error ? error.message : "Failed to read file",
      });
    }
  };

  const downloadFile = async (file: PodFileInfo) => {
    const target = joinPath(path, file.name);
    setDownloadError(null);
    try {
      const download = await downloadContainerFile(workspace.id, containerId, target);
      downloadBlob(file.name, new Blob([base64ToBytes(download.value_base64)]));
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "Failed to download file");
    }
  };

  return (
    <div
      className={cn(
        "grid min-h-0 grid-rows-[minmax(12rem,1fr)_minmax(12rem,1fr)] overflow-hidden lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:grid-rows-1",
        className,
      )}
    >
      <div className="flex min-h-0 flex-col overflow-hidden border-b border-border lg:border-b-0 lg:border-r">
        <div className="flex min-h-10 items-center gap-1 border-b border-border px-3 py-2 text-xs">
          <nav aria-label="Directory" className="flex min-w-0 flex-1 items-center overflow-x-auto">
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
          </nav>
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
                {upload.isPending ? (
                  <Loader2 className="animate-spin motion-reduce:animate-none" />
                ) : (
                  <Upload />
                )}
              </Button>
            </>
          ) : null}
        </div>
        {upload.isError || remove.isError || downloadError ? (
          <p role="alert" className="border-b border-border px-3 py-2 text-xs text-destructive">
            {downloadError ?? (upload.error || remove.error)?.message}
          </p>
        ) : null}
        <div
          aria-hidden="true"
          className={cn(
            "grid gap-2 border-b border-border/60 px-3 py-1.5 text-[11px] text-muted-foreground",
            columns,
          )}
        >
          <span>Name</span>
          <span className="text-right">Size</span>
          <span>Modified</span>
          <span />
        </div>
        <ContentTransition
          pending={query.isPending}
          className="min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto"
        >
          {query.isPending ? (
            <FileSkeleton />
          ) : query.isError ? (
            <PanelError message={query.error.message} />
          ) : query.data.files.length === 0 ? (
            <PanelEmpty message="Empty directory" className="p-4" />
          ) : (
            <>
              {[...query.data.files].sort(byDirThenName).map((file) => {
                const target = joinPath(path, file.name);
                const tooLarge = !file.is_dir && file.size > CONTAINER_FILE_DOWNLOAD_LIMIT_BYTES;
                return (
                  <div
                    key={file.name}
                    className={cn("grid min-h-9 items-center gap-2 pr-3 pl-1.5", columns)}
                  >
                    <button
                      type="button"
                      className="interactive-row flex min-w-0 items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-sm"
                      onClick={() =>
                        file.is_dir ? navigateDirectory(target) : void openFile(file)
                      }
                    >
                      {file.is_dir ? (
                        <Folder className="size-3.5 shrink-0 text-brand" aria-hidden="true" />
                      ) : (
                        <File
                          className="size-3.5 shrink-0 text-muted-foreground"
                          aria-hidden="true"
                        />
                      )}
                      <span className="mono truncate">{file.name}</span>
                    </button>
                    <span className="mono text-right text-[11px] tabular-nums text-muted-foreground">
                      {file.is_dir ? "" : formatBytes(file.size)}
                    </span>
                    <span className="truncate text-[11px] text-muted-foreground">
                      {file.mod_time ? (
                        <time dateTime={file.mod_time} title={exactTime(file.mod_time)}>
                          {relativeTime(file.mod_time)}
                        </time>
                      ) : null}
                    </span>
                    <span className="flex items-center justify-end gap-0.5">
                      {file.is_dir ? null : (
                        <DownloadButton
                          name={file.name}
                          tooLarge={tooLarge}
                          onDownload={() => void downloadFile(file)}
                        />
                      )}
                      {writable && !file.is_dir ? (
                        deleteTarget === target ? (
                          <>
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
                          </>
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
                    </span>
                  </div>
                );
              })}
              {query.data.truncated ? (
                <p className="px-3 py-2 text-xs text-muted-foreground">
                  Showing the first {CONTAINER_FILE_LIST_LIMIT.toLocaleString()} entries.
                </p>
              ) : null}
            </>
          )}
        </ContentTransition>
      </div>

      <div className="flex min-h-0 flex-col overflow-hidden">
        <div className="flex min-h-10 items-center border-b border-border px-3 py-2 text-xs text-muted-foreground">
          {preview ? (
            <span className="mono truncate text-foreground">{preview.path}</span>
          ) : (
            "Preview"
          )}
        </div>
        <ContentTransition
          pending={preview?.state === "reading"}
          className="min-h-0 flex-1 overflow-auto p-3"
        >
          {preview === null ? (
            <p className="text-sm text-muted-foreground">Select a file to preview.</p>
          ) : preview.state === "reading" ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin motion-reduce:animate-none" />
              Reading
            </div>
          ) : preview.state === "error" ? (
            <PanelError message={preview.message} />
          ) : preview.state === "note" ? (
            <p className="text-sm text-muted-foreground">{preview.message}</p>
          ) : (
            <pre className="mono whitespace-pre-wrap break-words text-xs text-foreground">
              {preview.content}
            </pre>
          )}
        </ContentTransition>
      </div>
    </div>
  );
}

function DownloadButton({
  name,
  tooLarge,
  onDownload,
}: {
  name: string;
  tooLarge: boolean;
  onDownload: () => void;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="size-7"
      aria-label={`Download ${name}`}
      title={tooLarge ? tooLargeMessage() : "Download"}
      disabled={tooLarge}
      onClick={onDownload}
    >
      <Download />
    </Button>
  );
}

function FileSkeleton() {
  return (
    <div aria-hidden="true">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="flex items-center gap-2 px-3 py-2">
          <Skeleton className="size-3.5" />
          <Skeleton className="h-3.5 w-40" />
        </div>
      ))}
    </div>
  );
}

function tooLargeMessage(size?: number): string {
  const limit = formatBytes(CONTAINER_FILE_DOWNLOAD_LIMIT_BYTES);
  return size === undefined
    ? `Files over ${limit} don't download here`
    : `This file is ${formatBytes(size)}. Files over ${limit} don't open or download here.`;
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

function decodePreview(path: string, valueBase64: string): Preview {
  const bytes = base64ToBytes(valueBase64);
  if (bytes.subarray(0, 512).some((byte) => byte === 0)) {
    return { path, state: "note", message: `Binary file, ${formatBytes(bytes.length)}.` };
  }
  const decoder = new TextDecoder("utf-8", { fatal: false });
  if (bytes.length > PREVIEW_LIMIT_BYTES) {
    return {
      path,
      state: "text",
      content: `[first ${formatBytes(PREVIEW_LIMIT_BYTES)} of ${formatBytes(bytes.length)}]\n${decoder.decode(bytes.subarray(0, PREVIEW_LIMIT_BYTES))}`,
    };
  }
  return { path, state: "text", content: decoder.decode(bytes) };
}
