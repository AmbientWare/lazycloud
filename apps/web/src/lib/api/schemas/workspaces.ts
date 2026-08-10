import { z } from "zod";

import { jsonValueSchema } from "./json";

const timestampSchema = z.string().datetime({ offset: true });

export const workspaceStatusSchema = z.enum(["active", "disabled", "deleting", "deleted"]);

export const workspaceStorageSchema = z
  .object({
    backend: z.string(),
    bucket: z.string().nullable(),
    prefix: z.string(),
  })
  .strict();

export const workspaceSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    status: workspaceStatusSchema,
    signing_key_prefix: z.string().nullable(),
    primary_token_id: z.string().nullable(),
    concurrency_limit_id: z.string().nullable(),
    storage: workspaceStorageSchema,
    labels: z.record(z.string()),
    metadata: z.record(jsonValueSchema),
    created_at: timestampSchema,
    updated_at: timestampSchema,
  })
  .strict();
export type Workspace = z.infer<typeof workspaceSchema>;
