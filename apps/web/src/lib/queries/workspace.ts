import { z } from "zod";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import { workspaceSchema, type Workspace } from "@/lib/api/schemas";

/** Administrators only: create a workspace owned by the signed-in account. 403 otherwise. */
export function createWorkspace(name: string): Promise<Workspace> {
  return postJson("/api/v1/workspaces", workspaceSchema, { name });
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
