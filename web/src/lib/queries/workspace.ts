import { api, ok, type Schemas } from "@/lib/api/client";

/**
 * Administrators only: create a workspace owned by the signed-in account. 403 otherwise.
 * A connection id places the workspace, its compute and its bucket, in the caller's
 * ready AWS connection for good; the server resolves which connection that is.
 */
export function createWorkspace(
  name: string,
  connectionId: string | null,
): Promise<Schemas["Workspace"]> {
  return ok(
    api.POST("/v1/workspaces", {
      body: connectionId === null ? { name } : { name, cloud: "aws" },
    }),
  );
}

/** Administrators only: delete a workspace; its data is removed in the background. */
export async function deleteWorkspace(workspaceName: string): Promise<null> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}", {
      params: { path: { workspace: workspaceName } },
    }),
  );
  return null;
}

export function updateWorkspace(
  workspaceName: string,
  name: string,
): Promise<Schemas["Workspace"]> {
  return ok(
    api.PATCH("/v1/workspaces/{workspace}", {
      params: { path: { workspace: workspaceName } },
      body: { name: name.trim() },
    }),
  );
}
