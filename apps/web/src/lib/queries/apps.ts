import { queryOptions, type QueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  appSummaryListSchema,
  appSchema,
  deploymentSchema,
  deploymentManifestSchema,
  deploymentUrlSchema,
} from "@/lib/api/schemas";

import { workspaceLiveQueryMeta, workspaceQueryKeys } from "./workspace-keys";

export function appQueryOptions(workspaceId: string, appId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.detail(workspaceId, appId),
    queryFn: () => apiRequest(withWorkspace(`/api/v1/apps/${appId}`, workspaceId), appSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function appSummariesQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.summaries(workspaceId),
    queryFn: () =>
      apiRequest(withWorkspace("/api/v1/apps/summaries", workspaceId), appSummaryListSchema),
    meta: workspaceLiveQueryMeta(true),
  });
}

export function invalidateAppLists(queryClient: QueryClient, workspaceId: string) {
  return Promise.all(
    [
      workspaceQueryKeys.apps.summaries(workspaceId),
      workspaceQueryKeys.deployments.root(workspaceId),
      workspaceQueryKeys.containers.root(workspaceId),
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

export function pauseAppMutationOptions(workspaceId: string, appId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/apps/${encodeURIComponent(appId)}/pause`, workspaceId),
        appSchema,
      ),
  };
}

export function resumeAppMutationOptions(workspaceId: string, appId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/apps/${encodeURIComponent(appId)}/resume`, workspaceId),
        appSchema,
      ),
  };
}

const noContentSchema = z.null();

export function deleteAppMutationOptions(workspaceId: string, appId: string) {
  return {
    mutationFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/apps/${encodeURIComponent(appId)}`, workspaceId),
        noContentSchema,
        { method: "DELETE" },
      ),
  };
}

export function deleteWorkloadMutationOptions(workspaceId: string, deploymentIds: string[]) {
  return {
    mutationFn: async () => {
      for (const deploymentId of deploymentIds) {
        await apiRequest(
          withWorkspace(`/api/v1/deployments/${encodeURIComponent(deploymentId)}`, workspaceId),
          noContentSchema,
          { method: "DELETE" },
        );
      }
      return null;
    },
  };
}

export function startDeploymentMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/deployments/${encodeURIComponent(deploymentId)}/start`, workspaceId),
        deploymentSchema,
      ),
  };
}

export function stopDeploymentMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/deployments/${encodeURIComponent(deploymentId)}/stop`, workspaceId),
        deploymentSchema,
      ),
  };
}

export function scaleDeploymentMutationOptions(
  workspaceId: string,
  deploymentId: string,
  replicas: number,
) {
  return {
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/deployments/${encodeURIComponent(deploymentId)}/scale`, workspaceId),
        deploymentSchema,
        { replicas },
      ),
  };
}

export function deleteDeploymentMutationOptions(workspaceId: string, deploymentId: string) {
  return {
    mutationFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/deployments/${encodeURIComponent(deploymentId)}`, workspaceId),
        noContentSchema,
        { method: "DELETE" },
      ),
  };
}

/**
 * Invoke manifest for one deployment: invoke URL built against this origin,
 * recorded input schema, and the typed client contract (the same data
 * `lazycloud client get` consumes for codegen).
 */
export function deploymentManifestQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.deploymentManifest(workspaceId, deploymentId),
    queryFn: () => {
      const externalUrl = encodeURIComponent(window.location.origin);
      return apiRequest(
        withWorkspace(
          `/api/v1/deployments/${deploymentId}/manifest?external_url=${externalUrl}`,
          workspaceId,
        ),
        deploymentManifestSchema,
      );
    },
  });
}

export function deploymentUrlQueryOptions(workspaceId: string, deploymentId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.apps.deploymentUrl(workspaceId, deploymentId),
    queryFn: () => {
      const externalUrl = encodeURIComponent(window.location.origin);
      return apiRequest(
        withWorkspace(
          `/api/v1/deployments/${deploymentId}/url?external_url=${externalUrl}`,
          workspaceId,
        ),
        deploymentUrlSchema,
      );
    },
  });
}
