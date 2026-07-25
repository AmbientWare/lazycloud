import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import {
  apiRequest,
  listWorkspaces,
  postJson,
  withWorkspace,
} from "@/lib/api/client";
import { workspaceSchema, type Workspace } from "@/lib/api/schemas";

export function workspacesQueryOptions() {
  return queryOptions({
    queryKey: ["workspaces"],
    queryFn: () => listWorkspaces({ includeDeleting: true }),
    staleTime: 30_000,
  });
}

/** Workspace that owns the authenticated token, used for protected actions. */
export function currentWorkspaceQueryOptions() {
  return queryOptions({
    queryKey: ["workspaces", "current"],
    queryFn: () => apiRequest("/api/v1/workspaces/current", workspaceSchema),
    staleTime: 5 * 60_000,
  });
}

/** Admin-only: create a workspace. 403 for non-admin tokens. */
export function createWorkspace(name: string): Promise<Workspace> {
  return postJson("/api/v1/workspaces", workspaceSchema, { name });
}

/** Admin-only: irreversibly delete an empty, non-system workspace. */
export function deleteWorkspace(workspaceId: string): Promise<null> {
  return apiRequest(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`,
    z.null(),
    {
      method: "DELETE",
    },
  );
}

export function updateWorkspace(
  workspaceId: string,
  name: string,
): Promise<Workspace> {
  return apiRequest(
    withWorkspace("/api/v1/workspaces/current", workspaceId),
    workspaceSchema,
    {
      method: "PATCH",
      body: JSON.stringify({ name: name.trim() }),
    },
  );
}
