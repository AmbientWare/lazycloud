import { queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  artifactListSchema,
  artifactPublicUrlSchema,
  type ArtifactList,
  type ArtifactPublicUrl,
} from "@/lib/api/schemas";

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
 * A short-lived direct link to one artifact. Minted on demand rather than
 * listed, so a link is only ever created for something the reader opened.
 */
export function artifactPublicUrl(
  workspaceId: string,
  artifactId: string,
  taskId: string,
  filename: string,
): Promise<ArtifactPublicUrl> {
  return postJson(
    withWorkspace("/api/v1/artifacts/public-url", workspaceId),
    artifactPublicUrlSchema,
    { id: artifactId, task_id: taskId, filename },
  );
}
