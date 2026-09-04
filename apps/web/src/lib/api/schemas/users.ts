import { z } from "zod";

import { workspaceSchema } from "./workspaces";

const timestampSchema = z.string().datetime({ offset: true });

export const platformRoleSchema = z.enum(["administrator", "member"]);
export type PlatformRole = z.infer<typeof platformRoleSchema>;
export const userStatusSchema = z.enum(["active", "disabled"]);
export type UserStatus = z.infer<typeof userStatusSchema>;

export const userRoleRequestSchema = z.object({ role: platformRoleSchema }).strict();
export type UserRoleRequest = z.infer<typeof userRoleRequestSchema>;

export const userStatusRequestSchema = z.object({ status: userStatusSchema }).strict();
export type UserStatusRequest = z.infer<typeof userStatusRequestSchema>;

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

/**
 * The address an invitation can be sent to, refused the way the server refuses it.
 *
 * The server's rule is the one that decides, so this mirrors it rather than
 * inventing a looser one: a form that accepts what the API rejects turns a typo
 * into an error the dialog was not written to explain.
 */
export const invitationEmailSchema = z
  .string()
  .trim()
  .toLowerCase()
  .max(320, "Enter a single address such as name@example.com")
  .refine((value) => {
    const parts = value.split("@");
    if (parts.length !== 2) return false;
    const [local, domain] = parts;
    if (!local || /\s/.test(value)) return false;
    return domain.includes(".") && !domain.startsWith(".") && !domain.endsWith(".");
  }, "Enter a single address such as name@example.com");

export const workspaceInvitationSchema = z
  .object({
    id: z.string(),
    workspace_id: z.string(),
    email: z.string(),
    role: invitableRoleSchema,
    invited_by_user_id: z.string(),
    invited_by_name: z.string(),
    // The server's answer against the server's clock. Never recomputed here: a
    // browser comparing `expires_at` to its own would label offers by how far
    // that clock had drifted.
    expired: z.boolean(),
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

/** What the invitation link opens onto, before it is answered. */
export const invitationPreviewSchema = z
  .object({
    workspace_id: z.string(),
    workspace_name: z.string(),
    email: z.string(),
    role: invitableRoleSchema,
    invited_by_name: z.string(),
    expired: z.boolean(),
    expires_at: timestampSchema,
  })
  .strict();
export type InvitationPreview = z.infer<typeof invitationPreviewSchema>;
