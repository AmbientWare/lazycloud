import { z } from "zod";

const timestampSchema = z.string().datetime({ offset: true });

export const tokenKindSchema = z.enum([
  "admin",
  "user",
  "session",
  "workspace-primary",
  "workspace",
  "workspace-restricted",
  "worker",
  "worker-private",
  "machine",
]);
export type TokenKind = z.infer<typeof tokenKindSchema>;

export const tokenStatusSchema = z.enum(["active", "revoked"]);
export type TokenStatus = z.infer<typeof tokenStatusSchema>;

export const authTokenSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    prefix: z.string(),
    kind: tokenKindSchema,
    // Exactly one is set: a credential names the person holding it or the single
    // workspace it was minted for.
    user_id: z.string(),
    workspace_id: z.string(),
    status: tokenStatusSchema,
    scopes: z.array(z.string()),
    reusable: z.boolean(),
    disabled_by_admin: z.boolean(),
    created_at: timestampSchema,
    last_used_at: timestampSchema.nullable(),
    expires_at: timestampSchema.nullable(),
    revoked_at: timestampSchema.nullable(),
  })
  .strict();
export type AuthToken = z.infer<typeof authTokenSchema>;

export const tokenListSchema = z
  .object({
    tokens: z.array(authTokenSchema),
  })
  .strict();
export type TokenListResponse = z.infer<typeof tokenListSchema>;

export const workspaceTokenCreateRequestSchema = z
  .object({
    name: z.string().trim().min(1),
    scopes: z.array(z.enum(["read", "write"])).min(1),
    expires_in_seconds: z.number().int().positive().nullable(),
    kind: z.literal("workspace"),
    workspace_id: z.string().min(1),
    reusable: z.literal(true),
  })
  .strict();
export type WorkspaceTokenCreateRequest = z.infer<typeof workspaceTokenCreateRequestSchema>;

export const tokenCreateResponseSchema = z
  .object({
    token: z.string(),
    record: authTokenSchema,
  })
  .strict();
export type TokenCreateResponse = z.infer<typeof tokenCreateResponseSchema>;

export const deviceCodeSchema = z.object({
  user_code: z.string(),
  client_name: z.string(),
  status: z.enum(["pending", "approved", "denied", "expired"]),
  created_at: z.string(),
  expires_at: z.string(),
});
export type DeviceCode = z.infer<typeof deviceCodeSchema>;
