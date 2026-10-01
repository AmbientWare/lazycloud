import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import { base64ToBytes, downloadBlob, fileBase64 } from "@/lib/files";
import {
  podEmptyMutationSchema,
  podFileDownloadSchema,
  podFileListSchema,
} from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

/** The most entries one directory listing shows; the server reports whether it stopped short. */
export const CONTAINER_FILE_LIST_LIMIT = 1000;

/** How much of a file a preview reads. */
export const CONTAINER_FILE_PREVIEW_BYTES = 256 * 1024;

/** The largest image a preview reads whole to draw it; a larger one previews as bytes. */
export const CONTAINER_FILE_IMAGE_PREVIEW_BYTES = 8 * 1024 * 1024;

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

/** Save a whole container file, up to the server's download limit, to the person's machine. */
export async function saveContainerFile(
  workspaceId: string,
  containerId: string,
  path: string,
  filename: string,
): Promise<void> {
  const params = new URLSearchParams({ container_path: path });
  const download = await apiRequest(
    withWorkspace(`${filesPath(containerId)}/download?${params.toString()}`, workspaceId),
    podFileDownloadSchema,
  );
  downloadBlob(filename, new Blob([base64ToBytes(download.value_base64)]));
}

/**
 * The first `maxBytes` of a file, with `truncated` set when there is more.
 *
 * Read afresh whenever a preview mounts and dropped once it unmounts, so a
 * reopened file shows its current bytes and a closed preview holds none.
 */
export function containerFilePreviewQueryOptions(
  workspaceId: string,
  containerId: string,
  path: string,
  maxBytes: number,
) {
  const params = new URLSearchParams({
    container_path: path,
    max_bytes: String(maxBytes),
    truncate: "true",
  });
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.filePreview(workspaceId, containerId, path, maxBytes),
    queryFn: ({ signal }) =>
      apiRequest(
        withWorkspace(`${filesPath(containerId)}/download?${params.toString()}`, workspaceId),
        podFileDownloadSchema,
        { signal },
      ),
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  });
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
