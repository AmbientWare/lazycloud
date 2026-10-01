import { queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";
import type {
  InvitableRole,
  InvitationPreview,
  WorkspaceInvitation,
  WorkspaceMember,
} from "@/lib/api/schemas";

import { accountQueryKeys, workspaceQueryKeys } from "./workspace-keys";

const workspacePath = (workspaceName: string) => ({ path: { workspace: workspaceName } });

export function workspaceMembersQueryOptions(workspaceId: string, workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.members(workspaceId),
    queryFn: async () => {
      const list = await ok(
        api.GET("/v1/workspaces/{workspace}/members", { params: workspacePath(workspaceName) }),
      );
      return { data: list.members as WorkspaceMember[], next: "" };
    },
    staleTime: 30_000,
  });
}

export function workspaceInvitationsQueryOptions(workspaceId: string, workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.invitations(workspaceId),
    queryFn: async () => {
      const list = await ok(
        api.GET("/v1/workspaces/{workspace}/invitations", { params: workspacePath(workspaceName) }),
      );
      return { data: list.invitations.map(viewInvitation), next: "" };
    },
    staleTime: 30_000,
  });
}

/** A withdrawn email reads as one that was never sent. */
function viewInvitation(invitation: {
  invited_by_user_id?: string;
  delivery: string;
}): WorkspaceInvitation {
  return {
    ...(invitation as WorkspaceInvitation),
    invited_by_user_id: invitation.invited_by_user_id ?? "",
    delivery: (invitation.delivery === "discarded"
      ? "queued"
      : invitation.delivery) as WorkspaceInvitation["delivery"],
  };
}

export async function inviteWorkspaceMember(
  workspaceName: string,
  email: string,
  role: InvitableRole,
): Promise<WorkspaceInvitation> {
  return viewInvitation(
    await ok(
      api.POST("/v1/workspaces/{workspace}/invitations", {
        params: workspacePath(workspaceName),
        body: { email, role },
      }),
    ),
  );
}

export async function resendWorkspaceInvitation(
  workspaceName: string,
  invitationId: string,
): Promise<WorkspaceInvitation> {
  return viewInvitation(
    await ok(
      api.POST("/v1/workspaces/{workspace}/invitations/{invitation}/resend", {
        params: { path: { workspace: workspaceName, invitation: invitationId } },
      }),
    ),
  );
}

export async function revokeWorkspaceInvitation(
  workspaceName: string,
  invitationId: string,
): Promise<null> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/invitations/{invitation}", {
      params: { path: { workspace: workspaceName, invitation: invitationId } },
    }),
  );
  return null;
}

export function setWorkspaceMemberRole(
  workspaceName: string,
  userId: string,
  role: InvitableRole,
): Promise<WorkspaceMember> {
  return ok(
    api.PATCH("/v1/workspaces/{workspace}/members/{user}", {
      params: { path: { workspace: workspaceName, user: userId } },
      body: { role },
    }),
  );
}

/** Remove a member, or leave when the id is your own. */
export async function removeWorkspaceMember(workspaceName: string, userId: string): Promise<null> {
  await ok(
    api.DELETE("/v1/workspaces/{workspace}/members/{user}", {
      params: { path: { workspace: workspaceName, user: userId } },
    }),
  );
  return null;
}

/** What the invitation link opens onto. Reading it never redeems the offer. */
export function invitationPreviewQueryOptions(token: string) {
  return queryOptions({
    queryKey: accountQueryKeys.invitation(token),
    queryFn: (): Promise<InvitationPreview> =>
      ok(api.GET("/v1/invitations/{token}", { params: { path: { token } } })),
    retry: false,
    staleTime: 0,
  });
}

export async function acceptInvitation(token: string): Promise<WorkspaceMember> {
  const accepted = await ok(
    api.POST("/v1/invitations/{token}/accept", { params: { path: { token } } }),
  );
  return accepted.member;
}

export async function declineInvitation(token: string): Promise<null> {
  await ok(api.POST("/v1/invitations/{token}/decline", { params: { path: { token } } }));
  return null;
}
