import { z } from "zod";

import { workspaceSchema } from "./workspaces";

const timestampSchema = z.string().datetime({ offset: true });

export const platformRoleSchema = z.enum(["administrator", "member"]);
export const userStatusSchema = z.enum(["active", "disabled"]);

export const userSchema = z
  .object({
    id: z.string(),
    username: z.string(),
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
  })
  .strict();
export type Session = z.infer<typeof sessionSchema>;

export const currentSessionSchema = z
  .object({
    user: userSchema,
    workspaces: z.array(workspaceSchema),
  })
  .strict();
export type CurrentSession = z.infer<typeof currentSessionSchema>;
