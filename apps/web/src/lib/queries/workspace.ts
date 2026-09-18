import { z } from "zod";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import { workspaceSchema, type Workspace } from "@/lib/api/schemas";

/**
 * Administrators only: create a workspace owned by the signed-in account. 403 otherwise.
 * A connection id places the workspace, its compute and its bucket, in that connected
 * AWS account for good.
 */
export function createWorkspace(name: string, connectionId: string | null): Promise<Workspace> {
  return postJson("/api/v1/workspaces", workspaceSchema, { name, connection_id: connectionId });
}

/** Administrators only: irreversibly delete an empty, non-system workspace. */
export function deleteWorkspace(workspaceId: string): Promise<null> {
  return apiRequest(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}`, z.null(), {
    method: "DELETE",
  });
}

export function updateWorkspace(workspaceId: string, name: string): Promise<Workspace> {
  return apiRequest(withWorkspace("/api/v1/workspaces/current", workspaceId), workspaceSchema, {
    method: "PATCH",
    body: JSON.stringify({ name: name.trim() }),
  });
}
