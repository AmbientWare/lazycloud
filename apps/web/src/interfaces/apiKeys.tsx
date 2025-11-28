import { z } from "zod";

export const EXPIRATION_OPTIONS = {
  "0": "Never",
  "1": "1 day",
  "30": "30 days",
  "100": "100 days",
  "365": "1 year",
};

export const ApiKeyRoleSchema = z.enum(["admin", "user"]);
export const ApiKeyExpiresAtOptionsSchema = z.enum(
  Object.keys(EXPIRATION_OPTIONS) as [string, ...string[]],
);

export const ApiKeySchema = z.object({
  id: z.string(),
  name: z.string(),
  value: z.string(),
  role: ApiKeyRoleSchema,
  expires_at: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

export type ApiKey = z.infer<typeof ApiKeySchema>;
export type ApiKeyExpiresAtOptions = z.infer<
  typeof ApiKeyExpiresAtOptionsSchema
>;
