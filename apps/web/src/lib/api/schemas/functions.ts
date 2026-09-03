import { z } from "zod";

import { jsonValueSchema } from "./json";

export const functionResultEncodings = ["json", "cloudpickle"] as const;
export type FunctionResultEncoding = (typeof functionResultEncodings)[number];

const functionJsonResultSchema = z
  .object({
    version: z.literal(1).default(1),
    encoding: z.literal("json"),
    value: jsonValueSchema.default(null),
  })
  .strict();

const functionCloudpickleResultSchema = z
  .object({
    version: z.literal(1).default(1),
    encoding: z.literal("cloudpickle"),
    value_base64: z.string().default(""),
    size_bytes: z
      .number()
      .int()
      .min(0)
      .max(16 * 1024 * 1024),
    sha256: z.string().regex(/^[0-9a-f]{64}$/),
  })
  .strict();

export const functionResultSchema = z.discriminatedUnion("encoding", [
  functionJsonResultSchema,
  functionCloudpickleResultSchema,
]);

export const functionInvokeResponseSchema = z
  .object({
    task_id: z.string().default(""),
    output: z.string().default(""),
    stream: z.enum(["stdout", "stderr", "system"]).default("system"),
    status: z.string().default(""),
    done: z.boolean().default(false),
    exit_code: z.number().int().default(0),
    result: functionResultSchema.nullable().default(null),
  })
  .strict();
export type FunctionInvokeResponse = z.infer<typeof functionInvokeResponseSchema>;
