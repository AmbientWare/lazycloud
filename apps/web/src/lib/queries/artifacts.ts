import { queryOptions } from "@tanstack/react-query";

import { apiBlob, apiRequest, withWorkspace } from "@/lib/api/client";
import { artifactListSchema, type ArtifactList } from "@/lib/api/schemas";

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
 * Fetch an artifact's bytes from the control plane as an object URL.
 *
 * Same-origin on purpose: a presigned URL names the object store, which is
 * routinely unreachable from wherever the dashboard is actually open. The
 * caller owns the returned URL and must revoke it.
 */
export async function fetchArtifactObjectUrl(
  workspaceId: string,
  artifact: { id: string; task_id: string; filename: string },
): Promise<string> {
  const query = new URLSearchParams({
    id: artifact.id,
    task_id: artifact.task_id,
    filename: artifact.filename,
  });
  const blob = await apiBlob(
    withWorkspace(`/api/v1/artifacts/content?${query.toString()}`, workspaceId),
  );
  return URL.createObjectURL(blob);
}
