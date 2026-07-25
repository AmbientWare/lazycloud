import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  secretMaskedListSchema,
  secretMaskedSchema,
  secretRevealResponseSchema,
  volumePathListSchema,
  volumeListSchema,
  volumeSchema,
  type SecretMasked,
  type Volume,
} from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

// --- Secrets (collection reads stay masked; cleartext is fetched only on demand) ---

export function secretsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.secrets(workspaceId),
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/secrets", workspaceId),
        secretMaskedListSchema,
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

const createSecretResponseSchema = z.object({
  id: z.string(),
  name: z.string(),
});

export function createSecret(
  workspaceId: string,
  name: string,
  value: string,
): Promise<{ id: string; name: string }> {
  return postJson(
    withWorkspace("/api/v1/secrets", workspaceId),
    createSecretResponseSchema,
    {
      name,
      value,
    },
  );
}

export function updateSecretValue(
  workspaceId: string,
  name: string,
  value: string,
): Promise<SecretMasked> {
  return postJson(
    withWorkspace(`/api/v1/secrets/${encodeURIComponent(name)}`, workspaceId),
    secretMaskedSchema,
    { value },
  );
}

export async function revealSecretValue(workspaceId: string, name: string): Promise<string> {
  const response = await apiRequest(
    withWorkspace(`/api/v1/secrets/${encodeURIComponent(name)}`, workspaceId),
    secretRevealResponseSchema,
  );
  if (!response.secret) throw new Error("Secret not found");
  return response.secret.value;
}

const emptyResponseSchema = z.object({}).passthrough();

export function deleteSecret(
  workspaceId: string,
  name: string,
): Promise<unknown> {
  return apiRequest(
    withWorkspace(`/api/v1/secrets/${encodeURIComponent(name)}`, workspaceId),
    emptyResponseSchema,
    { method: "DELETE" },
  );
}

// --- Volumes ---

export function volumesQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.volumes(workspaceId),
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/volumes", workspaceId),
        volumeListSchema,
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

const getOrCreateVolumeResponseSchema = z.object({
  volume: volumeSchema.nullish(),
});

export function createVolume(
  workspaceId: string,
  name: string,
): Promise<Volume | null> {
  return postJson(
    withWorkspace("/api/v1/volumes", workspaceId),
    getOrCreateVolumeResponseSchema,
    { name },
  ).then((response) => response.volume ?? null);
}

const deleteVolumeResponseSchema = z.object({}).passthrough();

export function deleteVolume(
  workspaceId: string,
  name: string,
): Promise<unknown> {
  return postJson(
    withWorkspace(
      `/api/v1/volumes/${encodeURIComponent(name)}/delete`,
      workspaceId,
    ),
    deleteVolumeResponseSchema,
    { name },
  );
}

export function volumePathQueryOptions(
  workspaceId: string,
  volumeName: string,
  path: string,
) {
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
  await postJson(
    withWorkspace("/api/v1/volumes/copy-path", workspaceId),
    copyPathResponseSchema,
    {
      path: destination,
      value_base64: await fileBase64(file),
    },
  );
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
    withWorkspace(
      `/api/v1/volumes/${encodePath(target)}/delete`,
      workspaceId,
    ),
    deletePathResponseSchema,
  );
}

const presignedUrlSchema = z.object({ url: z.string().url() });

export function volumeDownloadUrl(
  workspaceId: string,
  volumeName: string,
  path: string,
): Promise<string> {
  return postJson(
    withWorkspace("/api/v1/volumes/presigned-url", workspaceId),
    presignedUrlSchema,
    {
      volume_name: volumeName,
      volume_path: path,
      expires: 300,
      method: "get-object",
    },
  ).then((response) => response.url);
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

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Unable to read file"));
    reader.onload = () => {
      const encoded = String(reader.result ?? "");
      const separator = encoded.indexOf(",");
      if (separator < 0) {
        reject(new Error("Unable to encode file"));
        return;
      }
      resolve(encoded.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}
