import type { Workspace } from "@/lib/api/schemas";

type DeleteAvailability = { allowed: true } | { allowed: false; reason: string };

/**
 * Whether the delete control is offered for a workspace.
 *
 * A hint, not the decision: the server refuses a protected deletion regardless.
 * Both rules here are answerable from the workspace list the shell already holds,
 * so offering the button never depends on a second request resolving first.
 */
export function workspaceDeleteAvailability(
  candidate: Workspace,
  workspaceCount: number,
): DeleteAvailability {
  if (candidate.name === "default") {
    return { allowed: false, reason: "The default workspace is protected" };
  }
  if (workspaceCount <= 1) {
    return { allowed: false, reason: "The final workspace is protected" };
  }
  return { allowed: true };
}
