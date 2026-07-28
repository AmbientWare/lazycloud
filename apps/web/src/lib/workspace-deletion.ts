import type { Workspace } from "@/lib/api/schemas";

type DeleteAvailability = { allowed: true } | { allowed: false; reason: string };

export function workspaceDeleteAvailability(
  candidate: Workspace,
  tokenWorkspaceId: string | undefined,
  workspaceCount: number,
): DeleteAvailability {
  if (candidate.name === "default") {
    return { allowed: false, reason: "The default workspace is protected" };
  }
  if (!tokenWorkspaceId) {
    return { allowed: false, reason: "Checking workspace protection" };
  }
  if (candidate.id === tokenWorkspaceId) {
    return {
      allowed: false,
      reason: "The workspace that owns this admin token is protected",
    };
  }
  if (workspaceCount <= 1) {
    return { allowed: false, reason: "The final workspace is protected" };
  }
  return { allowed: true };
}
