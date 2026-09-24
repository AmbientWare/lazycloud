import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import { fileBase64 } from "@/lib/files";
import {
  podEmptyMutationSchema,
  podFileDownloadSchema,
  podFileListSchema,
  type PodFileDownload,
} from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

/** The most entries one directory listing returns; the server reports whether it stopped short. */
export const CONTAINER_FILE_LIST_LIMIT = 1000;

/** The largest file the browser reads; the server refuses a larger one before reading it. */
export const CONTAINER_FILE_DOWNLOAD_LIMIT_BYTES = 10 * 1024 * 1024;

export function containerFilesQueryOptions(workspaceId: string, containerId: string, path: string) {
  const params = new URLSearchParams({
    container_path: path || "/",
    limit: String(CONTAINER_FILE_LIST_LIMIT),
  });
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.files(workspaceId, containerId, path),
    queryFn: () =>
      apiRequest(
        withWorkspace(`${filesPath(containerId)}?${params.toString()}`, workspaceId),
        podFileListSchema,
      ),
    refetchInterval: 15_000,
  });
}

export function downloadContainerFile(
  workspaceId: string,
  containerId: string,
  path: string,
  signal?: AbortSignal,
): Promise<PodFileDownload> {
  const params = new URLSearchParams({
    container_path: path,
    max_bytes: String(CONTAINER_FILE_DOWNLOAD_LIMIT_BYTES),
  });
  return apiRequest(
    withWorkspace(`${filesPath(containerId)}/download?${params.toString()}`, workspaceId),
    podFileDownloadSchema,
    { signal },
  );
}

export function uploadContainerFileMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: async ({ path, file }: { path: string; file: File }) =>
      postJson(
        withWorkspace(`${filesPath(containerId)}/upload`, workspaceId),
        podEmptyMutationSchema,
        {
          container_path: path,
          value_base64: await fileBase64(file),
        },
      ),
  });
}

export function deleteContainerFileMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: (path: string) =>
      apiRequest(
        withWorkspace(
          `${filesPath(containerId)}?${new URLSearchParams({ container_path: path }).toString()}`,
          workspaceId,
        ),
        podEmptyMutationSchema,
        { method: "DELETE" },
      ),
  });
}

function filesPath(containerId: string): string {
  return `/api/v1/pods/${encodeURIComponent(containerId)}/files`;
}
