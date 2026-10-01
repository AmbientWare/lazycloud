import { api, ok, type Schemas } from "@/lib/api/client";

const path = (workspace: string) => ({ params: { path: { workspace } } });

/** Administrators only: create a workspace owned by the signed-in account. 403 otherwise. */
export function createWorkspace(name: string): Promise<Schemas["Workspace"]> {
  return ok(api.POST("/v1/workspaces", { body: { name } }));
}

/**
 * Administrators only: start deleting a workspace, or resume a deletion that has
 * not finished. It refuses every other request at once and disappears once its
 * tasks, containers and data are gone.
 */
export function deleteWorkspace(workspace: string): Promise<Schemas["Workspace"]> {
  return ok(api.DELETE("/v1/workspaces/{workspace}", path(workspace)));
}

export function renameWorkspace(workspace: string, name: string): Promise<Schemas["Workspace"]> {
  return ok(api.PATCH("/v1/workspaces/{workspace}", { ...path(workspace), body: { name } }));
}
