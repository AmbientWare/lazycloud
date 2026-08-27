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
