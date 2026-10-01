import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { nextPageCursor, selectPages } from "@/lib/queries/infinite-list";

import { workspaceQueryKeys } from "./workspace-keys";

// --- Secrets (collection reads stay masked; cleartext is fetched only on demand) ---

export type Secret = Schemas["Secret"];

export function secretsQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.secrets(workspace),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/secrets", {
          params: { path: { workspace }, query: { limit: 100, cursor: pageParam || undefined } },
        }),
      ),
    getNextPageParam: nextPageCursor,
  });
}

export function selectSecrets(
  data: { pages: readonly Schemas["SecretPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.secrets,
    hasNextPage,
    (secret) => secret.name,
  );
}

/** Create a secret; an existing name is a conflict. */
export function createSecret(workspace: string, name: string, value: string): Promise<Secret> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/secrets", {
      params: { path: { workspace } },
      body: { name, value },
    }),
  );
}

/** Replace an existing secret's value. */
export function updateSecretValue(
  workspace: string,
  secret: string,
  value: string,
): Promise<Secret> {
  return ok(
    api.PATCH("/v1/workspaces/{workspace}/secrets/{secret}", {
      params: { path: { workspace, secret } },
      body: { value },
    }),
  );
}

/** The only read that returns a value; the caller keeps it in memory only while shown. */
export async function revealSecretValue(workspace: string, secret: string): Promise<string> {
  const revealed = await ok(
    api.GET("/v1/workspaces/{workspace}/secrets/{secret}/value", {
      params: { path: { workspace, secret } },
    }),
  );
  return revealed.value;
}

export function deleteSecret(workspace: string, secret: string): Promise<void> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/secrets/{secret}", {
      params: { path: { workspace, secret } },
    }),
  );
}

// --- Disks (created by the workloads that declare them) ---

export type Disk = Schemas["Disk"];

export function disksQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.disks(workspace),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/disks", {
          params: { path: { workspace }, query: { limit: 50, cursor: pageParam || undefined } },
        }),
      ),
    getNextPageParam: nextPageCursor,
  });
}

export function selectDisks(
  data: { pages: readonly Schemas["DiskPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.disks,
    hasNextPage,
    (disk) => disk.id,
  );
}

/** Refused with a conflict while a container holds the disk. */
export function deleteDisk(workspace: string, disk: string): Promise<void> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/disks/{disk}", {
      params: { path: { workspace, disk } },
    }),
  );
}

// --- Volumes ---

export type Volume = Schemas["Volume"];
export type VolumeFile = Schemas["VolumeFile"];

export function volumesQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.volumes(workspace),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/volumes", {
          params: { path: { workspace }, query: { limit: 100, cursor: pageParam || undefined } },
        }),
      ),
    getNextPageParam: nextPageCursor,
  });
}

export function selectVolumes(
  data: { pages: readonly Schemas["VolumePage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.volumes,
    hasNextPage,
    (volume) => volume.id,
  );
}

/** Creates the volume, or returns the existing one of that name. */
export function createVolume(workspace: string, name: string): Promise<Volume> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/volumes", {
      params: { path: { workspace } },
      body: { name },
    }),
  );
}

/** Refused with a conflict while a container that mounts the volume has not stopped. */
export function deleteVolume(workspace: string, volume: string): Promise<void> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/volumes/{volume}", {
      params: { path: { workspace, volume } },
    }),
  );
}

/** One directory's entries; `path` is relative to the volume root, empty for the root. */
export function volumeFilesQueryOptions(workspace: string, volume: string, path: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.volumeFiles(workspace, volume, path),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/volumes/{volume}/files", {
          params: {
            path: { workspace, volume },
            query: { path, limit: 1000, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
    refetchInterval: 30_000,
  });
}

export function selectVolumeFiles(
  data: { pages: readonly Schemas["VolumeFilePage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.files,
    hasNextPage,
    (file) => file.path,
  );
}

/** Removes a file, or a directory and everything under it. */
export function deleteVolumePath(
  workspace: string,
  volume: string,
  path: string,
): Promise<Schemas["RemovedVolumeFiles"]> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/volumes/{volume}/files", {
      params: { path: { workspace, volume }, query: { path } },
    }),
  );
}

/** A short-lived presigned GET; the URL carries no session credential. */
export async function volumeDownloadUrl(
  workspace: string,
  volume: string,
  path: string,
): Promise<string> {
  const presigned = await ok(
    api.POST("/v1/workspaces/{workspace}/volumes/{volume}/files/url", {
      params: { path: { workspace, volume } },
      body: { path, method: "get", expires_seconds: 300 },
    }),
  );
  return presigned.url;
}

/** Files up to this size go up in one presigned PUT, larger ones as a multipart upload. */
const SINGLE_PUT_BYTES = 64 * 1024 * 1024;
const MIN_PART_BYTES = 16 * 1024 * 1024;
const MAX_PARTS = 10_000;
const PART_CONCURRENCY = 4;

/**
 * Upload a file into `directory`, straight to the object store through
 * presigned URLs. The store has to let this origin PUT and read `ETag`.
 */
export async function uploadVolumeFile(
  workspace: string,
  volume: string,
  directory: string,
  file: File,
): Promise<void> {
  const path = joinRelativePath(directory, file.name);
  if (file.size <= SINGLE_PUT_BYTES) {
    const presigned = await ok(
      api.POST("/v1/workspaces/{workspace}/volumes/{volume}/files/url", {
        params: { path: { workspace, volume } },
        body: { path, method: "put", expires_seconds: 3600 },
      }),
    );
    await putBytes(presigned.url, file);
    return;
  }

  const mib = 1024 * 1024;
  const partSize = Math.max(MIN_PART_BYTES, Math.ceil(file.size / MAX_PARTS / mib) * mib);
  const upload = await ok(
    api.POST("/v1/workspaces/{workspace}/volumes/{volume}/uploads", {
      params: { path: { workspace, volume } },
      body: { path, size_bytes: file.size, part_size_bytes: partSize },
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
        params: { path: { workspace, volume } },
        body: { path: upload.path, upload_id: upload.upload_id, parts },
      }),
    );
  } catch (error) {
    // Aborting makes the store drop the parts already sent.
    await ok(
      api.POST("/v1/workspaces/{workspace}/volumes/{volume}/uploads/abort", {
        params: { path: { workspace, volume } },
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

function joinRelativePath(path: string, name: string): string {
  const relative = path.replace(/^\/+|\/+$/g, "");
  return relative ? `${relative}/${name}` : name;
}
