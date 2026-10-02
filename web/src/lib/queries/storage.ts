import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

/** Every page of a collection; the storage tabs list all of it. */
export async function allPages<T>(
  read: (cursor: string | undefined) => Promise<{ items: T[]; next?: string }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await read(cursor);
    items.push(...page.items);
    cursor = page.next;
  } while (cursor);
  return items;
}

// --- Secrets (collection reads carry no value; cleartext is fetched only on demand) ---

export function secretsQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.secrets(workspace),
    queryFn: () =>
      allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/secrets", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        return { items: page.secrets, next: page.next_cursor };
      }),
    meta: workspaceLiveQueryMeta(true),
  });
}

const secretPath = (workspace: string, secret: string) => ({
  params: { path: { workspace, secret } },
});

export function createSecret(workspace: string, name: string, value: string) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/secrets", {
      params: { path: { workspace } },
      body: { name, value },
    }),
  );
}

export function updateSecretValue(workspace: string, name: string, value: string) {
  return ok(
    api.PATCH("/v1/workspaces/{workspace}/secrets/{secret}", {
      ...secretPath(workspace, name),
      body: { value },
    }),
  );
}

export async function revealSecretValue(workspace: string, name: string): Promise<string> {
  const revealed = await ok(
    api.GET("/v1/workspaces/{workspace}/secrets/{secret}/value", secretPath(workspace, name)),
  );
  return revealed.value;
}

export function deleteSecret(workspace: string, name: string) {
  return ok(api.DELETE("/v1/workspaces/{workspace}/secrets/{secret}", secretPath(workspace, name)));
}

// --- Disks (created by the workloads that declare them) ---

const DISK_PAGE_SIZE = 50;

export function disksQueryOptions(workspace: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.disks(workspace),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/disks", {
          params: {
            path: { workspace },
            query: { limit: DISK_PAGE_SIZE, cursor: pageParam || undefined },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor,
    meta: workspaceLiveQueryMeta(true),
  });
}

export function deleteDisk(workspace: string, disk: string) {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/disks/{disk}", {
      params: { path: { workspace, disk } },
    }),
  );
}

// --- Volumes ---

const volumePath = (workspace: string, volume: string) => ({
  params: { path: { workspace, volume } },
});

export function volumesQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumes(workspace),
    queryFn: () =>
      allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/volumes", {
            params: { path: { workspace }, query: { limit: 100, cursor } },
          }),
        );
        return { items: page.volumes, next: page.next_cursor };
      }),
    meta: workspaceLiveQueryMeta(true),
  });
}

/** Creates the volume, or returns the existing one of that name. */
export function createVolume(workspace: string, name: string) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/volumes", {
      params: { path: { workspace } },
      body: { name },
    }),
  );
}

/** Refused with a conflict while a container that mounts the volume has not stopped. */
export function deleteVolume(workspace: string, name: string) {
  return ok(api.DELETE("/v1/workspaces/{workspace}/volumes/{volume}", volumePath(workspace, name)));
}

export function volumeFilesQueryOptions(workspace: string, volume: string, path: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumePath(workspace, volume, path),
    queryFn: () =>
      allPages(async (cursor) => {
        const page = await ok(
          api.GET("/v1/workspaces/{workspace}/volumes/{volume}/files", {
            params: {
              ...volumePath(workspace, volume).params,
              query: { path: relativePath(path), limit: 1000, cursor },
            },
          }),
        );
        return { items: page.files, next: page.next_cursor };
      }),
    enabled: Boolean(volume),
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
  workspace: string,
  volume: string,
  path: string,
  file: File,
): Promise<void> {
  const target = volumePath(workspace, volume);
  const destination = joinRelativePath(path, file.name);
  if (file.size <= SINGLE_PUT_BYTES) {
    const presigned = await ok(
      api.POST("/v1/workspaces/{workspace}/volumes/{volume}/files/url", {
        ...target,
        body: { path: destination, method: "put", expires_seconds: 3600 },
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
export function deleteVolumePath(workspace: string, volume: string, path: string) {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/volumes/{volume}/files", {
      params: { ...volumePath(workspace, volume).params, query: { path: relativePath(path) } },
    }),
  );
}

/** A short-lived download link that browsers save as a file; it carries no session. */
export async function volumeDownloadUrl(
  workspace: string,
  volume: string,
  path: string,
): Promise<string> {
  const presigned = await ok(
    api.POST("/v1/workspaces/{workspace}/volumes/{volume}/files/url", {
      ...volumePath(workspace, volume),
      body: { path: relativePath(path), method: "get", expires_seconds: 300, download: true },
    }),
  );
  return presigned.url;
}

/** Have the browser save a download link; the store's response names the file. */
export function saveUrl(url: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener noreferrer";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

function relativePath(path: string): string {
  return path.replace(/^\/+|\/+$/g, "");
}

function joinRelativePath(path: string, name: string): string {
  const relative = relativePath(path);
  return relative ? `${relative}/${name}` : name;
}
