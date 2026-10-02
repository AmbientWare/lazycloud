import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Download, File, Folder, Loader2, Trash2, Upload } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelError } from "@/components/shared/PanelError";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { ContentTransition } from "@/components/shared/ContentTransition";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Schemas } from "@/lib/api/client";
import { formatBytes } from "@/lib/format";
import {
  createVolume,
  deleteVolume,
  deleteVolumePath,
  saveUrl,
  uploadVolumeFile,
  volumeDownloadUrl,
  volumeFilesQueryOptions,
  volumesQueryOptions,
} from "@/lib/queries/storage";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { ResourceWorkloadLinks } from "./ResourceWorkloadLinks";

export function VolumesTab({
  workspace,
  creating,
  onCreatingChange,
}: {
  workspace: string;
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
}) {
  const query = useQuery(volumesQueryOptions(workspace));
  const [selectedName, setSelectedName] = useState("");
  const selectedVolume =
    query.data?.find((volume) => volume.name === selectedName) ?? query.data?.[0];

  return (
    <ContentTransition
      pending={query.isPending}
      className="flex min-h-full flex-col lg:h-full lg:min-h-0"
    >
      <div className="grid min-h-[28rem] flex-1 overflow-visible lg:min-h-0 lg:grid-cols-[15rem_minmax(0,1fr)] lg:overflow-hidden">
        <aside className="min-h-0 overflow-visible border-b border-border lg:overflow-y-auto lg:border-b-0 lg:border-r">
          {creating ? (
            <VolumeForm
              workspace={workspace}
              onCreated={(name) => {
                onCreatingChange(false);
                setSelectedName(name);
              }}
              onCancel={() => onCreatingChange(false)}
            />
          ) : null}
          {query.isPending ? (
            <VolumesSkeleton />
          ) : query.isError ? (
            <PanelError message={query.error.message} />
          ) : query.data.length === 0 && !creating ? (
            <PanelEmpty message="No volumes. Create one to mount in a workload." className="p-6" />
          ) : (
            <div className="divide-y divide-border/60">
              {query.data.map((volume) => (
                <VolumeRow
                  key={volume.id}
                  workspace={workspace}
                  volume={volume}
                  selected={selectedVolume?.name === volume.name}
                  onSelect={() => setSelectedName(volume.name)}
                />
              ))}
            </div>
          )}
        </aside>
        {selectedVolume ? (
          <VolumeBrowser key={selectedVolume.id} workspace={workspace} volume={selectedVolume} />
        ) : (
          <div className="hidden place-items-center text-sm text-muted-foreground md:grid">
            Select a volume
          </div>
        )}
      </div>
    </ContentTransition>
  );
}

function VolumesSkeleton() {
  return (
    <div aria-hidden="true" className="divide-y divide-border/60">
      {Array.from({ length: 3 }, (_, index) => (
        <div key={index} className="space-y-2 px-3 py-3">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-20" />
        </div>
      ))}
    </div>
  );
}

function VolumeRow({
  workspace,
  volume,
  selected,
  onSelect,
}: {
  workspace: string;
  volume: Schemas["Volume"];
  selected: boolean;
  onSelect: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const remove = useMutation({
    mutationFn: () => deleteVolume(workspace, volume.name),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.storage.volumes(workspace) }),
  });

  return (
    <div className="interactive-row group px-3 py-2.5" data-selected={selected}>
      <div className="flex min-w-0 items-center gap-2">
        <button type="button" className="min-w-0 flex-1 text-left" onClick={onSelect}>
          <span className="mono block truncate text-[13px] font-medium text-foreground">
            {volume.name}
          </span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">
            {formatBytes(volume.size_bytes)}
          </span>
        </button>
        {!confirming ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-7 opacity-70 md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100"
            aria-label={`Delete volume ${volume.name}`}
            title="Delete volume"
            onClick={() => setConfirming(true)}
          >
            <Trash2 />
          </Button>
        ) : null}
      </div>
      <div className="mt-1">
        <ResourceWorkloadLinks workspace={workspace} workloads={volume.used_by} limit={1} />
      </div>
      {confirming ? (
        <div className="mt-2 flex items-center gap-1.5">
          <Button
            variant="destructive"
            size="sm"
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? <Loader2 className="animate-spin" /> : "Delete"}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Keep
          </Button>
        </div>
      ) : null}
      {remove.isError ? (
        <p className="mt-2 text-xs text-destructive">{remove.error.message}</p>
      ) : null}
    </div>
  );
}

function VolumeBrowser({ workspace, volume }: { workspace: string; volume: Schemas["Volume"] }) {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState("");
  const [confirmPath, setConfirmPath] = useState("");
  const [transferError, setTransferError] = useState("");
  const query = useQuery(volumeFilesQueryOptions(workspace, volume.name, path));
  const upload = useMutation({
    mutationFn: (file: File) => uploadVolumeFile(workspace, volume.name, path, file),
    onSuccess: () => refreshVolumeQueries(queryClient, workspace, volume.name),
    onError: (error) => setTransferError(error.message),
  });
  const remove = useMutation({
    mutationFn: (targetPath: string) => deleteVolumePath(workspace, volume.name, targetPath),
    onSuccess: () => {
      setConfirmPath("");
      void refreshVolumeQueries(queryClient, workspace, volume.name);
    },
  });

  const download = async (item: Schemas["VolumeFile"]) => {
    setTransferError("");
    try {
      saveUrl(await volumeDownloadUrl(workspace, volume.name, item.path), fileName(item.path));
    } catch (error) {
      setTransferError(error instanceof Error ? error.message : "Download failed");
    }
  };

  const items = [...(query.data ?? [])].sort(byDirectoryThenName);

  return (
    <section className="flex min-h-0 flex-col overflow-visible lg:overflow-hidden">
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto text-xs">
          {volumeBreadcrumbs(volume.name, path).map((crumb, index) => (
            <span key={crumb.path} className="flex shrink-0 items-center gap-1">
              {index > 0 ? <ChevronRight className="size-3 text-muted-foreground" /> : null}
              <button
                type="button"
                className={cn(
                  "interactive-link mono hover:text-foreground",
                  crumb.path === path ? "text-foreground" : "text-muted-foreground",
                )}
                onClick={() => setPath(crumb.path)}
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </div>
        <input
          ref={inputRef}
          type="file"
          className="sr-only"
          onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            if (file) {
              setTransferError("");
              upload.mutate(file);
            }
            event.currentTarget.value = "";
          }}
        />
        <Button
          size="sm"
          variant="outline"
          disabled={upload.isPending}
          onClick={() => inputRef.current?.click()}
        >
          {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />}
          Upload
        </Button>
      </div>

      {transferError ? (
        <p className="border-b border-border px-3 py-2 text-xs text-destructive">{transferError}</p>
      ) : null}
      <ContentTransition
        pending={query.isPending}
        className="min-h-0 flex-1 overflow-visible lg:overflow-y-auto"
      >
        {query.isPending ? (
          <FileSkeleton />
        ) : query.isError ? (
          <PanelError message={query.error.message} />
        ) : items.length === 0 ? (
          <PanelEmpty message="Empty directory" className="p-8" />
        ) : (
          <div className="divide-y divide-border/60">
            {items.map((item) => {
              const itemName = fileName(item.path);
              const confirming = confirmPath === item.path;
              return (
                <div
                  key={item.path}
                  className="interactive-row group flex min-w-0 items-center gap-2 px-3 py-2"
                >
                  {item.is_dir ? (
                    <Folder className="size-4 shrink-0 text-brand" />
                  ) : (
                    <File className="size-4 shrink-0 text-muted-foreground" />
                  )}
                  {item.is_dir ? (
                    <button
                      type="button"
                      className="interactive-link mono min-w-0 flex-1 truncate text-left text-[13px]"
                      onClick={() => setPath(item.path)}
                    >
                      {itemName}
                    </button>
                  ) : (
                    <span className="mono min-w-0 flex-1 truncate text-[13px]">{itemName}</span>
                  )}
                  <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
                    {item.modified_at ? <LiveRelativeTime value={item.modified_at} /> : null}
                  </span>
                  {!item.is_dir ? (
                    <span className="mono w-16 shrink-0 text-right text-[11px] text-muted-foreground">
                      {formatBytes(item.size_bytes)}
                    </span>
                  ) : null}
                  {confirming ? (
                    <span className="flex items-center gap-1">
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={remove.isPending}
                        onClick={() => remove.mutate(item.path)}
                      >
                        {remove.isPending ? <Loader2 className="animate-spin" /> : "Delete"}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirmPath("")}>
                        Keep
                      </Button>
                    </span>
                  ) : (
                    <span className="flex items-center">
                      {!item.is_dir ? (
                        <Button
                          size="icon"
                          variant="ghost"
                          className="size-7"
                          aria-label={`Download ${itemName}`}
                          title="Download file"
                          onClick={() => void download(item)}
                        >
                          <Download />
                        </Button>
                      ) : null}
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-7"
                        aria-label={`Delete ${itemName}`}
                        title="Delete path"
                        onClick={() => setConfirmPath(item.path)}
                      >
                        <Trash2 />
                      </Button>
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </ContentTransition>
      {remove.isError ? (
        <p className="border-t border-border px-3 py-2 text-xs text-destructive">
          {remove.error.message}
        </p>
      ) : null}
    </section>
  );
}

function FileSkeleton() {
  return (
    <div aria-hidden="true" className="divide-y divide-border/60">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="flex items-center gap-2 px-3 py-2">
          <Skeleton className="size-4" />
          <Skeleton className="h-3.5 w-48" />
          <Skeleton className="ml-auto h-3 w-16" />
        </div>
      ))}
    </div>
  );
}

function VolumeForm({
  workspace,
  onCreated,
  onCancel,
}: {
  workspace: string;
  onCreated: (name: string) => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => createVolume(workspace, name.trim()),
    onSuccess: (volume) => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.storage.volumes(workspace),
      });
      onCreated(volume.name);
    },
  });

  return (
    <form
      className="space-y-2 border-b border-border bg-muted/25 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      <input
        autoFocus
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="volume-name"
        className="mono h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm outline-none focus:border-ring"
      />
      <div className="flex items-center gap-1.5">
        <Button type="submit" size="sm" disabled={create.isPending || !name.trim()}>
          {create.isPending ? <Loader2 className="animate-spin" /> : "Create"}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {create.isError ? <p className="text-xs text-destructive">{create.error.message}</p> : null}
    </form>
  );
}

function refreshVolumeQueries(
  queryClient: ReturnType<typeof useQueryClient>,
  workspace: string,
  volume: string,
) {
  return Promise.all([
    queryClient.invalidateQueries({
      queryKey: workspaceQueryKeys.storage.volumePath(workspace, volume),
    }),
    queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.storage.volumes(workspace) }),
  ]);
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function byDirectoryThenName(left: Schemas["VolumeFile"], right: Schemas["VolumeFile"]): number {
  if (left.is_dir !== right.is_dir) return left.is_dir ? -1 : 1;
  return left.path.localeCompare(right.path);
}

function volumeBreadcrumbs(
  volumeName: string,
  path: string,
): Array<{ label: string; path: string }> {
  const crumbs = [{ label: volumeName, path: "" }];
  let current = "";
  for (const part of path.split("/").filter(Boolean)) {
    current = current ? `${current}/${part}` : part;
    crumbs.push({ label: part, path: current });
  }
  return crumbs;
}
