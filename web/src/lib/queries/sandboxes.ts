import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/unserved";
import {
  containerDetailSchema,
  containerSchema,
  podCreateImageSchema,
  podEmptyMutationSchema,
  podMemorySnapshotSchema,
  podProcessListSchema,
  podUrlsSchema,
  sandboxListSchema,
} from "@/lib/api/schemas";

import { workspaceQueryKeys } from "./workspace-keys";

export function sandboxesQueryOptions(
  workspaceId: string,
  options: { limit?: number; appId?: string } = {},
) {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.appId) params.set("app_id", options.appId);
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.list(
      workspaceId,
      options.limit ?? 50,
      options.appId ?? null,
    ),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/stubs/sandboxes?${params.toString()}`, workspaceId),
        sandboxListSchema,
      ),
  });
}

/** A sandbox container with its image, command, ports and lineage. */
export function sandboxContainerQueryOptions(workspace: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.container(workspace, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/containers/${containerId}`, workspace),
        containerDetailSchema,
      ),
  });
}

export function stopSandboxMutationOptions(workspace: string, containerId: string) {
  return mutationOptions({
    mutationFn: () =>
      postJson(
        withWorkspace(`/api/v1/containers/${encodeURIComponent(containerId)}/stop`, workspace),
        containerSchema,
        {},
      ),
  });
}

export function sandboxProcessesQueryOptions(workspaceId: string, containerId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.processes(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/processes`, workspaceId),
        podProcessListSchema,
      ),
    refetchInterval: 5_000,
  });
}

export function sandboxUrlsQueryOptions(
  workspaceId: string,
  containerId: string,
  enabled: boolean,
) {
  return queryOptions({
    queryKey: workspaceQueryKeys.sandboxes.urls(workspaceId, containerId),
    queryFn: () =>
      apiRequest(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/urls`, workspaceId),
        podUrlsSchema,
      ),
    enabled,
    refetchInterval: enabled ? 10_000 : false,
  });
}

export function killSandboxProcessMutationOptions(workspaceId: string, containerId: string) {
  return mutationOptions({
    mutationFn: (pid: number) =>
      postJson(
        withWorkspace(`/api/v1/pods/${encodeURIComponent(containerId)}/kill`, workspaceId),
        podEmptyMutationSchema,
        { pid },
      ),
  });
}

export function createSandboxImageMutationOptions(
  workspaceId: string,
  containerId: string,
  stubId: string,
) {
  return mutationOptions({
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/create-image-from-filesystem`,
          workspaceId,
        ),
        podCreateImageSchema,
        { stub_id: stubId },
      ),
  });
}

export function snapshotSandboxMemoryMutationOptions(
  workspaceId: string,
  containerId: string,
  stubId: string,
) {
  return mutationOptions({
    mutationFn: () =>
      postJson(
        withWorkspace(
          `/api/v1/pods/${encodeURIComponent(containerId)}/snapshot-memory`,
          workspaceId,
        ),
        podMemorySnapshotSchema,
        { stub_id: stubId },
      ),
  });
}
