import { z } from "zod";

import { stubKinds } from "./stubs";

export const resourceWorkloadReferenceSchema = z.object({
  app_id: z.string(),
  app_name: z.string(),
  name: z.string(),
  kind: z.enum(stubKinds),
  versions: z.array(z.number()).default([]),
  active_versions: z.array(z.number()).default([]),
});
export type ResourceWorkloadReference = z.infer<typeof resourceWorkloadReferenceSchema>;

export const volumeSchema = z.object({
  id: z.string(),
  name: z.string(),
  size: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
  workspace_id: z.string(),
  workspace_name: z.string(),
  workloads: z.array(resourceWorkloadReferenceSchema).default([]),
});
export type Volume = z.infer<typeof volumeSchema>;

export const volumeListSchema = z.object({
  volumes: z.array(volumeSchema).default([]),
});

export const secretMaskedSchema = z.object({
  name: z.string(),
  value: z.string(),
  created_at: z.string().nullish(),
  updated_at: z.string().nullish(),
  workloads: z.array(resourceWorkloadReferenceSchema).default([]),
});
export type SecretMasked = z.infer<typeof secretMaskedSchema>;

export const secretMaskedListSchema = z.object({
  secrets: z.array(secretMaskedSchema).default([]),
});

export const secretRevealResponseSchema = z.object({
  secret: z
    .object({
      name: z.string(),
      value: z.string(),
    })
    .nullable(),
});

const queueSchema = z.object({
  name: z.string(),
  size: z.number().default(0),
  oldest_message_age_seconds: z.number().nonnegative().nullable().default(null),
  put_rate_per_minute: z.number().nonnegative().default(0),
});
export type Queue = z.infer<typeof queueSchema>;

export const queueListSchema = z.object({
  queues: z.array(queueSchema).default([]),
});

const mapSchema = z.object({
  name: z.string(),
  count: z.number().default(0),
  size_bytes: z.number().nonnegative().default(0),
  expiring_keys: z.number().nonnegative().default(0),
  nearest_expiry_seconds: z.number().nonnegative().nullable().default(null),
});
export type MapRecord = z.infer<typeof mapSchema>;

export const mapListSchema = z.object({
  maps: z.array(mapSchema).default([]),
});

export const queueSizeSchema = z.object({ size: z.number().default(0) });
export const mapCountSchema = z.object({ count: z.number().default(0) });

export const volumePathInfoSchema = z.object({
  path: z.string(),
  size: z.number().default(0),
  mod_time: z.string(),
  is_dir: z.boolean(),
});
export type VolumePathInfo = z.infer<typeof volumePathInfoSchema>;

export const volumePathListSchema = z.object({
  path_infos: z.array(volumePathInfoSchema).default([]),
});

export const encodedValueSchema = z.object({
  value_base64: z.string().default(""),
});

export const mapKeysSchema = z.object({
  keys: z.array(z.string()).default([]),
});
