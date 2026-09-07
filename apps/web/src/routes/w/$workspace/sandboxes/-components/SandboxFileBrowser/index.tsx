import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Trash2, Upload } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { FileBreadcrumbs, FileRow, FileRowsSkeleton } from "@/components/shared/FileBrowser";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import type { PodFileInfo } from "@/lib/api/schemas";
import {
  deleteSandboxFileMutationOptions,
  downloadSandboxFile,
  sandboxFilesQueryOptions,
  sandboxPreviewQueryOptions,
  uploadSandboxFileMutationOptions,
} from "@/lib/queries/sandboxes";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

export function SandboxFileBrowser(props: {
  containerId: string;
  writable: boolean;
  className?: string;
}) {
  const { workspace } = useWorkspace();
  return (
    <SandboxFiles
      key={`${workspace.id}:${props.containerId}`}
      {...props}
      workspaceId={workspace.id}
    />
  );
}

function SandboxFiles({
  workspaceId,
  containerId,
  writable,
  className,
}: {
  workspaceId: string;
  containerId: string;
  writable: boolean;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState("/workspace");
  const [selection, setSelection] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const query = useQuery(sandboxFilesQueryOptions(workspaceId, containerId, path));
  const preview = useQuery(sandboxPreviewQueryOptions(workspaceId, containerId, selection));
  const invalidateFiles = () =>
    queryClient.invalidateQueries({
      queryKey: workspaceQueryKeys.sandboxes.files(workspaceId, containerId),
    });
  const upload = useMutation({
    ...uploadSandboxFileMutationOptions(workspaceId, containerId),
    onSuccess: invalidateFiles,
  });
  const remove = useMutation({
    ...deleteSandboxFileMutationOptions(workspaceId, containerId),
    onSuccess: async (_result, target) => {
      setDeleteTarget((current) => (current === target ? null : current));
      setSelection((current) => (current === target ? null : current));
      await invalidateFiles();
    },
  });
  const download = useMutation({
    mutationFn: async ({ target, name }: { target: string; name: string }) => {
      const response = await downloadSandboxFile(workspaceId, containerId, target);
      const bytes = Uint8Array.from(atob(response.value_base64), (character) =>
        character.charCodeAt(0),
      );
      const url = URL.createObjectURL(new Blob([bytes]));
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = name;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        URL.revokeObjectURL(url);
      }
    },
  });
  const navigate = (next: string) => {
    setSelection(null);
    setDeleteTarget(null);
    setPath(next);
  };
  const files = [...(query.data?.files ?? [])].sort(byDirectoryThenName);
  const transferError = upload.error ?? remove.error ?? download.error;

  return (
    <div
      className={cn(
        "grid min-h-0 grid-rows-[minmax(12rem,1fr)_minmax(12rem,1fr)] overflow-hidden lg:grid-cols-2 lg:grid-rows-1",
        className,
      )}
    >
      <div className="flex min-h-0 flex-col overflow-hidden border-b border-border lg:border-b-0 lg:border-r">
        <div className="flex min-h-11 items-center gap-2 border-b border-border px-3 py-2">
          <FileBreadcrumbs path={path} rootLabel="/" absolute onNavigate={navigate} />
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
                variant="outline"
                size="sm"
                pending={upload.isPending}
                onClick={() => fileInput.current?.click()}
              >
                <Upload />
                Upload
              </Button>
            </>
          ) : null}
        </div>
        {transferError ? (
          <ApiErrorNotice compact error={transferError} title="File operation failed" />
        ) : null}
        {query.isError && query.data ? (
          <ApiErrorNotice
            compact
            error={query.error}
            title="Directory could not be refreshed"
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : null}
        <div className="min-h-0 flex-1 divide-y divide-border/60 overflow-y-auto">
          {query.isPending ? (
            <FileRowsSkeleton />
          ) : query.isError && !query.data ? (
            <ApiErrorNotice
              error={query.error}
              title="Directory could not be loaded"
              onRetry={() => void query.refetch()}
              retrying={query.isFetching}
            />
          ) : files.length === 0 ? (
            <PanelEmpty message="Empty directory" className="p-4" />
          ) : (
            files.map((file) => {
              const target = joinPath(path, file.name);
              const downloading = download.isPending && download.variables.target === target;
              return (
                <FileRow
                  key={target}
                  name={file.name}
                  directory={file.is_dir}
                  size={file.size}
                  selected={selection === target}
                  onOpen={() => (file.is_dir ? navigate(target) : setSelection(target))}
                >
                  {!file.is_dir ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-7"
                      aria-label={`Download ${file.name}`}
                      title="Download"
                      pending={downloading}
                      disabled={download.isPending}
                      onClick={() => download.mutate({ target, name: file.name })}
                    >
                      <Download />
                    </Button>
                  ) : null}
                  {writable && !file.is_dir ? (
                    deleteTarget === target ? (
                      <>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          pending={remove.isPending}
                          onClick={() => remove.mutate(target)}
                        >
                          Delete
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          disabled={remove.isPending}
                          onClick={() => setDeleteTarget(null)}
                        >
                          Keep
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
                        disabled={remove.isPending}
                        onClick={() => setDeleteTarget(target)}
                      >
                        <Trash2 />
                      </Button>
                    )
                  ) : null}
                </FileRow>
              );
            })
          )}
        </div>
      </div>
      <div className="flex min-h-0 flex-col overflow-hidden">
        <div
          className="truncate border-b border-border px-3 py-2 text-xs text-muted-foreground"
          title={selection ?? undefined}
        >
          {selection ? <span className="mono text-foreground">{selection}</span> : "Preview"}
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-3">
          {!selection ? (
            <p className="text-sm text-muted-foreground">Select a file to preview.</p>
          ) : preview.isPending ? (
            <p role="status" className="text-sm text-muted-foreground">
              Reading file
            </p>
          ) : preview.isError ? (
            <ApiErrorNotice
              error={preview.error}
              title="File could not be read"
              onRetry={() => void preview.refetch()}
              retrying={preview.isFetching}
            />
          ) : preview.data ? (
            <>
              {preview.data.truncated ? (
                <p className="mb-2 text-xs text-muted-foreground">
                  Showing the first 256 KiB. Download the file for its full contents.
                </p>
              ) : null}
              <pre className="mono whitespace-pre-wrap break-words text-xs text-foreground">
                {decodePreview(preview.data.value_base64)}
              </pre>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function byDirectoryThenName(left: PodFileInfo, right: PodFileInfo): number {
  return left.is_dir === right.is_dir ? left.name.localeCompare(right.name) : left.is_dir ? -1 : 1;
}

function joinPath(base: string, name: string): string {
  return `${base.replace(/\/+$/, "")}/${name}`;
}

function decodePreview(valueBase64: string): string {
  const bytes = Uint8Array.from(atob(valueBase64), (character) => character.charCodeAt(0));
  if (bytes.subarray(0, 512).some((byte) => byte === 0))
    return "Binary file. Download it to view its contents.";
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}
