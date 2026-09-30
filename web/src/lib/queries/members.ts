import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";
import {
  invitationPreviewSchema,
  workspaceInvitationListSchema,
  workspaceInvitationSchema,
  workspaceMemberListSchema,
  workspaceMemberSchema,
  type InvitableRole,
  type WorkspaceInvitation,
  type WorkspaceMember,
} from "@/lib/api/schemas";

import { accountQueryKeys, workspaceQueryKeys } from "./workspace-keys";

const workspacePath = (workspaceName: string) =>
  `/api/v1/workspaces/${encodeURIComponent(workspaceName)}`;

export function workspaceMembersQueryOptions(workspaceId: string, workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.members(workspaceId),
    queryFn: () => apiRequest(`${workspacePath(workspaceName)}/members`, workspaceMemberListSchema),
    staleTime: 30_000,
  });
}

export function workspaceInvitationsQueryOptions(workspaceId: string, workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.invitations(workspaceId),
    queryFn: () =>
      apiRequest(`${workspacePath(workspaceName)}/invitations`, workspaceInvitationListSchema),
    staleTime: 30_000,
  });
}

export function inviteWorkspaceMember(
  workspaceName: string,
  email: string,
  role: InvitableRole,
): Promise<WorkspaceInvitation> {
  return apiRequest(`${workspacePath(workspaceName)}/invitations`, workspaceInvitationSchema, {
    method: "POST",
    body: JSON.stringify({ email, role }),
  });
}

export function resendWorkspaceInvitation(
  workspaceName: string,
  invitationId: string,
): Promise<WorkspaceInvitation> {
  return apiRequest(
    `${workspacePath(workspaceName)}/invitations/${encodeURIComponent(invitationId)}/resend`,
    workspaceInvitationSchema,
    { method: "POST" },
  );
}

export function revokeWorkspaceInvitation(
  workspaceName: string,
  invitationId: string,
): Promise<null> {
  return apiRequest(
    `${workspacePath(workspaceName)}/invitations/${encodeURIComponent(invitationId)}`,
    z.null(),
    { method: "DELETE" },
  );
}

export function setWorkspaceMemberRole(
  workspaceName: string,
  userId: string,
  role: InvitableRole,
): Promise<WorkspaceMember> {
  return apiRequest(
    `${workspacePath(workspaceName)}/members/${encodeURIComponent(userId)}`,
    workspaceMemberSchema,
    { method: "PUT", body: JSON.stringify({ role }) },
  );
}

/** Remove a member, or leave when the id is your own. */
export function removeWorkspaceMember(workspaceName: string, userId: string): Promise<null> {
  return apiRequest(
    `${workspacePath(workspaceName)}/members/${encodeURIComponent(userId)}`,
    z.null(),
    { method: "DELETE" },
  );
}

/** What the invitation link opens onto. Reading it never redeems the offer. */
export function invitationPreviewQueryOptions(token: string) {
  return queryOptions({
    queryKey: accountQueryKeys.invitation(token),
    queryFn: () =>
      apiRequest(`/api/v1/invitations/${encodeURIComponent(token)}`, invitationPreviewSchema),
    retry: false,
    staleTime: 0,
  });
}

export function acceptInvitation(token: string): Promise<WorkspaceMember> {
  return apiRequest(
    `/api/v1/invitations/${encodeURIComponent(token)}/accept`,
    workspaceMemberSchema,
    { method: "POST" },
  );
}

export function declineInvitation(token: string): Promise<null> {
  return apiRequest(`/api/v1/invitations/${encodeURIComponent(token)}/decline`, z.null(), {
    method: "POST",
  });
}
