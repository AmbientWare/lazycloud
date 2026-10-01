import { infiniteQueryOptions, queryOptions, type QueryClient } from "@tanstack/react-query";

import { api, ApiError, ok, type Schemas } from "@/lib/api/client";
import { workspaceName } from "@/lib/api/workspaces";
import type {
  Disk,
  ResourceWorkloadReference,
  SecretMasked,
  Volume,
  VolumePathInfo,
} from "@/lib/api/schemas";
import {
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";

import { appDirectory } from "./directory";
import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

// --- Secrets (collection reads stay masked; cleartext is fetched only on demand) ---

/** A secret as the list shows it: the API never returns a value outside a reveal. */
function maskedSecret(secret: Schemas["Secret"]): SecretMasked {
  return {
    name: secret.name,
    value: "********",
    created_at: secret.created_at,
    updated_at: secret.updated_at,
    workloads: [],
  };
}

export function secretsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.secrets(workspaceId),
    queryFn: async (): Promise<{ secrets: SecretMasked[] }> => {
      const workspace = workspaceName(workspaceId);
      const secrets: SecretMasked[] = [];
      let cursor: string | undefined;
      do {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/secrets", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        secrets.push(...page.secrets.map(maskedSecret));
        cursor = page.next_cursor;
      } while (cursor);
      return { secrets };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

const secretPath = (workspaceId: string, name: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), secret: name } },
});

export async function createSecret(
  workspaceId: string,
  name: string,
  value: string,
): Promise<{ id: string; name: string }> {
  const created = await ok(
    api.POST("/v1/workspaces/{workspace}/secrets", {
      params: { path: { workspace: workspaceName(workspaceId) } },
      body: { name, value },
    }),
  );
  return { id: created.name, name: created.name };
}

export async function updateSecretValue(
  workspaceId: string,
  name: string,
  value: string,
): Promise<SecretMasked> {
  return maskedSecret(
    await ok(
      api.PATCH("/v1/workspaces/{workspace}/secrets/{secret}", {
        ...secretPath(workspaceId, name),
        body: { value },
      }),
    ),
  );
}

export async function revealSecretValue(workspaceId: string, name: string): Promise<string> {
  const revealed = await ok(
    api.GET("/v1/workspaces/{workspace}/secrets/{secret}/value", secretPath(workspaceId, name)),
  );
  return revealed.value;
}

export async function deleteSecret(workspaceId: string, name: string): Promise<unknown> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/secrets/{secret}", secretPath(workspaceId, name)),
  );
  return {};
}

// --- Disks (created by the workloads that declare them) ---

const DISK_PAGE_SIZE = 50;

/**
 * A disk names only the container holding it; its workload comes from that
 * container. The API has no workload role, so a holder reads as a service.
 */
async function viewDisk(
  client: QueryClient,
  workspaceId: string,
  disk: Schemas["Disk"],
): Promise<Disk> {
  let workload: Disk["workload"] = null;
  const container = disk.holder_container_id
    ? await ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}", {
          params: {
            path: { workspace: workspaceName(workspaceId), container: disk.holder_container_id },
          },
        }),
      ).catch((error: unknown) => {
        // A holder that has since been removed leaves the disk without a workload.
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      })
    : null;
  if (container) {
    const app = (await appDirectory(client, workspaceId)).byName.get(container.app);
    workload = {
      app_id: app?.id ?? "",
      app_name: container.app,
      kind: "function",
      name: container.function,
      role: "service",
    };
  }
  return {
    id: disk.id,
    name: disk.name,
    size_bytes: disk.size_bytes,
    status: disk.status,
    generation: disk.generation,
    stored_bytes: disk.stored_bytes,
    holder_container_id: disk.holder_container_id ?? "",
    workload,
    created_at: disk.created_at,
    updated_at: disk.updated_at,
  };
}

export function disksQueryOptions(workspaceId: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.disks(workspaceId),
    initialPageParam: "",
    queryFn: async ({ pageParam, client }): Promise<{ data: Disk[]; next: string }> => {
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/disks", {
          params: {
            path: { workspace: workspaceName(workspaceId) },
            query: { limit: DISK_PAGE_SIZE, cursor: pageParam || undefined },
          },
        }),
      );
      return {
        data: await Promise.all(page.disks.map((disk) => viewDisk(client, workspaceId, disk))),
        next: page.next_cursor ?? "",
      };
    },
    getNextPageParam: nextListCursor,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function selectDiskList(
  data: InfiniteListQueryData<Disk> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (disk) => disk.id);
}

export async function deleteDisk(workspaceId: string, name: string): Promise<null> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/disks/{disk}", {
      params: { path: { workspace: workspaceName(workspaceId), disk: name } },
    }),
  );
  return null;
}

// --- Volumes ---

function viewVolume(
  volume: Schemas["Volume"],
  workspaceId: string,
  appIds: ReadonlyMap<string, string>,
): Volume {
  return {
    id: volume.id,
    name: volume.name,
    size: volume.size_bytes,
    created_at: volume.created_at,
    // Volumes have no update time; their creation is the last change the API records.
    updated_at: volume.created_at,
    workspace_id: workspaceId,
    workspace_name: workspaceName(workspaceId),
    // A deleted volume leaves the list at once.
    deletion_requested_at: null,
    workloads: volume.used_by.map((workload): ResourceWorkloadReference => ({
      app_id: appIds.get(workload.app) ?? "",
      app_name: workload.app,
      name: workload.name,
      kind: workload.kind,
      versions: [],
      active_versions: [],
    })),
  };
}

export function volumesQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumes(workspaceId),
    queryFn: async ({ client }): Promise<{ volumes: Volume[] }> => {
      const workspace = workspaceName(workspaceId);
      const volumes: Schemas["Volume"][] = [];
      let cursor: string | undefined;
      do {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/volumes", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        volumes.push(...page.volumes);
        cursor = page.next_cursor;
      } while (cursor);
      const apps = volumes.some((volume) => volume.used_by.length > 0)
        ? (await appDirectory(client, workspaceId)).byName
        : new Map<string, Schemas["App"]>();
      const appIds = new Map([...apps].map(([name, app]) => [name, app.id]));
      return { volumes: volumes.map((volume) => viewVolume(volume, workspaceId, appIds)) };
    },
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Creates the volume, or returns the existing one of that name. */
export async function createVolume(workspaceId: string, name: string): Promise<Volume | null> {
  const volume = await ok(
    api.POST("/v1/workspaces/{workspace}/volumes", {
      params: { path: { workspace: workspaceName(workspaceId) } },
      body: { name },
    }),
  );
  return viewVolume(volume, workspaceId, new Map());
}

/** Refused with a conflict while a container that mounts the volume has not stopped. */
export async function deleteVolume(
  workspaceId: string,
  name: string,
): Promise<{ deleted: boolean }> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/volumes/{volume}", volumePath(workspaceId, name)),
  );
  return { deleted: true };
}

const volumePath = (workspaceId: string, volume: string) => ({
  params: { path: { workspace: workspaceName(workspaceId), volume } },
});

export function volumePathQueryOptions(workspaceId: string, volumeName: string, path: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumePath(workspaceId, volumeName, path),
    queryFn: async (): Promise<{ path_infos: VolumePathInfo[] }> => {
      const files: Schemas["VolumeFile"][] = [];
      let cursor: string | undefined;
      do {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/volumes/{volume}/files", {
            params: {
              ...volumePath(workspaceId, volumeName).params,
              query: { path: relativePath(path), limit: 1000, cursor },
            },
          }),
        );
        files.push(...page.files);
        cursor = page.next_cursor;
      } while (cursor);
      return {
        path_infos: files.map((file) => ({
          path: file.path,
          size: file.size_bytes,
          // Directories have no modification time.
          mod_time: file.modified_at ?? "",
          is_dir: file.is_dir,
        })),
      };
    },
    enabled: Boolean(volumeName),
    refetchInterval: 30_000,
  });
}

/** Files up to this size go up in one presigned PUT, larger ones as a multipart upload. */
const SINGLE_PUT_BYTES = 64 * 1024 * 1024;
const MIN_PART_BYTES = 16 * 1024 * 1024;
const MAX_PARTS = 10_000;
const PART_CONCURRENCY = 4;

/** Upload a file into `path`, straight to the object store through presigned URLs. */
export async function uploadVolumeFile(
  workspaceId: string,
  volumeName: string,
  path: string,
  file: File,
): Promise<void> {
  const target = volumePath(workspaceId, volumeName);
  const destination = joinRelativePath(path, file.name);
  if (file.size <= SINGLE_PUT_BYTES) {
    const presigned = await ok(
      api.POST("/v1/workspaces/{workspace}/volumes/{volume}/files/url", {
        ...target,
        body: { path: destination, method: "put", expires_seconds: 3600, download: false },
      }),
    );
    await putBytes(presigned.url, file);
    return;
  }

  const mib = 1024 * 1024;
  const partSize = Math.max(MIN_PART_BYTES, Math.ceil(file.size / MAX_PARTS / mib) * mib);
  const upload = await ok(
    api.POST("/v1/workspaces/{workspace}/volumes/{volume}/uploads", {
      ...target,
      body: { path: destination, size_bytes: file.size, part_size_bytes: partSize },
    }),
  );
  try {
    const parts: Schemas["CompletedPart"][] = [];
    const pending = [...upload.parts];
    const worker = async () => {
      for (let part = pending.shift(); part; part = pending.shift()) {
        const etag = await putBytes(
          part.url,
          file.slice(part.offset, part.offset + part.size_bytes),
        );
        if (!etag) throw new Error("The object store did not return the part's ETag.");
        parts.push({ number: part.number, etag });
      }
    };
    await Promise.all(Array.from({ length: PART_CONCURRENCY }, worker));
    parts.sort((left, right) => left.number - right.number);
    await ok(
      api.POST("/v1/workspaces/{workspace}/volumes/{volume}/uploads/complete", {
        ...target,
        body: { path: upload.path, upload_id: upload.upload_id, parts },
      }),
    );
  } catch (error) {
    // Aborting makes the store drop the parts already sent.
    await ok(
      api.POST("/v1/workspaces/{workspace}/volumes/{volume}/uploads/abort", {
        ...target,
        body: { path: upload.path, upload_id: upload.upload_id },
      }),
    ).catch(() => undefined);
    throw error;
  }
}

async function putBytes(url: string, body: Blob): Promise<string | null> {
  const response = await fetch(url, { method: "PUT", body });
  if (!response.ok) throw new Error(`Upload failed (${response.status} ${response.statusText})`);
  return response.headers.get("ETag");
}

/** Removes a file, or a directory and everything under it. */
export async function deleteVolumePath(
  workspaceId: string,
  volumeName: string,
  path: string,
): Promise<{ deleted: string[] }> {
  const removed = await ok(
    api.DELETE("/v1/workspaces/{workspace}/volumes/{volume}/files", {
      params: {
        ...volumePath(workspaceId, volumeName).params,
        query: { path: relativePath(path) },
      },
    }),
  );
  return { deleted: removed.removed };
}

/** A short-lived presigned GET that browsers save as a file; it carries no session. */
export async function volumeDownloadUrl(
  workspaceId: string,
  volumeName: string,
  path: string,
): Promise<string> {
  const presigned = await ok(
    api.POST("/v1/workspaces/{workspace}/volumes/{volume}/files/url", {
      ...volumePath(workspaceId, volumeName),
      body: { path: relativePath(path), method: "get", expires_seconds: 300, download: true },
    }),
  );
  return presigned.url;
}

function relativePath(path: string): string {
  return path.replace(/^\/+|\/+$/g, "");
}

function joinRelativePath(path: string, name: string): string {
  const relative = relativePath(path);
  return relative ? `${relative}/${name}` : name;
}
