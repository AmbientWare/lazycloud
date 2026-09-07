import { queryOptions } from "@tanstack/react-query";

import { apiBlob, apiRequest, withWorkspace } from "@/lib/api/client";
import { artifactListSchema, artifactPreviewSchema, type ArtifactList } from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

/** Files a task produced, newest first. */
export function taskArtifactsQuery(workspaceId: string, taskId: string) {
  return queryOptions<ArtifactList>({
    queryKey: workspaceQueryKeys.tasks.artifacts(workspaceId, taskId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/artifacts?task_id=${encodeURIComponent(taskId)}`, workspaceId),
        artifactListSchema,
      ),
    enabled: Boolean(workspaceId && taskId),
  });
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

export function fetchArtifactPreview(
  workspaceId: string,
  artifact: { id: string; task_id: string; filename: string },
) {
  const query = new URLSearchParams(artifact);
  return apiRequest(
    withWorkspace(`/api/v1/artifacts/preview?${query}`, workspaceId),
    artifactPreviewSchema,
  );
}
