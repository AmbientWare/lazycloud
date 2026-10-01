import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { api, ok, type Schemas } from "@/lib/api/client";
import { apiRequest, postJson, withWorkspace } from "@/lib/api/unserved";
import { fileBase64 } from "@/lib/files";
import {
  diskListSchema,
  volumePathListSchema,
  volumeListSchema,
  volumeSchema,
  type Disk,
  type Volume,
} from "@/lib/api/schemas";
import {
  nextListCursor,
  nextPageCursor,
  selectInfiniteList,
  selectPages,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";

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

const DISK_PAGE_SIZE = 50;

export function disksQueryOptions(workspaceId: string) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.disks(workspaceId),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(DISK_PAGE_SIZE) });
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(
        withWorkspace(`/api/v1/disks?${params.toString()}`, workspaceId),
        diskListSchema,
      );
    },
    getNextPageParam: nextListCursor,
  });
}

export function selectDiskList(
  data: InfiniteListQueryData<Disk> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (disk) => disk.id);
}

export function deleteDisk(workspaceId: string, name: string): Promise<null> {
  return apiRequest(
    withWorkspace(`/api/v1/disks/${encodeURIComponent(name)}`, workspaceId),
    z.null(),
    { method: "DELETE" },
  );
}

// --- Volumes ---

export function volumesQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumes(workspaceId),
    queryFn: () => apiRequest(withWorkspace("/api/v1/volumes", workspaceId), volumeListSchema),
  });
}

const getOrCreateVolumeResponseSchema = z.object({
  volume: volumeSchema.nullish(),
});

export function createVolume(workspaceId: string, name: string): Promise<Volume | null> {
  return postJson(withWorkspace("/api/v1/volumes", workspaceId), getOrCreateVolumeResponseSchema, {
    name,
  }).then((response) => response.volume ?? null);
}

const deleteVolumeResponseSchema = z.object({ deleted: z.boolean() });

export function deleteVolume(
  workspaceId: string,
  name: string,
): Promise<z.infer<typeof deleteVolumeResponseSchema>> {
  return postJson(
    withWorkspace(`/api/v1/volumes/${encodeURIComponent(name)}/delete`, workspaceId),
    deleteVolumeResponseSchema,
    { name },
  );
}

export function volumePathQueryOptions(workspaceId: string, volumeName: string, path: string) {
  const target = joinVolumePath(volumeName, path);
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumePath(workspaceId, volumeName, path),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/volumes/${encodePath(target)}`, workspaceId),
        volumePathListSchema,
      ),
    enabled: Boolean(volumeName),
    refetchInterval: 30_000,
  });
}

const copyPathResponseSchema = z.object({ object_id: z.string().default("") });

export async function uploadVolumeFile(
  workspaceId: string,
  volumeName: string,
  path: string,
  file: File,
): Promise<void> {
  const destination = joinVolumePath(volumeName, joinRelativePath(path, file.name));
  await postJson(withWorkspace("/api/v1/volumes/copy-path", workspaceId), copyPathResponseSchema, {
    path: destination,
    value_base64: await fileBase64(file),
  });
}

const deletePathResponseSchema = z.object({
  deleted: z.array(z.string()).default([]),
});

export function deleteVolumePath(
  workspaceId: string,
  volumeName: string,
  path: string,
): Promise<{ deleted: string[] }> {
  const target = joinVolumePath(volumeName, path);
  return postJson(
    withWorkspace(`/api/v1/volumes/${encodePath(target)}/delete`, workspaceId),
    deletePathResponseSchema,
  );
}

const presignedUrlSchema = z.object({ url: z.string().url() });

export function volumeDownloadUrl(
  workspaceId: string,
  volumeName: string,
  path: string,
): Promise<string> {
  return postJson(withWorkspace("/api/v1/volumes/presigned-url", workspaceId), presignedUrlSchema, {
    volume_name: volumeName,
    volume_path: path,
    expires: 300,
    method: "get-object",
  }).then((response) => response.url);
}

function joinVolumePath(volumeName: string, path: string): string {
  const relative = path.replace(/^\/+|\/+$/g, "");
  return relative ? `${volumeName}/${relative}` : volumeName;
}

function joinRelativePath(path: string, name: string): string {
  const relative = path.replace(/^\/+|\/+$/g, "");
  return relative ? `${relative}/${name}` : name;
}

function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}
