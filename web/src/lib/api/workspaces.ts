/*
 * The dashboard addresses workspaces by ID, as its routes and query keys do;
 * the public API names them in every path. The session read records each
 * workspace the account reaches, and the query layer resolves names here.
 */
const names = new Map<string, string>();

export function rememberWorkspaces(workspaces: readonly { id: string; name: string }[]): void {
  for (const workspace of workspaces) names.set(workspace.id, workspace.name);
}

/** The name the API knows a workspace by, from its ID. */
export function workspaceName(workspaceId: string): string {
  const name = names.get(workspaceId);
  if (name === undefined) throw new Error(`Unknown workspace ${workspaceId}`);
  return name;
}
