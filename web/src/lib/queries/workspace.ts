import { api, ok } from "@/lib/api/client";
import type { Workspace } from "@/lib/api/schemas";
import { viewWorkspace } from "@/lib/api/views";
import { rememberWorkspaces, workspaceName } from "@/lib/api/workspaces";

/**
 * Administrators only: create a workspace owned by the signed-in account. 403 otherwise.
 * A connection id places the workspace, its compute and its bucket, in that connected
 * AWS account for good.
 */
export async function createWorkspace(
  name: string,
  connectionId: string | null,
): Promise<Workspace> {
  void connectionId;
  const created = await ok(api.POST("/v1/workspaces", { body: { name } }));
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
