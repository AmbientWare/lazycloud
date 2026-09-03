import { z } from "zod";

import { workspaceSchema } from "./workspaces";

const timestampSchema = z.string().datetime({ offset: true });

export const platformRoleSchema = z.enum(["administrator", "member"]);
export const userStatusSchema = z.enum(["active", "disabled"]);

export const userSchema = z
  .object({
    id: z.string(),
    // Normalized by the server to the GitHub name or, failing that, the login, so
    // this always renders as something and the browser needs no fallback chain.
    display_name: z.string(),
    email: z.string(),
    avatar_url: z.string(),
    // Empty for an account with no linked identity: it owns tokens and cannot
    // sign in. That is a real state, not a missing value.
    github_user_id: z.string(),
    github_login: z.string(),
    role: platformRoleSchema,
    status: userStatusSchema,
    created_at: timestampSchema,
    updated_at: timestampSchema,
  })
  .strict();
export type User = z.infer<typeof userSchema>;

export const sessionSchema = z
  .object({
    token: z.string(),
    expires_at: timestampSchema,
    user: userSchema,
    // Where the person was headed before signing in. Held server-side across the
    // round trip, so it arrives here rather than in the callback URL.
    return_to: z.string(),
  })
  .strict();

export const currentSessionSchema = z
  .object({
    user: userSchema,
    workspaces: z.array(workspaceSchema),
  })
  .strict();
export type CurrentSession = z.infer<typeof currentSessionSchema>;

export const workspaceMemberSchema = z
  .object({
    user_id: z.string(),
    display_name: z.string(),
    email: z.string(),
    role: z.enum(["owner", "administrator", "member"]),
    created_at: timestampSchema,
  })
  .strict();
export type WorkspaceMember = z.infer<typeof workspaceMemberSchema>;

export const workspaceMemberListSchema = z
  .object({
    data: z.array(workspaceMemberSchema),
    next: z.string(),
  })
  .strict();

export const workspaceRoleSchema = z.enum(["owner", "administrator", "member"]);
export type WorkspaceRole = z.infer<typeof workspaceRoleSchema>;

/** The roles an invitation can carry. Owner is transferred, never offered. */
export const invitableRoleSchema = z.enum(["administrator", "member"]);
export type InvitableRole = z.infer<typeof invitableRoleSchema>;

export const workspaceInvitationSchema = z
  .object({
    id: z.string(),
    workspace_id: z.string(),
    email: z.string(),
    role: workspaceRoleSchema,
    status: z.enum(["pending", "accepted", "declined", "revoked"]),
    invited_by_user_id: z.string(),
    invited_by_name: z.string(),
    expires_at: timestampSchema,
    created_at: timestampSchema,
    updated_at: timestampSchema,
  })
  .strict();
export type WorkspaceInvitation = z.infer<typeof workspaceInvitationSchema>;

export const workspaceInvitationListSchema = z
  .object({
    data: z.array(workspaceInvitationSchema),
    next: z.string(),
  })
  .strict();

/** An invitation as the person it was sent to sees it. */
export const pendingInvitationSchema = z
  .object({
    id: z.string(),
    workspace_id: z.string(),
    workspace_name: z.string(),
    email: z.string(),
    role: workspaceRoleSchema,
    invited_by_name: z.string(),
    expires_at: timestampSchema,
    created_at: timestampSchema,
  })
  .strict();
export type PendingInvitation = z.infer<typeof pendingInvitationSchema>;

export const pendingInvitationListSchema = z
  .object({
    data: z.array(pendingInvitationSchema),
    next: z.string(),
  })
  .strict();
