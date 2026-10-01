import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

import { accountQueryKeys, workspaceQueryKeys } from "./workspace-keys";

const workspacePath = (workspace: string) => ({ path: { workspace } });

export function workspaceMembersQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.members(workspace),
    queryFn: () =>
      ok(api.GET("/v1/workspaces/{workspace}/members", { params: workspacePath(workspace) })),
    staleTime: 30_000,
  });
}

export function workspaceInvitationsQueryOptions(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.invitations(workspace),
    queryFn: () =>
      ok(api.GET("/v1/workspaces/{workspace}/invitations", { params: workspacePath(workspace) })),
    staleTime: 30_000,
  });
}

export function inviteWorkspaceMember(
  workspace: string,
  email: string,
  role: Schemas["InvitationRole"],
): Promise<Schemas["Invitation"]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/invitations", {
      params: workspacePath(workspace),
      body: { email, role },
    }),
  );
}

export function resendWorkspaceInvitation(
  workspace: string,
  invitation: string,
): Promise<Schemas["Invitation"]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/invitations/{invitation}/resend", {
      params: { path: { workspace, invitation } },
    }),
  );
}

export function revokeWorkspaceInvitation(workspace: string, invitation: string): Promise<void> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/invitations/{invitation}", {
      params: { path: { workspace, invitation } },
    }),
  );
}

export function setWorkspaceMemberRole(
  workspace: string,
  user: string,
  role: Schemas["InvitationRole"],
): Promise<Schemas["Member"]> {
  return ok(
    api.PATCH("/v1/workspaces/{workspace}/members/{user}", {
      params: { path: { workspace, user } },
      body: { role },
    }),
  );
}

/** Remove a member, or leave when the id is your own. */
export function removeWorkspaceMember(workspace: string, user: string): Promise<void> {
  return ok(
    api.DELETE("/v1/workspaces/{workspace}/members/{user}", {
      params: { path: { workspace, user } },
    }),
  );
}

/** What the invitation link opens onto. Reading it never redeems the offer. */
export function invitationPreviewQueryOptions(token: string) {
  return queryOptions({
    queryKey: accountQueryKeys.invitation(token),
    queryFn: () => ok(api.GET("/v1/invitations/{token}", { params: { path: { token } } })),
    retry: false,
    staleTime: 0,
  });
}

export function acceptInvitation(token: string): Promise<Schemas["AcceptedInvitation"]> {
  return ok(api.POST("/v1/invitations/{token}/accept", { params: { path: { token } } }));
}

export function declineInvitation(token: string): Promise<void> {
  return ok(api.POST("/v1/invitations/{token}/decline", { params: { path: { token } } }));
}
