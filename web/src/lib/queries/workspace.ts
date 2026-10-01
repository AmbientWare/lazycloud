import { api, ok } from "@/lib/api/client";
import type { Workspace } from "@/lib/api/schemas";
import { viewWorkspace } from "@/lib/api/views";
import { rememberWorkspaces, workspaceName } from "@/lib/api/workspaces";

/**
 * Administrators only: create a workspace owned by the signed-in account. 403 otherwise.
 * A connection id places the workspace, its compute and its bucket, in the caller's
 * ready AWS connection for good; the server resolves which connection that is.
 */
export async function createWorkspace(
  name: string,
  connectionId: string | null,
): Promise<Workspace> {
  const created = await ok(
    api.POST("/v1/workspaces", {
      body: connectionId === null ? { name } : { name, cloud: "aws" },
    }),
  );
  rememberWorkspaces([created]);
  return viewWorkspace(created);
}

/** Administrators only: delete a workspace; its data is removed in the background. */
export async function deleteWorkspace(workspaceId: string): Promise<null> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}", {
      params: { path: { workspace: workspaceName(workspaceId) } },
    }),
  );
  return null;
}

export async function updateWorkspace(workspaceId: string, name: string): Promise<Workspace> {
  const updated = await ok(
    api.PATCH("/v1/workspaces/{workspace}", {
      params: { path: { workspace: workspaceName(workspaceId) } },
      body: { name: name.trim() },
    }),
  );
  rememberWorkspaces([updated]);
  return viewWorkspace(updated);
}
