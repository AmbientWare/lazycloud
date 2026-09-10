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

export const tokenStatusSchema = z.enum(["active", "revoked"]);

export const authTokenSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    prefix: z.string(),
    kind: tokenKindSchema,
    device_login: z.boolean().default(false),
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
    data: z.array(authTokenSchema),
    next: z.string(),
  })
  .strict();
export type TokenListResponse = z.infer<typeof tokenListSchema>;

export const tokenCreateRequestSchema = z
  .object({
    name: z.string().trim().min(1),
    // Omitted and null both mean "no expiry"; a positive number is a lifetime in
    // seconds, so zero is never a valid request rather than a silent "immediately".
    expires_in_seconds: z.number().int().positive().nullable().optional(),
  })
  .strict();
export type TokenCreateRequest = z.infer<typeof tokenCreateRequestSchema>;

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
