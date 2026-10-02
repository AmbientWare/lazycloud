import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { workspaceQueryKeys } from "./workspace-keys";

export type ArtifactFilters = {
  search?: string;
  task_id?: string;
  app?: string;
  content_type?: string;
  created_after?: string;
  created_before?: string;
};

const TASK_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function artifactsQuery(workspace: string, filters: ArtifactFilters) {
  return infiniteQueryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspace), "list", filters],
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<Schemas["ArtifactPage"]> => {
      const taskId = filters.task_id?.trim();
      // Artifacts match whole task IDs; a partial one matches none.
      if (taskId && !TASK_ID.test(taskId)) return { artifacts: [] };
      return ok(
        api.GET("/v1/workspaces/{workspace}/artifacts", {
          params: {
            path: { workspace },
            query: {
              search: filters.search || undefined,
              task_id: taskId || undefined,
              app: filters.app || undefined,
              content_type: filters.content_type || undefined,
              created_after: filters.created_after || undefined,
              created_before: filters.created_before || undefined,
              limit: 100,
              cursor: pageParam || undefined,
            },
          },
        }),
      );
    },
    getNextPageParam: (page) => page.next_cursor,
    refetchInterval: 15_000,
  });
}

export function artifactStorageQuery(workspace: string) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspace), "summary"],
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/artifacts/summary", {
          params: { path: { workspace } },
        }),
      ),
    refetchInterval: 15_000,
  });
}

/** Deletes up to 100 artifacts; one already gone counts as deleted. */
export function deleteArtifacts(workspace: string, ids: string[]) {
  return ok(
    api.POST("/v1/workspaces/{workspace}/artifacts/delete", {
      params: { path: { workspace } },
      body: { ids },
    }),
  );
}

/** A short-lived download link; with `download` browsers save the file rather than show it. */
export async function artifactUrl(
  workspace: string,
  artifact: string,
  download: boolean,
  signal?: AbortSignal,
): Promise<string> {
  const presigned = await ok(
    api.POST("/v1/workspaces/{workspace}/artifacts/{artifact}/url", {
      params: { path: { workspace, artifact } },
      body: { expires_seconds: 300, download },
      signal,
    }),
  );
  return presigned.url;
}

/**
 * An artifact's bytes for a preview. The Blob is returned rather than an
 * object URL so the component that renders it also owns creating and
 * revoking the URL.
 */
export function artifactContentQueryOptions(workspace: string, artifact: Schemas["Artifact"]) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspace), "content", artifact.id],
    queryFn: async ({ signal }) => {
      const url = await artifactUrl(workspace, artifact.id, false, signal);
      const response = await fetch(url, { signal });
      if (!response.ok) throw new Error(`Could not read ${artifact.filename} (${response.status})`);
      return response.blob();
    },
    staleTime: Infinity,
    gcTime: 60_000,
  });
}
