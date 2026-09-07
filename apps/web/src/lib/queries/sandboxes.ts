import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  podCreateImageSchema,
  podEmptyMutationSchema,
  podFileDownloadSchema,
  podFileListSchema,
  podFilePreviewSchema,
  podMemorySnapshotSchema,
  podProcessListSchema,
  podUrlsSchema,
  sandboxListSchema,
  type PodFileDownload,
} from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export function sandboxesQueryOptions(
  workspaceId: string,
  options: { limit?: number; appId?: string } = {},
) {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.appId) params.set("app_id", options.appId);
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.list(
      workspaceId,
      options.limit ?? 50,
      options.appId ?? null,
    ),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/stubs/sandboxes?${params.toString()}`, workspaceId),
        sandboxListSchema,
      ),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function sandboxFilesQueryOptions(workspaceId: string, containerId: string, path: string) {
  const params = new URLSearchParams({ container_path: path || "/" });
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.files(workspaceId, containerId, path),
    queryFn: () =>
      apiRequest(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/files?${params.toString()}`,
          workspaceId,
        ),
        podFileListSchema,
      ),
    refetchInterval: 15_000,
  });
}

export function sandboxProcessesQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.processes(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/processes`, workspaceId),
        podProcessListSchema,
      ),
    refetchInterval: 5_000,
  });
}

export function sandboxUrlsQueryOptions(
  workspaceId: string,
  containerId: string,
  enabled: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.urls(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/urls`, workspaceId),
        podUrlsSchema,
      ),
    enabled,
    refetchInterval: enabled ? 10_000 : false,
  });
}

export function uploadSandboxFileMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: async ({ path, file }: { path: string; file: File }) =>
      postJson(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/files/upload`, workspaceId),
        podEmptyMutationSchema,
        {
          container_path: path,
          value_base64: bytesToBase64(new Uint8Array(await file.arrayBuffer())),
        },
      ),
  });
}

export function deleteSandboxFileMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: (path: string) =>
      apiRequest(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/files?${new URLSearchParams({ container_path: path })}`,
          workspaceId,
        ),
        podEmptyMutationSchema,
        { method: "DELETE" },
      ),
  });
}

export function killSandboxProcessMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: (pid: number) =>
      postJson(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/kill`, workspaceId),
        podEmptyMutationSchema,
        { pid },
      ),
  });
}

export function createSandboxImageMutationOptions(
  workspaceId: string,
  containerId: string,
  stubId: string,
) {
  return mutationOptions({
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/create-image-from-filesystem`,
          workspaceId,
        ),
        podCreateImageSchema,
        { stub_id: stubId },
      ),
  });
}

export function snapshotSandboxMemoryMutationOptions(
  workspaceId: string,
  containerId: string,
  stubId: string,
) {
  return mutationOptions({
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/snapshot-memory`,
          workspaceId,
        ),
        podMemorySnapshotSchema,
        { stub_id: stubId },
      ),
  });
}

export function downloadSandboxFile(
  workspaceId: string,
  containerId: string,
  path: string,
): Promise<PodFileDownload> {
  return apiRequest(
    withWorkspace(
      `/api/v1/pods/${encodeURIComponent(containerId)}/files/download?${new URLSearchParams({ container_path: path })}`,
      workspaceId,
    ),
    podFileDownloadSchema,
  );
}

export function sandboxPreviewQueryOptions(
  workspaceId: string,
  containerId: string,
  path: string | null,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.preview(workspaceId, containerId, path),
    enabled: path !== null,
    gcTime: 0,
    queryFn: ({ signal }) =>
      apiRequest(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/files/preview?${new URLSearchParams({ container_path: path ?? "" })}`,
          workspaceId,
        ),
        podFilePreviewSchema,
        { signal },
      ),
  });
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunkSize = 32_768;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}
