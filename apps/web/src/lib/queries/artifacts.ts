import { queryOptions, infiniteQueryOptions } from "@tanstack/react-query";
import { z } from "zod";
import {
  artifactStorageSummarySchema,
  artifactSummarySchema,
  artifactRetentionPolicySchema,
  artifactRetentionPreviewSchema,
} from "@/lib/api/schemas/artifacts";

import { apiBlob, apiRequest, withWorkspace } from "@/lib/api/client";
import { artifactListSchema } from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

export type ArtifactFilters = {
  search?: string;
  task_id?: string;
  app_id?: string;
  content_type?: string;
  created_after?: string;
  created_before?: string;
};

export function artifactsQuery(workspaceId: string, filters: ArtifactFilters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  return infiniteQueryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspaceId), "list", filters],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      apiRequest(
        withWorkspace(
          `/api/v1/artifacts?${params}&cursor=${encodeURIComponent(pageParam)}`,
          workspaceId,
        ),
        artifactListSchema,
      ),
    getNextPageParam: (page) => page.next || undefined,
    refetchInterval: 15_000,
  });
}
export function artifactStorageQuery(workspaceId: string) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.storage.artifacts(workspaceId), "summary"],
    queryFn: () =>
      apiRequest(
        withWorkspace("/api/v1/artifacts/summary", workspaceId),
        artifactStorageSummarySchema,
      ),
    refetchInterval: 15_000,
  });
}
export function deleteArtifact(workspaceId: string, id: string) {
  return apiRequest(
    withWorkspace(`/api/v1/artifacts/${encodeURIComponent(id)}`, workspaceId),
    z.null(),
    { method: "DELETE" },
  );
}
export function updateArtifactRetention(
  workspaceId: string,
  id: string,
  retention_seconds: number | null,
) {
  return apiRequest(
    withWorkspace(`/api/v1/artifacts/${encodeURIComponent(id)}/retention`, workspaceId),
    artifactSummarySchema,
    { method: "PATCH", body: JSON.stringify({ retention_seconds }) },
  );
}
export function updateWorkspaceArtifactRetention(
  workspaceId: string,
  retention_seconds: number | null,
) {
  return apiRequest(
    withWorkspace("/api/v1/artifacts/retention", workspaceId),
    artifactRetentionPolicySchema,
    { method: "PUT", body: JSON.stringify({ retention_seconds }) },
  );
}
export function applyArtifactRetention(
  workspaceId: string,
  ids: string[],
  retention_seconds: number | null,
  apply = false,
) {
  return apiRequest(
    withWorkspace(`/api/v1/artifacts/retention/${apply ? "apply" : "preview"}`, workspaceId),
    artifactRetentionPreviewSchema,
    { method: "POST", body: JSON.stringify({ ids, retention_seconds }) },
  );
}

/**
 * Fetch an artifact's bytes from the control plane.
 *
 * Same-origin on purpose: a presigned URL names the object store, which is
 * routinely unreachable from wherever the dashboard is actually open. The
 * Blob is returned rather than an object URL so the caller that renders it
 * also owns creating and revoking the URL.
 */
export async function fetchArtifactBlob(
  workspaceId: string,
  artifact: { id: string; task_id: string; filename: string },
): Promise<Blob> {
  const query = new URLSearchParams({
    id: artifact.id,
    task_id: artifact.task_id,
    filename: artifact.filename,
  });
  return apiBlob(withWorkspace(`/api/v1/artifacts/content?${query.toString()}`, workspaceId));
}
