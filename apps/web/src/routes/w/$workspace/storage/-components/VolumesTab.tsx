import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Trash2, Upload } from "lucide-react";
import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { FileBreadcrumbs, FileRow, FileRowsSkeleton } from "@/components/shared/FileBrowser";
import { FormField } from "@/components/shared/FormField";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Volume, VolumePathInfo } from "@/lib/api/schemas";
import { formatBytes } from "@/lib/format";
import {
  createVolume,
  deleteVolume,
  deleteVolumePath,
  uploadVolumeFile,
  volumeDownloadUrl,
  volumePathQueryOptions,
  volumesQueryOptions,
} from "@/lib/queries/storage";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { ResourceWorkloadLinks } from "./ResourceWorkloadLinks";

export function VolumesTab({
  workspaceId,
  workspaceName,
  creating,
  onCreatingChange,
}: {
  workspaceId: string;
  workspaceName: string;
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
}) {
  const query = useQuery(volumesQueryOptions(workspaceId));
  const [selectedName, setSelectedName] = useState("");
  const selectedVolume =
    query.data?.volumes.find((volume) => volume.name === selectedName) ?? query.data?.volumes[0];

  return (
    <div className="flex min-h-full flex-col lg:h-full lg:min-h-0">
      <div className="grid min-h-[28rem] flex-1 overflow-visible lg:min-h-0 lg:grid-cols-[15rem_minmax(0,1fr)] lg:overflow-hidden">
        <aside className="min-h-0 overflow-visible border-b border-border lg:overflow-y-auto lg:border-b-0 lg:border-r">
          {creating ? (
            <VolumeForm
              workspaceId={workspaceId}
              onCreated={(name) => {
                onCreatingChange(false);
                setSelectedName(name);
              }}
              onCancel={() => onCreatingChange(false)}
            />
          ) : null}
          {query.isError && query.data ? (
            <ApiErrorNotice
              compact
              error={query.error}
              title="Volumes could not be refreshed"
              onRetry={() => void query.refetch()}
              retrying={query.isFetching}
            />
          ) : null}
          {query.isPending ? (
            <VolumesSkeleton />
          ) : query.isError && !query.data ? (
            <ApiErrorNotice
              error={query.error}
              title="Volumes could not be loaded"
              onRetry={() => void query.refetch()}
              retrying={query.isFetching}
            />
          ) : query.data?.volumes.length === 0 && !creating ? (
            <PanelEmpty message="No volumes. Create one to mount in a workload." className="p-6" />
          ) : (
            <div className="divide-y divide-border/60">
              {query.data?.volumes.map((volume) => (
                <VolumeRow
                  key={volume.id}
                  workspaceId={workspaceId}
                  workspaceName={workspaceName}
                  volume={volume}
                  selected={selectedVolume?.name === volume.name}
                  onSelect={() => setSelectedName(volume.name)}
                />
              ))}
            </div>
          )}
        </aside>
        {selectedVolume ? (
          <VolumeBrowser
            key={selectedVolume.name}
            workspaceId={workspaceId}
            volume={selectedVolume}
          />
        ) : (
          <div className="hidden place-items-center text-sm text-muted-foreground md:grid">
            Select a volume
          </div>
        )}
      </div>
    </div>
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
  workspaceId,
  workspaceName,
  volume,
  selected,
  onSelect,
}: {
  workspaceId: string;
  workspaceName: string;
  volume: Volume;
  selected: boolean;
  onSelect: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const remove = useMutation({
    mutationFn: () => deleteVolume(workspaceId, volume.name),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.storage.volumes(workspaceId),
      }),
  });

  return (
    <div className="interactive-row group px-3 py-2.5" data-selected={selected}>
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          className="min-w-0 flex-1 text-left"
          aria-pressed={selected}
          onClick={onSelect}
        >
          <span className="mono block truncate text-[13px] font-medium text-foreground">
            {volume.name}
          </span>
          <span className="mt-0.5 block text-[11px] text-muted-foreground">
            {formatBytes(volume.size)} · updated <LiveRelativeTime value={volume.updated_at} />
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
        <ResourceWorkloadLinks
          workspaceName={workspaceName}
          workloads={volume.workloads}
          limit={1}
        />
      </div>
      {confirming ? (
        <div className="mt-2 flex items-center gap-1.5">
          <Button
            variant="destructive"
            size="sm"
            pending={remove.isPending}
            onClick={() => remove.mutate()}
          >
            Delete
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={remove.isPending}
            onClick={() => setConfirming(false)}
          >
            Keep
          </Button>
        </div>
      ) : null}
      {remove.isError ? (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {remove.error.message}
        </p>
      ) : null}
    </div>
  );
}

function VolumeBrowser({ workspaceId, volume }: { workspaceId: string; volume: Volume }) {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState("");
  const [confirmPath, setConfirmPath] = useState("");
  const [transferError, setTransferError] = useState("");
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null);
  const query = useQuery(volumePathQueryOptions(workspaceId, volume.name, path));
  const upload = useMutation({
    mutationFn: (file: File) => uploadVolumeFile(workspaceId, volume.name, path, file),
    onSuccess: () => refreshVolumeQueries(queryClient, workspaceId, volume.name),
    onError: (error) => setTransferError(error.message),
  });
  const remove = useMutation({
    mutationFn: (targetPath: string) => deleteVolumePath(workspaceId, volume.name, targetPath),
    onSuccess: () => {
      setConfirmPath("");
      void refreshVolumeQueries(queryClient, workspaceId, volume.name);
    },
  });

  const download = async (item: VolumePathInfo) => {
    setTransferError("");
    setDownloadingPath(item.path);
    try {
      const url = await volumeDownloadUrl(workspaceId, volume.name, item.path);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = fileName(item.path);
      anchor.rel = "noopener noreferrer";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch (error) {
      setTransferError(error instanceof Error ? error.message : "Download failed");
    } finally {
      setDownloadingPath(null);
    }
  };

  const items = [...(query.data?.path_infos ?? [])].sort(byDirectoryThenName);

  return (
    <section className="flex min-h-0 flex-col overflow-visible lg:overflow-hidden">
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <FileBreadcrumbs
          path={path}
          rootLabel={volume.name}
          onNavigate={(next) => {
            setPath(next);
            setConfirmPath("");
          }}
        />
        <input
          ref={inputRef}
          type="file"
          className="hidden"
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
          pending={upload.isPending}
          onClick={() => inputRef.current?.click()}
        >
          <Upload />
          Upload
        </Button>
      </div>

      {transferError ? (
        <ApiErrorNotice compact error={new Error(transferError)} title="File transfer failed" />
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
      <div className="min-h-0 flex-1 overflow-visible lg:overflow-y-auto">
        {query.isPending ? (
          <FileRowsSkeleton />
        ) : query.isError && !query.data ? (
          <ApiErrorNotice
            error={query.error}
            title="Directory could not be loaded"
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : items.length === 0 ? (
          <PanelEmpty message="Empty directory" className="p-8" />
        ) : (
          <div className="divide-y divide-border/60">
            {items.map((item) => {
              const itemName = fileName(item.path);
              const confirming = confirmPath === item.path;
              return (
                <FileRow
                  key={item.path}
                  name={itemName}
                  directory={item.is_dir}
                  size={item.size}
                  modifiedAt={item.mod_time}
                  onOpen={
                    item.is_dir
                      ? () => {
                          setPath(item.path);
                          setConfirmPath("");
                        }
                      : undefined
                  }
                >
                  {confirming ? (
                    <span className="flex items-center gap-1">
                      <Button
                        size="sm"
                        variant="destructive"
                        pending={remove.isPending}
                        onClick={() => remove.mutate(item.path)}
                      >
                        Delete
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
                          pending={downloadingPath === item.path}
                          disabled={downloadingPath !== null}
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
                </FileRow>
              );
            })}
          </div>
        )}
      </div>
      {remove.isError ? (
        <p className="border-t border-border px-3 py-2 text-xs text-destructive">
          {remove.error.message}
        </p>
      ) : null}
    </section>
  );
}

function VolumeForm({
  workspaceId,
  onCreated,
  onCancel,
}: {
  workspaceId: string;
  onCreated: (name: string) => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => createVolume(workspaceId, name.trim()),
    onSuccess: (volume) => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.storage.volumes(workspaceId),
      });
      onCreated(volume?.name ?? name.trim());
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
      <FormField
        label="Volume name"
        disabled={create.isPending}
        autoFocus
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="volume-name"
        className="mono h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm outline-none focus:border-ring"
      />
      <div className="flex items-center gap-1.5">
        <Button type="submit" size="sm" pending={create.isPending} disabled={!name.trim()}>
          Create
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
  workspaceId: string,
  volumeName: string,
) {
  return Promise.all([
    queryClient.invalidateQueries({
      queryKey: workspaceQueryKeys.storage.volumePath(workspaceId, volumeName),
    }),
    queryClient.invalidateQueries({
      queryKey: workspaceQueryKeys.storage.volumes(workspaceId),
    }),
  ]);
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function byDirectoryThenName(left: VolumePathInfo, right: VolumePathInfo): number {
  if (left.is_dir !== right.is_dir) return left.is_dir ? -1 : 1;
  return left.path.localeCompare(right.path);
}
