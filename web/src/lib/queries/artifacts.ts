import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type { ArtifactStorageSummary, ArtifactSummary } from "@/lib/api/schemas";
import { workspaceName } from "@/lib/api/workspaces";

import { appDirectory } from "./directory";
import { workspaceQueryKeys } from "./workspace-keys";

export type ArtifactFilters = {
  search?: string;
  task_id?: string;
  app_id?: string;
  content_type?: string;
  created_after?: string;
  created_before?: string;
};

const TASK_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function viewArtifact(artifact: Schemas["Artifact"], appId: string | null): ArtifactSummary {
  return {
    id: artifact.id,
    task_id: artifact.task_id ?? "",
    filename: artifact.filename,
    content_type: artifact.content_type,
    size: artifact.size_bytes,
    created_at: artifact.stored_at ?? artifact.created_at,
    app_id: appId,
    app_name: artifact.app ?? "",
    // Listed artifacts are stored, and retention starts when they are.
    expires_at: artifact.expires_at ?? "",
    // Deleting is immediate; the list never shows one in progress.
    deleting: false,
    deletion_failed: false,
  };
}

export function artifactsQuery(workspaceId: string, filters: ArtifactFilters) {
  return infiniteQueryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspaceId), "list", filters],
    initialPageParam: "",
    queryFn: async ({ pageParam, client }): Promise<{ data: ArtifactSummary[]; next: string }> => {
      const taskId = filters.task_id?.trim();
      // Artifacts match whole task IDs; a partial one matches none.
      if (taskId && !TASK_ID.test(taskId)) return { data: [], next: "" };
      const apps = await appDirectory(client, workspaceId);
      // The app filter carries an app's name, or its ID from a link.
      const app = filters.app_id ? (apps.byId.get(filters.app_id)?.name ?? filters.app_id) : "";
      const page = await ok(
        api.GET("/v1/workspaces/{workspace}/artifacts", {
          params: {
            path: { workspace: workspaceName(workspaceId) },
            query: {
              search: filters.search || undefined,
              task_id: taskId || undefined,
              app: app || undefined,
              content_type: filters.content_type || undefined,
              created_after: filters.created_after || undefined,
              created_before: filters.created_before || undefined,
              limit: 100,
              cursor: pageParam || undefined,
            },
          },
        }),
      );
      return {
        data: page.artifacts.map((artifact) =>
          viewArtifact(artifact, artifact.app ? (apps.byName.get(artifact.app)?.id ?? null) : null),
        ),
        next: page.next_cursor ?? "",
      };
    },
    getNextPageParam: (page) => page.next || undefined,
    refetchInterval: 15_000,
  });
}

export function artifactStorageQuery(workspaceId: string) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspaceId), "summary"],
    queryFn: async (): Promise<ArtifactStorageSummary> => {
      const summary = await ok(
        api.GET("/v1/workspaces/{workspace}/artifacts/summary", {
          params: { path: { workspace: workspaceName(workspaceId) } },
        }),
      );
      return {
        count: summary.count,
        size_bytes: summary.size_bytes,
        // Storage pricing has no rate here yet; nothing has been charged for it.
        estimated_monthly_nanos: null,
        accrued_nanos: 0,
        accrued_since: new Date().toISOString(),
        retention_seconds: summary.retention_seconds,
      };
    },
    refetchInterval: 15_000,
  });
}

export async function deleteArtifact(workspaceId: string, id: string): Promise<null> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/artifacts/{artifact}", {
      params: { path: { workspace: workspaceName(workspaceId), artifact: id } },
    }),
  );
  return null;
}

/**
 * Fetch an artifact's bytes through a short-lived presigned GET. The Blob is
 * returned rather than an object URL so the caller that renders it also owns
 * creating and revoking the URL.
 */
export async function fetchArtifactBlob(
  workspaceId: string,
  artifact: { id: string; task_id: string; filename: string },
  signal?: AbortSignal,
): Promise<Blob> {
  const presigned = await ok(
    api.POST("/v1/workspaces/{workspace}/artifacts/{artifact}/url", {
      params: { path: { workspace: workspaceName(workspaceId), artifact: artifact.id } },
      body: { expires_seconds: 300, download: false },
      signal,
    }),
  );
  const response = await fetch(presigned.url, { signal });
  if (!response.ok) throw new Error(`Could not read ${artifact.filename} (${response.status})`);
  return response.blob();
}

export function artifactContentQueryOptions(
  workspaceId: string,
  artifact: { id: string; task_id: string; filename: string },
) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspaceId), "content", artifact.id],
    queryFn: ({ signal }) => fetchArtifactBlob(workspaceId, artifact, signal),
    staleTime: Infinity,
    gcTime: 60_000,
  });
}
