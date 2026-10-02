import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { accountQueryKeys, workspaceQueryKeys } from "./workspace-keys";

const workspacePath = (workspaceName: string) => ({ path: { workspace: workspaceName } });

export function workspaceMembersQueryOptions(workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.members(workspaceName),
    queryFn: () =>
      ok(api.GET("/v1/workspaces/{workspace}/members", { params: workspacePath(workspaceName) })),
    staleTime: 30_000,
  });
}

export function workspaceInvitationsQueryOptions(workspaceName: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.invitations(workspaceName),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/invitations", { params: workspacePath(workspaceName) }),
      ),
    staleTime: 30_000,
  });
}

export function inviteWorkspaceMember(
  workspaceName: string,
  email: string,
  role: Schemas["InvitationRole"],
): Promise<Schemas["Invitation"]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/invitations", {
      params: workspacePath(workspaceName),
      body: { email, role },
    }),
  );
}

export function resendWorkspaceInvitation(
  workspaceName: string,
  invitationId: string,
): Promise<Schemas["Invitation"]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/invitations/{invitation}/resend", {
      params: { path: { workspace: workspaceName, invitation: invitationId } },
    }),
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
  role: Schemas["InvitationRole"],
): Promise<Schemas["Member"]> {
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
    queryFn: (): Promise<Schemas["InvitationPreview"]> =>
      ok(api.GET("/v1/invitations/{token}", { params: { path: { token } } })),
    retry: false,
    staleTime: 0,
  });
}

export function acceptInvitation(token: string): Promise<Schemas["AcceptedInvitation"]> {
  return ok(api.POST("/v1/invitations/{token}/accept", { params: { path: { token } } }));
}

export async function declineInvitation(token: string): Promise<null> {
  await ok(api.POST("/v1/invitations/{token}/decline", { params: { path: { token } } }));
  return null;
}
