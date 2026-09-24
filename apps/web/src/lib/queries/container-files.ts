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

/** The most entries one directory listing shows; the server reports whether it stopped short. */
export const CONTAINER_FILE_LIST_LIMIT = 1000;

/** How much of a file a preview reads. */
export const CONTAINER_FILE_PREVIEW_BYTES = 256 * 1024;

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

/** A whole file, up to the server's download limit. */
export function downloadContainerFile(
  workspaceId: string,
  containerId: string,
  path: string,
): Promise<PodFileDownload> {
  const params = new URLSearchParams({ container_path: path });
  return apiRequest(
    withWorkspace(`${filesPath(containerId)}/download?${params.toString()}`, workspaceId),
    podFileDownloadSchema,
  );
}

/** The first `CONTAINER_FILE_PREVIEW_BYTES` of a file, with `truncated` set when there is more. */
export function readContainerFilePreview(
  workspaceId: string,
  containerId: string,
  path: string,
  signal: AbortSignal,
): Promise<PodFileDownload> {
  const params = new URLSearchParams({
    container_path: path,
    max_bytes: String(CONTAINER_FILE_PREVIEW_BYTES),
    truncate: "true",
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
