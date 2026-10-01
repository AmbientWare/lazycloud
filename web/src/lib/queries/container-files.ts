import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import { downloadBlob } from "@/lib/files";

import { workspaceQueryKeys } from "./workspace-keys";

/** The most entries one directory listing shows; the server reports whether it stopped short. */
export const CONTAINER_FILE_LIST_LIMIT = 1000;

/** How much of a file a preview reads. */
export const CONTAINER_FILE_PREVIEW_BYTES = 256 * 1024;

/** The largest image a preview reads whole to draw it; a larger one previews as bytes. */
export const CONTAINER_FILE_IMAGE_PREVIEW_BYTES = 8 * 1024 * 1024;

export function containerFilesQueryOptions(workspace: string, containerId: string, path: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.files(workspace, containerId, path),
    queryFn: ({ signal }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/containers/{container}/files", {
          params: {
            path: { workspace, container: containerId },
            query: { path: path || "/", limit: CONTAINER_FILE_LIST_LIMIT },
          },
          signal,
        }),
      ),
    refetchInterval: 15_000,
  });
}

/** Save a whole container file, up to the server's download limit, to the person's machine. */
export async function saveContainerFile(
  workspace: string,
  containerId: string,
  path: string,
  filename: string,
): Promise<void> {
  const blob = await ok(
    api.GET("/v1/workspaces/{workspace}/containers/{container}/files/content", {
      params: { path: { workspace, container: containerId }, query: { path } },
      parseAs: "blob",
    }),
  );
  downloadBlob(filename, blob as Blob);
}

/**
 * The first `maxBytes` of a file, with `truncated` set when there is more.
 *
 * Read afresh whenever a preview mounts and dropped once it unmounts, so a
 * reopened file shows its current bytes and a closed preview holds none.
 */
export function containerFilePreviewQueryOptions(
  workspace: string,
  containerId: string,
  path: string,
  maxBytes: number,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.containers.filePreview(workspace, containerId, path, maxBytes),
    queryFn: async ({ signal }) => {
      const result = await api.GET(
        "/v1/workspaces/{workspace}/containers/{container}/files/content",
        {
          params: {
            path: { workspace, container: containerId },
            query: { path, max_bytes: maxBytes, truncate: true },
          },
          parseAs: "arrayBuffer",
          signal,
        },
      );
      const bytes = await ok(Promise.resolve(result));
      return {
        bytes: new Uint8Array(bytes as ArrayBuffer),
        truncated: result.response.headers.get("Lazycloud-Truncated") === "true",
      };
    },
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function uploadContainerFileMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: ({ path, file }: { path: string; file: File }) =>
      ok(
        api.PUT("/v1/workspaces/{workspace}/containers/{container}/files/content", {
          params: { path: { workspace, container: containerId }, query: { path } },
          // The body is the file's bytes, sent as they are.
          body: "",
          bodySerializer: () => file,
          headers: { "Content-Type": "application/octet-stream" },
        }),
      ),
  });
}

export function deleteContainerFileMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: (path: string) =>
      ok(
        api.DELETE("/v1/workspaces/{workspace}/containers/{container}/files", {
          params: { path: { workspace, container: containerId }, query: { path } },
        }),
      ),
  });
}
