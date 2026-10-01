import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { api, apiRequest, ok, postJson, withWorkspace, type Schemas } from "@/lib/api/client";
import { workspaceName } from "@/lib/api/workspaces";
import { fileBase64 } from "@/lib/files";
import {
  diskListSchema,
  volumePathListSchema,
  volumeListSchema,
  volumeSchema,
  type Disk,
  type SecretMasked,
  type Volume,
} from "@/lib/api/schemas";
import {
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";

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
    meta: workspaceLiveQueryMeta(true),
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
    meta: workspaceLiveQueryMeta(true),
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
